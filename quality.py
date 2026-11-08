"""v0.5 参考资产、人工连戏记录与本地技术质检。"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import uuid
import datetime
import editing

STATE = '反馈/连戏资料.json'
LOCK = threading.RLock()
ACTIVE = set()
CHECKS = ['人物服装', '道具状态', '站位视线', '运动方向', '光线', '动作台词']


def read(app):
    p = app.safe(STATE)
    if not p:
        raise PermissionError('资料路径越界')
    return json.loads(p.read_text()) if p.exists() else {'schema': 1, 'revision': 0, 'assets': [], 'shots': {}, 'history': []}


def image_file(app, path):
    p = app.safe(path) if isinstance(path,str) else None
    if not p or not p.is_file() or app.kind(p) != 'image':
        raise ValueError('请选择项目内的参考图片')
    return p


def media_file(app, path):
    if path in {app.rel(p) for p,_,_ in app.final_videos()}:
        return app.safe(path)
    return app.candidate_file(path)


def context(app, sid, final):
    ctx = app.review_context(sid,final)
    data = read(app)
    shot = data['shots'].get(sid, {'requirements': '', 'references': [], 'reviews': []})
    refs = []
    for ref in shot['references']:
        asset = next((x for x in data['assets'] if x['id'] == ref['id']), None)
        rev = next((x for x in asset['versions'] if x['version'] == ref['version']), None) if asset else None
        refs.append({'id':ref['id'], 'version':ref['version'], 'record':rev,
                     'current_hash':app.file_hash(app.safe(rev['image'])) if rev else None,
                     'new_version':bool(asset and asset['versions'][-1]['version'] != ref['version'])})
    signature = hashlib.sha256(json.dumps({'context':ctx.get('signature'), 'requirements':shot['requirements'], 'refs':refs},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    review = next((x for x in reversed(shot['reviews']) if x['final'] == final),None)
    status = '待检查' if not review else '需复核' if review['signature'] != signature else '有问题' if 'fail' in review['checks'].values() else '待检查' if 'pending' in review['checks'].values() else '已检查'
    return {'context':ctx,'shot':shot,'references':refs,'signature':signature,'review':review,'status':status,'revision':data['revision'],'checks':CHECKS}


def mutate(app, body):
    with LOCK:
        data = read(app)
        if body.get('revision') != data['revision']:
            raise ValueError('资料已变化，请刷新后再保存')
        action = body.get('action')
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if action == 'asset':
            name,note,kind = str(body.get('name','')).strip()[:120],str(body.get('note','')).strip()[:4000],body.get('kind')
            if not name or kind not in ('人物','服装','道具','场景'):
                raise ValueError('请填写名称并选择资料类型')
            picture = image_file(app,body.get('image'))
            token = body.get('id')
            asset = next((x for x in data['assets'] if x['id'] == token),None)
            if token and not asset:
                raise ValueError('参考卡不存在')
            if not asset:
                asset = {'id':uuid.uuid4().hex,'versions':[]};data['assets'].append(asset)
            asset['versions'].append({'version':len(asset['versions'])+1,'name':name,'kind':kind,'note':note,'image':app.rel(picture),'fingerprint':app.file_hash(picture),'at':now})
        elif action in ('shot','review'):
            sid = body.get('segment');app.segment_row(sid)
            shot = data['shots'].setdefault(sid,{'requirements':'','references':[],'reviews':[]})
            if action == 'shot':
                ids = body.get('assets',[])
                if not isinstance(ids,list) or len(ids)>100 or any(not isinstance(x,str) for x in ids):
                    raise ValueError('参考卡选择无效')
                refs = []
                for token in dict.fromkeys(ids):
                    asset = next((x for x in data['assets'] if x['id']==token),None)
                    if not asset:
                        raise ValueError('参考卡不存在')
                    refs.append({'id':token,'version':asset['versions'][-1]['version']})
                shot.update(requirements=str(body.get('requirements','')).strip()[:8000],references=refs)
            else:
                ctx = context(app,sid,body.get('final'))
                if not ctx['context'].get('confirmed') or not ctx['context'].get('clip'):
                    raise ValueError('请先在进度页核对并确认此成片的剪辑对应关系')
                if body.get('signature') != ctx['signature']:
                    raise ValueError('成片、参考或剪辑已变化，请重新检查')
                checks = body.get('checks',{})
                if set(checks) != set(CHECKS) or any(x not in ('pass','fail','pending') for x in checks.values()):
                    raise ValueError('连戏检查项无效')
                note = str(body.get('note','')).strip()[:4000]
                if 'fail' in checks.values() and not note:
                    raise ValueError('发现问题时请填写说明')
                shot['reviews'].append({'final':body['final'],'signature':ctx['signature'],'checks':checks,'note':note,'at':now})
        else:
            raise ValueError('未知资料操作')
        data['revision'] += 1
        data['history'].append({'action':action,'at':now,'snapshot':{'assets':data['assets'],'shots':data['shots']}})
        # 保存历史状态副本；后续读取后再修改，不共享内存对象。
        editing.write(app,STATE,data)
        return data


def reports(app):
    p = app.safe('反馈/技术质检')
    if not p:
        raise PermissionError('质检目录越界')
    items = []
    for file in sorted(p.glob('*.json'),reverse=True) if p.exists() else []:
        item = json.loads(file.read_text())
        if item['status'] == 'running' and item['id'] not in ACTIVE:
            item.update(status='interrupted',error='服务中断，请重新检查')
        item['stale'] = app.file_hash(app.safe(item['path'])) != item['fingerprint']
        items.append(item)
    return {'items':sorted(items,key=lambda x:x['at'],reverse=True)}


def start(app, body):
    with LOCK:
        if ACTIVE:
            raise ValueError('已有技术质检正在运行')
        executable = os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
        if not executable:
            raise ValueError('未找到 FFmpeg')
        source = media_file(app,body.get('path'))
        token = uuid.uuid4().hex
        report = {'id':token,'path':app.rel(source),'fingerprint':app.file_hash(source),'status':'running','events':[],'error':'','at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        editing.write(app,f'反馈/技术质检/{token}.json',report)
        ACTIVE.add(token)
        threading.Thread(target=run,args=(app,executable,report),daemon=True).start()
        return report


def run(app, executable, report):
    try:
        source = media_file(app,report['path'])
        info = app.probe(source)
        if not info.get('duration'):
            raise ValueError('无法探测视频信息')
        report['metadata'] = info
        args = [executable,'-nostdin','-hide_banner','-v','info','-xerror','-i',str(source),'-map','0:v:0',
                '-vf','blackdetect=d=0.1:pix_th=0.1,freezedetect=n=-50dB:d=1']
        if info.get('has_audio'):
            args += ['-map','0:a:0','-af','silencedetect=n=-50dB:d=0.5,volumedetect']
        else:
            report['events'].append({'type':'缺失音轨','t':0,'end':info['duration']})
        result = subprocess.run(args+['-f','null','-'],capture_output=True,text=True,timeout=1800)
        # 只解析工具日志中的时间码，不执行日志内容。
        patterns = [('黑帧','black_start', 'black_end'),('异常静帧','freeze_start','freeze_end'),('静音段','silence_start','silence_end')]
        for label,start_key,end_key in patterns:
            begin = None
            for match in re.finditer(r'\b('+start_key+'|'+end_key+r'):\s*([\d.]+)',result.stderr):
                if match[1] == start_key:
                    begin = float(match[2])
                elif begin is not None:
                    report['events'].append({'type':label,'t':begin,'end':float(match[2])});begin=None
            if begin is not None:
                report['events'].append({'type':label,'t':begin,'end':info['duration']})
        peak = re.search(r'max_volume:\s*([-\d.]+) dB',result.stderr)
        if peak:
            report['peak_db'] = float(peak[1])
            if report['peak_db'] >= 0:
                report['events'].append({'type':'峰值触顶疑点','t':0,'end':info['duration']})
        if result.returncode:
            raise ValueError('解码或检测失败：'+result.stderr[-1800:])
        if app.file_hash(source) != report['fingerprint']:
            raise ValueError('检测期间视频发生变化，请重新检查')
        report.update(status='completed',decode='通过')
    except Exception as e:
        report.update(status='failed',error=str(e))
    finally:
        editing.write(app,f"反馈/技术质检/{report['id']}.json",report)
        with LOCK:
            ACTIVE.discard(report['id'])
