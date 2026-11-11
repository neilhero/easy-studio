import os
import subprocess
import time
import unittest
from unittest.mock import patch
import test_automation as fixtures
import generation
import phosphene
import studio as app


class PhospheneTests(unittest.TestCase):
    setUp = fixtures.AutomationTests.setUp
    tearDown = fixtures.AutomationTests.tearDown

    def request(self):
        image = app.ROOT / '素材库/reference.png'
        image.write_bytes(b'fixture-image')
        return {'provider':'phosphene','segment':'S1','issue':'试动作','preserve':'角色与构图',
                'prompt':'fixture shot','first_frame':'素材库/reference.png','seconds':1,'seed':21,'request_id':'a'*32}

    def wait_job(self, identity):
        deadline = time.monotonic() + 10
        while identity in phosphene.ACTIVE and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertNotIn(identity, phosphene.ACTIVE)
        return generation.get(app, identity)

    def test_new_shot_collects_real_candidate_without_a_timeline(self):
        request = self.request()
        source=app.ROOT/'素材库/long.mp4'
        subprocess.run([generation.tool('FFMPEG'),'-v','error','-stream_loop','1','-i',str(app.ROOT/'素材库/0.mp4'),'-c','copy',str(source)],check=True)
        calls = []
        def api(url, path, data=None, content_type=None):
            calls.append(path)
            if path == '/upload': return {'path':'/fixture/reference.png'}
            if path == '/queue/add': return {'id':'native-one'}
            return {'current':None,'queue':[],'history':[{'id':'native-one','status':'done',
                     'output_path':str(source),'params':{'engine':'ltx','mode':'i2v'}}]}
        with patch.object(phosphene,'config',return_value=('http://127.0.0.1:8930',app.ROOT)), patch.object(phosphene,'http',side_effect=api):
            job = generation.submit(app, request)
            done = self.wait_job(job['id'])
            self.assertEqual(done['status'],'completed',done['error'])
            self.assertEqual(done['attempts'][0]['task_id'],'native-one')
            self.assertEqual(done['budget_used'],0)
            self.assertEqual(done['review_status'],'pending')
            self.assertTrue(app.safe(done['output']).is_file())
            generation.submit(app,request)
            self.assertEqual(calls.count('/queue/add'),1)

    def test_unknown_receipt_never_submits_again(self):
        request = self.request()
        def api(url,path,data=None,content_type=None):
            if path == '/upload':return {'path':'/fixture/ref.png'}
            raise TimeoutError('receipt lost')
        with patch.object(phosphene,'config',return_value=('http://127.0.0.1:8930',app.ROOT)), patch.object(phosphene,'http',side_effect=api):
            job = generation.submit(app,request)
            done = self.wait_job(job['id'])
            self.assertEqual(done['status'],'needs_reconciliation')
            with self.assertRaisesRegex(ValueError,'回执'):phosphene.resume(app,job['id'])
            generation.submit(app,request)
            self.assertEqual(generation.get(app,job['id'])['status'],'needs_reconciliation')

    def test_reference_and_host_validation(self):
        request = self.request()
        request['first_frame'] = '../outside.png'
        with patch.object(phosphene,'config',return_value=('http://127.0.0.1:8930',app.ROOT)):
            with self.assertRaises(ValueError):generation.submit(app,request)
        with patch.dict(os.environ,{'STUDIO_PHOSPHENE_URL':'http://example.com','STUDIO_PHOSPHENE_OUTPUTS':str(app.ROOT)}):
            with self.assertRaises(ValueError):phosphene.config()
