"""Phosphene local LTX jobs, with durable receipts and candidate collection."""
import json
import hashlib
import os
import pathlib
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

ACTIVE = set()


def config():
    url = os.environ.get('STUDIO_PHOSPHENE_URL', '').rstrip('/')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('请配置本机 Phosphene HTTP 地址')
    root = pathlib.Path(os.environ.get('STUDIO_PHOSPHENE_OUTPUTS', '')).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('请配置 Phosphene 成片目录')
    return url, root.resolve()


def capability():
    try:
        url, _ = config()
        return {'transport': 'local_http', 'ready': True, 'url': url, 'model': 'LTX', 'modes': ['image-to-video'], 'concurrency': 1}
    except ValueError:
        return {'transport': 'local_http', 'ready': False, 'model': 'LTX', 'modes': ['image-to-video']}


def http(url, path, data=None, content_type=None):
    headers = {'Content-Type': content_type} if content_type else {}
    req = urllib.request.Request(url + path, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as response:
        return json.load(response)


def submit(app, body):
    import generation as g
    config()
    if ACTIVE or g.ACTIVE:
        raise ValueError('本地模型已有任务，请等待完成')
    row = app.segment_row(body.get('segment'))
    reference = app.safe(body.get('first_frame', ''))
    if not reference or not reference.is_file() or app.kind(reference) != 'image':
        raise ValueError('请选择项目内的参考首帧图片')
    prompt = g.string(body.get('prompt'), '生成提示词', 12000)
    seconds = g.integer(body.get('seconds', 5), '时长秒', 1, 10)
    seed = g.integer(body.get('seed', 21), '随机种子', 0, 2147483647)
    identity = body.get('request_id') or uuid.uuid4().hex
    job = {'id': identity, 'segment': row['id'], 'provider': 'phosphene', 'original_request': body,
           'issue': g.string(body.get('issue'), '生成要求', 4000),
           'preserve': g.string(body.get('preserve'), '保留要求', 4000), 'evidence': body.get('evidence', ''),
           'source': None, 'base': None, 'max_attempts': 1, 'max_credits': 0, 'attempts': [],
           'request': {'model': 'ltx', 'prompt': prompt, 'seconds': seconds, 'seed': seed,
                       'first_frame': app.rel(reference), 'reference_hash': app.file_hash(reference)}}
    g.attempt(job, 0)
    g.save(app, job)
    launch(app, job)
    return g.public(job)


def launch(app, job):
    if ACTIVE:
        raise ValueError('Phosphene 任务正在执行')
    ACTIVE.add(job['id'])
    threading.Thread(target=work, args=(app, job), daemon=True).start()


def resume(app, identity):
    import generation as g
    job = g.read(app, identity)
    if job['provider'] != 'phosphene' or not job['attempts'][-1].get('task_id'):
        raise ValueError('缺少 Phosphene 回执，请先到面板核对原任务')
    if job['status'] == 'completed':
        return g.public(job)
    if identity in ACTIVE:
        return g.public(job)
    job.update(status='running', phase='poll', error='')
    g.save(app, job)
    launch(app, job)
    return g.public(job)


def work(app, job):
    import generation as g
    entry = job['attempts'][-1]
    try:
        url, output_root = config()
        if not entry.get('task_id'):
            reference = app.safe(job['request']['first_frame'])
            if app.file_hash(reference) != job['request']['reference_hash']:
                raise ValueError('参考图已变化，请重新提交')
            if reference.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('参考首帧限 32 MB')
            image_bytes = reference.read_bytes()
            if hashlib.sha256(image_bytes).hexdigest() != job['request']['reference_hash']:
                raise ValueError('读取期间参考图已变化，请重新提交')
            boundary = uuid.uuid4().hex
            payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="reference{reference.suffix}"\r\n'
                       'Content-Type: application/octet-stream\r\n\r\n').encode() + image_bytes + f'\r\n--{boundary}--\r\n'.encode()
            uploaded = http(url, '/upload', payload, f'multipart/form-data; boundary={boundary}')
            fields = {'engine': 'ltx', 'mode': 'i2v', 'image': uploaded['path'], 'prompt': job['request']['prompt'],
                      'quality': 'quick', 'width': 768, 'height': 448, 'frames': 24 * job['request']['seconds'] + 1,
                      'seed': job['request']['seed'], 'upscale': 'off', 'preset_label': 'EasyStudio-' + job['id']}
            # Record before submitting. A dropped receipt must never trigger another generation.
            job.update(status='running', phase='dispatching', stage='提交 Phosphene')
            g.save(app, job)
            try:
                receipt = http(url, '/queue/add', urllib.parse.urlencode(fields).encode(), 'application/x-www-form-urlencoded')
                entry['task_id'] = g.string(receipt.get('id'), 'Phosphene 任务号', 200)
            except urllib.error.HTTPError as error:
                if 400 <= error.code < 500:
                    job['phase'] = 'refused'
                raise
            job.update(phase='poll', stage='Phosphene 本地生成中')
            g.save(app, job)
        while True:
            data = http(url, '/status?include_hidden=1')
            items = ([data['current']] if data.get('current') else []) + data.get('queue', []) + data.get('history', [])
            item = next((x for x in items if x['id'] == entry['task_id']), None)
            if not item:
                raise ValueError('原任务未出现在面板中，请保留任务号核对')
            if item['status'] in ('failed', 'error', 'cancelled'):
                job['phase'] = 'refused'
                raise ValueError(str(item.get('error') or 'Phosphene 任务未完成')[:1500])
            if item['status'] in ('completed', 'done'):
                source = pathlib.Path(item.get('output_path') or item.get('raw_path') or '').resolve()
                if not source.is_relative_to(output_root) or not source.is_file():
                    raise ValueError('Phosphene 输出不在配置目录内')
                if (app.probe(source).get('duration') or 0) + .1 < job['request']['seconds']:
                    raise ValueError('生成视频短于请求时长，请核对原任务')
                params = item.get('params') or {}
                if params.get('engine', 'ltx') != 'ltx' or params.get('mode') != 'i2v':
                    raise ValueError('实际任务模型或生成模式不符')
                job['delivery'] = {'task_id': entry['task_id'], 'provider': 'phosphene', 'model': 'ltx', 'params': params}
                import agent_api
                with agent_api.LOCK:
                    g.publish(app, job, source, app.file_hash(source))
                    g.save(app, job)
                break
            time.sleep(3)
    except Exception as error:
        uncertain = job.get('phase') == 'dispatching'
        job.update(status='needs_reconciliation' if uncertain else 'failed',
                   stage='请核对 Phosphene 原任务' if uncertain else 'Phosphene 任务需处理', error=str(error)[:1800])
        g.save(app, job)
    finally:
        ACTIVE.discard(job['id'])
