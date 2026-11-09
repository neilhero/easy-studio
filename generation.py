"""Model jobs and reviewed, equal-length single-shot repairs."""
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import editing
import workspace

BASE = '反馈/生成返修'
ACTIVE = set()


def local_config():
    binary = os.environ.get('STUDIO_RIFE_BIN')
    model = os.environ.get('STUDIO_RIFE_MODEL')
    if binary and model:
        tool, weights = pathlib.Path(binary), pathlib.Path(model)
        if tool.is_absolute() and weights.is_absolute() and tool.is_file() and os.access(tool, os.X_OK) and all((weights / name).is_file() for name in ('flownet.bin', 'flownet.param')):
            return str(tool), str(weights)
    return None


def capabilities():
    return {
        'libtv': {'transport': 'host_mcp', 'modes': ['text-to-video', 'reference-to-video'], 'credits': True},
        'rife': {'transport': 'local_worker', 'model': 'RIFE 4.6', 'ready': bool(local_config()),
                 'modes': ['interpolate-2x'], 'source': 'current_clip', 'max_seconds': 12,
                 'audio': 'same speed, source audio retained', 'concurrency': 1},
    }


def path_for(app, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise ValueError('返修任务ID无效')
    return app.safe(f'{BASE}/{job_id}.json')


def read(app, job_id):
    path = path_for(app, job_id)
    if not path or not path.is_file():
        raise ValueError('返修任务不存在')
    job = json.loads(path.read_text())
    if job['provider'] == 'rife' and job['status'] == 'running' and job_id not in ACTIVE:
        job.update(status='interrupted', error='本地推理中断，可按原镜头重试')
    lease = job.get('lease')
    if lease and lease['until'] < time.time():
        if lease['phase'] == 'submit':
            job.update(status='needs_reconciliation', error='提交回执未知，只能恢复原请求')
        else:
            job.update(status='waiting_host', error='认领已到期，可继续查询或下载原任务')
    return job


def budget_used(job):
    return sum(a['credits'] if a['credits'] is not None else a['reserved'] for a in job['attempts'])


def public(job):
    result = {k: v for k, v in job.items() if k not in ('lease', 'base', 'original_request')}
    result['budget_used'] = budget_used(job)
    if job.get('lease'):
        result['claimed_until'] = job['lease']['until']
    return result


def get(app, job_id):
    job = read(app, job_id)
    result = public(job)
    review = review_record(app, job)
    result['review_status'] = review['status'] if review else 'pending'
    return result


def jobs(app):
    folder = app.safe(BASE)
    if not folder or not folder.is_dir():
        return []
    return sorted((get(app, p.stem) for p in folder.glob('*.json') if re.fullmatch(r'[0-9a-f]{32}', p.stem)),
                  key=lambda j: j['updated'], reverse=True)


def save(app, job):
    job['updated'] = time.time()
    editing.write(app, f"{BASE}/{job['id']}.json", job)


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name}须为 {minimum}–{maximum} 的整数')
    return value


def string(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{name}不能为空，最多 {limit} 字符')
    return value.strip()


def attempt(job, quoted_credits):
    if len(job['attempts']) >= job['max_attempts'] or budget_used(job) + quoted_credits > job['max_credits']:
        raise ValueError('返修预算或重试次数已用完')
    number = len(job['attempts']) + 1
    entry = {'number': number, 'key': f"lazy-repair-{job['id']}-{number}", 'reserved': quoted_credits,
             'credits': 0 if job['provider'] == 'rife' else None,
             'task_id': None, 'state': 'queued', 'started': time.time()}
    job['attempts'].append(entry)
    job.update(status='queued', phase='submit', stage='等待提交', lease=None, error='', output=None,
               output_hash=None, delivery=None)
    return entry


def submit(app, body):
    request_id = body.get('request_id') or uuid.uuid4().hex
    path = path_for(app, request_id)
    if not path:
        raise PermissionError('返修任务目录越界')
    if path.exists():
        previous = read(app, request_id)
        if previous['original_request'] != body:
            raise ValueError('同一请求编号不能用于不同返修')
        return public(previous)
    provider = body.get('provider')
    if provider not in ('rife', 'libtv'):
        raise ValueError('请选择 RIFE 或 LibTV')
    row = app.segment_row(body.get('segment'))
    plan = workspace.source(app)
    indices = [i for i, clip in enumerate(plan['clips']) if clip['id'] in (row['id'], row['scene'])]
    if len(indices) != 1:
        raise ValueError('当前剪辑中必须有且只有一处对应镜头')
    clip = plan['clips'][indices[0]]
    issue = string(body.get('issue'), '返修问题', 4000)
    preserve = string(body.get('preserve'), '必须保留的内容', 4000)
    evidence = body.get('evidence', '')
    if not isinstance(evidence, str) or len(evidence) > 2000:
        raise ValueError('证据说明最多 2000 字符')
    request = {}
    existing_task = body.get('resume_task_id')
    if existing_task and provider != 'libtv':
        raise ValueError('只有 LibTV 可恢复已有外部任务')
    max_attempts = integer(body.get('max_attempts', 1), '总尝试次数', 1, 5)
    if provider == 'libtv':
        request = {
            'project_id': string(body.get('libtv_project'), 'LibTV 画布 ID', 200),
            'model': string(body.get('model'), 'LibTV 模型', 200),
            'prompt': '' if existing_task else string(body.get('prompt'), '生成提示词', 12000),
            'params': body.get('params', {}),
            'references': body.get('references', {}),
            'watermark': body.get('watermark', 'keep'),
        }
        refs = request['references']
        if not isinstance(request['params'], dict) or not isinstance(refs, dict) or set(refs) - {'image', 'video', 'audio', 'start_frame'}:
            raise ValueError('生成参数或参考节点格式无效')
        if any(not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(node, str) or not node or len(node) > 200 for node in ids) for ids in refs.values()):
            raise ValueError('参考必须是 LibTV 画布中的节点 ID 列表')
        if 'start_frame' in refs and (len(refs) != 1 or len(refs['start_frame']) != 1):
            raise ValueError('单首帧不能与其他参考混用')
        if request['watermark'] not in ('keep', 'remove'):
            raise ValueError('请选择保留或移除水印')
        if existing_task:
            existing_task = string(existing_task, 'LibTV 原任务 ID', 200)
            max_credits = quoted = 0
            max_attempts = 1
        else:
            max_credits = integer(body.get('max_credits'), '预算上限', 1, 1000000)
            quoted = integer(body.get('quoted_credits'), '本次预留额度', 1, max_credits)
            if body.get('confirm_spend') is not True:
                raise ValueError('LibTV 提交需确认本次预算')
    else:
        check_local(app, clip)
        request = {'model': 'RIFE 4.6', 'mode': 'interpolate-2x'}
        max_credits = quoted = 0
    first = app.probe(app.safe(plan['clips'][0]['src']))
    canvas = [first['width'], first['height']]
    prior = next((j for j in editing.jobs(app) if j.get('revision') == plan['draft_revision'] and j.get('canvas')), None)
    if prior:
        canvas = prior['canvas']
    job = {'id': request_id, 'segment': row['id'], 'provider': provider,
           'request': request, 'original_request': body, 'issue': issue,
           'preserve': preserve, 'evidence': evidence, 'source': clip,
           'base': {'signature': plan['signature'], 'revision': plan['draft_revision'], 'index': indices[0], 'canvas': canvas},
           'max_attempts': max_attempts, 'max_credits': max_credits, 'attempts': []}
    entry = attempt(job, quoted)
    if provider == 'rife':
        launch_local(app, job)
    else:
        if existing_task:
            entry.update(task_id=existing_task, state='resumed')
            job.update(status='waiting_host', phase='poll', stage='等待查询 LibTV 原任务')
        else:
            job['stage'] = '等待智能体提交 LibTV'
        save(app, job)
    return public(job)


def claim(app, body):
    job = read(app, body.get('job_id'))
    if job['provider'] != 'libtv' or job['phase'] not in ('submit', 'poll', 'collect'):
        raise ValueError('该任务没有待执行的 LibTV 步骤')
    if job.get('lease') and job['lease']['until'] >= time.time():
        raise ValueError('该步骤已被认领')
    if job['status'] == 'needs_reconciliation' and body.get('recover') is not True:
        raise ValueError('提交结果未知，请恢复同一请求或登记已知任务 ID')
    recovering = job['status'] == 'needs_reconciliation'
    if job['phase'] == 'submit' and not recovering:
        if workspace.source(app)['signature'] != job['base']['signature']:
            raise ValueError('原剪辑已变化，请按当前镜头重新提交')
    actor = string(body.get('actor'), '执行者', 120)
    lease = {'token': uuid.uuid4().hex, 'phase': job['phase'], 'actor': actor, 'until': time.time() + 1800}
    job.update(lease=lease, status='running')
    save(app, job)
    return {**public(job), 'lease_token': lease['token'], 'recovering': recovering}


def record(app, body):
    job = read(app, body.get('job_id'))
    lease = job.get('lease') or {}
    if not lease or body.get('lease_token') != lease['token'] or lease['until'] < time.time():
        raise ValueError('认领已失效，请重新认领或恢复原请求')
    current = job['attempts'][-1]
    outcome = body.get('outcome')
    if outcome == 'unknown':
        job.update(status='needs_reconciliation' if lease['phase'] == 'submit' else 'waiting_host',
                   stage='待核对原任务', error='当前步骤未确认，保留原请求与预算')
    elif lease['phase'] == 'collect':
        if outcome != 'downloaded':
            raise ValueError('请登记已按 LibTV 交付清单保存的文件')
        collect_libtv(app, job, body)
    else:
        if outcome not in ('submitted', 'pending', 'succeeded', 'failed'):
            raise ValueError('请登记已核对的提供方任务状态')
        task_id = string(body.get('task_id'), 'LibTV 原任务 ID', 200)
        if current['task_id'] and current['task_id'] != task_id:
            raise ValueError('同一次尝试不能改绑其他任务')
        actual = body.get('actual_credits')
        if actual is not None:
            actual = integer(actual, '实际积分', 0, 1000000)
            if current['credits'] is not None and current['credits'] != actual:
                raise ValueError('实际费用与已登记账单不一致')
        current.update(task_id=task_id, state=outcome)
        if actual is not None:
            current['credits'] = actual
        if outcome == 'failed':
            job.update(status='failed', phase='done', stage='LibTV 生成失败',
                       error=string(body.get('reason'), '失败原因', 2000))
        elif outcome == 'succeeded':
            current['node_id'] = string(body.get('node_id'), '已完成的 LibTV 视频节点 ID', 200)
            current['output_index'] = integer(body.get('output_index', 0), '输出序号', 0, 99)
            job.update(status='waiting_host', phase='collect', stage='等待按交付清单下载', error='')
        else:
            job.update(status='waiting_host', phase='poll', stage='等待查询 LibTV 原任务', error='')
    job['lease'] = None
    save(app, job)
    return public(job)


def collect_libtv(app, job, body):
    info = body.get('delivery')
    if not isinstance(info, dict):
        raise ValueError('缺少 LibTV 授权交付记录')
    entry = job['attempts'][-1]
    if info.get('project_id') != job['request']['project_id'] or info.get('node_id') != entry['node_id'] or info.get('watermark') != job['request']['watermark']:
        raise ValueError('交付须匹配原画布、视频节点与水印选择')
    identity = string(info.get('source_identity'), '交付素材身份', 300)
    index = integer(info.get('output_index'), '输出序号', 0, 99)
    if index != entry['output_index']:
        raise ValueError('交付文件须对应已登记的输出序号')
    source = pathlib.Path(string(body.get('source'), '本地交付路径', 4000))
    if not source.is_absolute():
        raise PermissionError('本地交付须使用绝对路径')
    source = source.resolve()
    if not any(source.is_relative_to(p) for p in app.IMPORT_ROOTS):
        raise PermissionError('交付文件须在已授权导入目录内')
    fingerprint = string(body.get('sha256'), '本地文件 SHA-256', 64)
    if not source.is_file() or not re.fullmatch(r'[0-9a-f]{64}', fingerprint) or app.file_hash(source) != fingerprint:
        raise ValueError('交付文件与校验值不一致')
    job['delivery'] = {'project_id': info['project_id'], 'node_id': info['node_id'],
                       'watermark': info['watermark'], 'source_identity': identity, 'output_index': index}
    publish(app, job, source, fingerprint)


def tool(name):
    executable = os.environ.get(name + '_BIN') or shutil.which(name.lower())
    if not executable:
        raise ValueError(f'请配置 {name}_BIN')
    return executable


def command(args, timeout=600):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError(result.stderr.strip()[-1500:] or '媒体命令失败')


def publish(app, job, source, fingerprint):
    if not source.is_file() or app.kind(source) != 'video' or source.suffix.lower() not in ('.mp4', '.mov'):
        raise ValueError('候选须为可解码的 MP4 或 MOV')
    if source.stat().st_size > 1024 ** 3:
        raise ValueError('单个候选文件限 1 GiB')
    info = app.probe(source)
    if not info.get('width') or not 0 < (info.get('duration') or 0) <= 60:
        raise ValueError('候选视频须在 60 秒内')
    current = job['attempts'][-1]
    rel = f"生成/返修/{job['id']}/a{current['number']}{source.suffix.lower()}"
    target = app.safe(rel)
    if not target:
        raise PermissionError('候选输出路径越界')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=source.suffix) as temp:
        shutil.copyfile(source, temp.name)
        if app.file_hash(pathlib.Path(temp.name)) != fingerprint:
            raise ValueError('复制期间候选发生变化')
        command([tool('FFMPEG'), '-nostdin', '-v', 'error', '-xerror', '-i', temp.name,
                 '-map', '0:v:0', '-map', '0:a?', '-f', 'null', '-'])
        if target.exists():
            if app.file_hash(target) != fingerprint:
                raise ValueError('该次尝试已有不同产物，请先核对')
        else:
            os.link(temp.name, target)
    app.review_mutate({'action': 'associate', 'segment': job['segment'], 'path': rel})
    current.update(state='completed', output=rel, output_hash=fingerprint, delivery=job.get('delivery'),
                   bytes=target.stat().st_size, elapsed=round(time.time() - current['started'], 2))
    job.update(output=rel, output_hash=fingerprint, status='completed', phase='done', stage='新候选待审', error='')


def check_local(app, clip):
    if not local_config():
        raise ValueError('本地 RIFE 尚未配置')
    if ACTIVE:
        raise ValueError('本地模型正在处理另一镜')
    if not .16 <= clip['out'] - clip['in'] <= 12:
        raise ValueError('本地补帧支持 0.16–12 秒')
    if app.file_hash(app.candidate_file(clip['src'])) != clip['fingerprint']:
        raise ValueError('返修源片已变化')


def launch_local(app, job):
    config = local_config()
    binary, weights = map(pathlib.Path, config)
    fingerprint = workspace.digest([app.file_hash(p) for p in (binary, weights / 'flownet.param', weights / 'flownet.bin')])
    job['attempts'][-1]['model_fingerprint'] = fingerprint
    job.update(status='running', phase='local', stage='本地 RIFE 推理')
    save(app, job)
    ACTIVE.add(job['id'])
    threading.Thread(target=local_worker, args=(app, job, config), daemon=True).start()


def local_worker(app, job, config):
    import agent_api

    try:
        clip = job['source']
        source = app.candidate_file(clip['src'])
        if app.file_hash(source) != clip['fingerprint']:
            raise ValueError('返修源片已变化')
        ff = tool('FFMPEG')
        length = clip['out'] - clip['in']
        info = app.probe(source)
        fps = min(30, max(12, round(info['fps'])))
        with tempfile.TemporaryDirectory() as directory:
            work = pathlib.Path(directory)
            frames, inferred = work / 'input', work / 'output'
            frames.mkdir()
            inferred.mkdir()
            command([ff, '-nostdin', '-v', 'error', '-ss', clip['in'], '-i', source, '-t', length,
                     '-vf', f'fps={fps},scale=min(1280\\,iw):min(720\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2', '-start_number', '0', frames / '%08d.png'])
            count = len(list(frames.glob('*.png')))
            if count < 2:
                raise ValueError('镜头至少需要两帧')
            binary, weights = config
            command([binary, '-i', frames, '-o', inferred, '-m', weights, '-n', 2 * count,
                     '-f', '%08d.png', '-j', '1:2:1', '-g', os.environ.get('STUDIO_RIFE_DEVICE', '0')])
            if len(list(inferred.glob('*.png'))) != 2 * count:
                raise ValueError('模型输出帧数不符')
            output = work / 'candidate.mp4'
            command([ff, '-nostdin', '-v', 'error', '-framerate', fps * 2, '-start_number', '0',
                     '-i', inferred / '%08d.png', '-ss', clip['in'], '-i', source,
                     '-t', length, '-map', '0:v:0', '-map', '1:a:0?', '-vf', 'tpad=stop_mode=clone:stop_duration=0.25', '-c:v', 'libx264',
                     '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', output])
            with agent_api.LOCK:
                if app.file_hash(source) != clip['fingerprint']:
                    raise ValueError('推理期间源片变化')
                publish(app, job, output, app.file_hash(output))
                save(app, job)
    except Exception as error:
        with agent_api.LOCK:
            job['attempts'][-1].update(state='failed', elapsed=round(time.time() - job['attempts'][-1]['started'], 2))
            job.update(status='failed', phase='done', stage='本地推理失败', error=str(error))
            save(app, job)
    finally:
        ACTIVE.discard(job['id'])


def review_record(app, job):
    if not job.get('output'):
        return None
    current = app.file_hash(app.safe(job['output']))
    if current != job['output_hash']:
        return None
    row = app.segment_row(job['segment'])
    linked = {c['path'] for c in row['files']['candidates']}
    if job['output'] not in linked:
        return None
    record = row['review_entry'].get('reviews', {}).get(job['output'])
    return record if record and record['fingerprint'] == current else None


def retry(app, body):
    job = read(app, body.get('job_id'))
    if body.get('attempt') != len(job['attempts']):
        raise ValueError('任务已更新，请刷新')
    reviewed = review_record(app, job)
    if job.get('applied_revision') is not None or (job['status'] not in ('failed', 'interrupted') and not (reviewed and reviewed['status'] == 'rejected')):
        raise ValueError('仅可重试明确失败、中断的本地任务或已退回候选')
    if job['provider'] == 'rife':
        check_local(app, job['source'])
        attempt(job, 0)
        launch_local(app, job)
    else:
        if body.get('confirm_spend') is not True:
            raise ValueError('再次提交 LibTV 须确认预算')
        quoted = integer(body.get('quoted_credits'), '本次预留额度', 1, job['max_credits'])
        attempt(job, quoted)
        job['stage'] = '等待智能体提交 LibTV'
        save(app, job)
    return public(job)


def apply(app, body):
    job = read(app, body.get('job_id'))
    review = review_record(app, job)
    if job['status'] != 'completed' or not review or review['status'] != 'approved':
        raise ValueError('请先审查并通过此候选')
    if job.get('applied_revision') is not None:
        raise ValueError('该候选已采用')
    state = editing.read(app)
    pending = job.get('apply_pending')
    if pending and pending == {'revision': state['revision'], 'clips': workspace.digest(state['clips'])}:
        job.update(applied_revision=state['revision'], stage='已采用，可导出新版')
        job.pop('apply_pending')
        save(app, job)
        return public(job)
    plan = workspace.source(app)
    if plan['signature'] != job['base']['signature'] or plan['draft_revision'] != job['base']['revision']:
        raise ValueError('原剪辑已变化，不能直接采用这次返修')
    start = body.get('in', 0)
    if type(start) not in (int, float) or not math.isfinite(start) or start < 0:
        raise ValueError('候选入点无效')
    index = job['base']['index']
    previous = plan['clips'][index]
    end = start + (previous['out'] - previous['in'])
    candidate = app.candidate_file(job['output'])
    if end > app.probe(candidate)['duration'] + .001:
        raise ValueError('候选可用区间短于原镜头，请先选足等长片段')
    clips = list(plan['clips'])
    clips[index] = {**previous, 'src': job['output'], 'in': start, 'out': end}
    clips = editing.validate(app, clips)
    if clips[index]['fingerprint'] != job['output_hash'] or any(
        actual != expected for i, (actual, expected) in enumerate(zip(clips, plan['clips'])) if i != index
    ):
        raise ValueError('素材已变化，请重新核对')
    job['apply_pending'] = {'revision': plan['draft_revision'] + 1, 'clips': workspace.digest(clips)}
    save(app, job)
    state = editing.mutate(app, {'revision': plan['draft_revision'], 'clips': clips}, expected_clips=clips)
    job.update(applied_revision=state['revision'], stage='已采用，可导出新版')
    job.pop('apply_pending')
    save(app, job)
    return public(job)



def export(app, body):
    job = read(app, body.get('job_id'))
    revision = job.get('applied_revision')
    if revision is None:
        raise ValueError('请先采用通过审查的候选')
    prior = next((j for j in editing.jobs(app) if j.get('repair_job') == job['id']), None)
    if prior:
        return prior
    state = editing.read(app)
    if state['revision'] != revision:
        raise ValueError('草稿已变化，请检查当前剪辑后导出')
    review = review_record(app, job)
    if not review or review['status'] != 'approved':
        raise ValueError('候选审查已变化，请复核')
    return editing.start(app, {}, snapshot={
        'revision': revision, 'clips': state['clips'], 'canvas': job['base']['canvas'],
        'repair_job': job['id'],
    })
