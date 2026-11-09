#!/usr/bin/env python3
"""macOS用户级常驻服务：与终端/智能体会话分离，由launchd管理。"""
import argparse
import hashlib
import json
import os
import pathlib
import plistlib
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE=pathlib.Path(__file__).resolve().parent
MEDIA_ENV=('FFMPEG_BIN','FFPROBE_BIN','REALESRGAN_BIN','REALESRGAN_MODELS','PYTHONPYCACHEPREFIX','STUDIO_ASR_PYTHON','STUDIO_ASR_MODEL','STUDIO_REVIEW_COMMAND','STUDIO_RIFE_BIN','STUDIO_RIFE_MODEL','STUDIO_RIFE_DEVICE','STUDIO_JIANYING_PYTHON','STUDIO_JIANYING_DRAFT_ROOT')


def config(root,port,imports,python=sys.executable,environment=None):
    root=pathlib.Path(root).resolve()
    label='local.lazy-studio.'+hashlib.sha256(str(root).encode()).hexdigest()[:12]
    args=[str(python),str(HERE/'run.py'),'--root',str(root),'--port',str(port)]
    for source in imports:args+=['--import-root',str(pathlib.Path(source).resolve())]
    logs=root/'.studio'/'logs'
    env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONUNBUFFERED':'1'}
    env.update({key:value for key,value in (environment or {}).items() if key in MEDIA_ENV})
    return {'Label':label,'ProgramArguments':args,'WorkingDirectory':str(HERE),'EnvironmentVariables':env,
            'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
            'StandardOutPath':str(logs/'service.out.log'),'StandardErrorPath':str(logs/'service.err.log')}


def probe(root,port):
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/api/project',timeout=2) as response:data=json.load(response)
        if pathlib.Path(data['root']).resolve()!=pathlib.Path(root).resolve():
            raise ValueError('端口已绑定其他项目')
        return data
    except (OSError,ValueError) as e:
        if isinstance(e,ValueError) and str(e)=='端口已绑定其他项目':raise
        return None


def launch(*args,check=True):
    return subprocess.run(['/bin/launchctl',*args],capture_output=True,text=True,check=check)


def install(root,port,imports):
    settings=config(root,port,imports,environment=os.environ)
    directory=pathlib.Path.home()/'Library'/'LaunchAgents'
    directory.mkdir(parents=True,exist_ok=True)
    file=directory/(settings['Label']+'.plist')
    target=f'gui/{os.getuid()}/'+settings['Label']
    if file.exists():
        previous=plistlib.loads(file.read_bytes())
        if previous.get('Label')!=settings['Label'] or str(HERE/'run.py') not in previous.get('ProgramArguments',[]):
            raise ValueError('已有同名非本工具服务，拒绝覆盖')
    if probe(root,port) and launch('print',target,check=False).returncode:
        raise ValueError('此项目已有手动启动的服务，请先确认任务空闲并停止它，再安装常驻服务')
    private=pathlib.Path(root).resolve()/'.studio'
    if private.is_symlink() or (private/'logs').is_symlink():raise ValueError('日志目录不能是符号链接')
    pathlib.Path(settings['StandardOutPath']).parent.mkdir(parents=True,exist_ok=True)
    # 重复安装前不终止活跃任务；运行中服务需先停止并确认空闲。
    if launch('print',target,check=False).returncode==0:
        raise ValueError('常驻服务已加载；使用status查看。修改配置前请先确认任务空闲并stop')
    fd,tmp=tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd,'wb') as stream:plistlib.dump(settings,stream)
        os.chmod(tmp,0o600);os.replace(tmp,file)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    launch('bootstrap',f'gui/{os.getuid()}',str(file))
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        data=probe(root,port)
        if data:return {'status':'running','version':data['version'],'url':f'http://127.0.0.1:{port}','plist':str(file),'logs':str(private/'logs')}
        time.sleep(.25)
    raise RuntimeError('服务未就绪，请检查 '+str(private/'logs/service.err.log'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('install','status','start','stop'))
    p.add_argument('--root',required=True);p.add_argument('--port',type=int,default=8790)
    p.add_argument('--import-root',action='append',default=[])
    a=p.parse_args()
    if sys.platform!='darwin':p.error('常驻服务管理仅支持macOS；其他系统仍可运行run.py')
    settings=config(a.root,a.port,a.import_root)
    target=f'gui/{os.getuid()}/'+settings['Label']
    file=pathlib.Path.home()/'Library'/'LaunchAgents'/(settings['Label']+'.plist')
    if a.action=='install':print(json.dumps(install(a.root,a.port,a.import_root),ensure_ascii=False));return
    if a.action=='status':
        data=probe(a.root,a.port)
        print(json.dumps({'loaded':launch('print',target,check=False).returncode==0,'running':bool(data),'version':data.get('version') if data else None,'plist':str(file)},ensure_ascii=False));return
    if a.action=='stop':
        for endpoint in ('exports','upscale','quality-reports','tasks'):
            if not probe(a.root,a.port):break
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f'http://127.0.0.1:{a.port}/api/{endpoint}',timeout=5) as response:info=json.load(response)
            if any(j['status'] in ('queued','running') for j in info.get('jobs',info.get('items',[]))):raise ValueError('有任务未结束，先等待或取消任务')
        launch('bootout',target);print('服务已停止；再次启动用start');return
    if not file.is_file():raise ValueError('尚未安装，请先install')
    launch('bootstrap',f'gui/{os.getuid()}',str(file));print('已请求启动；用status检查状态')


if __name__=='__main__':
    try:main()
    except Exception as e:sys.exit(str(e))
