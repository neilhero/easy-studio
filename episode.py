"""Durable episode coordinator over the existing review, render, repair and finishing tools."""
import copy
import json
import re
import threading
import uuid

import automation
import editing
import finishing
import generation
import review_evidence
import workspace

BASE = '反馈/整集制作'
ACTIVE = {}
TEMPLATES = {'review': '仅素材审查', 'rough': '已有素材出初剪',
             'repair': '指定镜头返修', 'episode': '整集制作'}


def record_path(run_id):
    if not isinstance(run_id, str) or not re.fullmatch('[a-f0-9]{32}', run_id):
        raise ValueError('制作运行编号无效')
    return f'{BASE}/{run_id}/run.json'


def load(app, run_id):
    path = app.safe(record_path(run_id))
    if not path or not path.is_file():
        raise ValueError('制作运行不存在')
    return json.loads(path.read_text())


def save(app, run):
    run['updated'] = automation.now()
    editing.write(app, record_path(run['id']), run)


def cycle(run):
    return run['cycles'][-1]


def subid(run, label):
    return uuid.uuid5(uuid.UUID(run['id']), f"{len(run['cycles'])}:{label}").hex


def existing(app, folder, identity, name):
    path = app.safe(f'{folder}/{identity}/{name}')
    return bool(path and path.is_file())


def budget(app, run):
    reservations, charges, unknown = 0, 0, []
    jobs = []
    for c in run['cycles']:
        for repair in c['repairs']:
            identity = repair['job_id']
            if not app.safe(f'{generation.BASE}/{identity}.json').is_file():
                continue
            job = generation.read(app, identity)
            jobs.append(job)
            for attempt in job['attempts']:
                if attempt['credits'] is None:
                    reservations += attempt['reserved']
                    unknown.append({'job_id': identity, 'attempt': attempt['number'], 'task_id': attempt['task_id']})
                else:
                    charges += attempt['credits']
    return {'limit': run['max_credits'], 'reserved': reservations, 'charged': charges,
            'remaining': max(0, run['max_credits'] - reservations - charges),
            'exceeded': charges + reservations > run['max_credits'],
            'unknown': unknown, 'jobs': [generation.public(job) for job in jobs]}


def view(run):
    current = cycle(run)
    state = run['status']
    if run['enabled'] and run['id'] not in ACTIVE:
        state = 'interrupted'
    return {**run, 'status': state, 'stage': current['stage'], 'final': current.get('delivery', {}).get('path'),
            'report': current.get('report')}


def listing(app):
    directory = app.safe(BASE)
    runs = [view(load(app, p.parent.name)) for p in directory.glob('*/run.json')
            if re.fullmatch('[a-f0-9]{32}', p.parent.name)]
    return {'templates': TEMPLATES, 'items': sorted(runs, key=lambda x: x['updated'], reverse=True)}


def status(run, state, detail):
    run['status'] = state
    cycle(run)['stage'] = detail


def blank_cycle(run, repairs):
    number = len(run['cycles']) + 1
    run['cycles'].append({'number': number, 'stage': '核对制作范围', 'initial': subkey(run, number, 'initial'),
                          'repairs': [{'request': r, 'job_id': subkey(run, number, f'repair:{i}')}
                                      for i, r in enumerate(repairs)], 'audits': []})


def subkey(run, number, label):
    return uuid.uuid5(uuid.UUID(run['id']), f'{number}:{label}').hex


def repair_requests(body, segments, cap, max_attempts):
    repairs = body.get('repairs', [])
    if not isinstance(repairs, list) or len(repairs) > len(segments):
        raise ValueError('repairs 须为指定镜头返修列表')
    seen = set()
    result = []
    for repair in repairs:
        if not isinstance(repair, dict) or repair.get('segment') not in segments or repair['segment'] in seen:
            raise ValueError('每轮只允许为授权范围内的镜头各登记一个返修')
        allowed = {'segment','provider','issue','preserve','evidence','libtv_project','model','prompt','params','references','watermark','quoted_credits'}
        if set(repair) - allowed:
            raise ValueError('返修请求包含未支持的字段')
        seen.add(repair['segment'])
        provider = repair.get('provider')
        if provider not in ('rife', 'libtv'):
            raise ValueError('返修提供方须为 rife 或 libtv')
        for field in ('issue', 'preserve'):
            generation.string(repair.get(field), field, 4000)
        if provider == 'libtv':
            generation.integer(repair.get('quoted_credits'), '单次 LibTV 授权额度', 1, cap)
            for field in ('libtv_project', 'model', 'prompt'):
                generation.string(repair.get(field), field, 12000 if field=='prompt' else 200)
        elif repair.get('quoted_credits', 0) != 0:
            raise ValueError('本地补帧使用零平台积分')
        result.append({**repair, 'max_attempts': max_attempts})
    return result


def start(app, body):
    run_id = body.get('request_id')
    path = app.safe(record_path(run_id))
    request = {k: v for k, v in body.items() if k != 'request_id'}
    if path.is_file():
        run = load(app, run_id)
        if run['request'] != request:
            raise ValueError('同一授权编号已用于其他制作请求')
        return view(run)
    if body.get('authorize') is not True:
        raise ValueError('请明确授权本次制作范围和预算')
    if any(item['enabled'] and item['status'] != 'interrupted' for item in listing(app)['items']):
        raise ValueError('已有制作运行，先暂停或完成')
    template = body.get('template', 'episode')
    source = body.get('source', 'auto')
    target = body.get('deliver', 'rough')
    acceptance = body.get('acceptance', 'director')
    if not isinstance(template,str) or template not in TEMPLATES or source not in ('auto', 'current', 'candidates') or target not in ('rough', 'jianying'):
        raise ValueError('制作模板、来源或交付方式无效')
    if acceptance not in ('director', 'reviewed') or target=='jianying' and acceptance!='director':
        raise ValueError('剪映回收版需要实际全片复查')
    if target=='jianying' and template!='episode':
        raise ValueError('剪映交接适用于整集制作模板')
    ordered = [row['id'] for row in app.api_segments().get('rows', [])]
    segments = body.get('segments', ordered)
    if (not isinstance(segments,list) or not 1<=len(segments)<=20 or any(not isinstance(s,str) or s not in ordered for s in segments)
            or len(set(segments))!=len(segments)):
        raise ValueError('请选择分段表中 1–20 个不重复镜头')
    expected = body.get('expected_dialogue', {})
    if not isinstance(expected,dict) or any(k not in segments or not isinstance(v,str) or len(v)>4000 for k,v in expected.items()):
        raise ValueError('台词须按授权镜号填写')
    confidence = body.get('confidence', .8)
    if type(confidence) not in (int,float) or not .5<=confidence<=1:
        raise ValueError('置信门槛须为 0.5–1')
    cap = generation.integer(body.get('max_credits', 0), '整集积分上限', 0, 1_000_000)
    limit = generation.integer(body.get('max_attempts', 1), '每镜总尝试次数', 1, 5)
    repairs = repair_requests(body, segments, cap, limit)
    if (template in ('review','rough') and repairs) or (template=='repair' and len(repairs)!=1):
        raise ValueError('指定镜头返修模板需要一项返修，素材审查和初剪模板不接收返修')
    if sum(r.get('quoted_credits',0) for r in repairs)>cap:
        raise ValueError('各镜首轮的合计授权报价超过整集预算')
    run = {'id': run_id, 'version': app.VERSION, 'request': request, 'template': template, 'segments': segments,
           'source': source, 'deliver': target, 'acceptance': acceptance, 'expected_dialogue': expected,
           'confidence': confidence, 'max_credits': cap, 'max_attempts': limit, 'concurrency': 1,
           'created': automation.now(), 'cycles': [], 'enabled': True, 'status': 'queued'}
    blank_cycle(run, repairs)
    save(app, run)
    launch(app, run)
    return view(run)


def launch(app, run):
    if run['id'] in ACTIVE:
        return
    event = threading.Event()
    ACTIVE[run['id']] = event
    threading.Thread(target=work, args=(app,run['id'],event), daemon=True).start()


def pause(app, body):
    run = load(app, body.get('id'))
    if run['status']=='completed':
        raise ValueError('交付已完成')
    run['enabled'] = False
    status(run, 'paused', '已暂停派发，已启动的子任务仍保留回执')
    save(app, run)
    if run['id'] in ACTIVE:
        ACTIVE.pop(run['id']).set()
    return view(run)


def resume(app, body):
    run = load(app, body.get('id'))
    if any(item['id']!=run['id'] and item['enabled'] and item['status']!='interrupted' for item in listing(app)['items']):
        raise ValueError('另一个制作运行尚未暂停或完成')
    c = cycle(run)
    if body.get('refresh') is True:
        children = [c['initial'], *c['audits']]
        children += [r['candidate_review'] for r in c['repairs'] if r.get('candidate_review')]
        if any(child in automation.ACTIVE for child in children) or any(r['job_id'] in generation.ACTIVE for r in c['repairs']):
            raise ValueError('本轮子任务仍在执行，请等它保留回执后再刷新')
        unresolved = {'queued','waiting_host','needs_reconciliation'}
        if any(j['id'] in {r['job_id'] for r in c['repairs']} and j['status'] in unresolved for j in budget(app,run)['jobs']):
            raise ValueError('本轮还有待处理的外部任务，请查询原任务并登记结果后再刷新')
        if body.get('repairs') and body.get('authorize_repairs') is not True:
            raise ValueError('新增返修须明确授权本轮请求')
        repairs = repair_requests(body,run['segments'],run['max_credits'],run['max_attempts'])
        if run['template'] in ('review','rough') and repairs:
            raise ValueError('此模板不支持返修')
        if not c.get('report'):
            save_report(app,run,'进度报告')
        blank_cycle(run, repairs)
    elif run['status']=='completed':
        return view(run)
    else:
        for child in (c['initial'], *c['audits'], *(r.get('candidate_review') for r in c['repairs'])):
            if child and existing(app,automation.BASE,child,'run.json'):
                job = automation.get(app,child)
                if job['status'] in ('failed','cancelled') and not job['input_stale'] and not automation.ACTIVE:
                    automation.resume(app,child)
                    break
    run['enabled'] = True
    status(run,'queued','恢复并核对上次回执')
    save(app,run)
    launch(app,run)
    return view(run)


def check(app, run, token, body, *, review_only=False, paths=None):
    if not existing(app,automation.BASE,token,'run.json'):
        if automation.ACTIVE or editing.ACTIVE:
            status(run,'queued','等待审片或导出执行器空闲')
            return None
        automation.start(app, {**body, 'confidence': run['confidence']}, request_id=token, owner=run['id'],
                         review_only=review_only, candidate_paths=paths)
    result = automation.get(app,token)
    if result['input_stale']:
        status(run,'needs_decision','制作输入已变化。核对后使用“按新输入续做”')
    elif result['status']=='interrupted':
        if not automation.ACTIVE:
            automation.resume(app,token)
        status(run,'running','恢复审片子任务')
    elif result['status'] in ('completed','needs_decision'):
        return result
    else:
        state = result['status']
        status(run,state,result.get('error') or result['stage'])
    return None


def initial_check(app,run):
    c = cycle(run)
    template = run['template']
    if 'mode' not in c:
        source = run['source']
        if template in ('review','rough') or source=='candidates':
            c['mode'] = 'rebuild'
        elif source=='current' or editing.read(app)['clips'] or app.api_timeline().get('V'):
            c['mode'] = 'inspect'
        else:
            c['mode'] = 'rebuild'
        save(app,run)
    body = {'expected_dialogue':run['expected_dialogue'],'mode':c['mode']}
    if c['mode']=='rebuild':
        body['segments'] = run['segments']
    else:
        _,entries,_ = workspace.inspection(app,'current',None,run['expected_dialogue'])
        if [e['segment'] for e in entries] != run['segments']:
            raise ValueError('当前时间线镜头与授权范围或顺序不一致，请核对后续跑')
    return check(app,run,c['initial'],body,review_only=template=='review')


def approve_candidate(app, job, packet):
    review_evidence.verify(app,packet)
    review = packet['review']
    note = f"整集返修候选审查 {packet['id']} · {review['actor']} · {review['reason']}"
    state = 'approved' if review['eligible'] else 'rejected'
    current = generation.review_record(app,job)
    if not current or current['status']!=state or current['note']!=note[:4000]:
        app.review_mutate({'action':'review','segment':job['segment'],'path':job['output'],
                           'fingerprint':job['output_hash'],'status':state,'note':note})


def repair_next(app,run,repair):
    request = repair['request']
    identity = repair['job_id']
    totals = budget(app,run)
    attempts = sum(len(j['attempts']) for j in totals['jobs'] if j['segment']==request['segment'])
    job = next((j for j in totals['jobs'] if j['id']==identity), None)
    quoted = request.get('quoted_credits',0)
    if totals['exceeded']:
        status(run,'needs_decision','提供方回执超过授权预算，已停止新派发，请核对实际账单')
        return False
    if job and job.get('applied_revision') is not None:
        return True
    if not job:
        if attempts>=run['max_attempts'] or quoted>totals['remaining']:
            status(run,'needs_decision','整集积分或该镜尝试次数不足')
            return False
        if generation.ACTIVE and request['provider']=='rife':
            status(run,'queued','等待本地模型空闲')
            return False
        generation.submit(app,{**request,'request_id':identity,'max_credits':run['max_credits'],
                               'confirm_spend':True},owner=run['id'])
        status(run,'running',f"已登记 {request['segment']} 返修")
        return False
    if job['status']=='completed':
        token = subid(run,f"candidate:{identity}:{len(job['attempts'])}")
        if repair.get('candidate_review')!=token:
            repair['candidate_review']=token
            save(app,run)
        sid = job['segment']
        expected = {sid:run['expected_dialogue'][sid]} if sid in run['expected_dialogue'] else {}
        result = check(app,run,token,{'mode':'rebuild','segments':[sid],'expected_dialogue':expected},
                       review_only=True,paths={sid:[job['output']]})
        if result is None:
            return False
        packet = result['packets'][0]
        review = packet['review']
        if review['eligible']:
            if review['usable'][1]-review['usable'][0]+.000001 < job['source']['out']-job['source']['in']:
                status(run,'needs_decision','返修候选过审区间短于原镜头，请核对切点')
                return False
            approve_candidate(app,job,packet)
            generation.apply(app,{'job_id':identity,'in':review['usable'][0]})
            return True
        if not any(item['status']=='fail' for item in review['checks'].values()):
            status(run,'needs_decision','返修审查证据不足，请补审后续跑')
            return False
        approve_candidate(app,job,packet)
    elif job['status'] not in ('failed','interrupted'):
        state = 'waiting_host' if job['provider']=='libtv' and job['status']=='queued' else job['status']
        status(run,state,job.get('error') or job['stage'])
        return False
    if attempts>=run['max_attempts'] or quoted>totals['remaining']:
        status(run,'needs_decision','返修未通过，整集积分或该镜尝试次数已用完')
        return False
    if generation.ACTIVE and request['provider']=='rife':
        status(run,'queued','等待本地模型空闲')
        return False
    generation.retry(app,{'job_id':identity,'attempt':len(job['attempts']),'quoted_credits':quoted,
                          'confirm_spend':True},owner=run['id'])
    status(run,'running',f"在授权额度内重试 {job['segment']}")
    return False


def handoff_lineage(app,root):
    items = finishing.listing(app)['items']
    ids = {root}
    # A managed revision is always created after its parent.
    for item in reversed(items):
        if item['parent_id'] and item['parent_id'] in ids:
            ids.add(item['id'])
    return [item for item in items if item['id'] in ids]


def approved_film(app,run,check_result):
    c = cycle(run)
    rough = check_result['final']
    if run['deliver']=='rough':
        if run['acceptance']=='director' and not c.get('decision'):
            status(run,'waiting_review','初剪已复查，等待成片验收')
            return None
        if c.get('decision',{}).get('status')=='rejected':
            status(run,'needs_decision','成片已退回，请修改受影响镜头后续做')
            return None
        return {'path':rough,'film_hash':check_result['final_hash'],'acceptance':c.get('decision',{'status':'approved','actor':'authorized_review_policy'})}
    if 'handoff' not in c:
        previous = next((item['handoff'] for item in reversed(run['cycles'][:-1])
                         if item.get('handoff') and item.get('rough')==c['rough']),None)
        c['handoff'] = previous or subid(run,'jianying')
        save(app,run)
    token = c['handoff']
    if not app.safe(finishing.record_path(token)).is_file():
        snapshot = finishing.preview(app,{'source':'final','final':rough})
        finishing.create(app,{'source':'final','final':rough,'signature':snapshot.get('signature'),'request_id':token})
    else:
        saved = finishing.load(app,token)
        if saved['status']=='preparing' and (finishing.managed(app,saved)/'jianying_mcp_project.json').is_file():
            saved.update(finishing.verify_package(app,saved),status='prepared')
            finishing.save(app,saved)
    lineage = handoff_lineage(app,token)
    handoff = next(item for item in lineage if item['id']==token)
    if handoff['status']=='needs_reconciliation':
        status(run,'needs_reconciliation','剪映草稿回执待核对')
        return None
    outputs = [item for revision in lineage for item in revision['outputs']
               if not item['stale'] and item['reviews'] and item['reviews'][-1]['status']=='approved']
    if c.get('decision'):
        outputs = [item for item in outputs if item['final']==c['decision']['path'] and item['film_hash']==c['decision']['film_hash']]
    if len(outputs)!=1:
        status(run,'waiting_host','请在剪映精修面板发布草稿、实际导出、回收并完成复查；有多版时选择交付版')
        return None
    item = outputs[0]
    return {'path':item['final'],'film_hash':item['film_hash'],'acceptance':item['reviews'][-1],
            'handoff':item['handoff_id'],'technical_report':item['reviews'][-1]['scan_id']}


def advance(app,run):
    c = cycle(run)
    result = automation.get(app,c['initial']) if c.get('repair_ready') else initial_check(app,run)
    if result is None:
        return
    if result.get('final') and app.file_hash(app.safe(result['final']))!=result['final_hash']:
        status(run,'needs_decision','初剪文件内容变化，请按新输入续做')
        return
    if c['repairs'] and result.get('final'):
        # With a newly selected cut, using the working draft requires explicit authority.
        if result['mode']=='rebuild' and not c.get('adopted'):
            if result['status']!='completed' or run['request'].get('adopt_rough') is not True:
                status(run,'needs_decision','需先过审初剪并授权采用为返修工作草稿')
                return
            before = editing.read(app)
            clips = editing.validate(app,[{k:x[k] for k in ('id','src','in','out','a')} for x in result['selection']])
            if before['revision']==result['draft_revision']:
                automation.apply_suggestion(app,{'run_id':result['id'],'draft_revision':before['revision']})
            elif before['revision']!=result['draft_revision']+1 or before['clips']!=clips:
                raise ValueError('草稿另有编辑，请核对后按新输入续做')
            c['adopted'] = True
        if not c.get('repair_ready'):
            c['repair_ready'] = True
            save(app,run)
        if not c['audits']:
            ids = {r['job_id'] for r in c['repairs']}
            applied = [j for j in budget(app,run)['jobs'] if j['id'] in ids and j.get('applied_revision') is not None]
            latest = max(applied,key=lambda j:j['applied_revision'],default=None)
            if latest and editing.read(app)['revision']!=latest['applied_revision']:
                # An apply may have committed its draft before its own receipt was persisted.
                pending = [generation.read(app,j['id']).get('apply_pending') for j in budget(app,run)['jobs']
                           if j['id'] in ids and j.get('applied_revision') is None]
                state = editing.read(app)
                if {'revision':state['revision'],'clips':workspace.digest(state['clips'])} not in pending:
                    raise ValueError('已返修草稿之后又有修改，请按新输入续做')
            if not latest and result['mode']=='inspect' and result['input_stale']:
                pending = any(generation.read(app,j['id']).get('apply_pending') for j in budget(app,run)['jobs'] if j['id'] in ids)
                if not pending:
                    raise ValueError('工作剪辑已变化，请按新输入续做')
        for repair in c['repairs']:
            if not repair_next(app,run,repair):
                return
        if not c['audits']:
            c['audits'].append(subid(run,'after-repairs'))
            save(app,run)
        result = check(app,run,c['audits'][-1],{'mode':'inspect','expected_dialogue':run['expected_dialogue']})
        if result is None:
            return
    c['checked'] = result['id']
    if result['status']!='completed':
        status(run,'needs_decision','有待解决的候选、接缝或全片复查问题')
        return
    if run['template']=='review':
        complete(app,run)
        return
    c['rough'] = {'path':result['final'],'film_hash':result['final_hash']}
    if not c['rough']['film_hash'] or app.file_hash(app.safe(c['rough']['path']))!=c['rough']['film_hash']:
        status(run,'needs_decision','初剪文件已变化，请重新复查')
        return
    for packet in result['packets']:
        review_evidence.verify(app,packet)
    if run['template']=='rough':
        c['delivery']={**c['rough'],'acceptance':{'status':'reviewed_rough','actor':'review'}}
    else:
        delivery = approved_film(app,run,result)
        if delivery is None:
            return
        c['delivery'] = delivery
    complete(app,run)


def decide(app,body):
    run = load(app,body.get('id'))
    c = cycle(run)
    if (not c.get('rough') or run['status'] not in ('waiting_host','waiting_review')
            or not c.get('checked') or automation.get(app,c['checked'])['input_stale']):
        raise ValueError('当前没有可验收的成片，请先完成新版复查')
    path, fingerprint = body.get('path'), body.get('film_hash')
    decision = body.get('status')
    if run['deliver']=='jianying':
        item = finishing.receipt(app,path)
        ids = {x['id'] for x in handoff_lineage(app,c.get('handoff'))}
        if item['handoff_id'] not in ids or not item['reviews'] or item['reviews'][-1]['status']!='approved' or decision!='approved':
            raise ValueError('请选择本轮已实际复查通过的剪映回收版')
    elif path!=c['rough']['path'] or decision not in ('approved','rejected'):
        raise ValueError('请选择本轮初剪并提交通过或退回')
    if not fingerprint or app.file_hash(app.safe(path))!=fingerprint:
        raise ValueError('成片指纹不符，请刷新后重新核对')
    c['decision'] = {'path':path,'film_hash':fingerprint,'status':decision,'actor':'operator',
                     'note':generation.string(body.get('note'),'验收说明',4000),'at':automation.now()}
    save(app,run)
    return view(run)


def report(app,run,number=None):
    if number is not None and (type(number) is not int or not 1<=number<=len(run['cycles'])):
        raise ValueError('制作轮次不存在')
    c = cycle(run) if number is None else run['cycles'][number-1]
    if c.get('report'):
        return json.loads(app.safe(c['report']).read_text())
    totals = budget(app,run)
    checks, artifacts, findings = [], [], []
    for token in (c['initial'], *c['audits'], *(r.get('candidate_review') for r in c['repairs'])):
        if not token or not existing(app,automation.BASE,token,'run.json'):
            continue
        result = automation.get(app,token)
        checks.append({'id':token,'mode':result['mode'],'status':result['status'],'input_signature':result['input_signature'],
                       'stale':result['input_stale'],'selection':result['selection'],'shots':result['shots'],
                       'timeline_sources':result.get('plan',{}).get('clips',result['selection']),
                       'review_profile':result.get('profile'),
                       'packets':[{k:p.get(k) for k in ('id','kind','segment','source','source_hash','range','current_range','reused_from','signature','technical','review')}
                                  for p in result['packets']]})
        findings.extend(result['findings'])
        if result.get('final'):
            artifacts.append({'path':result['final'],'film_hash':result['final_hash'],'kind':'rough'})
        if result.get('export_id'):
            encoded = next((x for x in editing.jobs(app) if x['id']==result['export_id']),None)
            if encoded and not result.get('reused_run'):
                checks[-1]['encoding']={k:encoded[k] for k in ('id','encoded_segments','reused_segments') if k in encoded}
    if c.get('handoff') and app.safe(finishing.record_path(c['handoff'])).is_file():
        for handoff in handoff_lineage(app,c['handoff']):
            artifacts.append({'kind':'editable_jianying','id':handoff['id'],'parent_id':handoff['parent_id'],
                              'path':handoff['package'],'status':handoff['status'],
                              'native_state':handoff['native_state'],'outputs':handoff['outputs']})
    if c.get('delivery'):
        path = app.safe(c['delivery']['path'])
        artifacts.append({'kind':'delivery',**c['delivery'],'metadata':app.probe(path) if path and path.is_file() else {}})
    unresolved = [] if c.get('delivery') or c.get('checked') and run['status']=='completed' else [c['stage']]
    return {'run_id':run['id'],'cycle':c['number'],'version':run['version'],'template':run['template'],
            'authorization':run['request'],'segments':run['segments'],'concurrency':run['concurrency'],
            'state':run['status'],'stage':c['stage'],'budget':totals,'checks':checks,'artifacts':artifacts,
            'findings':findings,'unresolved':unresolved,'handoff':c.get('handoff'),'delivery':c.get('delivery'),'decision':c.get('decision')}


def save_report(app,run,label):
    c = cycle(run)
    output = f"{BASE}/{run['id']}/{label}_v{c['number']:03d}.json"
    editing.write(app,output,report(app,run))
    c['report'] = output


def complete(app,run):
    status(run,'completed','已生成交付报告')
    save_report(app,run,'交付报告')
    run['enabled'] = False


def work(app,identity,event):
    import agent_api
    try:
        while not event.is_set():
            with agent_api.LOCK:
                run = load(app,identity)
                if not run['enabled'] or ACTIVE.get(identity) is not event:
                    break
                before = copy.deepcopy(run)
                try:
                    advance(app,run)
                except (ValueError, OSError, RuntimeError) as error:
                    status(run,'needs_decision',str(error))
                if before!=run:
                    save(app,run)
                if not run['enabled']:
                    break
            event.wait(.8)
    finally:
        with agent_api.LOCK:
            if ACTIVE.get(identity) is event:
                ACTIVE.pop(identity)


ACTIONS = {'start':start,'pause':pause,'resume':resume,'decide':decide}
