"""v0.3 单人剪辑草稿、不可变导出快照和本地任务。"""
import copy
import datetime
import json
import math
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import uuid

DRAFT = '剪辑/草稿.json'
LOCK = threading.RLock()
ACTIVE = set()


def write(app, path, data):
    p = app.safe(path)
    if not p:
        raise PermissionError('剪辑路径越界')
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix='edit-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read(app):
    p = app.safe(DRAFT)
    if not p:
        raise PermissionError('草稿路径越界')
    if p.exists():
        return json.loads(p.read_text())
    return {'schema': 1, 'revision': 0, 'clips': [], 'undo': [], 'redo': []}


def validate(app, clips):
    if not isinstance(clips, list) or len(clips) > 1000:
        raise ValueError('镜头列表无效')
    result = []
    ids = set()
    for c in clips:
        if not isinstance(c, dict):
            raise ValueError('镜头格式无效')
        sid = str(c.get('id', '')).strip()
        if not sid or sid in ids:
            raise ValueError('镜头ID须非空且唯一')
        ids.add(sid)
        p = app.candidate_file(c.get('src'))
        values = [float(c.get(k, default)) for k, default in [('in', 0), ('out', 0), ('a', 1)]]
        start, end, volume = values
        duration = app.probe(p).get('duration')
        if not all(math.isfinite(x) for x in values) or not (0 <= start < end) or not (0 <= volume <= 4):
            raise ValueError('入出点或音量无效')
        if not duration or end > duration + .001:
            raise ValueError('无法探测素材时长，或出点超过素材时长')
        result.append({'id': sid, 'src': app.rel(p), 'in': start, 'out': end, 'a': volume,
                       'fingerprint': app.file_hash(p)})
    return result


def timeline(clips):
    t, items = 0, []
    for i, c in enumerate(clips):
        d = c['out'] - c['in']
        items.append({**{k: v for k, v in c.items() if k != 'fingerprint'}, 'i': i, 'start': round(t, 3),
                      'dur': round(d, 3), 'kind': 'live', 'exists': True, 'grade': None,
                      'matte': True, 'fi': None, 'fo': None, 'freeze': None})
        t += d
    return {'found': bool(items), 'V': items, 'A': [], 'subs': [], 'total': round(t, 3),
            'look': 'none', 'target_i': None, 'duck_db': None}


def mutate(app, body):
    with LOCK:
        state = read(app)
        if body.get('revision') != state['revision']:
            raise ValueError('草稿已变化，请关闭编辑窗后重新打开')
        action = body.get('action', 'save')
        if action in ('undo', 'redo'):
            source, target = ('undo', 'redo') if action == 'undo' else ('redo', 'undo')
            if not state[source]:
                raise ValueError('没有可撤销或重做的修改')
            state[target].append(state['clips'])
            state['clips'] = state[source].pop()
        else:
            if action == 'import':
                tl = app.api_timeline()
                if tl.get('A') or tl.get('subs') or tl.get('look') not in (None, 'none') or any(c.get('freeze') or c.get('fi') or c.get('fo') or c.get('grade') for c in tl.get('V', [])):
                    raise ValueError('现有剪辑含复杂声音、字幕、调色或定格；请保留原剪辑，在此建立基础初剪')
                clips = tl.get('V', [])
            elif action == 'selected':
                clips = copy.deepcopy(state['clips'])
                for row in app.api_segments().get('rows', []):
                    selected = row['review_entry'].get('selected')
                    if not selected:
                        continue
                    p = app.candidate_file(selected['path'])
                    if selected['fingerprint'] != app.file_hash(p):
                        raise ValueError('已选素材发生变化，请先复核：' + row['id'])
                    c = {'id': row['id'], 'src': selected['path'], 'in': 0, 'out': app.probe(p).get('duration'), 'a': 1}
                    idx = next((i for i, x in enumerate(clips) if x['id'] in (row['id'], row['scene'])), None)
                    if idx is None:
                        clips.append(c)
                    else:
                        c['id'] = clips[idx]['id']
                        clips[idx] = c
            elif action == 'save':
                clips = body.get('clips')
            else:
                raise ValueError('未知剪辑操作')
            clips = validate(app, clips)
            state['undo'] = (state['undo'] + [state['clips']])[-50:]
            state['redo'] = []
            state['clips'] = clips
        state['revision'] += 1
        write(app, DRAFT, state)
        return state


def manifest(app, final):
    p = app.safe(final + '.json')
    if p and p.is_file():
        data = json.loads(p.read_text())
        if data.get('schema') == 'lazy-export-1' and data.get('film_hash') == app.file_hash(app.safe(final)):
            return data
    return None


def jobs(app):
    directory = app.safe('剪辑/导出任务')
    result = []
    if directory and directory.exists():
        for p in sorted(directory.glob('*.json'), reverse=True):
            job = json.loads(p.read_text())
            if job['status'] in ('queued', 'running') and job['id'] not in ACTIVE:
                job.update(status='failed', error='服务已重启，导出中断；可重新导出，旧成片保留')
            result.append(job)
    return result


def start(app, body):
    with LOCK:
        if ACTIVE:
            raise ValueError('已有导出任务正在运行')
        state = read(app)
        if body.get('revision') != state['revision']:
            raise ValueError('请先保存最新草稿')
        clips = validate(app, state['clips'])
        if not clips:
            raise ValueError('请先加入镜头')
        if clips != state['clips']:
            raise ValueError('素材内容已变化，请重新保存并核对')
        executable = os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
        if not executable:
            raise ValueError('未找到 FFmpeg，请配置 FFMPEG_BIN 后重启服务')
        token = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
        final_dir = app.safe('final')
        if not final_dir:
            raise PermissionError('成片路径越界')
        final_dir.mkdir(exist_ok=True)
        existing = [int(m.group(1)) for p in final_dir.glob('初剪_v*.mp4') if (m := app.re.search(r'_v(\d+)', p.name))]
        output = f'final/初剪_v{max(existing, default=0)+1:03d}.mp4'
        job = {'id': token, 'status': 'queued', 'completed': 0, 'total': len(clips), 'final': output,
               'revision': state['revision'], 'clips': clips, 'error': ''}
        write(app, f'剪辑/导出任务/{token}.json', job)
        ACTIVE.add(token)
        threading.Thread(target=render, args=(app, executable, job), daemon=True).start()
        return job


def render(app, executable, job):
    path = f"剪辑/导出任务/{job['id']}.json"
    def run(args):
        p = subprocess.run([executable, '-nostdin', '-v', 'error', '-y', *args], capture_output=True, text=True, timeout=21600)
        if p.returncode:
            raise ValueError(p.stderr[-3000:] or 'FFmpeg 导出失败')
    try:
        job['status'] = 'running'
        write(app, path, job)
        with tempfile.TemporaryDirectory(dir=app.safe('剪辑')) as folder:
            folder = pathlib.Path(folder)
            first = app.probe(app.safe(job['clips'][0]['src']))
            w, h = int(first['width']) // 2 * 2, int(first['height']) // 2 * 2
            for i, c in enumerate(job['clips']):
                source = app.safe(c['src'])
                if app.file_hash(source) != c['fingerprint']:
                    raise ValueError('源素材已变化，导出已停止')
                d = c['out'] - c['in']
                args = ['-ss', str(c['in']), '-i', str(source)]
                if not app.probe(source).get('has_audio'):
                    args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
                args += ['-t', str(d), '-map', '0:v:0', '-map', '0:a:0' if app.probe(source).get('has_audio') else '1:a:0',
                         '-vf', f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25',
                         '-af', f"volume={c['a']},apad,atrim=duration={d},asetpts=PTS-STARTPTS", '-c:v', 'libx264', '-preset', 'fast',
                         '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ar', '48000', '-ac', '2', str(folder / f'{i}.mp4')]
                run(args)
                job['completed'] = i + 1
                write(app, path, job)
            listing = folder / 'concat.txt'
            listing.write_text(''.join(f"file '{i}.mp4'\n" for i in range(len(job['clips']))))
            rendered = folder / 'result.mp4'
            run(['-f', 'concat', '-safe', '0', '-i', str(listing), '-c', 'copy', '-movflags', '+faststart', str(rendered)])
            for c in job['clips']:
                if app.file_hash(app.safe(c['src'])) != c['fingerprint']:
                    raise ValueError('导出期间源素材变化，结果未发布')
            film_hash = app.file_hash(rendered)
            data = {'schema': 'lazy-export-1', 'film_hash': film_hash, 'timeline': timeline(job['clips']), 'job': job['id'], 'revision': job['revision'], 'sources': {c['src']: c['fingerprint'] for c in job['clips']}}
            # 先登记，再以硬链接发布：目标存在时失败，永不覆盖旧成片。
            write(app, job['final'] + '.json', data)
            os.link(rendered, app.safe(job['final']))
        job['status'] = 'completed'
    except Exception as e:
        job.update(status='failed', error=str(e))
    finally:
        write(app, path, job)
        with LOCK:
            ACTIVE.discard(job['id'])


def issues(app, body=None):
    with LOCK:
        p = app.safe('反馈/返修问题.json')
        if not p:
            raise PermissionError('问题记录路径越界')
        state = json.loads(p.read_text()) if p.exists() else {'schema': 1, 'items': []}
        if body is None:
            for item in state['items']:
                if item['status'] in ('fixed', 'verified'):
                    latest = item['history'][-1]
                    if app.file_hash(app.safe(latest.get('final', ''))) != latest.get('film_hash'):
                        item['status'] = 'pending'
                        item['needs_review'] = True
            return state
        action = body.get('action')
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if action == 'create':
            final = body.get('final')
            if final not in {app.rel(p) for p, _, _ in app.final_videos()}:
                raise ValueError('请选择具体成片')
            note = str(body.get('note', '')).strip()[:4000]
            t = float(body.get('t', 0))
            if not note or not math.isfinite(t) or t < 0:
                raise ValueError('请填写问题和有效时间码')
            exported = manifest(app, final)
            tl = exported['timeline'] if exported else app.api_timeline()
            clip = next((c for c in tl.get('V', []) if c['start'] <= t < c['start'] + c['dur']), None)
            state['items'].append({'id': uuid.uuid4().hex, 'note': note, 'final': final, 'film_hash': app.file_hash(app.safe(final)),
                                   't': t, 'clip': clip, 'frame': body.get('frame', ''), 'status': 'pending',
                                   'history': [{'status': 'pending', 'at': now}]})
        elif action == 'status':
            item = next((i for i in state['items'] if i['id'] == body.get('id')), None)
            if not item:
                raise ValueError('问题不存在')
            status = body.get('status')
            if status not in ('pending', 'fixed', 'verified'):
                raise ValueError('问题状态无效')
            event = {'status': status, 'at': now}
            if status in ('fixed', 'verified'):
                final = body.get('final')
                if final not in {app.rel(p) for p, _, _ in app.final_videos()}:
                    raise ValueError('请选择修复后的具体成片')
                event.update(final=final, film_hash=app.file_hash(app.safe(final)))
                if status == 'verified':
                    fixed = next((x for x in reversed(item['history']) if x['status'] == 'fixed'), None)
                    if item['status'] != 'fixed' or not fixed or fixed.get('film_hash') != event['film_hash'] or fixed.get('final') != final:
                        raise ValueError('请先登记此成片为已修复，再看片复核')
            item.update(status=status)
            item['history'].append(event)
        else:
            raise ValueError('未知问题操作')
        write(app, '反馈/返修问题.json', state)
        return state
