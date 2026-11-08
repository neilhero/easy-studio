"""审片证据：实际视频/声音/采样帧、技术报告、可选本地转写与响度。"""
import difflib
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import time
import uuid
import quality
import editing

CHECKS=('identity','props','direction','action','dialogue')


def execute(args,event,input_text=None,timeout=300):
    with subprocess.Popen([str(x) for x in args],stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as p:
        started=time.monotonic();first=True
        try:
            while True:
                if event.is_set():raise editing.ExportCancelled('自动审片已取消')
                if time.monotonic()-started>timeout:raise TimeoutError('媒体处理或审查命令超时')
                try:
                    out,err=p.communicate(input=input_text if first else None,timeout=.2)
                    if p.returncode:raise ValueError(err[-1800:] or '执行失败')
                    return out,err
                except subprocess.TimeoutExpired:first=False
        finally:
            if p.poll() is None:
                p.terminate()
                try:p.communicate(timeout=3)
                except subprocess.TimeoutExpired:p.kill();p.communicate()


def technical(app,path,event):
    while True:
        if event.is_set():raise editing.ExportCancelled('自动审片已取消')
        try: job=quality.start(app,{'path':path});break
        except ValueError:
            if not quality.ACTIVE:raise
            event.wait(.1)
    while job['id'] in quality.ACTIVE:
        if event.wait(.1):raise editing.ExportCancelled('自动审片已取消；已发出的独立质检可继续')
    report=next(x for x in quality.reports(app)['items'] if x['id']==job['id'])
    if report['status']!='completed' or report['stale']:raise ValueError('技术检查失败：'+report.get('error',''))
    return report


def make(app,run_id,path,start,end,kind,requirements,expected,event):
    source=quality.media_file(app,path);fingerprint=app.file_hash(source);info=app.probe(source)
    if not math.isfinite(start) or not math.isfinite(end) or not 0<=start<end<=info.get('duration',0)+.001:raise ValueError('证据范围无效')
    ff=os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
    if not ff:raise ValueError('需要FFmpeg')
    token=uuid.uuid4().hex;folder=app.safe(f'反馈/自动审片/{run_id}/{token}')
    if not folder:raise PermissionError('证据路径越界')
    folder.mkdir(parents=True)
    duration=end-start
    # 长片只限制采样帧数量；完整视频证据仍覆盖要求范围。
    count=max(2,min(12,int(math.ceil(duration))+1))
    times=[start+(max(0,duration-.04)*i/(count-1)) for i in range(count)]
    frames=[]
    for i,t in enumerate(times):
        dest=folder/f'frame-{i:02d}.jpg'
        execute([ff,'-nostdin','-v','error','-ss',t,'-i',source,'-frames:v','1','-vf','scale=640:-2','-y',dest],event)
        frames.append({'id':f'frame-{i}','path':app.rel(dest),'t':round(t,4),'fingerprint':app.file_hash(dest)})
    clip=folder/'review.mp4'
    execute([ff,'-nostdin','-v','error','-ss',start,'-i',source,'-t',duration,'-map','0:v:0','-map','0:a:0?',
             '-vf','scale=640:-2','-c:v','libx264','-preset','fast','-crf','22','-c:a','aac','-y',clip],event)
    report=technical(app,path,event)
    sound={'status':'not_applicable','transcript':None,'loudness':None}
    audio=None
    if info.get('has_audio'):
        wav=folder/'audio.wav'
        execute([ff,'-nostdin','-v','error','-ss',start,'-i',source,'-t',duration,'-vn','-ar','16000','-ac','1','-y',wav],event)
        audio={'id':'audio','path':app.rel(wav),'range':[start,end],'fingerprint':app.file_hash(wav)}
        _,log=execute([ff,'-nostdin','-hide_banner','-ss',start,'-i',source,'-t',duration,'-vn','-af','loudnorm=print_format=json','-f','null','-'],event)
        matches=re.findall(r'\{\s*"input_i"[\s\S]*?\}',log)
        if matches:
            metrics=json.loads(matches[-1]);sound['loudness']={k:(float(v) if math.isfinite(float(v)) else None) for k,v in metrics.items() if k.startswith('input_')}
        sound['status']='unconfigured'
        python,model=os.environ.get('STUDIO_ASR_PYTHON'),os.environ.get('STUDIO_ASR_MODEL')
        if python and model:
            try:
                out,_=execute([python,pathlib.Path(__file__).with_name('asr_local.py'),'--model',model,'--audio',wav],event,timeout=300)
                transcript=json.loads(out)
                for s in transcript['segments']:
                    s['start']+=start;s['end']+=start
                    for w in s.get('words',[]):w['start']+=start;w['end']+=start
                sound.update(status='completed',transcript=transcript,model_fingerprint=app.file_hash(pathlib.Path(model)/'model.bin'))
            except editing.ExportCancelled:raise
            except Exception as e:sound.update(status='failed',error=str(e))
        if expected and sound['transcript']:
            text=''.join(s['text'] for s in sound['transcript']['segments'])
            normalize=lambda s:''.join(re.findall(r'[\w]',s))
            sound['dialogue_hint']={'expected':expected,'transcribed':text,'similarity':difflib.SequenceMatcher(None,normalize(expected),normalize(text)).ratio(),'scope':'文字疑点，仅供复核，不代替试听'}
    if app.file_hash(source)!=fingerprint:raise ValueError('生成证据期间原片变化')
    report={**report,'events':[{**e,'t':max(start,e['t']),'end':min(end,e['end'])} for e in report['events'] if e['t']<end and e['end']>start],'evidence_range':[start,end]}
    refs=requirements.get('references',[]) + [r for s in requirements.get('shots',[]) for r in s.get('references',[])]
    refs += [r for k in ('left_shot','right_shot') for r in requirements.get(k,{}).get('references',[])]
    reference_images=[{'id':f'reference-{i}','path':r['image'],'fingerprint':r['fingerprint'],'name':r['name'],'version':r['version']} for i,r in enumerate(refs)]
    packet={'id':token,'kind':kind,'source':path,'source_hash':fingerprint,'range':[start,end],'metadata':info,
            'frames':frames,'reference_images':reference_images,'clip':{'id':'clip','path':app.rel(clip),'range':[start,end],'fingerprint':app.file_hash(clip)},
            'audio':audio,'sound':sound,'technical':report,'requirements':requirements,'expected_dialogue':expected,
            'coverage':{'sampled_frames':len(frames),'sampling_interval_seconds':round((duration-.04)/(count-1),3),'video_available':[start,end],
                        'video_watched':[],'audio_listened':[],'note':'抽帧不证明完整动作，转写不证明已试听；审查方须明确报告实际检查范围'},
            'status':'pending','lease':None,'review':None}
    packet['signature']=hashlib.sha256(json.dumps(packet,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return packet


def covered(ranges,start,end):
    cursor=start
    for a,b in sorted(ranges):
        if a>cursor+.05:return False
        cursor=max(cursor,b)
        if cursor>=end-.05:return True
    return False


def validate(packet,result,threshold):
    if not isinstance(result,dict) or not isinstance(result.get('checks'),dict) or set(result['checks'])!=set(CHECKS):raise ValueError('须逐项提交identity/props/direction/action/dialogue检查')
    if not isinstance(result.get('model'),str) or not result['model'].strip() or not isinstance(result.get('reason'),str) or not result['reason'].strip():raise ValueError('须记录审查模型/人工身份与判断理由')
    start,end=packet['range'];usable=result.get('usable',[start,end])
    if not isinstance(usable,list) or len(usable)!=2 or any(type(x) not in (float,int) or not math.isfinite(x) for x in usable) or not start<=usable[0]<usable[1]<=end:raise ValueError('可用源区间无效')
    score=result.get('score')
    if type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=100:raise ValueError('评分须在0—100')
    ids={f['id'] for f in packet['frames']+packet.get('reference_images',[])}|{'clip'}
    if packet['audio']:ids.add('audio')
    ranges={}
    for key in ('watched_ranges','listened_ranges'):
        value=result.get(key,[])
        if not isinstance(value,list) or any(not isinstance(x,list) or len(x)!=2 or any(type(n) not in (float,int) or not math.isfinite(n) for n in x) or not start<=x[0]<x[1]<=end for x in value):raise ValueError('实际检查范围无效')
        ranges[key]=value
    eligible=True
    for key,item in result['checks'].items():
        if not isinstance(item,dict) or item.get('status') not in ('pass','fail','pending','na') or not item.get('note'):raise ValueError('检查状态与说明必填')
        evidence=item.get('evidence',[]);confidence=item.get('confidence',0)
        if not isinstance(evidence,list) or any(not isinstance(x,str) or x not in ids for x in evidence):raise ValueError('引用的证据ID无效')
        if type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0<=confidence<=1:raise ValueError('置信度须在0—1')
        if key=='dialogue' and packet['expected_dialogue'] and not packet['metadata'].get('has_audio') and item['status']=='pass':raise ValueError('有明确台词要求但视频无音轨，不能判台词通过')
        if item['status']=='pass':
            if not evidence:raise ValueError('通过判断须附证据ID')
            if confidence<threshold:eligible=False
            if key=='action' and (not covered(ranges['watched_ranges'],*usable) or 'clip' not in evidence):raise ValueError('动作通过须实际检查可用区间视频，不能仅凭抽帧')
            if key=='dialogue' and packet['metadata'].get('has_audio') and (not covered(ranges['listened_ranges'],*usable) or not {'clip','audio'}&set(evidence)):raise ValueError('声音/台词通过须实际试听，不能仅凭转写')
        if item['status'] in ('fail','pending'):eligible=False
        if key=='dialogue' and item['status']=='na' and packet['expected_dialogue']:raise ValueError('有明确台词要求时不能标记台词不适用')
    if result.get('technical_disposition') is not None and not isinstance(result['technical_disposition'],str):raise ValueError('技术疑点处理须填写具体说明')
    if packet['technical']['events'] and not str(result.get('technical_disposition','')).strip():eligible=False
    if not any(x['status']=='pass' for x in result['checks'].values()):eligible=False
    if packet['metadata'].get('has_audio') and not covered(ranges['listened_ranges'],*usable):eligible=False
    # 全片与接缝复查必须检查实际视频；有声时须试听，不能用NA绕过覆盖。
    if packet['kind']!='candidate':
        if not covered(ranges['watched_ranges'],start,end):eligible=False
        if packet['metadata'].get('has_audio') and not covered(ranges['listened_ranges'],start,end):eligible=False
    return {**result,'usable':usable,'eligible':eligible,'coverage':ranges}


def verify(app,packet):
    if app.file_hash(app.safe(packet['source']))!=packet['source_hash']:raise ValueError('证据对应的视频已变化')
    for item in packet['frames']+packet.get('reference_images',[])+[packet['clip']]+([packet['audio']] if packet['audio'] else []):
        if app.file_hash(app.safe(item['path']))!=item['fingerprint']:raise ValueError('证据文件缺失或变化，请重新准备')
