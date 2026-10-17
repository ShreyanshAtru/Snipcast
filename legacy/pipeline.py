#!/usr/bin/env python3
"""Single-operator resumable video -> Postiz pipeline. Python 3.11+, FFmpeg."""
import argparse, contextlib, datetime as dt, fcntl, hashlib, http.client, json, math, os, pathlib, subprocess, sys, uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
UTC=dt.timezone.utc

def save(path, data):
    tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(data,indent=2)); tmp.replace(path)

def probe(path):
    return json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)]))

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1048576),b''): h.update(b)
    return h.hexdigest()

def slots(start,times,zone,count):
    z=ZoneInfo(zone); day=dt.date.fromisoformat(start); out=[]
    times=sorted(set(dt.time.fromisoformat(t) for t in times))
    if not times: raise ValueError('At least one posting time required')
    while len(out)<count:
        for t in times:
            local=dt.datetime.combine(day,t,z)
            # Reject ambiguous and nonexistent wall times rather than silently shifting.
            if local.fold==0 and local.utcoffset()!=local.replace(fold=1).utcoffset():
                raise ValueError('Ambiguous/nonexistent DST slot; choose another time')
            utc=local.astimezone(UTC)
            if utc.astimezone(z).replace(tzinfo=None)!=local.replace(tzinfo=None):
                raise ValueError('Nonexistent local time')
            out.append(utc.isoformat().replace('+00:00','Z'))
            if len(out)==count: break
        day+=dt.timedelta(days=1)
    return out

class Postiz:
    def __init__(self):
        self.url=urlsplit(os.environ['POSTIZ_BASE_URL'].rstrip('/'))
        self.key=os.environ['POSTIZ_API_KEY']
        if self.url.scheme not in ('https','http') or not self.url.hostname or self.url.username or self.url.query:
            raise ValueError('Invalid Postiz base URL')
        if self.url.scheme=='http' and self.url.hostname not in ('localhost','127.0.0.1','host.docker.internal'):
            raise ValueError('Use HTTPS for remote Postiz credentials')
    def call(self,method,route,data=None,file=None):
        cls=http.client.HTTPSConnection if self.url.scheme=='https' else http.client.HTTPConnection
        conn=cls(self.url.hostname,self.url.port,timeout=300)
        headers={'Authorization':self.key}
        if file:
            boundary=uuid.uuid4().hex
            prefix=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="clip.mp4"\r\nContent-Type: video/mp4\r\n\r\n').encode()
            suffix=f'\r\n--{boundary}--\r\n'.encode()
            headers.update({'Content-Type':f'multipart/form-data; boundary={boundary}','Content-Length':str(len(prefix)+file.stat().st_size+len(suffix))})
        else:
            body=json.dumps(data).encode() if data is not None else None
            headers['Content-Type']='application/json'
        try:
            path=self.url.path.rstrip('/')+route
            if file:
                conn.putrequest(method,path)
                for k,v in headers.items(): conn.putheader(k,v)
                conn.endheaders(); conn.send(prefix)
                with file.open('rb') as f:
                    for chunk in iter(lambda:f.read(1048576),b''): conn.send(chunk)
                conn.send(suffix)
            else: conn.request(method,path,body,headers)
            response=conn.getresponse(); raw=response.read()
            if not 200<=response.status<300: raise RuntimeError(f'Postiz HTTP {response.status}; inspect server logs. No automatic retry.')
            return json.loads(raw)
        finally: conn.close()

def payload(item,media):
    return {'type':'schedule','date':item['date'],'shortLink':False,'tags':[], 'posts':[{'integration':{'id':item['integration_id']},'value':[{'content':item['description'],'image':[{'id':media['id'],'path':media['path']}]}], 'settings':{'__type':'youtube','title':item['title'],'type':item['visibility'],'selfDeclaredMadeForKids':item['made_for_kids'],'tags':[{'value':t,'label':t} for t in item['tags']]}}]}

def prepare(config,root):
    source=pathlib.Path(config['input']).expanduser().resolve()
    seconds=float(config.get('clip_seconds',59))
    if not 0<seconds<=60: raise ValueError('clip_seconds must be between 0 and 60')
    info=probe(source); duration=float(info['format']['duration'])
    if not math.isfinite(duration) or duration<=0: raise ValueError('Invalid source duration')
    if not any(s['codec_type']=='video' for s in info['streams']): raise ValueError('Source has no video')
    count=math.ceil(duration/seconds)
    fingerprint=hashlib.sha256((digest(source)+json.dumps(config,sort_keys=True)).encode()).hexdigest()
    batch=root/fingerprint[:20]; batch.mkdir(parents=True,exist_ok=True); manifest=batch/'manifest.json'
    if manifest.exists(): return manifest
    clips=[{'index':i+1,'start':i*seconds,'duration':min(seconds,duration-i*seconds),'path':f'clips/clip_{i+1:04d}.mp4','state':'pending'} for i in range(count)]
    posts=[]; seen=set()
    for ch in config['channels']:
        cid=ch['integration_id']
        if cid in seen: raise ValueError('Duplicate channel')
        seen.add(cid)
        if ch.get('visibility') not in ('private','unlisted','public'): raise ValueError('Choose explicit visibility')
        if ch.get('made_for_kids') not in ('yes','no'): raise ValueError('Choose made_for_kids explicitly')
        dates=slots(ch['start_date'],ch['times'],ch.get('timezone','Asia/Kolkata'),count)
        for i,date in enumerate(dates):
            title=ch['title'].format(video_name=source.stem,part=i+1)
            description=ch.get('description','')+'\n\n'+' '.join(ch.get('hashtags',[]))
            if not 2<=len(title)<=100 or len(description)>5000: raise ValueError('YouTube title/description length invalid')
            posts.append(dict(clip=i,integration_id=cid,date=date,title=title,description=description.strip(),visibility=ch['visibility'],made_for_kids=ch['made_for_kids'],tags=ch.get('tags',[]),state='pending'))
    if not posts: raise ValueError('Select at least one channel')
    save(manifest,dict(source=str(source),fingerprint=fingerprint,clips=clips,posts=posts))
    return manifest

def process(path):
    m=json.loads(path.read_text()); (path.parent/'clips').mkdir(exist_ok=True)
    for c in m['clips']:
        target=path.parent/c['path']
        if c['state']=='ready' and target.exists() and digest(target)==c['sha256']: continue
        tmp=target.with_suffix('.partial.mp4')
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(c['start']),'-i',m['source'],'-t',str(c['duration']),'-map','0:v:0','-map','0:a:0?','-vf','scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30','-c:v','libx264','-threads','2','-preset','fast','-crf','23','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-movflags','+faststart',str(tmp)],check=True,timeout=7200)
        p=probe(tmp); v=next(s for s in p['streams'] if s['codec_type']=='video')
        if (v['width'],v['height'])!=(1080,1920) or abs(float(p['format']['duration'])-c['duration'])>0.2: raise ValueError('Output validation failed')
        tmp.replace(target); c.update(state='ready',sha256=digest(target)); save(path,m)
        print(f"Clip {c['index']} ready",flush=True)

def schedule(path):
    m=json.loads(path.read_text()); client=Postiz()
    # Entire batch preflight before any writes.
    for p in m['posts']:
        if p['state']=='scheduled': continue
        if p['state'] in ('submitting','unknown'): raise ValueError('Uncertain prior submission: reconcile in Postiz before proceeding')
        if dt.datetime.fromisoformat(p['date'].replace('Z','+00:00'))<=dt.datetime.now(UTC)+dt.timedelta(minutes=5): raise ValueError('Posting slot is past or less than five minutes away; review plan')
        c=m['clips'][p['clip']]; f=path.parent/c['path']
        if c['state']!='ready' or not f.exists() or digest(f)!=c['sha256']: raise ValueError('Process and validate all clips first')
    for p in m['posts']:
        if p['state']=='scheduled': continue
        if not p.get('media'):
            p['media']=client.call('POST','/upload',file=path.parent/m['clips'][p['clip']]['path']); save(path,m)
        body=payload(p,p['media']); p['state']='submitting'; save(path,m)
        try:
            result=client.call('POST','/posts',data=body)
            # Keep raw receipt for manual reconciliation across Postiz versions.
            p.update(state='scheduled',receipt=result); save(path,m)
        except Exception:
            p['state']='unknown'; save(path,m); raise
        print(f"Scheduled clip {p['clip']+1} to {p['integration_id']} at {p['date']}",flush=True)

@contextlib.contextmanager
def lock(root):
    root.mkdir(parents=True,exist_ok=True)
    with (root/'.lock').open('w') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['plan','process','schedule','run','channels'])
    parser.add_argument('--config',default='config/batch.json'); parser.add_argument('--state',default='data')
    parser.add_argument('--manifest'); parser.add_argument('--publish',action='store_true')
    a=parser.parse_args(); root=pathlib.Path(a.state).resolve()
    if a.command=='channels': print(json.dumps(Postiz().call('GET','/integrations'),indent=2)); return
    with lock(root):
        path=pathlib.Path(a.manifest).resolve() if a.manifest else prepare(json.loads(pathlib.Path(a.config).read_text()),root)
        print('Manifest:',path)
        if a.command=='plan': print(path.read_text())
        if a.command in ('process','run'): process(path)
        if a.command=='schedule' or (a.command=='run' and a.publish):
            if not a.publish: raise ValueError('Add --publish to explicitly schedule external posts')
            schedule(path)
if __name__=='__main__':
    try: main()
    except Exception as e: print(f'{type(e).__name__}: {e}',file=sys.stderr); sys.exit(1)
