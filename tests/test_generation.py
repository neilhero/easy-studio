"""Exercise the same generation ledger used by HTTP and MCP."""
import time
import subprocess
import unittest

import test_automation as fixtures
import editing
import generation
import studio as app


class GenerationTests(unittest.TestCase):
    setUp = fixtures.AutomationTests.setUp
    tearDown = fixtures.AutomationTests.tearDown
    call = fixtures.AutomationTests.call

    def draft(self):
        return editing.mutate(app, {'revision': 0, 'clips': [
            {'id': 'S1', 'src': '素材库/0.mp4', 'in': .1, 'out': .5, 'a': .7},
            {'id': 'S2', 'src': '素材库/2.mp4', 'in': 0, 'out': .4, 'a': 1},
        ]})

    def submit(self, **kwargs):
        request = {
            'provider': 'libtv', 'segment': 'S1', 'issue': '动作断开', 'preserve': '机位和角色',
            'libtv_project': 'fixture-project', 'model': 'fixture-model', 'prompt': 'Same shot',
            'max_credits': 12, 'quoted_credits': 6, 'confirm_spend': True, 'max_attempts': 3,
        }
        return self.call('generation.submit', {**request, **kwargs})['result']

    def claim(self, job, **kwargs):
        return self.call('generation.claim', {'job_id': job['id'], 'actor': 'fixture', **kwargs})['result']

    def record(self, job, **kwargs):
        return self.call('generation.record', {'job_id': job['id'], 'lease_token': job['lease_token'], **kwargs})['result']

    def test_unknown_dispatch_reuses_key_and_budget_blocks_new_attempt(self):
        self.draft()
        job = self.submit()
        claim = self.claim(job)
        with self.assertRaisesRegex(ValueError, '已被认领'):
            self.claim(job)
        unknown = self.record(claim, outcome='unknown')
        self.assertEqual(unknown['status'], 'needs_reconciliation')
        with self.assertRaisesRegex(ValueError, '仅可重试'):
            self.call('generation.retry', {'job_id':job['id'], 'attempt':1, 'quoted_credits':6,'confirm_spend':True})
        recovered = self.claim(job, recover=True)
        self.assertTrue(recovered['recovering'])
        self.assertEqual(recovered['attempts'][0]['key'], claim['attempts'][0]['key'])
        running = self.record(recovered, outcome='submitted', task_id='task-a')
        poll = self.claim(running)
        with self.assertRaisesRegex(ValueError,'不能改绑'):
            self.record(poll,outcome='failed',task_id='different',reason='fixture')
        failed = self.record(poll,outcome='failed',task_id='task-a',reason='fixture')
        second = self.call('generation.retry',{'job_id':failed['id'],'attempt':1,'quoted_credits':6,'confirm_spend':True})['result']
        self.assertEqual(len(second['attempts']),2)
        self.assertIsNone(second['attempts'][0]['credits'])
        self.assertNotEqual(second['attempts'][1]['key'],second['attempts'][0]['key'])
        self.record(self.claim(second),outcome='failed',task_id='task-b',reason='fixture')
        with self.assertRaisesRegex(ValueError,'预算'):
            self.call('generation.retry',{'job_id':job['id'],'attempt':2,'quoted_credits':1,'confirm_spend':True})

    def test_resume_collect_review_replace_export_only_the_target(self):
        draft = self.draft()
        job = self.submit(resume_task_id='task-known')
        self.assertEqual(job['phase'],'poll')
        self.assertEqual(job['max_credits'],0)
        claim = self.claim(job)
        self.record(claim,outcome='succeeded',task_id='task-known',node_id='known-node',output_index=1)
        claim = self.claim(job)
        source = app.ROOT/'素材库/不同尺寸.mp4'
        subprocess.run([generation.tool('FFMPEG'),'-v','error','-i',str(app.safe('素材库/1.mp4')),
                        '-vf','scale=96:64','-c:v','libx264',str(source)],check=True)
        saved = app.IMPORT_ROOTS
        app.IMPORT_ROOTS = [app.ROOT]
        self.addCleanup(setattr, app, 'IMPORT_ROOTS', saved)
        info = {'project_id':'fixture-project','node_id':'known-node','output_index':1,'source_identity':'fixture-resource','watermark':'keep'}
        args = {'outcome':'downloaded','delivery':info,'source':str(source),'sha256':app.file_hash(source)}
        with self.assertRaisesRegex(ValueError,'输出序号'):
            self.record(claim, **{**args,'delivery':{**info,'output_index':0}})
        done = self.record(claim, **args)
        self.assertEqual(editing.read(app),draft)
        self.assertEqual(done['status'],'completed')
        self.assertTrue(any(c['path']==done['output'] for c in app.segment_row('S1')['files']['candidates']))
        self.assertFalse(any(c['path']==done['output'] for c in app.segment_row('S2')['files']['candidates']))
        with self.assertRaisesRegex(ValueError,'先审查'):
            self.call('generation.apply',{'job_id':job['id']})
        app.review_mutate({'segment':'S1','action':'review','path':done['output'],'fingerprint':done['output_hash'],'status':'approved','note':'fixture manual review'})
        new = self.call('generation.apply',{'job_id':job['id'],'in':.1})['result']
        updated = editing.read(app)
        self.assertEqual(updated['clips'][1],draft['clips'][1])
        self.assertEqual(updated['clips'][0]['src'],done['output'])
        self.assertAlmostEqual(updated['clips'][0]['out']-updated['clips'][0]['in'],.4)
        self.assertEqual(updated['clips'][0]['a'],.7)
        self.assertEqual(new['applied_revision'],updated['revision'])
        export = self.call('generation.export',{'job_id':job['id']})['result']
        self.wait_export(export['id'])
        self.assertEqual(self.call('generation.export',{'job_id':job['id']})['result']['id'],export['id'])
        self.assertEqual(editing.manifest(app,export['final'])['timeline']['V'][1]['src'],'素材库/2.mp4')
        self.assertEqual(app.probe(app.safe(export['final']))['width'],64)

    def wait_export(self, job_id):
        deadline=time.monotonic()+15
        while job_id in editing.ACTIVE and time.monotonic()<deadline:
            time.sleep(.03)
        job=next(x for x in editing.jobs(app) if x['id']==job_id)
        self.assertEqual(job['status'],'completed',job['error'])

    def test_local_real_inference_and_review_gate(self):
        if not generation.local_config():
            self.skipTest('需要配置真实 RIFE 模型')
        original=self.draft()
        job=self.call('generation.submit',{'provider':'rife','segment':'S1','issue':'需要补帧','preserve':'同构图'})['result']
        deadline=time.monotonic()+60
        while job['id'] in generation.ACTIVE and time.monotonic()<deadline:
            time.sleep(.05)
        result=generation.get(app,job['id'])
        self.assertEqual(result['status'],'completed',result['error'])
        self.assertGreater(app.probe(app.safe(result['output']))['fps'],25)
        self.assertEqual(result['review_status'],'pending')
        self.assertEqual(editing.read(app),original)
        self.assertEqual(result['attempts'][0]['credits'],0)
