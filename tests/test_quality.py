import pathlib
import os
import subprocess
import time
import unittest
import test_review as fixtures
import studio as app
import quality as q


class QualityTests(unittest.TestCase):
    setUp=fixtures.ReviewTests.setUp
    tearDown=fixtures.ReviewTests.tearDown
    write=fixtures.ReviewTests.write
    edl=fixtures.ReviewTests.edl

    def change(self, **body):
        return q.mutate(app,{'revision':q.read(app)['revision'],**body})

    def test_reference_versions_requirements_and_review_invalidation(self):
        self.write('素材库/角色.png','image-v1')
        state=self.change(action='asset',name='悟空',kind='人物',image='素材库/角色.png',note='红衣')
        token=state['assets'][0]['id']
        self.change(action='shot',segment='S1',requirements='右手拿桃核',assets=[token])
        ctx=q.context(app,'S1','final/a.mp4')
        with self.assertRaises(ValueError):
            self.change(action='review',segment='S1',final='final/a.mp4',signature=ctx['signature'],checks={k:'pass' for k in q.CHECKS})
        review=app.review_context('S1','final/a.mp4')
        app.review_mutate({'segment':'S1','action':'bind','final':'final/a.mp4','signature':review['signature']})
        self.change(action='review',segment='S1',final='final/a.mp4',signature=ctx['signature'],checks={k:'pass' for k in q.CHECKS})
        self.assertEqual(q.context(app,'S1','final/a.mp4')['status'],'已检查')
        self.change(action='asset',id=token,name='悟空',kind='人物',image='素材库/角色.png',note='红衣黑靴')
        updated=q.context(app,'S1','final/a.mp4')
        self.assertEqual(updated['status'],'需复核')
        self.assertEqual(updated['references'][0]['version'],1)
        self.assertTrue(updated['references'][0]['new_version'])
        with self.assertRaises(ValueError):
            self.change(action='review',segment='S1',final='final/a.mp4',signature=ctx['signature'],checks={k:'pass' for k in q.CHECKS})
        self.change(action='shot',segment='S1',requirements='右手拿桃核',assets=[token])
        self.assertEqual(q.context(app,'S1','final/a.mp4')['references'][0]['version'],2)
        self.assertEqual(q.context(app,'S1','final/b.mp4')['status'],'待检查')
        self.write('素材库/角色.png','changed-image')
        self.assertNotEqual(q.context(app,'S1','final/a.mp4')['references'][0]['current_hash'],q.context(app,'S1','final/a.mp4')['references'][0]['record']['fingerprint'])

    def test_validation_rejects_outside_and_stale_updates(self):
        for path in ('../outside.png','.secret/image.png','素材库/镜1.mp4'):
            with self.assertRaises(ValueError):
                self.change(action='asset',name='x',kind='人物',image=path)
        self.assertEqual(q.read(app)['revision'],0)
        self.write('素材库/ref.png','image')
        self.change(action='asset',name='x',kind='道具',image='素材库/ref.png')
        with self.assertRaises(ValueError):
            q.mutate(app,{'revision':0,'action':'shot','segment':'S1','assets':[]})
        for path in ('../outside.mp4','.private/a.mp4'):
            with self.assertRaises(ValueError):q.start(app,{'path':path})

    def test_real_black_silence_decode_and_stale_report(self):
        ff=os.environ.get('FFMPEG_BIN')
        if not ff or not os.environ.get('FFPROBE_BIN'):self.skipTest('需要FFmpeg/FFprobe')
        app.probe=self.old_probe
        target=app.ROOT/'素材库/黑帧.mp4'
        subprocess.run([ff,'-v','error','-f','lavfi','-i','color=c=black:s=64x48:r=25:d=2',
                        '-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','2','-c:v','libx264','-c:a','aac',str(target)],check=True)
        q.start(app,{'path':'素材库/黑帧.mp4'})
        deadline=time.monotonic()+10
        while q.ACTIVE and time.monotonic()<deadline:time.sleep(.05)
        self.assertFalse(q.ACTIVE)
        r=q.reports(app)['items'][0]
        self.assertEqual(r['status'],'completed',r['error'])
        self.assertIn('黑帧',[e['type'] for e in r['events']])
        self.assertIn('静音段',[e['type'] for e in r['events']])
        self.assertIn('异常静帧',[e['type'] for e in r['events']])
        self.assertTrue(all(0<=e['t']<=e['end']<=2.1 for e in r['events']))
        self.write('素材库/黑帧.mp4','changed')
        self.assertTrue(q.reports(app)['items'][0]['stale'])

if __name__=='__main__':unittest.main()
