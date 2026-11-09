"""v0.6 本机智能体业务入口：SQLite身份/回执/任务索引，复用既有业务函数。"""
import contextlib
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
import datetime
import editing
import quality
import upscaling
import automation
import workspace
import generation
import finishing

LOCK = threading.RLock()
API_VERSION = '1'
READS = {
    'finishing.list':'列出剪映交接、修订关系、原生变化与回收片验收',
    'finishing.preview':'source=current/draft/final/legacy；final 选填；预检微秒时间线、素材和不支持效果',

    'generation.providers':'查看 LibTV 宿主桥与本机 RIFE 可用能力',
    'generation.list':'查询返修任务、预算预留、已知实扣及候选',
    'generation.get':'job_id，查询返修任务与提供方原任务身份',
    'automation.workspace':'当前镜头与候选：当前使用素材、入出点、备用数量和可选工作版本',
    'automation.evaluate':'run_id与人工labels数组(packet_id/check/expected)，统计误报、漏报和决定覆盖率，不推算未标注准确率',
    'automation.list':'列出自动审片运行及候选/接缝/全片复查状态',
    'automation.get':'run_id，读取运行快照、选片理由和未决问题',
    'automation.image':'run_id/packet_id/image_id；读取指定采样帧或reference-N参考图，MCP返回图片内容',
    'automation.packet':'run_id/packet_id，读取指纹绑定证据包',
    'assets.list':'列出素材稳定ID、路径和内容指纹；offset/limit分页',
    'shots.list':'列出镜头稳定ID、原段号、候选及内容评审状态',
    'edit.get':'读取已保存初剪草稿和修订号',
    'review.context':'读取指定segment/final的验收上下文与签名',
    'quality.get':'读取参考卡和连戏资料',
    'quality.context':'读取指定segment/final的连戏上下文',
    'issues.list':'读取返修问题与复核历史',
    'tasks.list':'统一查询所有任务，含网页创建的任务；offset/limit分页',
    'tasks.get':'按task_id读取统一状态、原始进度、错误与产物',
    'quality.reports':'读取带时间码证据的技术质检报告',
    'timeline.get':'读取指定final的冻结快照，省略final时读取当前剪辑',
}
WRITES = {
    'finishing.create':'source/final/signature；从预览生成一份独立可编辑剪映草稿',
    'finishing.revise':'id/operations；基于管理副本创建新修订，保留原生编辑',
    'finishing.publish':'id；剪映完全退出后注册一份新原生草稿',
    'finishing.receive':'id/source/confirm_export=true；从项目或获准导入目录回收剪映实际导出的 MP4 或 MOV',
    'finishing.scan':'final；检查回收成片的解码、黑静帧与声音',
    'finishing.review':'final/film_hash/scan_id/status=approved|rejected/note；记录实看片复查',

    'generation.submit':'segment/provider/issue/preserve/evidence/max_attempts；LibTV另填libtv_project/model/prompt/params/references/watermark/max_credits/quoted_credits/confirm_spend；resume_task_id 仅回收已有任务',
    'generation.claim':'job_id/actor；认领 LibTV 当前步骤；未知受理仅 recover=true 后用同一请求恢复',
    'generation.record':'job_id/lease_token/outcome；登记真实原任务回执或授权交付下载',
    'generation.retry':'job_id/attempt；失败或候选退回后重试；LibTV再次给quoted_credits/confirm_spend',
    'generation.apply':'job_id/in；采用已审查通过的候选，按原镜头长度替换草稿',
    'generation.export':'job_id；保留原剪辑画幅，导出已采用返修的新版成片',
    'automation.apply':'run_id/draft_revision，显式采用已复查的重剪建议；草稿另有修改时拒绝覆盖',
    'automation.start':'mode默认inspect检查当前剪辑；rebuild才重新选片。source=current/draft/final/legacy，final可选；segments勾选时强制复查指定镜头；expected_dialogue/confidence可选',
    'automation.claim':'run_id/actor；认领一份待审证据，返回15分钟租约与要求',
    'automation.submit':'run_id/packet_id/lease_token/signature/result；提交实际审查范围、逐项判断与选片评分',
    'automation.reopen':'run_id/packet_id/note，保留旧判断并补充复查；不改写已导出候选依据',
    'automation.resume':'run_id；明确续接中断或等待的运行，不自动重提未知导出',
    'automation.cancel':'run_id；取消自动流程，已发出的独立质检可能继续',
    'assets.import':'source为获授权import-root内的本地绝对路径；复制入素材库并校验内容',
    'assets.relink':'asset_id/path；为已移动的相同内容重新绑定路径，旧评审不自动转移',
    'shots.create':'segment/scene/text/duration；新增镜头，保留原分段表',
    'review.mutate':'复用评审动作 associate/unlink/select/review/bind/accept；segment可用稳定镜头ID，asset_id可代替path，选用/评审须给fingerprint',
    'edit.save':'clips数组：id/src或asset_id/in/out/a；保存初剪，返回原生草稿修订',
    'edit.action':'action=selected/import/undo/redo；复用草稿操作',
    'export.start':'导出已保存草稿；可选revision，否则使用当前草稿修订',
    'upscale.start':'asset_ids数组或paths，scale=2/3,height=720/1080/1440/2160',
    'quality.scan':'asset_id或path，后台技术检测',
    'quality.mutate':'复用参考asset、镜头shot、人工review操作；支持segment稳定ID',
    'issues.mutate':'复用问题create/status操作；不得以任务成功冒充内容批准',
    'tasks.cancel':'task_id；仅在原执行器支持时取消，返回请求回执并继续查询终态',
    'tasks.retry':'task_id；失败/取消/中断后按原任务快照重试，不自动重复执行未知请求',
    'evidence.extract':'asset_id或path，t为源视频秒；提取新的PNG时间码证据',
}


def private(app):
    p = app.ROOT / '.studio'
    if p.is_symlink() or (p.exists() and not p.is_dir()):
        raise PermissionError('工作台私有目录无效')
    p.mkdir(mode=0o700,exist_ok=True)
    if p.resolve().parent != app.ROOT.resolve():
        raise PermissionError('工作台目录越界')
    for name in ('state.sqlite3','agent-token','service.lock'):
        if (p/name).is_symlink():
            raise PermissionError('私有文件不能是符号链接')
    return p


def token(app):
    p = private(app)/'agent-token'
    try:
        fd = os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        return p.read_text().strip()
    value=secrets.token_urlsafe(32)
    with os.fdopen(fd,'w') as f:f.write(value+'\n')
    return value


@contextlib.contextmanager
def database(app):
    path=private(app)/'state.sqlite3'
    with sqlite3.connect(path,timeout=15) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,fingerprint TEXT NOT NULL,revision INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS shots(id TEXT PRIMARY KEY,segment TEXT UNIQUE NOT NULL);
          CREATE TABLE IF NOT EXISTS receipts(key TEXT PRIMARY KEY,request TEXT NOT NULL,state TEXT NOT NULL,result TEXT);
          CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,kind TEXT NOT NULL,native_id TEXT NOT NULL,snapshot TEXT NOT NULL,UNIQUE(kind,native_id));
        ''')
        db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('project_id',str(uuid.uuid4())))
        db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('sequence','0'))
        os.chmod(path,0o600)
        yield db


def asset_register(app,db,p,identity=None):
    path=app.rel(p);fp=app.file_hash(p)
    row=db.execute('SELECT * FROM assets WHERE path=?',(path,)).fetchone()
    if row:
        if row['fingerprint']!=fp:
            db.execute('UPDATE assets SET fingerprint=?,revision=revision+1 WHERE id=?',(fp,row['id']))
        identity=row['id']
    else:
        identity=identity or str(uuid.uuid4())
        db.execute('INSERT INTO assets VALUES(?,?,?,1)',(identity,path,fp))
    return dict(db.execute('SELECT * FROM assets WHERE id=?',(identity,)).fetchone())


def sync_assets(app,db):
    for folder in (app.C.LIB,app.C.GEN):
        for p in app.walk(folder):
            safe=app.safe(app.rel(p))
            if safe and safe.is_file() and app.kind(safe) in ('video','image','audio'):
                asset_register(app,db,safe)
    for row in app.api_segments().get('rows',[]):
        db.execute('INSERT OR IGNORE INTO shots VALUES(?,?)',(str(uuid.uuid4()),row['id']))


def revision(app,db):
    files=[app.SEG,app.REVIEW,app.FB,editing.DRAFT,quality.STATE,'反馈/返修问题.json']
    state={'files':{p:app.file_hash(app.safe(p)) for p in files},
           'finishing':finishing.revision_state(app),
           'assets':[tuple(r) for r in db.execute('SELECT * FROM assets ORDER BY id')],
           'sequence':db.execute("SELECT value FROM meta WHERE key='sequence'").fetchone()[0]}
    return hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()


def page(args):
    offset,limit=args.get('offset',0),args.get('limit',50)
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=200:
        raise ValueError('offset须非负，limit范围1—200')
    return offset,limit


def assets(app,db,args):
    offset,limit=page(args)
    rows=[dict(r) for r in db.execute('SELECT * FROM assets ORDER BY path')]
    for r in rows:r['missing']=not bool(app.safe(r['path']) and app.safe(r['path']).is_file())
    return {'items':rows[offset:offset+limit],'total':len(rows),'next_offset':offset+limit if offset+limit<len(rows) else None}


def resolve_asset(app,db,args):
    if args.get('asset_id'):
        row=db.execute('SELECT * FROM assets WHERE id=?',(args['asset_id'],)).fetchone()
        if not row:raise ValueError('素材ID不存在')
        return row['path']
    return args.get('path')


def segment(db,value):
    row=db.execute('SELECT segment FROM shots WHERE id=? OR segment=?',(value,value)).fetchone()
    if not row:raise ValueError('镜头ID不存在')
    return row[0]


def task_sync(app,db):
    all_jobs=[('export',x) for x in editing.jobs(app)]+[('upscale',x) for x in upscaling.status(app)['jobs']]+[('quality',x) for x in quality.reports(app)['items']]+[('automation',x) for x in automation.jobs(app)]+[('generation',x) for x in generation.jobs(app)]
    pid=db.execute("SELECT value FROM meta WHERE key='project_id'").fetchone()[0]
    for kind,job in all_jobs:
        status=job['status']
        if kind=='export' and status=='failed' and job.get('error','').startswith('服务已重启'):
            status='interrupted'
        status={'completed':'succeeded'}.get(status,status)
        identity=str(uuid.uuid5(uuid.UUID(pid),kind+':'+job['id']))
        entry={'id':identity,'kind':kind,'native_id':job['id'],'status':status,'can_cancel':kind in ('export','upscale','automation') and status in ('running','queued','waiting_review','needs_decision'),
               'can_retry':kind!='generation' and status in ('failed','cancelled','interrupted'),'progress':job,'outputs':[job['final'] if kind=='export' else job.get('output',job.get('final'))] if status=='succeeded' and kind!='quality' and (job.get('final') or job.get('output')) else [],
               'error':job.get('error',''),'content_status':'not_inferred'}
        db.execute('INSERT INTO tasks VALUES(?,?,?,?) ON CONFLICT(kind,native_id) DO UPDATE SET snapshot=excluded.snapshot',
                   (identity,kind,job['id'],json.dumps(entry,ensure_ascii=False)))
    return [json.loads(r[0]) for r in db.execute('SELECT snapshot FROM tasks ORDER BY rowid DESC')]


def project(app):
    with LOCK,database(app) as db:
        sync_assets(app,db)
        tasks=task_sync(app,db)
        return {**app.api_project(),'api_version':API_VERSION,'project_id':db.execute("SELECT value FROM meta WHERE key='project_id'").fetchone()[0],
                'revision':revision(app,db),'import_roots':[str(p) for p in getattr(app,'IMPORT_ROOTS',[])],
                'task_counts':{s:sum(t['status']==s for t in tasks) for s in ('queued','running','waiting_review','waiting_host','needs_reconciliation','needs_decision','succeeded','failed','cancelled','interrupted')},
                'operations':{'read':READS,'write':WRITES},'capabilities':{'quality_cancel':False,'transport':'stdio proxy to local authenticated HTTP','automatic_generation':bool(generation.local_config()),'generation_providers':generation.capabilities(),'automatic_review':{'host_bridge':True,'local_command_configured':bool(os.environ.get('STUDIO_REVIEW_COMMAND')),'asr_configured':bool(os.environ.get('STUDIO_ASR_PYTHON') and os.environ.get('STUDIO_ASR_MODEL'))}}}


def dispatch(app,body):
    if not isinstance(body,dict):raise ValueError('请求须为JSON对象')
    op,args=body.get('operation'),body.get('arguments',{})
    if not isinstance(args,dict):raise ValueError('arguments须为对象')
    if op not in READS and op not in WRITES:raise ValueError('不支持的操作')
    with LOCK,database(app) as db:
        sync_assets(app,db)
        current=revision(app,db)
        if op in WRITES and body.get('project_id') != db.execute("SELECT value FROM meta WHERE key='project_id'").fetchone()[0]:
            raise ValueError('写操作的project_id与当前项目不符')
        if op in READS:
            return {'result':read_operation(app,db,op,args),'revision':current,'api_version':API_VERSION}
        key=body.get('idempotency_key')
        if not isinstance(key,str) or not key.strip() or len(key)>200:raise ValueError('写操作需要1—200字符的idempotency_key')
        request=json.dumps(body,sort_keys=True,ensure_ascii=False,allow_nan=False)
        old=db.execute('SELECT * FROM receipts WHERE key=?',(key,)).fetchone()
        if old:
            if old['request']!=request:raise ValueError('同一幂等键不能用于不同请求')
            if old['state']=='completed':return {**json.loads(old['result']),'replayed':True}
            return {'state':'needs_reconciliation','idempotency_key':key,'message':'前次操作回执未完整保存；先查询任务和项目，禁止自动重新提交'}
        if body.get('expected_revision')!=current:raise ValueError('项目已变化，请重新读取studio_project的revision')
        db.execute('INSERT INTO receipts VALUES(?,?,?,NULL)',(key,request,'dispatching'))
        db.commit()  # 先落盘；崩溃窗口保持待核对，不能自动重放副作用。
        try:
            if op in ('finishing.create','finishing.revise'):
                args={**args,'request_id':uuid.uuid5(uuid.UUID(body['project_id']),key).hex}
            result=write_operation(app,db,op,args)
            sync_assets(app,db)
            db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='sequence'")
            tasks=task_sync(app,db)
            response={'result':result,'revision':revision(app,db),'api_version':API_VERSION}
            if op in ('export.start','upscale.start','quality.scan','finishing.scan','automation.start','automation.resume','generation.submit','generation.retry','generation.record','generation.export','tasks.retry'):
                native_ids={j['id'] for j in result.get('jobs',[result]) if isinstance(j,dict) and 'id' in j}
                response['tasks']=[t for t in tasks if t['native_id'] in native_ids]
            db.execute("UPDATE receipts SET state='completed',result=? WHERE key=?",(json.dumps(response,ensure_ascii=False),key))
            db.commit()
            return response
        except Exception as e:
            # 回执保存失败时也不自动撤销已执行的本地副作用。调用方可查询并显式用新键重试。
            db.rollback()
            db.execute("UPDATE receipts SET state='needs_reconciliation',result=? WHERE key=?",(json.dumps({'error':str(e)},ensure_ascii=False),key))
            db.commit()
            raise


def read_operation(app,db,op,args):
    if op=='finishing.list':return finishing.listing(app)
    if op=='finishing.preview':return finishing.preview(app,args)
    if op=='generation.providers':return generation.capabilities()
    if op=='generation.list':return {'items':generation.jobs(app)}
    if op=='generation.get':return generation.get(app,args.get('job_id'))
    if op=='automation.workspace':return workspace.overview(app,args.get('final'),args.get('source','current'))
    if op=='automation.evaluate':return automation.evaluate(app,args)
    if op=='automation.list':return {'items':automation.jobs(app)}
    if op=='automation.get':return automation.get(app,args.get('run_id'))
    if op=='automation.image':
        p=automation.packet(app,args.get('run_id'),args.get('packet_id'));image=next((x for x in p['frames']+p.get('reference_images',[]) if x['id']==args.get('image_id')),None)
        if not image:raise ValueError('证据图ID不存在')
        return {**image,'mime_type':app.MEDIA.get(app.safe(image['path']).suffix.lower(),'image/png')}
    if op=='automation.packet':return automation.packet(app,args.get('run_id'),args.get('packet_id'))
    if op=='assets.list':return assets(app,db,args)
    if op=='shots.list':
        rows=app.api_segments().get('rows',[])
        for r in rows:r['shot_id']=db.execute('SELECT id FROM shots WHERE segment=?',(r['id'],)).fetchone()[0]
        return {'items':rows}
    if op=='edit.get':return editing.read(app)
    if op=='quality.get':return quality.read(app)
    if op=='issues.list':return editing.issues(app)
    if op=='quality.reports':return quality.reports(app)
    if op in ('review.context','quality.context'):
        sid=segment(db,args.get('segment'))
        return app.review_context(sid,args.get('final')) if op=='review.context' else quality.context(app,sid,args.get('final'))
    if op=='timeline.get':
        final=args.get('final')
        if not final:return app.api_timeline()
        if final not in {app.rel(p) for p,_,_ in app.final_videos()}:raise ValueError('成片不存在')
        if finishing.returned(final):return finishing.UNMAPPED
        m=editing.manifest(app,final)
        if not m:raise ValueError('该成片没有指纹匹配的导出快照；请通过review.context核对旧项目')
        return m['timeline']
    tasks=task_sync(app,db)
    if op=='tasks.get':
        item=next((x for x in tasks if x['id']==args.get('task_id')),None)
        if not item:raise ValueError('任务ID不存在')
        return item
    offset,limit=page(args)
    return {'items':tasks[offset:offset+limit],'total':len(tasks)}


def write_operation(app,db,op,args):
    if op.startswith('finishing.'):
        return finishing.ACTIONS[op.split('.',1)[1]](app,args)
    if op=='generation.submit':
        return generation.submit(app,{**args,'segment':segment(db,args.get('segment'))})
    if op=='generation.claim':return generation.claim(app,args)
    if op=='generation.record':return generation.record(app,args)
    if op=='generation.retry':return generation.retry(app,args)
    if op=='generation.apply':return generation.apply(app,args)
    if op=='generation.export':return generation.export(app,args)
    if op=='automation.apply':return automation.apply_suggestion(app,args)
    if op=='automation.start':return automation.start(app,args)
    if op=='automation.claim':return automation.claim(app,args)
    if op=='automation.submit':return automation.submit(app,args)
    if op=='automation.reopen':return automation.reopen(app,args)
    if op=='automation.resume':return automation.resume(app,args.get('run_id'))
    if op=='automation.cancel':return automation.cancel(app,args.get('run_id'))
    if op=='assets.import':
        value=args.get('source')
        if not isinstance(value,str) or not pathlib.Path(value).is_absolute():raise ValueError('source须为绝对路径')
        source=pathlib.Path(value).resolve()
        roots=getattr(app,'IMPORT_ROOTS',[])
        if not any(source.is_relative_to(p) for p in roots) or not source.is_file() or app.kind(source) not in ('video','image','audio'):
            raise PermissionError('只可导入--import-root授权目录内的媒体')
        identity=str(uuid.uuid4());target=app.safe('素材库/导入/'+identity+'_'+source.name)
        if not target:raise PermissionError('导入目标路径无效')
        target.parent.mkdir(parents=True,exist_ok=True)
        fp=app.file_hash(source)
        fd,tmp=tempfile.mkstemp(dir=target.parent)
        os.close(fd)
        try:
            shutil.copyfile(source,tmp)
            if app.file_hash(pathlib.Path(tmp))!=fp or app.file_hash(source)!=fp:raise ValueError('复制期间源文件变化')
            os.link(tmp,target)
        finally:os.unlink(tmp)
        return asset_register(app,db,target,identity)
    if op=='assets.relink':
        row=db.execute('SELECT * FROM assets WHERE id=?',(args.get('asset_id'),)).fetchone()
        p=app.safe(args.get('path',''))
        if not row or not p or not p.is_file() or not any(p.is_relative_to(app.ROOT/d) for d in (app.C.LIB,app.C.GEN)) or app.file_hash(p)!=row['fingerprint']:
            raise ValueError('重关联仅接受素材区内相同内容')
        original=app.safe(row['path'])
        if original and original.is_file() and original != p:
            raise ValueError('原素材仍存在，不能将另一份素材冒充移动后的原件')
        other=db.execute('SELECT id FROM assets WHERE path=?',(app.rel(p),)).fetchone()
        if other and other[0]!=row['id']:
            # 新路径已被扫描登记时，保留原身份，移除临时新身份。
            db.execute('DELETE FROM assets WHERE id=?',(other[0],))
        db.execute('UPDATE assets SET path=? WHERE id=?',(app.rel(p),row['id']))
        return {'asset_id':row['id'],'path':app.rel(p),'note':'旧评审/草稿不自动改写，请按镜头重新核对关联'}
    if op=='shots.create':
        sid=args.get('segment');scene=args.get('scene',sid)
        if not isinstance(sid,str) or not sid.strip() or sid!=sid.strip() or len(sid)>100 or any(c in sid for c in '|\r\n') or any(r['id']==sid for r in app.api_segments().get('rows',[])):
            raise ValueError('段号须非空且不能重复')
        p=app.safe(app.SEG)
        if not p:raise PermissionError('分段表越界')
        text=p.read_text() if p.exists() else '| 段 | 秒 | 节拍 | 内容 | 脚本 | 参考 | 生成 | 过审 | 入片 | 验收 |\n|---|---|---|---|---|---|---|---|---|---|\n'
        header=next((l for l in text.splitlines() if l.strip().startswith('| 段 |')),None)
        if not header:raise ValueError('现有分段表表头不能安全追加，请先整理')
        cols=[s.strip() for s in header.strip().strip('|').split('|')]
        values={'段':sid,'秒':args.get('duration',''),'节拍':scene,'内容':args.get('text','')}
        line='| '+' | '.join(str(values.get(k,'')).replace('|','｜').replace('\n',' ').replace('\r',' ') for k in cols)+' |\n'
        p.parent.mkdir(parents=True,exist_ok=True)
        if p.exists():shutil.copyfile(p,str(p)+'.bak')
        fd,tmp=tempfile.mkstemp(dir=p.parent)
        try:
            with os.fdopen(fd,'w') as f:f.write(text.rstrip()+'\n'+line)
            os.replace(tmp,p)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
        return {'segment':sid}
    if op in ('review.mutate','quality.mutate'):
        body=dict(args)
        if 'segment' in body:body['segment']=segment(db,body['segment'])
        if body.get('asset_id'):body['path']=resolve_asset(app,db,body);body.pop('asset_id')
        if op=='review.mutate':return app.review_mutate(body)
        body.setdefault('revision',quality.read(app)['revision']);return quality.mutate(app,body)
    if op=='issues.mutate':return editing.issues(app,args)
    if op in ('edit.save','edit.action'):
        body={'revision':editing.read(app)['revision'],**args}
        if op=='edit.save':
            body['action']='save';clips=[]
            for clip in body.get('clips',[]):
                clip=dict(clip)
                if clip.get('asset_id'):clip['src']=resolve_asset(app,db,clip);clip.pop('asset_id')
                clips.append(clip)
            body['clips']=clips
        return editing.mutate(app,body)
    if op=='export.start':return editing.start(app,{'revision':editing.read(app)['revision'],**args})
    if op=='upscale.start':
        body=dict(args)
        if 'asset_ids' in body:body['paths']=[resolve_asset(app,db,{'asset_id':x}) for x in body.pop('asset_ids')]
        return upscaling.submit(app,body)
    if op=='quality.scan':return quality.start(app,{'path':resolve_asset(app,db,args)})
    if op in ('tasks.cancel','tasks.retry'):
        task=read_operation(app,db,'tasks.get',args)
        kind,native=task['kind'],task['native_id']
        if kind=='automation':
            return automation.cancel(app,native) if op=='tasks.cancel' else automation.resume(app,native)
        if op=='tasks.cancel':
            if not task['can_cancel']:raise ValueError('此执行器当前不支持取消该任务')
            return editing.cancel(app,{'id':native}) if kind=='export' else upscaling.cancel(app,{'id':native})
        if not task['can_retry']:raise ValueError('任务未失败、取消或中断')
        if kind=='export':return editing.start(app,{'retry':native})
        if kind=='upscale':return upscaling.submit(app,{'retry':native})
        progress=task['progress']
        if app.file_hash(app.safe(progress['path']))!=progress['fingerprint']:raise ValueError('质检源视频已变化，请新建检测任务')
        return quality.start(app,{'path':progress['path']})
    if op=='evidence.extract':
        import math
        source=quality.media_file(app,resolve_asset(app,db,args));t=float(args.get('t',0));info=app.probe(source)
        if not math.isfinite(t) or not 0<=t<info.get('duration',0):raise ValueError('证据时间超出源视频')
        executable=os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
        if not executable:raise ValueError('未找到FFmpeg')
        path='反馈/帧/agent-'+uuid.uuid4().hex+'.png';target=app.safe(path)
        if not target:raise PermissionError('证据路径越界')
        target.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run([executable,'-nostdin','-v','error','-ss',str(t),'-i',str(source),'-frames:v','1','-vf','scale=960:-2','-n',str(target)],check=True,capture_output=True,timeout=30)
        return {'path':path,'t':t,'source':app.rel(source),'source_fingerprint':app.file_hash(source),'fingerprint':app.file_hash(target),'url':'/f/'+path}
    raise ValueError('不支持的操作')


def acquire_service_lock(app):
    import fcntl
    p=private(app)/'service.lock'
    handle=p.open('a+')
    try: fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ValueError('同一项目已有Studio服务，请连接原服务，勿启动重复执行器')
    return handle
