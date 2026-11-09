import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_api
import editing
import finishing
import studio as app


class FinishingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name).resolve()
        config = patch.multiple(app, ROOT=self.root, IMPORT_ROOTS=[])
        config.start()
        self.addCleanup(config.stop)
        (self.root / '素材库').mkdir()

    def test_conversion_and_stale_signature(self):
        for name in ('a.mp4', 'b.mp4', 'voice.wav'):
            (self.root / '素材库' / name).write_bytes(name.encode())
        legacy = """V = [
{'id': 'S1', 'src': '素材库/a.mp4', 'in': 0.111111, 'out': 0.711113, 'speed': 2, 'a': .45},
{'id': 'S2', 'src': '素材库/b.mp4', 'in': 0, 'out': 1}]
A = [{'src': '素材库/voice.wav', 'at': ('V', 1, 0.1), 'dur': .3, 'in': .2, 'gain': -6}]
SUBS = [('V', 1, .1, .2, '可编辑字幕')]
"""
        edl = self.root / app.EDL
        edl.parent.mkdir()
        edl.write_text(legacy)
        with patch.object(app, 'probe', return_value={'width': 1280, 'height': 720, 'fps': 25, 'duration': 4}):
            plan = finishing.preview(app, {'source': 'legacy'})
            self.assertEqual([c['start_us'] for c in plan['clips']], [0, 300001, 400001, 400001])
            self.assertEqual(plan['clips'][0]['source_start_us'], 111111)
            self.assertEqual(plan['clips'][0]['volume'], .45)
            self.assertAlmostEqual(plan['clips'][2]['volume'], .501187, places=5)
            (self.root / '素材库/a.mp4').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, '初剪已变化'):
                finishing.create(app, {'source': 'legacy', 'signature': plan['signature']})
            edl.write_text(legacy + "\nV[0]['transition'] = 'dissolve'\n")
            plan = finishing.preview(app, {'source': 'legacy'})
            self.assertIn('转场', plan['unsupported'][0])

    def test_running_app_refusal_can_retry_and_unknown_cannot(self):
        native = self.root / 'native' / 'draft_info.json'
        record = {'id': '1'*32, 'status': 'prepared', 'name': 'test', 'at': finishing.now()}
        finishing.save(app, record)
        dry = {'native_project_dir': str(native.parent), 'draft_id': 'native-test'}
        refusal = finishing.ToolRefusal('Jianying is running; fully quit the app before publishing')
        with patch.object(finishing, 'check_package'), patch.object(finishing, 'bridge', side_effect=[dry, refusal]):
            with self.assertRaisesRegex(ValueError, '完全退出'):
                finishing.publish(app, {'id': record['id']})
        self.assertEqual(finishing.load(app, record['id'])['status'], 'prepared')
        with patch.object(finishing, 'check_package'), patch.object(finishing, 'bridge', side_effect=[dry, ValueError('transport interrupted')]):
            with self.assertRaisesRegex(ValueError, 'transport'):
                finishing.publish(app, {'id': record['id']})
        with self.assertRaisesRegex(ValueError, '禁止重复'):
            finishing.publish(app, {'id': record['id']})
        self.assertEqual(finishing.listing(app)['items'][0]['status'], 'needs_reconciliation')

    def test_real_mp4_roundtrip_requires_completed_scan_and_blocks_old_mapping(self):
        ffmpeg = os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
        if not ffmpeg or not (os.environ.get('FFPROBE_BIN') or shutil.which('ffprobe')):
            self.skipTest('FFmpeg and FFprobe required')
        mp4 = self.root / '导出验收测试素材.mp4'
        subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=128x72:rate=25:duration=1',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-c:a', 'aac', str(mp4)],
                       check=True, timeout=20)
        native = self.root / 'draft_info.json'
        native.write_text('{}')
        record = {'id': '2'*32, 'status': 'published', 'name': 'test', 'at': finishing.now(), 'native_path': str(native),
                  'native_id': 'fixture', 'native_hash': app.file_hash(native), 'snapshot': {'signature': 'fixture'}}
        finishing.save(app, record)
        request = {'id': record['id'], 'source': str(mp4)}
        with self.assertRaisesRegex(ValueError, '请确认'):
            finishing.receive(app, request)
        request['confirm_export'] = True
        film = finishing.receive(app, request)
        self.assertEqual(finishing.receive(app, request), film)
        self.assertEqual(film['film_hash'], app.file_hash(mp4))
        self.assertEqual(app.api_finals()['videos'][0]['name'], mp4.name)
        self.assertEqual(agent_api.dispatch(app, {'operation':'timeline.get', 'arguments':{'final':film['final']}})['result'], finishing.UNMAPPED)
        issue = editing.issues(app, {'action':'create', 'final':film['final'], 'note':'片尾复查', 't':.5})['items'][0]
        self.assertIsNone(issue['clip'])
        decision = {'final':film['final'], 'film_hash':film['film_hash'], 'status':'approved', 'note':'测试全片复查'}
        with self.assertRaisesRegex(ValueError, '技术质检'):
            finishing.review(app, decision)
        scan = finishing.scan(app, film)
        decision['scan_id'] = scan['id']
        for _ in range(100):
            reports = finishing.listing(app)['items'][0]['outputs']
            if reports[0]['scan']['status'] != 'running':
                break
            time.sleep(.05)
        self.assertEqual(reports[0]['scan']['status'], 'completed')
        self.assertEqual(finishing.review(app, decision)['reviews'][0]['status'], 'approved')
        (self.root / film['final']).write_bytes(b'replaced')
        self.assertTrue(finishing.listing(app)['items'][0]['outputs'][0]['stale'])
        with self.assertRaisesRegex(ValueError, '回收版已变化'):
            finishing.review(app, decision)
