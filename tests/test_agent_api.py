import json
import os
import pathlib
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import studio as app
import agent_api as api
import editing


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.imports=tempfile.TemporaryDirectory()
        self.oldroot,self.oldimports=app.ROOT,app.IMPORT_ROOTS
        app.ROOT=pathlib.Path(self.tmp.name).resolve()
        app.IMPORT_ROOTS=[pathlib.Path(self.imports.name).resolve()]
        (app.ROOT/'素材库').mkdir()
        (app.ROOT/'素材库/a.mp4').write_bytes(b'a')
        self.count=0

    def tearDown(self):
        app.ROOT,app.IMPORT_ROOTS=self.oldroot,self.oldimports
        self.tmp.cleanup();self.imports.cleanup()

    def call(self,operation,arguments=None,**extra):
        state=api.project(app);self.count+=1
        body={'operation':operation,'arguments':arguments or {},'project_id':state['project_id'],'expected_revision':state['revision'],'idempotency_key':str(self.count),**extra}
        return api.dispatch(app,body)

    def test_stable_ids_replay_conflicts_and_web_changes(self):
        before=api.project(app)
        body={'operation':'shots.create','arguments':{'segment':'S1','scene':'镜1'},'project_id':before['project_id'],'expected_revision':before['revision'],'idempotency_key':'shot'}
        first=api.dispatch(app,body)
        self.assertTrue(api.dispatch(app,body)['replayed'])
        self.assertEqual(first['result']['segment'],'S1')
        self.assertEqual(len(app.api_segments()['rows']),1)
        with self.assertRaises(ValueError):api.dispatch(app,{**body,'arguments':{'segment':'S2'}})
        with self.assertRaises(ValueError):api.dispatch(app,{**body,'idempotency_key':'new'})
        assets=self.call('assets.list')['result']['items']
        identity=assets[0]['id']
        self.assertEqual(self.call('assets.list')['result']['items'][0]['id'],identity)
        current=api.project(app)
        app.review_mutate({'segment':'S1','action':'associate','path':'素材库/a.mp4'})
        with self.assertRaises(ValueError):
            self.call('shots.create',{'segment':'S2'},expected_revision=current['revision'])
        (app.ROOT/'素材库/a.mp4').rename(app.ROOT/'素材库/moved.mp4')
        result=self.call('assets.relink',{'asset_id':identity,'path':'素材库/moved.mp4'})
        self.assertEqual(result['result']['asset_id'],identity)
        self.assertEqual(self.call('assets.list')['result']['total'],1)

    def test_import_roots_and_private_files(self):
        source=app.IMPORT_ROOTS[0]/'source.mp4';source.write_bytes(b'media')
        copied=self.call('assets.import',{'source':str(source)})['result']
        self.assertEqual((app.ROOT/copied['path']).read_bytes(),b'media')
        with self.assertRaises(PermissionError):self.call('assets.import',{'source':str(app.ROOT/'素材库/a.mp4')})
        alias=app.IMPORT_ROOTS[0]/'external.mp4';alias.symlink_to(app.ROOT/'素材库/a.mp4')
        with self.assertRaises(PermissionError):self.call('assets.import',{'source':str(alias)})
        secret=api.token(app)
        self.assertGreater(len(secret),30)
        self.assertIsNone(app.safe('.studio/agent-token'))
        self.assertEqual((api.private(app)/'agent-token').stat().st_mode & 0o777,0o600)
        with self.assertRaises(ValueError):self.call('shots.create',{'segment':'X'},project_id='wrong-project')
        lock=api.acquire_service_lock(app)
        try:
            with self.assertRaises(ValueError):api.acquire_service_lock(app)
        finally:lock.close()

    def test_unknown_receipt_never_replays_side_effect(self):
        current=api.project(app)
        body={'operation':'shots.create','arguments':{'segment':'S1'},'expected_revision':current['revision'],'project_id':current['project_id'],'idempotency_key':'uncertain'}
        with api.database(app) as db:
            db.execute('INSERT INTO receipts VALUES(?,?,?,NULL)',('uncertain',json.dumps(body,sort_keys=True,ensure_ascii=False),'dispatching'))
        result=api.dispatch(app,body)
        self.assertEqual(result['state'],'needs_reconciliation')
        self.assertFalse((app.ROOT/app.SEG).exists())

    def test_unified_web_task_and_restart_state(self):
        editing.write(app,'剪辑/导出任务/test.json',{'id':'test','status':'running','final':'final/test.mp4'})
        task=self.call('tasks.list')['result']['items'][0]
        self.assertEqual(task['status'],'interrupted')
        self.assertFalse(task['can_cancel'])
        self.assertEqual(self.call('tasks.get',{'task_id':task['id']})['result']['id'],task['id'])
        self.assertEqual(task['content_status'],'not_inferred')

    def test_unified_cancel_and_retry_preserve_native_snapshot(self):
        probe={'duration':2,'width':64,'height':48,'has_audio':False}
        with patch.object(app,'probe',return_value=probe), patch.dict(os.environ,{'FFMPEG_BIN':sys.executable}):
            self.call('edit.save',{'clips':[{'id':'S1','src':'素材库/a.mp4','in':0,'out':1,'a':1}]})
            with patch.object(editing.threading,'Thread'):
                created=self.call('export.start')['tasks'][0]
            self.call('tasks.cancel',{'task_id':created['id']})
            native=editing.jobs(app)[0]
            editing.render(app,sys.executable,native)
            state=self.call('tasks.get',{'task_id':created['id']})['result']
            self.assertEqual(state['status'],'cancelled')
            with patch.object(editing.threading,'Thread'):
                retried=self.call('tasks.retry',{'task_id':created['id']})['tasks'][0]
            try:
                self.assertNotEqual(retried['id'],created['id'])
                self.assertEqual(retried['progress']['retry_of'],created['native_id'])
                self.assertEqual(retried['progress']['clips'][0]['out'],1)
            finally:
                editing.ACTIVE.discard(retried['native_id']);editing.CANCEL.pop(retried['native_id'],None)

    def test_http_auth_and_stdio_end_to_end(self):
        ff=os.environ.get('FFMPEG_BIN');fp=os.environ.get('FFPROBE_BIN')
        if not ff or not fp:self.skipTest('需要FFmpeg/FFprobe')
        # 3条独立源文件，通过MCP完成导入、建镜、关联、质检、选片、裁切和导出。
        for i in range(3):
            subprocess.run([ff,'-v','error','-f','lavfi','-i',f"color=c={['blue','red','green'][i]}:s=64x48:r=25:d=1",'-c:v','libx264','-pix_fmt','yuv420p',str(app.IMPORT_ROOTS[0]/f'{i}.mp4')],check=True)
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.H)
        oldport=app.PORT;app.PORT=server.server_port
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        url=f'http://127.0.0.1:{app.PORT}'
        secret=api.token(app)
        proxy=None
        try:
            for headers in ({},{'Authorization':'Bearer incorrect'},{'Authorization':'Bearer '+secret,'Origin':url}):
                req=urllib.request.Request(url+'/api/agent/project',headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req,timeout=3)
                self.assertEqual(error.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(url+'/f/.studio/agent-token',timeout=3)
            proxy=subprocess.Popen([sys.executable,str(pathlib.Path(__file__).resolve().parents[1]/'mcp_server.py'),'--url',url,'--token-file',str(api.private(app)/'agent-token')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            counter=0
            def rpc(method,params):
                nonlocal counter
                counter+=1;proxy.stdin.write(json.dumps({'jsonrpc':'2.0','id':counter,'method':method,'params':params})+'\n');proxy.stdin.flush()
                selector=selectors.DefaultSelector();selector.register(proxy.stdout,selectors.EVENT_READ)
                try:self.assertTrue(selector.select(timeout=20),'MCP响应超时')
                finally:selector.close()
                result=json.loads(proxy.stdout.readline())
                self.assertNotIn('error',result)
                self.assertFalse(result['result'].get('isError'),result['result'])
                return result['result']
            self.assertEqual(rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'acceptance','version':'1'}})['protocolVersion'],'2025-11-25')
            self.assertEqual(len(rpc('tools/list',{})['tools']),2)
            def status():return rpc('tools/call',{'name':'studio_project','arguments':{}})['structuredContent']
            def tool(op,args):
                state=status()
                return rpc('tools/call',{'name':'studio_call','arguments':{'operation':op,'arguments':args,'project_id':state['project_id'],'expected_revision':state['revision'],'idempotency_key':f'mcp-{counter}'}})['structuredContent']
            def wait(task):
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    item=tool('tasks.get',{'task_id':task['id']})['result']
                    if item['status'] not in ('running','queued'):
                        self.assertEqual(item['status'],'succeeded',item);return item
                    time.sleep(.05)
                self.fail('任务未结束')
            clips=[]
            for i in range(3):
                asset=tool('assets.import',{'source':str(app.IMPORT_ROOTS[0]/f'{i}.mp4')})['result']
                sid=f'S{i+1}'
                tool('shots.create',{'segment':sid,'scene':f'镜{i+1}'})
                tool('review.mutate',{'segment':sid,'action':'associate','asset_id':asset['id']})
                tool('review.mutate',{'segment':sid,'action':'select','asset_id':asset['id'],'fingerprint':asset['fingerprint']})
                scan=tool('quality.scan',{'asset_id':asset['id']});wait(scan['tasks'][0])
                clips.append({'id':sid,'asset_id':asset['id'],'in':.1,'out':.7,'a':1})
            tool('edit.save',{'clips':clips})
            export=tool('export.start',{})
            result=wait(export['tasks'][0])
            self.assertTrue(app.safe(result['outputs'][0]).is_file())
            self.assertEqual(len(app.api_timeline().get('V',[])),0)  # 草稿独立，不覆盖旧剪辑表。
            timeline=tool('timeline.get',{'final':result['outputs'][0]})['result']
            self.assertEqual(len(timeline['V']),3)
            self.assertAlmostEqual(timeline['total'],1.8)
            evidence=tool('evidence.extract',{'asset_id':asset['id'],'t':.2})['result']
            self.assertTrue(app.safe(evidence['path']).is_file())
            self.assertEqual(len(app.api_segments()['rows']),3)
            self.assertEqual(len(editing.read(app)['clips']),3)
            self.assertTrue(all(x['cells']['过审']=='待审' for x in app.api_segments()['rows']))
            with urllib.request.urlopen(url+'/api/tasks',timeout=3) as response:
                self.assertEqual(json.load(response)['total'],4)
        finally:
            if proxy:
                proxy.terminate();proxy.communicate(timeout=5)
            server.shutdown();server.server_close();worker.join();app.PORT=oldport

if __name__=='__main__':unittest.main()
