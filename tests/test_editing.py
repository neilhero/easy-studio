import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import studio as app
import editing


class EditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = app.ROOT
        app.ROOT = pathlib.Path(self.temp.name).resolve()
        (app.ROOT / '素材库').mkdir()
        (app.ROOT / '素材库/a.mp4').write_bytes(b'a')
        self.clip = {'id': 'S1', 'src': '素材库/a.mp4', 'in': 0, 'out': 1, 'a': 1}
        self.probe = patch.object(app, 'probe', return_value={'duration': 3, 'width': 64, 'height': 64, 'has_audio': True})
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        app.ROOT = self.old_root
        self.temp.cleanup()

    def save(self, clips=None):
        return editing.mutate(app, {'action': 'save', 'revision': editing.read(app)['revision'], 'clips': clips if clips is not None else [self.clip]})

    def test_persist_undo_redo_and_stale_write(self):
        self.save()
        second = self.save([{**self.clip, 'in': .5}])
        self.assertEqual(editing.read(app)['clips'][0]['in'], .5)
        with self.assertRaises(ValueError):
            editing.mutate(app, {'revision': 0, 'clips': []})
        undone = editing.mutate(app, {'action': 'undo', 'revision': second['revision']})
        self.assertEqual(undone['clips'][0]['in'], 0)
        redone = editing.mutate(app, {'action': 'redo', 'revision': undone['revision']})
        self.assertEqual(redone['clips'][0]['in'], .5)

    def test_bad_inputs_leave_draft_untouched(self):
        self.save()
        before = (app.ROOT / editing.DRAFT).read_bytes()
        for change in ({'in': -1}, {'out': 4}, {'a': float('nan')}, {'src': '../bad.mp4'}, {'src': '.secret/a.mp4'}):
            with self.assertRaises((ValueError, PermissionError)):
                self.save([{**self.clip, **change}])
            self.assertEqual((app.ROOT / editing.DRAFT).read_bytes(), before)
        with self.assertRaises(ValueError):
            self.save([self.clip, self.clip])

    def test_interrupted_job_is_reported_failed(self):
        editing.write(app, '剪辑/导出任务/test.json', {'id': 'test', 'status': 'running'})
        self.assertEqual(editing.jobs(app)[0]['status'], 'failed')

    def test_import_refuses_silent_loss(self):
        with patch.object(app, 'api_timeline', return_value={'A': [{'src': 'music.wav'}]}):
            with self.assertRaises(ValueError):
                editing.mutate(app, {'action': 'import', 'revision': 0})
        self.assertFalse((app.ROOT / editing.DRAFT).exists())

    def test_issue_lifecycle_binds_evidence_version(self):
        (app.ROOT / 'final').mkdir()
        film = app.ROOT / 'final/a.mp4'
        film.write_bytes(b'film')
        with patch.object(app, 'api_timeline', return_value=editing.timeline([self.clip])):
            state = editing.issues(app, {'action': 'create', 'final': 'final/a.mp4', 't': .3, 'note': '动作不完整'})
        item = state['items'][0]
        self.assertEqual(item['clip']['id'], 'S1')
        with self.assertRaises(ValueError):
            editing.issues(app, {'action': 'status', 'id': item['id'], 'status': 'verified', 'final': 'final/a.mp4'})
        for status in ('fixed', 'verified'):
            editing.issues(app, {'action': 'status', 'id': item['id'], 'status': status, 'final': 'final/a.mp4'})
        self.assertEqual(editing.issues(app)['items'][0]['status'], 'verified')
        film.write_bytes(b'replaced')
        self.assertTrue(editing.issues(app)['items'][0]['needs_review'])
        self.assertEqual(editing.issues(app)['items'][0]['status'], 'pending')

    def test_cancel_terminates_process_and_retry_uses_original_snapshot(self):
        # 可取消的长任务，写入启动标志，避免靠固定睡眠猜进程状态。
        marker = app.ROOT / 'started'
        fake = app.ROOT / 'slow-ffmpeg'
        fake.write_text('#!/usr/bin/python3\nimport pathlib,time\npathlib.Path(' + repr(str(marker)) + ').touch()\ntime.sleep(60)\n')
        fake.chmod(0o755)
        state = self.save()
        with patch.dict(os.environ, {'FFMPEG_BIN': str(fake)}):
            job = editing.start(app, {'revision': state['revision']})
            deadline = time.monotonic() + 8
            while not marker.exists() and editing.ACTIVE and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists())
            editing.cancel(app, {'id': job['id']})
            while editing.ACTIVE and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(editing.ACTIVE)
            self.assertEqual(editing.jobs(app)[0]['status'], 'cancelled')
            self.assertFalse(app.safe(job['final']).exists())
            self.save([{**self.clip, 'out': 2}])
            with patch.object(editing.threading, 'Thread'):
                retried = editing.start(app, {'retry': job['id']})
            try:
                self.assertEqual(retried['clips'][0]['out'], 1)
                self.assertEqual(retried['retry_of'], job['id'])
                self.assertNotEqual(retried['final'], job['final'])
            finally:
                editing.ACTIVE.discard(retried['id'])
                editing.CANCEL.pop(retried['id'], None)
            (app.ROOT / self.clip['src']).write_bytes(b'changed')
            with self.assertRaises(ValueError):
                editing.start(app, {'retry': job['id']})

    def test_failed_retry_keeps_original_clip_after_draft_changes(self):
        state = self.save()
        with patch.dict(os.environ, {'FFMPEG_BIN': '/usr/bin/false'}):
            old = editing.start(app, {'revision': state['revision']})
            deadline = time.monotonic() + 5
            while editing.ACTIVE and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(editing.jobs(app)[0]['status'], 'failed')
            self.save([{**self.clip, 'out': 2}])
            with patch.object(editing.threading, 'Thread'):
                new = editing.start(app, {'retry': old['id']})
            try:
                self.assertEqual(new['clips'][0]['out'], 1)
            finally:
                editing.ACTIVE.discard(new['id'])
                editing.CANCEL.pop(new['id'], None)

    def test_issue_location_tracks_reordered_shortened_clip_and_refuses_ambiguity(self):
        (app.ROOT / 'final').mkdir()
        for name in ('old', 'new'):
            (app.ROOT / f'final/{name}.mp4').write_bytes(name.encode())
        old = editing.timeline([{**self.clip, 'out': 3}])
        with patch.object(app, 'api_timeline', return_value=old):
            item = editing.issues(app, {'action': 'create', 'final': 'final/old.mp4', 't': 2, 'note': '动作问题'})['items'][0]
        clips = [{**self.clip, 'id': 'S2', 'out': 2}, {**self.clip, 'out': .5}]
        data = {'schema': 'lazy-export-1', 'film_hash': app.file_hash(app.ROOT / 'final/new.mp4'), 'timeline': editing.timeline(clips)}
        editing.write(app, 'final/new.mp4.json', data)
        point = editing.issue_location(app, item['id'], 'final/new.mp4')
        self.assertFalse(point['exact'])
        self.assertEqual(point['t'], 2.46)
        self.assertEqual(editing.issue_location(app, item['id'], 'final/old.mp4')['t'], 2)
        data['timeline']['V'].append(data['timeline']['V'][1])
        editing.write(app, 'final/new.mp4.json', data)
        with self.assertRaises(ValueError):
            editing.issue_location(app, item['id'], 'final/new.mp4')

    def test_real_export_audio_silent_and_version_snapshot(self):
        ffmpeg = os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
        ffprobe = os.environ.get('FFPROBE_BIN') or shutil.which('ffprobe')
        if not ffmpeg or not ffprobe:
            self.skipTest('需要 FFmpeg 与 FFprobe')
        self.probe.stop()
        for filename, audio in [('a', True), ('b', False)]:
            cmd = [ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:r=25:d=1.2']
            if audio:
                cmd += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.2']
            subprocess.run(cmd + ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(app.ROOT / f'素材库/{filename}.mp4')], check=True)
        self.clip['out'] = .8
        state = self.save([self.clip, {**self.clip, 'id': 'S2', 'src': '素材库/b.mp4', 'a': 0}])
        first = editing.start(app, {'revision': state['revision']})
        deadline = time.monotonic() + 40
        while editing.ACTIVE and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertFalse(editing.ACTIVE)
        job = editing.jobs(app)[0]
        self.assertEqual(job['status'], 'completed', job['error'])
        output = app.safe(job['final'])
        info = app.probe(output)
        self.assertTrue(info['has_audio'])
        self.assertAlmostEqual(info['duration'], 1.6, delta=.12)
        self.assertEqual((info['width'], info['height']), (64, 64))
        digest = app.file_hash(output)
        snapshot = editing.manifest(app, job['final'])
        self.assertEqual(snapshot['timeline']['V'][1]['start'], .8)
        # 导出结果能直接验收，后续改草稿/原素材不篡改旧成片依据。
        (app.ROOT / '脚本').mkdir()
        (app.ROOT / app.SEG).write_text('| 段 | 节拍 |\n|---|---|\n| S1 | 镜1 |\n| S2 | 镜2 |\n')
        ctx = app.review_context('S1', job['final'])
        self.assertTrue(ctx['confirmed'])
        app.review_mutate({'segment': 'S1', 'action': 'accept', 'final': job['final'], 'signature': ctx['signature'], 'status': 'approved'})
        self.save([{**self.clip, 'out': .4}])
        self.assertEqual(app.review_context('S1', job['final'])['status'], '通过')
        new = editing.start(app, {'revision': editing.read(app)['revision']})
        while editing.ACTIVE and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertNotEqual(new['final'], job['final'])
        self.assertEqual(app.file_hash(output), digest)
        self.assertEqual(editing.jobs(app)[0]['status'], 'completed')
        self.assertEqual(app.review_context('S1', job['final'])['signature'], ctx['signature'])

if __name__ == '__main__':
    unittest.main()
