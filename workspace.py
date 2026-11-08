"""正在剪的版本：保存草稿优先；已确认成片或旧剪辑表可显式选择。"""
import hashlib
import json
import copy
import pathlib
import editing
import quality


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def source(app,kind='current',final=None):
    draft=editing.read(app)
    if kind=='current':kind='draft' if draft['clips'] else 'final' if final and editing.manifest(app,final) else 'legacy'
    film=None;render_context={}
    if kind=='draft':
        if not draft['clips']:raise ValueError('还没有保存的剪辑，请先在初剪编辑保存，或选择重新初剪')
        clips=editing.validate(app,draft['clips']);label='正在剪的草稿'
    elif kind=='final':
        if final not in {app.rel(p) for p,_,_ in app.final_videos()}:raise ValueError('所选成片不存在')
        manifest=editing.manifest(app,final)
        if not manifest:raise ValueError('这版成片尚无可靠镜头对应，请先使用已保存的草稿检查')
        film=final;tl=manifest['timeline'];clips=[];render_context={'film_hash':manifest['film_hash'],**{k:tl.get(k) for k in ('look','A','subs')}}
        for c in tl['V']:
            clips.append({k:c[k] for k in ('id','src','in','out','a')})
            clips[-1]['fingerprint']=manifest['sources'].get(c['src'])
        label='已导出版本：'+pathlib.Path(final).name
    elif kind=='legacy':
        tl=app.api_timeline()
        if not tl.get('found') or not tl.get('V'):raise ValueError('还没有当前剪辑，请先保存初剪，或选择从候选重新初剪')
        if tl.get('A') or tl.get('subs') or tl.get('look') not in (None,'none') or any(c.get('grade') or c.get('freeze') or c.get('fi') or c.get('fo') for c in tl['V']):
            raise ValueError('现有剪辑含额外声音/字幕/特效，请先导入可确认的成片版本，避免检查时漏掉效果')
        clips=editing.validate(app,tl['V']);label='当前剪辑（来自已有剪辑表）'
    else:raise ValueError('剪辑来源无效')
    if not film:
        first=app.probe(app.safe(clips[0]['src'])) if clips else {}
        render_context={'width':first.get('width'),'height':first.get('height')}
    timeline=editing.timeline(clips)
    signature=digest({'kind':kind,'clips':clips,'film':film,'film_hash':app.file_hash(app.safe(film)) if film else None})
    return {'kind':kind,'label':label,'clips':clips,'timeline':timeline,'film':film,'film_hash':app.file_hash(app.safe(film)) if film else None,
            'draft_revision':draft['revision'],'signature':signature,'render_context':render_context}


def row_metadata(app,sid,expected):
    row=next((r for r in app.api_segments().get('rows',[]) if sid in (r['id'],r['scene'])),None)
    sid=row['id'] if row else str(sid)
    data=quality.read(app);record=data['shots'].get(sid,{});refs=[]
    for ref in record.get('references',[]):
        a=next((x for x in data['assets'] if x['id']==ref['id']),None)
        v=next((x for x in a['versions'] if x['version']==ref['version']),None) if a else None
        if not v or app.file_hash(app.safe(v['image']))!=v['fingerprint']:raise ValueError('参考图片已变化，请先复核')
        refs.append(v)
    return {'id':sid,'scene':row['scene'] if row else sid,'text':row['text'] if row else '',
            'target_duration':row['dur'] if row else '', 'requirements':record.get('requirements',''),'references':refs,'expected_dialogue':expected.get(sid,'')}


def inspection(app,kind,final,expected):
    plan=source(app,kind,final)
    entries=[]
    for i,c in enumerate(plan['clips']):
        meta=row_metadata(app,str(c['id']),expected)
        # 原声增益、源区间或要求变化都使该镜需复查；未选候选变化不影响当前剪辑。
        entries.append({'index':i,'segment':meta['id'],'clip':c,'metadata':meta,'key':digest({'clip':c,'metadata':meta,'render_context':plan['render_context']})})
    signature=digest({'source':plan['signature'],'entries':entries})
    return plan,entries,signature


def overview(app,final=None,kind='current'):
    rows=app.api_segments().get('rows',[])
    try:plan=source(app,kind,final);error=''
    except ValueError as e:plan=None;error=str(e)
    result=[];used=set()
    for row in rows:
        uses=[{'src':c['src'],'in':c['in'],'out':c['out'],'position':i+1} for i,c in enumerate(plan['clips']) if str(c['id']) in (row['id'],row['scene'])] if plan else []
        used.update(u['position'] for u in uses)
        result.append({'id':row['id'],'scene':row['scene'],'uses':uses,'candidate_count':len(row['files']['candidates']),'spare_count':sum(c['path'] not in {u['src'] for u in uses} for c in row['files']['candidates'])})
    if plan:
        for i,c in enumerate(plan['clips']):
            if i+1 not in used:result.append({'id':str(c['id']),'scene':'当前剪辑中的镜头','uses':[{'src':c['src'],'in':c['in'],'out':c['out'],'position':i+1}],'candidate_count':0,'spare_count':0})
    alternatives=[]
    if editing.read(app)['clips']:alternatives.append({'kind':'draft','label':'正在剪的草稿','final':None})
    for p,_,_ in app.final_videos():
        if editing.manifest(app,app.rel(p)):alternatives.append({'kind':'final','label':p.name,'final':app.rel(p)})
    return {'source':{k:v for k,v in plan.items() if k not in ('clips','timeline')} if plan else None,'error':error,'shots':result,'alternatives':alternatives}


def reusable(app,packet,key):
    if packet.get('logical_key')!=key or not (packet.get('review') or {}).get('eligible'):return False
    import review_evidence
    try:review_evidence.verify(app,packet)
    except (ValueError,OSError):return False
    return True


def copy_review(packet,run_id):
    p=copy.deepcopy(packet);p['reused_from']=run_id;p['lease']=None
    return p
