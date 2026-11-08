import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import studio as app
import upscaling as sr


class UpscaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=app.ROOT
        app.ROOT=pathlib.Path(self.tmp.name).resolve()
        (app.ROOT/'素材库').mkdir()
        self.source=app.ROOT/'素材库/镜1.mp4'
        self.source.write_bytes(b'source')
        self.config=sr.configuration(app)

    def tearDown(self):
        deadline=time.monotonic()+5
        for e in sr.EVENTS.values(): e.set()
        while sr.EVENTS and time.monotonic()<deadline: time.sleep(.05)
        sr.WORKER=None
        app.ROOT=self.root
        self.tmp.cleanup()

    def test_missing_tools_and_invalid_parameters_preserve_project(self):
        with patch.dict(os.environ,{'REALESRGAN_BIN':'/missing'}):
            with self.assertRaises(ValueError): sr.submit(app,{'paths':['素材库/镜1.mp4']})
        self.assertFalse((app.ROOT/sr.DIRECTORY).exists())

    def test_queue_cancel_restart_and_invalid_paths(self):
        if not self.config['ready']: self.skipTest('需要已配置的工具')
        with patch.object(sr.threading,'Thread'):
            for bad in ('../outside.mp4','.private/video.mp4'):
                with self.assertRaises((ValueError,PermissionError)): sr.submit(app,{'paths':[bad]})
            job=sr.submit(app,{'paths':['素材库/镜1.mp4']})['jobs'][0]
        sr.cancel(app,{'id':job['id']})
        sr.process(app,self.config,job)
        sr.EVENTS.pop(job['id'])
        self.assertEqual(sr.stored(app)[0]['status'],'cancelled')
        self.assertFalse(app.safe(job['output']).exists())
        self.source.write_bytes(b'changed')
        with self.assertRaises(ValueError): sr.submit(app,{'retry':job['id']})
        job.update(status='running');sr.save(app,job)
        self.assertEqual(sr.status(app)['jobs'][0]['status'],'interrupted')

    def test_running_engine_can_be_cancelled_without_publishing(self):
        if not self.config['ready']: self.skipTest('需要已配置的工具')
        self.source.unlink()
        subprocess.run([self.config['ffmpeg'],'-v','error','-f','lavfi','-i','color=s=64x48:r=2:d=1','-c:v','libx264',str(self.source)],check=True)
        marker=app.ROOT/'engine-started'
        fake=app.ROOT/'slow-engine'
        fake.write_text('#!/usr/bin/python3\nimport pathlib,time\npathlib.Path('+repr(str(marker))+').touch()\ntime.sleep(60)\n')
        fake.chmod(0o755)
        with patch.dict(os.environ,{'REALESRGAN_BIN':str(fake)}):
            job=sr.submit(app,{'paths':['素材库/镜1.mp4']})['jobs'][0]
            deadline=time.monotonic()+8
            while not marker.exists() and time.monotonic()<deadline: time.sleep(.05)
            self.assertTrue(marker.exists())
            sr.cancel(app,{'id':job['id']})
            while sr.EVENTS and time.monotonic()<deadline:time.sleep(.05)
            self.assertFalse(sr.EVENTS)
            self.assertEqual(sr.stored(app)[0]['status'],'cancelled')
            self.assertFalse(app.safe(job['output']).exists())

    def test_real_engine_queue_cache_compare_audio_and_source_integrity(self):
        if not self.config['ready']: self.skipTest('需要Real-ESRGAN、模型、FFmpeg和FFprobe')
        self.source.unlink()
        subprocess.run([self.config['ffmpeg'],'-v','error','-f','lavfi','-i','color=c=blue:s=64x48:r=2:d=1',
                        '-f','lavfi','-i','sine=duration=1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(self.source)],check=True)
        original=app.file_hash(self.source)
        silent=app.ROOT/'素材库/镜2.mp4'
        subprocess.run([self.config['ffmpeg'],'-v','error','-i',str(self.source),'-an','-c:v','copy',str(silent)],check=True)
        result=sr.submit(app,{'paths':['素材库/镜1.mp4','素材库/镜2.mp4'],'scale':2,'height':720})
        deadline=time.monotonic()+90
        while sr.EVENTS and time.monotonic()<deadline: time.sleep(.1)
        self.assertFalse(sr.EVENTS)
        jobs=sr.stored(app)
        self.assertEqual(len(jobs),2)
        for job in jobs:
            self.assertEqual(job['status'],'completed',job.get('error'))
            self.assertEqual(job['metadata']['frames'],2)
            self.assertEqual(job['metadata']['height'],720)
            self.assertEqual(job['metadata']['has_audio'],job['source'].endswith('镜1.mp4'))
            self.assertEqual(sr.comparison(app,job['id'])['fps'],2)
        cached=sr.submit(app,{'paths':['素材库/镜1.mp4'],'scale':2,'height':720})['jobs'][0]
        self.assertTrue(cached['reused'])
        self.assertEqual(len(sr.stored(app)),2)
        self.assertEqual(app.file_hash(self.source),original)
        target=app.safe(jobs[0]['output']);target.write_bytes(b'changed')
        with self.assertRaises(ValueError):sr.comparison(app,jobs[0]['id'])

if __name__=='__main__':unittest.main()
