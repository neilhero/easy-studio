#!/usr/bin/env python3
"""轻量MCP stdio客户端代理；执行器始终是正在运行的Studio服务。"""
import argparse
import base64
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOLS=('2025-11-25','2025-06-18')
DESCRIPTION='Lazy Studio本机工具。先studio_project获取project_id、revision和operations。studio_call复用网页业务。所有写操作须提交project_id、expected_revision、稳定idempotency_key；重放必须保留原请求。返回needs_reconciliation时先查询任务，禁止自动重提。任务成功不等于内容过审。'


def request(url,token,path,body=None):
    headers={'Authorization':'Bearer '+token}
    if body is not None:headers['Content-Type']='application/json'
    req=urllib.request.Request(url+path,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers=headers)
    # 不使用系统代理转发本机令牌。
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req,timeout=120) as response:return json.load(response)
    except urllib.error.HTTPError as e:
        raise ValueError(e.read().decode(errors='replace')) from None


def tools():
    return [
      {'name':'studio_project','description':DESCRIPTION,'inputSchema':{'type':'object','properties':{},'additionalProperties':False},'annotations':{'readOnlyHint':True}},
      {'name':'studio_call','description':DESCRIPTION+' operation名称及arguments见studio_project.operations和docs/AGENT-API.md。读操作不需要写入字段。',
       'inputSchema':{'type':'object','required':['operation'],'properties':{
         'operation':{'type':'string'},'arguments':{'type':'object','description':'具体操作参数，例如edit.save的clips或tasks.get的task_id'},
         'project_id':{'type':'string'},'expected_revision':{'type':'string'},'idempotency_key':{'type':'string'}},'additionalProperties':False}}
    ]


def handle(message,url,token,initialized):
    method=message.get('method');params=message.get('params',{})
    if not isinstance(params,dict):raise ValueError('params须为对象')
    if method=='initialize':
        preferred=params.get('protocolVersion')
        protocol=preferred if preferred in PROTOCOLS else PROTOCOLS[0]
        return {'protocolVersion':protocol,'capabilities':{'tools':{}},'serverInfo':{'name':'lazy-studio','version':pathlib.Path(__file__).with_name('VERSION').read_text().strip()},'instructions':DESCRIPTION},True
    if method=='ping':return {},initialized
    if not initialized:raise ValueError('请先initialize；支持2025-11-25/2025-06-18协议')
    if method=='tools/list':return {'tools':tools()},initialized
    if method=='tools/call':
        args=params.get('arguments',{})
        if not isinstance(args,dict):raise ValueError('arguments须为对象')
        try:
            if params.get('name')=='studio_project':result=request(url,token,'/api/agent/project')
            elif params.get('name')=='studio_call':result=request(url,token,'/api/agent/call',args)
            else:raise ValueError('工具不存在')
            content=[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]
            if params.get('name')=='studio_call' and args.get('operation')=='evidence.extract' and result.get('result',{}).get('path'):
                path=result['result']['path']
                req=urllib.request.Request(url+'/f/'+urllib.parse.quote(path),headers={'Authorization':'Bearer '+token})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=30) as response:data=response.read(4000001)
                if len(data)<=4000000:content.append({'type':'image','data':base64.b64encode(data).decode(),'mimeType':'image/png'})
            return {'content':content,'structuredContent':result,'isError':False},initialized
        except Exception as e:
            return {'content':[{'type':'text','text':str(e)}],'isError':True},initialized
    raise LookupError('方法不支持')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8790')
    parser.add_argument('--token-file',required=True)
    args=parser.parse_args()
    url=urllib.parse.urlsplit(args.url)
    if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost') or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
        parser.error('只允许本机HTTP根地址')
    secret=pathlib.Path(args.token_file).read_text().strip()
    initialized=False
    for line in sys.stdin:
        message=None
        try:
            message=json.loads(line)
            if not isinstance(message,dict) or message.get('jsonrpc')!='2.0' or not isinstance(message.get('method'),str):raise ValueError('无效JSON-RPC消息')
            if 'id' not in message:continue
            result,initialized=handle(message,args.url.rstrip('/'),secret,initialized)
            response={'jsonrpc':'2.0','id':message['id'],'result':result}
        except Exception as e:
            code=-32601 if isinstance(e,LookupError) else -32700 if isinstance(e,json.JSONDecodeError) else -32602
            response={'jsonrpc':'2.0','id':message.get('id') if isinstance(message,dict) else None,'error':{'code':code,'message':str(e)}}
        sys.stdout.write(json.dumps(response,ensure_ascii=False)+'\n');sys.stdout.flush()


if __name__=='__main__':main()
