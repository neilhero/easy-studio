"""One-way editable Jianying handoffs and fingerprint-bound whole-film review."""
import datetime
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import uuid
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction

import editing
import quality
import workspace

RECORDS = '剪辑/剪映交接'
DRAFTS = '剪辑/剪映草稿'
TIMEBASE = 1_000_000
UNMAPPED = {'found': False, 'external': True, 'reason': '剪映回收版只提供全片时间码，请在剪映精修面板复查'}


class ToolRefusal(ValueError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def us(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('剪辑时间值无效')
    return int((Decimal(str(value)) * TIMEBASE).to_integral_value(rounding=ROUND_HALF_UP))


def path(app, name):
    p = app.safe(name)
    if p is None:
        raise PermissionError('剪映交接路径越界')
    return p


def configured():
    python = pathlib.Path(os.environ.get('STUDIO_JIANYING_PYTHON', ''))
    return python.is_absolute() and python.is_file() and os.access(python, os.X_OK)


def bridge(app, tool, **arguments):
    if not configured():
        raise ValueError('请先配置 STUDIO_JIANYING_PYTHON 并重启 Studio')
    environment = {'JIANYING_MCP_WORKSPACE': str(path(app, DRAFTS))}
    if os.environ.get('FFMPEG_BIN'):
        environment['JIANYING_FFMPEG'] = os.environ['FFMPEG_BIN']
    request = {'tool': tool, 'arguments': arguments, 'environment': environment}
    try:
        result = subprocess.run(
            [os.environ['STUDIO_JIANYING_PYTHON'], str(pathlib.Path(__file__).with_name('jianying_bridge.py'))],
            input=json.dumps(request, ensure_ascii=False, allow_nan=False), text=True,
            capture_output=True, timeout=930, check=True)
        response = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError('剪映工具连接中断，请检查交接记录和草稿目录，再决定是否继续') from exc
    if 'error' in response:
        raise ToolRefusal(response['error'])
    return response['data']


def preview(app, body):
    kind, final = body.get('source', 'current'), body.get('final')
    draft = editing.read(app)
    if kind == 'current':
        kind = 'draft' if draft['clips'] else 'legacy'
    unsupported = []
    manifest = None
    if kind == 'draft':
        clips = editing.validate(app, draft['clips'])
        if clips != draft['clips']:
            raise ValueError('草稿素材已变化，请先重新核对并保存初剪')
        timeline = editing.timeline(clips)
        revision = draft['revision']
    elif kind == 'final':
        if final not in {app.rel(p) for p, _, _ in app.final_videos()}:
            raise ValueError('请选择具体的 Studio 初剪成片')
        manifest = editing.manifest(app, final)
        if not manifest:
            raise ValueError('此成片没有可转换的 Studio 时间线快照')
        timeline, revision = manifest['timeline'], manifest['revision']
    elif kind == 'legacy':
        timeline, revision = app.api_timeline(precise=True), None
    else:
        raise ValueError('剪辑来源无效')
    if not timeline.get('V'):
        raise ValueError('当前没有可交接的初剪，请先保存初剪')
    if timeline.get('look') not in (None, 'none') or timeline.get('target_i') is not None:
        unsupported.append('整片调色或调色目标')
    if timeline.get('invalid_subs'):
        unsupported.append('旧剪辑表中有无法解析的字幕')
    entries = []
    t = 0
    for i, clip in enumerate(timeline['V']):
        shot = str(clip['id'])
        if (clip.get('kind') != 'live' or any(clip.get(k) for k in ('grade', 'freeze', 'fi', 'fo', 'transition'))
                or clip.get('matte') is False):
            unsupported.append(f'{shot} 的画面效果、特殊素材或转场')
            continue
        start, end = us(clip['in']), us(clip['out'])
        speed = clip.get('speed', 1)
        if not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0:
            raise ValueError(f'{shot} 的速度无效')
        duration = round((end - start) / speed)
        entries.append({'id': f'v{i+1:04d}', 'shot': shot, 'kind': 'video', 'track': '主轨',
                        'path': clip['src'], 'start_us': t, 'duration_us': duration,
                        'source_start_us': start, 'speed': speed, 'volume': clip.get('a', 1)})
        t += duration
    for i, clip in enumerate(timeline.get('A', [])):
        if any(clip.get(k) for k in ('loop', 'fi', 'fo', 'duck', 'norm', 'fx', 'hits', 'env', 'raw', 'clarity')):
            unsupported.append(f"{clip['name']} 的声音处理")
            continue
        entries.append({'id': f'a{i+1:04d}', 'kind': 'audio', 'track': f'音频 {i+1}', 'path': clip['src'],
                        'start_us': us(clip['start']), 'duration_us': us(clip['dur']),
                        'source_start_us': us(clip['in'] or 0), 'speed': 1, 'volume': 10 ** (clip['gain'] / 20)})
    for i, clip in enumerate(timeline.get('subs', [])):
        start, end = us(clip['start']), us(clip['end'])
        entries.append({'id': f't{i+1:04d}', 'kind': 'text', 'track': '字幕', 'start_us': start,
                        'duration_us': end - start, 'text': clip['text']})
    snapshot = {'source': kind, 'final': final if manifest else None,
                'film_hash': manifest['film_hash'] if manifest else None, 'revision': revision,
                'timebase': TIMEBASE, 'unsupported': unsupported, 'clips': entries}
    if unsupported:
        return snapshot
    for clip in entries:
        if clip['start_us'] < 0 or clip['duration_us'] <= 0 or clip['start_us'] + clip['duration_us'] > t:
            raise ValueError('画面、字幕或音轨超出初剪范围，请核对时间线')
        if 'path' in clip:
            media = path(app, clip['path'])
            clip['fingerprint'] = app.file_hash(media)
            if not clip['fingerprint'] or app.kind(media) != clip['kind']:
                raise ValueError(f'找不到对应媒体 {clip["path"]}')
            if manifest and manifest['sources'].get(clip['path']) != clip['fingerprint']:
                raise ValueError('初剪快照的原素材已变化，请重新核对')
    first = app.probe(path(app, final if manifest else entries[0]['path']))
    if not first.get('width') or not first.get('height'):
        raise ValueError('无法读取初剪画幅')
    fps = 25 if kind == 'draft' else float(Fraction(first.get('fps') or '25'))
    if not fps:
        raise ValueError('无法读取初剪帧率')
    snapshot.update(width=first['width'], height=first['height'], fps=fps, duration_us=t)
    snapshot['signature'] = workspace.digest(snapshot)
    return snapshot


def external_plan(app, snapshot, name):
    clips = []
    for source in snapshot['clips']:
        clip = {key: value for key, value in source.items() if key not in ('shot', 'fingerprint') and not key.endswith('_us')}
        for key in ('start', 'duration', 'source_start'):
            if key + '_us' in source:
                clip[key] = source[key + '_us'] / TIMEBASE
        if 'path' in clip:
            clip['path'] = str(path(app, clip['path']))
        clips.append(clip)
    return {'name': name, **{key: snapshot[key] for key in ('width', 'height', 'fps')}, 'clips': clips}


def record_path(token):
    if not isinstance(token, str) or not re.fullmatch('[a-f0-9]{32}', token):
        raise ValueError('交接编号无效')
    return f'{RECORDS}/{token}.json'


def load(app, token):
    p = path(app, record_path(token))
    if not p.is_file():
        raise ValueError('找不到这个剪映交接')
    return json.loads(p.read_text())


def save(app, record):
    editing.write(app, record_path(record['id']), record)
    return record


def managed(app, record):
    return path(app, f"{DRAFTS}/{record['name']}")


def new_record(app, body, snapshot, parent=None):
    token = body.get('request_id')
    if not token:
        raise ValueError('缺少交接请求编号，请刷新后重试')
    p = path(app, record_path(token))
    identity = workspace.digest({'snapshot': snapshot['signature'], 'parent': parent, 'operations': body.get('operations')})
    if p.exists():
        prior = load(app, token)
        if prior['identity'] != identity:
            raise ValueError('同一交接请求编号只能用于同一份草稿')
        return prior, False
    record = {'schema': 1, 'id': token, 'parent_id': parent, 'name': 'EasyStudio-' + token,
              'at': now(), 'status': 'preparing', 'identity': identity, 'snapshot': snapshot}
    return record, True


def verify_package(app, record):
    root = managed(app, record)
    manifest = root / 'jianying_mcp_project.json'
    native = root / 'draft_info.json'
    if not manifest.is_file() or not native.is_file():
        raise ValueError('剪映工具未返回完整草稿包，请检查交接目录')
    plan = json.loads(manifest.read_text())['plan']
    if plan['name'] != record['name']:
        raise ValueError('草稿包标识不一致')
    known = {c['id']: c['fingerprint'] for c in record['snapshot']['clips'] if 'fingerprint' in c}
    for clip in plan['clips']:
        if 'path' not in clip:
            continue
        media = pathlib.Path(clip['path']).resolve()
        expected = known.get(clip['id'])
        if not expected or not media.is_relative_to(root / 'Resources') or app.file_hash(media) != expected:
            raise ValueError('草稿包媒体与初剪快照不一致')
    return {'package_hash': app.file_hash(manifest), 'package_native_hash': app.file_hash(native),
            'clips': [{k: v for k, v in c.items() if k != 'path'} for c in plan['clips']]}


def check_package(app, record):
    current = verify_package(app, record)
    if any(current[k] != record.get(k) for k in ('package_hash', 'package_native_hash')):
        raise ValueError('交接草稿包已被修改，请保留该副本并新建交接')


def create(app, body):
    snapshot = preview(app, body)
    if snapshot['unsupported']:
        raise ValueError('这些效果暂时不能转换：' + '、'.join(snapshot['unsupported']))
    if body.get('signature') != snapshot['signature']:
        raise ValueError('初剪已变化，请重新预览')
    record, fresh = new_record(app, body, snapshot)
    if not fresh:
        return record
    plan = external_plan(app, snapshot, record['name'])
    bridge(app, 'create_draft', plan=plan, dry_run=True)
    save(app, record)
    bridge(app, 'create_draft', plan=plan, dry_run=False)
    record.update(verify_package(app, record), status='prepared')
    return save(app, record)


def revise(app, body):
    source = load(app, body.get('id'))
    if source['status'] not in ('prepared', 'published'):
        raise ValueError('请先确认上一份草稿已准备好')
    ops = body.get('operations')
    if not isinstance(ops, list) or not ops or len(ops) > 100:
        raise ValueError('请提交 1–100 项精修操作')
    for op in ops:
        if not isinstance(op, dict):
            raise ValueError('精修操作格式无效')
        if op.get('op') == 'update' and set(op) == {'op', 'id', 'changes'}:
            changes = op['changes']
            if isinstance(changes, dict) and changes and not set(changes) - {'start', 'duration', 'source_start', 'speed', 'volume', 'text'}:
                continue
        if op.get('op') == 'append' and set(op) == {'op', 'clip'}:
            clip = op['clip']
            if isinstance(clip, dict) and clip.get('kind') == 'text' and not set(clip) - {'id', 'kind', 'track', 'start', 'duration', 'text'}:
                continue
        raise ValueError('Studio 精修支持改入点、时长、位置、速度、音量及新增或修改字幕')
    record, fresh = new_record(app, body, source['snapshot'], source['id'])
    if not fresh:
        return record
    check_package(app, source)
    args = {'name': source['name'], 'new_name': record['name'], 'operations': ops}
    bridge(app, 'edit_draft', **args, dry_run=True)
    record['operations'] = ops
    save(app, record)
    bridge(app, 'edit_draft', **args, dry_run=False)
    record.update(verify_package(app, record), status='prepared')
    return save(app, record)


def publish(app, body):
    record = load(app, body.get('id'))
    if record['status'] == 'published':
        return view(app, record)
    if record['status'] != 'prepared':
        raise ValueError('交接结果待核对，禁止重复发布')
    check_package(app, record)
    default_root = pathlib.Path.home() / 'Movies/JianyingPro/User Data/Projects/com.lveditor.draft'
    args = {'name': record['name'], 'draft_root': os.environ.get('STUDIO_JIANYING_DRAFT_ROOT') or str(default_root)}
    expected = bridge(app, 'publish_draft', **args, dry_run=True)
    native = pathlib.Path(expected['native_project_dir']) / 'draft_info.json'
    record.update(status='publishing', native_path=str(native), native_id=expected['draft_id'])
    save(app, record)
    try:
        result = bridge(app, 'publish_draft', **args, dry_run=False)
    except ToolRefusal as exc:
        if 'Jianying is running; fully quit the app before publishing' in str(exc) and not native.parent.exists():
            record.pop('native_path')
            record.pop('native_id')
            record['status'] = 'prepared'
            save(app, record)
            raise ValueError('请完全退出剪映，再点击发布到剪映') from exc
        raise
    if result.get('status') != 'published' or result['draft_id'] != record['native_id'] or not app.file_hash(native):
        raise ValueError('原生发布回执无法核对，请检查剪映项目列表')
    record.update(status='published', published_at=now(), native_hash=app.file_hash(native))
    return save(app, record)


def returned(final):
    return isinstance(final, str) and re.fullmatch(r'final/精修_[a-f0-9]{32}_[a-f0-9]{64}\.(?:mp4|mov)', final) is not None


def receipt(app, final):
    if not returned(final):
        raise ValueError('请选择剪映回收版')
    p = path(app, final + '.finishing.json')
    if not p.is_file() or not path(app, final).is_file():
        raise ValueError('未找到完整回收记录')
    return json.loads(p.read_text())


def view(app, record, reports=None):
    native = record.get('native_path')
    current = app.file_hash(pathlib.Path(native)) if native else None
    result = {**record, 'package': str(managed(app, record)),
              'status': {'preparing': 'needs_reconciliation', 'publishing': 'needs_reconciliation'}.get(record['status'], record['status']),
              'native_state': ('missing' if not current else 'changed' if current != record.get('native_hash') else 'same') if native else None}
    final_dir = path(app, 'final')
    outputs = []
    for p in sorted(final_dir.glob(f"精修_{record['id']}_*")):
        final = app.rel(p)
        if not returned(final) or not path(app, final + '.finishing.json').is_file():
            continue
        item = receipt(app, final)
        stale = app.file_hash(p) != item['film_hash']
        scan = next((r for r in reports or [] if r['path'] == final and r['fingerprint'] == item['film_hash']), None)
        outputs.append({**item, 'stale': stale, 'scan': scan})
    result['outputs'] = sorted(outputs, key=lambda x: x['at'], reverse=True)
    return result


def revision_state(app):
    return {app.rel(p): app.file_hash(p)
            for folder, pattern in ((RECORDS, '*.json'), ('final', '*.finishing.json'))
            for p in sorted(path(app, folder).glob(pattern))}


def listing(app):
    directory = path(app, RECORDS)
    reports = quality.reports(app)['items']
    entries = [view(app, load(app, p.stem), reports) for p in directory.glob('*.json') if re.fullmatch('[a-f0-9]{32}', p.stem)]
    finals = [app.rel(p) for p, _, _ in app.final_videos() if editing.manifest(app, app.rel(p))]
    return {'items': sorted(entries, key=lambda x: x['at'], reverse=True), 'configured': configured(),
            'finals': finals, 'import_roots': [str(app.ROOT), *map(str, app.IMPORT_ROOTS)]}


def receive(app, body):
    record = load(app, body.get('id'))
    if record['status'] != 'published':
        raise ValueError('请先将这份草稿发布到剪映')
    source = body.get('source')
    if not isinstance(source, str) or not pathlib.Path(source).is_absolute():
        raise ValueError('请填写实际导出的 MP4 或 MOV 绝对路径')
    source = pathlib.Path(source).resolve()
    suffix = source.suffix.lower()
    if (suffix not in ('.mp4', '.mov') or not source.is_file() or
            not any(source.is_relative_to(root) for root in [app.ROOT, *app.IMPORT_ROOTS]) or
            source.is_relative_to(path(app, 'final')) or source.is_relative_to(path(app, DRAFTS))):
        raise ValueError('请从项目或获准的导入目录选择剪映导出的 MP4 或 MOV')
    if body.get('confirm_export') is not True:
        raise ValueError('请确认这个文件来自所选剪映修订的实际导出')
    fingerprint = app.file_hash(source)
    final = f"final/精修_{record['id']}_{fingerprint}{suffix}"
    dest = path(app, final)
    if dest.exists():
        item = receipt(app, final)
        if app.file_hash(dest) != item['film_hash']:
            raise ValueError('回收版内容已变化，原版本保留供核对')
        return item
    dest.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        copy = pathlib.Path(tmp) / 'export.mp4'
        shutil.copyfile(source, copy)
        if app.file_hash(copy) != fingerprint or app.file_hash(source) != fingerprint:
            raise ValueError('导出文件仍在变化，请等剪映导出结束')
        info = app.probe(copy)
        if not info.get('duration') or not info.get('width'):
            raise ValueError('文件无法识别为可播放的视频')
        native_path = pathlib.Path(record['native_path'])
        item = {'schema': 1, 'final': final, 'film_hash': fingerprint, 'handoff_id': record['id'],
                'source_signature': record['snapshot']['signature'], 'source_name': source.name,
                'draft_id': record['native_id'], 'draft_hash_at_import': app.file_hash(native_path),
                'provenance': 'operator_confirmed_jianying_export', 'mapping': 'whole_film_only',
                'metadata': info, 'at': now(), 'reviews': []}
        editing.write(app, final + '.finishing.json', item)
        os.link(copy, dest)
    return item


def scan(app, body):
    item = receipt(app, body.get('final'))
    if item['film_hash'] != app.file_hash(path(app, item['final'])):
        raise ValueError('回收版文件已变化，请重新回收')
    return quality.start(app, {'path': item['final']})


def review(app, body):
    item = receipt(app, body.get('final'))
    if item['film_hash'] != body.get('film_hash') or item['film_hash'] != app.file_hash(path(app, item['final'])):
        raise ValueError('回收版已变化，请重新看片')
    report = next((r for r in quality.reports(app)['items'] if r['id'] == body.get('scan_id') and r['path'] == item['final']
                   and r['fingerprint'] == item['film_hash'] and r['status'] == 'completed' and not r['stale']), None)
    status, note = body.get('status'), str(body.get('note', '')).strip()[:4000]
    if not report or status not in ('approved', 'rejected') or not note:
        raise ValueError('请先完成技术质检，实际看片并填写复查结论')
    item['reviews'].append({'status': status, 'note': note, 'scan_id': report['id'], 'at': now()})
    editing.write(app, item['final'] + '.finishing.json', item)
    return item


ACTIONS = {'create': create, 'revise': revise, 'publish': publish, 'receive': receive, 'scan': scan, 'review': review}
