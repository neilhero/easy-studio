import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import studio as app
import automation as flow
import review_evidence as ev
import editing
import quality
import agent_api


def review_stub(p,score=90):
    # 测试协议用替身，不是对真实内容质量的模型准确率评测。
    return {'model':'TEST-STUB/1','reason':'已知测试色块素材，验证流程契约','score':score,'usable':p['range'],
            'checks':{k:{'status':'pass','confidence':.95,'evidence':['clip'],'note':'测试替身判断'} for k in ev.CHECKS},
            'watched_ranges':[p['range']],'listened_ranges':[p['range']] if p['audio'] else [],'technical_disposition':'测试素材为静态色块/静音，按测试设计接受'}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get('FFMPEG_BIN') or not os.environ.get('FFPROBE_BIN'):self.skipTest('需要FFmpeg/FFprobe')
        self.tmp=tempfile.TemporaryDirectory();self.root=app.ROOT;app.ROOT=pathlib.Path(self.tmp.name).resolve()
        self.counter=0
        self.saved_env={k:os.environ.pop(k,None) for k in ('STUDIO_REVIEW_COMMAND','STUDIO_ASR_PYTHON','STUDIO_ASR_MODEL')}
        (app.ROOT/'素材库').mkdir();(app.ROOT/'脚本').mkdir()
        (app.ROOT/app.SEG).write_text('| 段 | 秒 | 节拍 | 内容 |\n|---|---|---|---|\n| S1 | 0.8 | 镜1 | 测试静态画面 |\n| S2 | 0.8 | 镜2 | 测试静态画面 |\n')
        for i,color in enumerate(('blue','red','green')):
            p=app.ROOT/f'素材库/{i}.mp4'
            subprocess.run([os.environ['FFMPEG_BIN'],'-v','error','-f','lavfi','-i',f'color=c={color}:s=64x48:r=25:d=0.8','-c:v','libx264','-pix_fmt','yuv420p',str(p)],check=True)
            app.review_mutate({'action':'associate','segment':'S1' if i<2 else 'S2','path':f'素材库/{i}.mp4'})

    def tearDown(self):
        for event in list(flow.ACTIVE.values()):event.set()
        deadline=time.monotonic()+10
        while (flow.ACTIVE or editing.ACTIVE or quality.ACTIVE) and time.monotonic()<deadline:time.sleep(.05)
        app.ROOT=self.root;self.tmp.cleanup()
        for k,v in self.saved_env.items():
            if v is not None:os.environ[k]=v
            else:os.environ.pop(k,None)

    def call(self,operation,arguments):
        current=agent_api.project(app);self.counter+=1
        return agent_api.dispatch(app,{'operation':operation,'arguments':arguments,'project_id':current['project_id'],'expected_revision':current['revision'],'idempotency_key':f'workflow-{self.counter}'})

    def wait(self,identity):
        deadline=time.monotonic()+25
        while identity in flow.ACTIVE and time.monotonic()<deadline:time.sleep(.03)
        self.assertNotIn(identity,flow.ACTIVE)
        return flow.get(app,identity)

    def test_host_review_to_rough_cut_then_seam_and_full_recheck(self):
        job=self.call('automation.start',{'segments':['S1','S2']})['result'];job=self.wait(job['id'])
        self.assertEqual(job['status'],'waiting_review',job['error'])
        self.assertEqual(len(job['packets']),3)
        first=True
        while job['status']=='waiting_review':
            claimed=self.call('automation.claim',{'run_id':job['id'],'actor':'test-host'})['result']
            p=claimed['packet'];self.assertIsNotNone(p)
            result=review_stub(p,score=95 if p['source'].endswith('/1.mp4') else 80)
            self.call('automation.submit',{'run_id':job['id'],'packet_id':p['id'],'lease_token':p['lease']['token'],'signature':p['signature'],'result':result})
            job=self.wait(job['id'])
            self.assertNotEqual(job['status'],'failed',job['error'])
            if first:
                self.assertEqual(job['status'],'waiting_review');first=False
        self.assertEqual(job['status'],'completed',job['error'])
        self.assertEqual(job['selection'][0]['src'],'素材库/1.mp4')
        self.assertTrue(app.safe(job['final']).is_file())
        self.assertEqual({p['kind'] for p in job['packets']},{'candidate','final','seam'})
        self.assertTrue(all(r['cells']['过审']=='待审' for r in app.api_segments()['rows']))
        self.assertIn('待导演验收',job['content_status'])
        metrics=flow.evaluate(app,{'run_id':job['id'],'labels':[{'packet_id':job['packets'][0]['id'],'check':'props','expected':'fail'}]})
        self.assertEqual(metrics['counts']['fn'],1)
        self.assertEqual(metrics['decision_coverage'],1)
        with agent_api.database(app) as db:
            tasks=agent_api.task_sync(app,db)
        self.assertTrue(any(t['kind']=='automation' and t['status']=='succeeded' for t in tasks))
        final=next(p for p in job['packets'] if p['kind']=='final')
        reopened=flow.reopen(app,{'run_id':job['id'],'packet_id':final['id'],'note':'补充试听'})
        self.assertEqual(reopened['status'],'waiting_review')
        self.assertEqual(next(p for p in reopened['packets'] if p['id']==final['id'])['review_history'][0]['note'],'补充试听')

    def test_scope_confidence_lease_and_changed_source_are_not_bypassed(self):
        job=self.wait(flow.start(app,{'segments':['S1']})['id']);claimed=flow.claim(app,{'run_id':job['id'],'actor':'one'});p=claimed['packet']
        self.assertIsNotNone(p)
        bad=review_stub(p);bad['watched_ranges']=[]
        with self.assertRaises(ValueError):ev.validate(p,bad,.8)
        low=review_stub(p);low['checks']['identity']['confidence']=.2
        self.assertFalse(ev.validate(p,low,.8)['eligible'])
        with self.assertRaises(ValueError):flow.submit(app,{'run_id':job['id'],'packet_id':p['id'],'lease_token':'wrong','signature':p['signature'],'result':review_stub(p)})
        app.safe(p['source']).write_bytes(b'changed')
        self.assertTrue(flow.get(app,job['id'])['input_stale'])
        with self.assertRaises(ValueError):flow.submit(app,{'run_id':job['id'],'packet_id':p['id'],'lease_token':p['lease']['token'],'signature':p['signature'],'result':review_stub(p)})

    def test_user_draft_edit_is_never_overwritten(self):
        job=self.wait(flow.start(app,{'segments':['S2']})['id'])
        editing.mutate(app,{'revision':0,'clips':[{'id':'manual','src':'素材库/0.mp4','in':0,'out':.4,'a':1}]})
        p=flow.claim(app,{'run_id':job['id'],'actor':'host'})['packet']
        flow.submit(app,{'run_id':job['id'],'packet_id':p['id'],'signature':p['signature'],'lease_token':p['lease']['token'],'result':review_stub(p)})
        job=self.wait(job['id'])
        self.assertEqual(job['status'],'failed')
        self.assertIn('草稿',job['error'])
        self.assertEqual(editing.read(app)['clips'][0]['id'],'manual')

    def test_full_review_cannot_pass_without_continuous_video_and_sound(self):
        job=self.wait(flow.start(app,{'segments':['S2']})['id']);p=copy.deepcopy(job['packets'][0])
        p['kind']='final';r=review_stub(p)
        r['checks']['action']['status']='na';r['watched_ranges']=[]
        self.assertFalse(ev.validate(p,r,.8)['eligible'])
        p['expected_dialogue']='必须有台词'
        with self.assertRaises(ValueError):ev.validate(p,review_stub(p),.8)

    def test_packet_tampering_draft_conflict_and_unknown_export_window(self):
        job=self.wait(flow.start(app,{'segments':['S2']})['id']);p=job['packets'][0]
        app.safe(p['frames'][0]['path']).write_bytes(b'tampered')
        with self.assertRaises(ValueError):flow.claim(app,{'run_id':job['id'],'actor':'host'})
        job['phase']='dispatching';job['status']='running';flow.save(app,job)
        self.assertEqual(flow.get(app,job['id'])['status'],'interrupted')
        with self.assertRaises(ValueError):flow.resume(app,job['id'])
        flow.cancel(app,job['id'])
        self.assertEqual(flow.get(app,job['id'])['status'],'cancelled')

    def test_local_command_backend_completes_and_keeps_failure_pending(self):
        script=app.ROOT/'reviewer.py'
        script.write_text('import sys,json\np=json.load(sys.stdin)["packet"]\n'+
            'r={"model":"TEST-COMMAND/1","reason":"fixture","score":90,"usable":p["range"],"watched_ranges":[p["range"]],"listened_ranges":[p["range"]] if p["audio"] else [],"technical_disposition":"fixture",'+
            '"checks":{k:{"status":"pass","confidence":.95,"evidence":["clip"],"note":"fixture"} for k in ("identity","props","direction","action","dialogue")}}\nprint(json.dumps(r))\n')
        os.environ['STUDIO_REVIEW_COMMAND']=json.dumps([sys.executable,str(script)])
        job=self.wait(flow.start(app,{'segments':['S2']})['id'])
        self.assertEqual(job['status'],'completed',job['error'])
        self.assertTrue(all(p['review']['actor']=='configured-command' for p in job['packets']))
        # 明确台词要求不能被NA绕过；即使无声也须判待审/需修。
        p=copy.deepcopy(job['packets'][0]);p['expected_dialogue']='不能遗漏的台词'
        result=review_stub(p);result['checks']['dialogue']['status']='na'
        with self.assertRaises(ValueError):ev.validate(p,result,.8)

if __name__=='__main__':unittest.main()
