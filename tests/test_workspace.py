"""真实临时剪辑验证；语义审查使用明确标记的测试替身。"""
import copy
import unittest
import test_automation as fixtures
import studio as app
import automation as flow
import editing
import workspace
import agent_api


class WorkspaceTests(unittest.TestCase):
    setUp=fixtures.AutomationTests.setUp
    tearDown=fixtures.AutomationTests.tearDown
    wait=fixtures.AutomationTests.wait
    call=fixtures.AutomationTests.call

    def draft(self,clips=None):
        clips=clips or [{'id':'S1','src':'素材库/0.mp4','in':.1,'out':.5,'a':.7},
                        {'id':'S2','src':'素材库/2.mp4','in':0,'out':.4,'a':1}]
        return editing.mutate(app,{'revision':editing.read(app)['revision'],'clips':clips})

    def complete(self,job):
        job=self.wait(job['id'])
        while job['status']=='waiting_review':
            p=flow.claim(app,{'run_id':job['id'],'actor':'TEST-HOST'})['packet']
            self.assertIsNotNone(p)
            flow.submit(app,{'run_id':job['id'],'packet_id':p['id'],'lease_token':p['lease']['token'],'signature':p['signature'],'result':fixtures.review_stub(p)})
            job=self.wait(job['id'])
        self.assertEqual(job['status'],'completed',job['error'])
        return job

    def test_default_checks_saved_source_range_and_gain_without_changing_draft(self):
        draft=self.draft();job=self.wait(flow.start(app,{})['id'])
        self.assertEqual(job['status'],'waiting_review',job['error'])
        self.assertEqual(job['mode'],'inspect')
        self.assertEqual(job['plan']['clips'],draft['clips'])
        self.assertEqual({p['kind'] for p in job['packets']},{'current','seam','final'})
        self.assertEqual(editing.read(app),draft)
        for p in job['packets']:
            self.assertTrue(all(p['range'][0]<=e['t']<e['end']<=p['range'][1] for e in p['technical']['events']))
        rows=agent_api.dispatch(app,{'operation':'automation.workspace','arguments':{}})['result']['shots']
        self.assertEqual(rows[0]['uses'][0]['src'],'素材库/0.mp4')
        self.assertEqual(rows[0]['spare_count'],1)
        # 未入片候选变化，不使当前剪辑检查过期。
        app.safe('素材库/1.mp4').write_bytes(b'changed-unused-candidate')
        self.assertFalse(flow.get(app,job['id'])['input_stale'])
        # 保存换片后旧报告保持原样，并明确提示已过期。
        clips=copy.deepcopy(draft['clips']);clips[0]['src']='素材库/2.mp4'
        self.draft(clips)
        old=flow.get(app,job['id'])
        self.assertTrue(old['input_stale'])
        self.assertIn('剪辑已修改',old['stale_reason'])
        self.assertEqual(old['plan']['clips'][0]['src'],'素材库/0.mp4')
        with self.assertRaisesRegex(ValueError,'剪辑已修改'):flow.claim(app,{'run_id':job['id'],'actor':'host'})

    def test_replace_middle_checks_two_joins_reuses_unchanged_clips_and_keeps_history(self):
        with (app.ROOT/app.SEG).open('a') as f:f.write('| S3 | 0.8 | 镜3 | 测试静态画面 |\n')
        app.review_mutate({'action':'associate','segment':'S3','path':'素材库/0.mp4'})
        clips=[{'id':'S1','src':'素材库/0.mp4','in':0,'out':.4,'a':1},
               {'id':'S2','src':'素材库/2.mp4','in':0,'out':.4,'a':1},
               {'id':'S3','src':'素材库/0.mp4','in':.2,'out':.6,'a':1}]
        self.draft(clips);first=self.complete(flow.start(app,{}))
        frozen=app.safe(f"{flow.BASE}/{first['id']}/run.json").read_bytes()
        self.assertEqual(len(first['packets']),6)
        for p in first['packets']:
            if p['kind']=='seam':self.assertLessEqual(p['range'][1]-p['range'][0],.801)
        unchanged=flow.start(app,{})
        self.assertEqual(unchanged['id'],first['id']);self.assertTrue(unchanged['reused_run'])
        clips[1].update(src='素材库/1.mp4',out=.6)
        updated=self.draft(clips);second=self.wait(flow.start(app,{})['id'])
        self.assertEqual(second['status'],'waiting_review',second['error'])
        reused=[p for p in second['packets'] if p.get('reused_from')]
        self.assertEqual({p['segment'] for p in reused},{'S1','S3'})
        self.assertEqual(len(reused),2)
        self.assertTrue(all(not p.get('reused_from') for p in second['packets'] if p['kind'] in ('seam','final')))
        self.assertEqual(second['plan']['clips'][1]['src'],'素材库/1.mp4')
        shifted=next(p for p in reused if p['segment']=='S3')
        self.assertNotEqual(shifted['range'],shifted['current_range'])
        second=self.complete(second)
        self.assertEqual(editing.read(app),updated)
        self.assertNotEqual(first['final'],second['final'])
        self.assertEqual(app.safe(f"{flow.BASE}/{first['id']}/run.json").read_bytes(),frozen)
        self.assertTrue(app.safe(first['final']).is_file())
        # 显式勾选可强制复查一镜及其相邻接缝。
        forced=self.wait(flow.start(app,{'segments':['S1']})['id'])
        fresh_slots={p['inspection_slot'] for p in forced['packets'] if not p.get('reused_from')}
        self.assertEqual(fresh_slots,{'current:0','seam:1','full'})

    def test_rebuild_is_separate_version_adoption_is_explicit_and_undoable(self):
        original=self.draft();review_before=app.review_read()
        job=self.complete(flow.start(app,{'mode':'rebuild','segments':['S2']}))
        self.assertEqual(editing.read(app),original)
        self.assertEqual(app.review_read(),review_before)
        self.assertTrue(app.safe(job['final']).exists())
        self.call('automation.apply',{'run_id':job['id'],'draft_revision':original['revision']})
        adopted=editing.read(app)
        self.assertEqual(adopted['clips'][0]['src'],'素材库/2.mp4')
        self.assertEqual(app.review_read(),review_before)
        editing.mutate(app,{'revision':adopted['revision'],'action':'undo'})
        self.assertEqual(editing.read(app)['clips'],original['clips'])
        with self.assertRaisesRegex(ValueError,'草稿另有修改'):
            flow.apply_suggestion(app,{'run_id':job['id'],'draft_revision':editing.read(app)['revision']})

    def test_check_chosen_final_does_not_follow_later_draft_changes(self):
        self.draft();first=self.complete(flow.start(app,{}))
        self.draft([{'id':'S1','src':'素材库/1.mp4','in':0,'out':.6,'a':1}])
        checked=self.wait(flow.start(app,{'source':'final','final':first['final']})['id'])
        self.assertEqual(checked['final'],first['final'])
        self.assertEqual(checked['plan']['clips'][0]['src'],'素材库/0.mp4')
        self.draft([{'id':'S1','src':'素材库/2.mp4','in':0,'out':.4,'a':1}])
        self.assertFalse(flow.get(app,checked['id'])['input_stale'])

    def test_missing_or_complex_timeline_is_explained_not_silently_rebuilt(self):
        with self.assertRaisesRegex(ValueError,'没有当前剪辑'):flow.start(app,{})
        original=app.api_timeline
        try:
            app.api_timeline=lambda:{'found':True,'V':[{'id':'S1','src':'素材库/0.mp4','in':.1,'out':.5,'a':.6}],'A':[],'subs':[],'look':'none'}
            plan=workspace.source(app)
            self.assertEqual(plan['kind'],'legacy')
            self.assertEqual(plan['clips'][0]['in'],.1)
            legacy=self.wait(flow.start(app,{})['id'])
            self.assertEqual(legacy['status'],'waiting_review',legacy['error'])
            self.draft()
            self.assertTrue(flow.get(app,legacy['id'])['input_stale'])
            app.api_timeline=lambda:{'found':True,'V':[{'id':'S1'}],'A':[{'kind':'music'}]}
            with self.assertRaisesRegex(ValueError,'额外声音'):flow.start(app,{'source':'legacy'})
        finally:app.api_timeline=original

    def test_tampered_old_evidence_requires_new_check(self):
        self.draft();first=self.complete(flow.start(app,{}))
        p=next(p for p in first['packets'] if p['kind']=='current')
        app.safe(p['frames'][0]['path']).write_bytes(b'tampered')
        second=self.wait(flow.start(app,{})['id'])
        self.assertNotEqual(second['id'],first['id'])
        current=next(p for p in second['packets'] if p['inspection_slot']=='current:0')
        self.assertNotIn('reused_from',current)

if __name__=='__main__':unittest.main()
