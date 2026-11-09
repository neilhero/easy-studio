"""Episode regression with real encodes/inference and explicitly stubbed semantic review."""
import json
import os
import subprocess
import time
import unittest

import agent_api
import automation
import editing
import episode
import generation
import studio as app
import test_automation as fixtures


class EpisodeTests(unittest.TestCase):
    setUp = fixtures.AutomationTests.setUp
    call = fixtures.AutomationTests.call

    def tearDown(self):
        for token in list(episode.ACTIVE):
            with agent_api.LOCK:
                episode.pause(app, {'id':token})
        deadline = time.monotonic()+15
        while generation.ACTIVE and time.monotonic()<deadline:
            time.sleep(.05)
        fixtures.AutomationTests.tearDown(self)

    def write_draft(self,count):
        clips=[{'id':'S1','src':'素材库/0.mp4','in':0,'out':.4,'a':1},
               {'id':'S2','src':'素材库/2.mp4','in':0,'out':.4,'a':1}]
        for i in range(3,count+1):
            with app.safe(app.SEG).open('a') as f:
                f.write(f'| S{i} | .4 | 镜{i} | 自动制作验收夹具 |\n')
            rel=f'素材库/shot-{i}.mp4'
            subprocess.run([os.environ['FFMPEG_BIN'],'-v','error','-f','lavfi','-i',
                            'testsrc2=s=64x48:r=25:d=0.8',
                            '-vf',f'hue=h={i*25}', '-c:v','libx264',str(app.safe(rel))],check=True)
            app.review_mutate({'action':'associate','segment':f'S{i}','path':rel})
            clips.append({'id':f'S{i}','src':rel,'in':0,'out':.4,'a':1})
        return editing.mutate(app,{'revision':0,'clips':clips})

    def wait_for(self,identity,until,review=True):
        deadline=time.monotonic()+80
        while time.monotonic()<deadline:
            with agent_api.LOCK:
                run=episode.view(episode.load(app,identity))
                if until(run):
                    return run
                if run['status']=='failed':
                    self.fail(run['stage'])
                if review:
                    for job in automation.jobs(app):
                        if job.get('owner')==identity and job['status']=='waiting_review' and not job['input_stale']:
                            p=automation.claim(app,{'run_id':job['id'],'actor':'TEST-HOST'})['packet']
                            if p:
                                automation.submit(app,{'run_id':job['id'],'packet_id':p['id'],'lease_token':p['lease']['token'],
                                                        'signature':p['signature'],'result':fixtures.review_stub(p)})
            time.sleep(.06)
        self.fail(f'{run["status"]} {run["stage"]}')

    def start(self,**args):
        project=agent_api.project(app)
        body={'operation':'episode.start','arguments':{'authorize':True,'acceptance':'reviewed',**args},
              'idempotency_key':'episode-start','project_id':project['project_id'],'expected_revision':project['revision']}
        run=agent_api.dispatch(app,body)['result']
        self.assertTrue(agent_api.dispatch(app,body)['replayed'])
        return run['id']

    def test_ten_shots_resume_then_repair_and_incrementally_deliver(self):
        if not generation.local_config():
            self.skipTest('需要已配置的真实本地 RIFE')
        initial=self.write_draft(10)
        token=self.start(source='current',max_credits=0,max_attempts=2)
        waiting=self.wait_for(token,lambda r:r['status']=='waiting_review',review=False)
        initial_id=episode.cycle(waiting)['initial']
        packets=[p['id'] for p in automation.get(app,initial_id)['packets']]
        self.assertEqual(len(packets),20)
        self.call('episode.pause',{'id':token})
        self.assertEqual(episode.view(episode.load(app,token))['status'],'paused')
        # Simulate a service restart while the persisted coordinator was enabled.
        with agent_api.LOCK:
            raw=episode.load(app,token)
            raw['enabled']=True
            episode.save(app,raw)
        self.assertEqual(episode.view(episode.load(app,token))['status'],'interrupted')
        self.call('episode.resume',{'id':token})
        first=self.wait_for(token,lambda r:r['status']=='completed')
        self.assertEqual(episode.cycle(first)['initial'],initial_id)
        self.assertEqual([p['id'] for p in automation.get(app,initial_id)['packets']],packets)
        first_path=first['final']
        frozen=app.safe(first['report']).read_bytes()

        self.call('episode.resume',{'id':token,'refresh':True,'authorize_repairs':True,
            'repairs':[{'segment':'S5','provider':'rife','issue':'补齐动作采样','preserve':'原画面与剪辑时长'}]})
        second=self.wait_for(token,lambda r:r['status']=='completed')
        current=editing.read(app)
        self.assertEqual(current['revision'],initial['revision']+1)
        self.assertNotEqual(current['clips'][4]['src'],initial['clips'][4]['src'])
        self.assertEqual([c for c in current['clips'] if c['id']!='S5'],[c for c in initial['clips'] if c['id']!='S5'])
        self.assertNotEqual(first_path,second['final'])
        self.assertTrue(app.safe(first_path).is_file())
        self.assertEqual(app.safe(first['report']).read_bytes(),frozen)
        report=episode.report(app,episode.load(app,token))
        self.assertEqual(report['budget']['charged'],0)
        self.assertEqual(len(report['budget']['jobs']),1)
        self.assertEqual(report['budget']['jobs'][0]['segment'],'S5')
        self.assertTrue(report['budget']['jobs'][0]['attempts'][0]['model_fingerprint'])
        audit=next(x for x in report['checks'] if x['id']==episode.cycle(second)['audits'][0])
        self.assertEqual(audit['encoding']['encoded_segments'],['S5'])
        self.assertEqual(len(audit['encoding']['reused_segments']),9)
        self.assertEqual(len([p for p in audit['packets'] if p['reused_from']]),16)
        self.assertEqual(sorted(p['kind'] for p in audit['packets'] if not p['reused_from']),['current','final','seam','seam'])
        self.assertTrue(all(not p['reused_from'] for p in audit['packets'] if p.get('segment')=='S5'))
        self.assertEqual(report['unresolved'],[])

    def test_credit_reservation_unknown_and_budget_limited_retry(self):
        self.write_draft(2)
        request={'segment':'S1','provider':'libtv','issue':'动作异常','preserve':'人物与机位','libtv_project':'test-only',
                 'model':'test-only','prompt':'测试替身，不向外部提交','quoted_credits':6}
        token=self.start(max_credits=12,max_attempts=3,repairs=[request])
        run=self.wait_for(token,lambda r:r['status']=='waiting_host')
        job_id=episode.cycle(run)['repairs'][0]['job_id']
        def claim(**extra):
            return self.call('generation.claim',{'job_id':job_id,'actor':'TEST-HOST',**extra})['result']
        def record(claimed,**result):
            return self.call('generation.record',{'job_id':job_id,'lease_token':claimed['lease_token'],**result})['result']
        first=claim()
        record(first,outcome='unknown')
        self.wait_for(token,lambda r:r['status']=='needs_reconciliation',review=False)
        self.assertEqual(len(generation.get(app,job_id)['attempts']),1)
        self.assertEqual(episode.report(app,episode.load(app,token))['budget']['reserved'],6)
        self.call('episode.pause',{'id':token})
        self.call('episode.resume',{'id':token})
        recovered=claim(recover=True)
        self.assertEqual(recovered['attempts'][0]['key'],first['attempts'][0]['key'])
        record(recovered,outcome='failed',task_id='test-1',actual_credits=6,reason='测试失败')
        self.wait_for(token,lambda r:len(generation.get(app,job_id)['attempts'])==2)
        second=claim()
        with self.assertRaisesRegex(ValueError,'整集预算'):
            self.call('generation.retry',{'job_id':job_id,'attempt':2,'quoted_credits':6,'confirm_spend':True})
        record(second,outcome='failed',task_id='test-2',actual_credits=6,reason='测试失败')
        self.wait_for(token,lambda r:r['status']=='needs_decision')
        budget=episode.report(app,episode.load(app,token))['budget']
        self.assertEqual((budget['charged'],budget['reserved'],budget['remaining']),(12,0,0))
        self.assertEqual(len(generation.get(app,job_id)['attempts']),2)

    def test_review_template_creates_no_film_and_requires_explicit_authority(self):
        with self.assertRaisesRegex(ValueError,'明确授权'):
            self.call('episode.start',{'template':'review','authorize':False})
        token=self.start(template='review')
        self.wait_for(token,lambda r:r['status']=='completed')
        run=episode.load(app,token)
        report=json.loads(app.safe(episode.cycle(run)['report']).read_text())
        self.assertEqual(report['artifacts'],[])
        self.assertEqual(len(report['checks']),1)
        self.assertTrue(all(p['kind']=='candidate' for p in report['checks'][0]['packets']))
