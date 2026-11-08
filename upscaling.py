"""持久化的本机超分队列，单进程串行执行，完成文件按内容与工具指纹复用。"""
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from fractions import Fraction
import editing

LOCK = threading.RLock()
WORKER = None
EVENTS = {}
DIRECTORY = '剪辑/超分任务'


def configuration(app):
    ff = os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
    fp = os.environ.get('FFPROBE_BIN') or shutil.which('ffprobe')
    sr = os.environ.get('REALESRGAN_BIN') or shutil.which('realesrgan-ncnn-vulkan')
    models = os.environ.get('REALESRGAN_MODELS')
    ready = bool(ff and fp and sr and models and all(pathlib.Path(x).is_file() for x in (ff, fp, sr)) and all((pathlib.Path(models) / ('realesr-animevideov3-x2.' + suffix)).is_file() for suffix in ('bin', 'param')))
    return {'ready': ready, 'ffmpeg': ff, 'ffprobe': fp, 'engine': sr, 'models': models,
            'reason': '' if ready else '请配置 FFMPEG_BIN、FFPROBE_BIN、REALESRGAN_BIN、REALESRGAN_MODELS（须包含 animevideov3 x2/x3 模型）'}


def stored(app):
    directory = app.safe(DIRECTORY)
    if not directory:
        raise PermissionError('超分任务目录越界')
    return [json.loads(p.read_text()) for p in sorted(directory.glob('*.json'))] if directory.exists() else []


def save(app, job):
    editing.write(app, f"{DIRECTORY}/{job['id']}.json", job)


def status(app):
    with LOCK:
        result = stored(app)
        for j in result:
            if j['status'] in ('queued', 'running') and j['id'] not in EVENTS:
                j.update(status='interrupted', stage='服务重启，等待手动重试')
        return {'configuration': configuration(app), 'jobs': list(reversed(result))}


def metadata(config, source):
    p = subprocess.run([config['ffprobe'], '-v', 'error', '-count_frames', '-show_streams', '-show_format', '-of', 'json', str(source)], check=True, capture_output=True, text=True, timeout=120)
    data = json.loads(p.stdout)
    v = next(x for x in data['streams'] if x['codec_type'] == 'video')
    fps = v['avg_frame_rate']
    if Fraction(fps) <= 0 or Fraction(fps) != Fraction(v['r_frame_rate']):
        raise ValueError('暂不支持可变帧率，请先转换为恒定帧率')
    return {'fps': fps, 'frames': int(v['nb_read_frames']), 'width': int(v['width']), 'height': int(v['height']),
            'duration': float(data['format']['duration']), 'has_audio': any(x['codec_type'] == 'audio' for x in data['streams'])}


def submit(app, body):
    global WORKER
    with LOCK:
        config = configuration(app)
        if not config['ready']:
            raise ValueError(config['reason'])
        retry = body.get('retry')
        if retry:
            old = next((j for j in status(app)['jobs'] if j['id'] == retry), None)
            if not old or old['status'] not in ('failed', 'cancelled', 'interrupted'):
                raise ValueError('只能重试失败、取消或中断任务')
            paths, scale, height = [old['source']], old['scale'], old['height']
            if app.file_hash(app.candidate_file(paths[0])) != old['source_hash']:
                raise ValueError('原片已变化，请作为新任务提交')
        else:
            paths, scale, height = body.get('paths'), body.get('scale', 2), body.get('height', 1080)
        if type(scale) is not int or scale not in (2, 3) or type(height) is not int or height not in (720, 1080, 1440, 2160):
            raise ValueError('超分倍率或目标高度无效')
        if not isinstance(paths, list) or not paths or len(paths) > 100 or not all(isinstance(p, str) for p in paths):
            raise ValueError('请选择1—100条视频')
        # 整批先验证，非法路径或工具缺失时不创建部分任务。
        sources = [app.candidate_file(p) for p in dict.fromkeys(paths)]
        model_files = [pathlib.Path(config['models']) / f'realesr-animevideov3-x{scale}.{ext}' for ext in ('bin', 'param')]
        if not all(p.is_file() for p in model_files):
            raise ValueError('所选倍率模型文件缺失')
        tool_hashes = {k: app.file_hash(pathlib.Path(config[k])) for k in ('engine', 'ffmpeg', 'ffprobe')}
        tool_hashes.update({p.name: app.file_hash(p) for p in model_files})
        created = []
        known = stored(app)
        for source in sources:
            fingerprint = app.file_hash(source)
            key = hashlib.sha256(json.dumps({'source': fingerprint, 'scale': scale, 'height': height, 'tools': tool_hashes, 'pipeline': 1}, sort_keys=True).encode()).hexdigest()
            existing = next((j for j in reversed(known) if j['key'] == key and (j['id'] in EVENTS or (j['status'] == 'completed' and app.file_hash(app.safe(j['output'])) == j.get('output_hash')))), None)
            if existing:
                created.append({**existing, 'reused': True})
                continue
            token = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
            output = f'素材库/Studio超分/{source.parent.name}_{source.stem}_{key[:12]}.mp4'
            if app.safe(output).exists():
                output = output[:-4] + '_' + token + '.mp4'
            job = {'id': token, 'source': app.rel(source), 'source_hash': fingerprint, 'key': key, 'tools': tool_hashes,
                   'scale': scale, 'height': height, 'output': output, 'status': 'queued', 'stage': '排队', 'frames_done': 0,
                   'frames': 0, 'elapsed': 0, 'eta': None, 'error': '', 'retry_of': retry}
            save(app, job)
            EVENTS[token] = threading.Event()
            known.append(job)
            created.append(job)
        if not WORKER or not WORKER.is_alive():
            WORKER = threading.Thread(target=work, args=(app, config), daemon=True)
            WORKER.start()
        return {'jobs': created}


def cancel(app, body):
    with LOCK:
        event = EVENTS.get(body.get('id'))
        if not event:
            raise ValueError('任务已结束或不存在')
        event.set()
        return {'ok': True}


def work(app, config):
    global WORKER
    while True:
        with LOCK:
            job = next((j for j in stored(app) if j['id'] in EVENTS), None)
            if job is None:
                WORKER = None
                return
        process(app, config, job)
        with LOCK:
            EVENTS.pop(job['id'], None)


def process(app, config, job):
    begin = time.monotonic()
    def check():
        if EVENTS[job['id']].is_set():
            raise editing.ExportCancelled('任务已取消')
    def update(stage, **extra):
        job.update(stage=stage, elapsed=round(time.monotonic()-begin, 1), **extra)
        save(app, job)
    def run(args, log, frames=None):
        check()
        with open(log, 'w') as stream, subprocess.Popen([str(x) for x in args], stdout=stream, stderr=stream) as proc:
            try:
                while proc.poll() is None:
                    check()
                    if time.monotonic()-begin > 172800:
                        raise TimeoutError('任务超过48小时，请检查工具')
                    if frames:
                        n = len(list(frames.glob('*.png')))
                        elapsed = time.monotonic()-infer_begin
                        update('超分', frames_done=n, eta=round(elapsed/max(1,n)*max(0,job['frames']-n)) if n else None)
                    EVENTS[job['id']].wait(.5)
                check()
                if proc.returncode:
                    raise ValueError(pathlib.Path(log).read_text(errors='replace')[-1800:] or '媒体工具执行失败')
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill(); proc.wait()
    try:
        check()
        job['status'] = 'running'
        update('检查原片')
        source = app.candidate_file(job['source'])
        if app.file_hash(source) != job['source_hash']:
            raise ValueError('原片内容发生变化')
        for k in ('engine','ffmpeg','ffprobe'):
            if app.file_hash(pathlib.Path(config[k])) != job['tools'][k]:
                raise ValueError('执行工具发生变化，请重新提交')
        for ext in ('bin','param'):
            model = pathlib.Path(config['models']) / f"realesr-animevideov3-x{job['scale']}.{ext}"
            if app.file_hash(model) != job['tools'][model.name]:
                raise ValueError('模型发生变化，请重新提交')
        meta = metadata(config, source)
        estimate = meta['frames'] * meta['width'] * meta['height'] * 4 * (1 + job['scale']**2) * 1.2
        job['estimated_disk_bytes'] = int(estimate)
        if shutil.disk_usage(app.ROOT).free < estimate + 512*1024*1024:
            raise ValueError('可用磁盘不足以保存估算的中间帧')
        parent = app.safe(DIRECTORY)
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            folder = pathlib.Path(tmp); inp = folder/'input'; enhanced = folder/'enhanced'
            inp.mkdir(); enhanced.mkdir()
            ff = [config['ffmpeg'],'-nostdin','-v','error','-y']
            update('抽帧', frames=meta['frames'])
            run(ff+['-i',source,'-map','0:v:0','-fps_mode','passthrough',inp/'%08d.png'],folder/'extract.log')
            if len(list(inp.glob('*.png'))) != meta['frames']:
                raise ValueError('抽帧数量不匹配')
            infer_begin = time.monotonic()
            run([config['engine'],'-i',inp,'-o',enhanced,'-m',config['models'],'-n','realesr-animevideov3','-s',job['scale'],'-t','128','-j','1:1:1','-f','png'],folder/'infer.log',enhanced)
            if len(list(enhanced.glob('*.png'))) != meta['frames']:
                raise ValueError('超分帧数量不匹配')
            update('编码并保留原声', frames_done=meta['frames'], eta=None)
            output = folder/'result.mp4'
            width = round(job['height'] * meta['width']/meta['height']/2)*2
            run(ff+['-framerate',meta['fps'],'-i',enhanced/'%08d.png','-i',source,'-map','0:v:0','-map','1:a:0?',
                    '-vf',f"scale={width}:{job['height']}:flags=lanczos,setsar=1",'-c:v','libx264','-preset','medium','-crf','17',
                    '-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',output],folder/'encode.log')
            update('完整解码检查')
            outmeta = metadata(config, output)
            if outmeta['frames'] != meta['frames'] or outmeta['has_audio'] != meta['has_audio'] or Fraction(outmeta['fps']) != Fraction(meta['fps']):
                raise ValueError('输出帧数、帧率或声音检查失败')
            run(ff+['-i',output,'-f','null','-'],folder/'decode.log')
            if app.file_hash(source) != job['source_hash']:
                raise ValueError('处理期间原片变化，结果未发布')
            target = app.safe(job['output'])
            if not target:
                raise PermissionError('结果路径越界')
            target.parent.mkdir(parents=True, exist_ok=True)
            with LOCK:
                check()
                os.link(output, target)
                job.update(status='completed', output_hash=app.file_hash(target), metadata=outmeta)
                update('完成，内容待审', eta=0)
    except editing.ExportCancelled as e:
        job.update(status='cancelled', error=str(e)); update('已取消', eta=None)
    except Exception as e:
        job.update(status='failed', error=str(e)); update('失败', eta=None)


def comparison(app, token):
    job = next((j for j in stored(app) if j['id'] == token and j['status'] == 'completed'), None)
    if not job or app.file_hash(app.safe(job['source'])) != job['source_hash'] or app.file_hash(app.safe(job['output'])) != job.get('output_hash'):
        raise ValueError('原片或结果已变化，不能自动对照，请重新核验')
    return {'source': job['source'], 'output': job['output'], 'fps': float(Fraction(job['metadata']['fps']))}
