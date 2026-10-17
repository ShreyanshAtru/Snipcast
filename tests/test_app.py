import os,tempfile,subprocess,json,datetime as dt
from pathlib import Path
ROOT=Path(tempfile.mkdtemp(prefix='clipdesk-tests-'))
os.environ.update(APP_DATA=str(ROOT/'data'),MEDIA_ROOT=str(ROOT),POSTIZ_MODE='demo',APP_PASSWORD='test-password',SESSION_SECRET='test-session-secret')
import pytest
from fastapi.testclient import TestClient
from app import db,service,media,tasks
from app.models import Batch,Channel
from app.main import app
from app.postiz import Postiz,PostizError

@pytest.fixture(scope='module')
def source():
    p=ROOT/'sample.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=30','-f','lavfi','-i','sine=frequency=440','-t','2.3','-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac',str(p)],check=True)
    return p

def spec(source,folder,**kw):
    return Batch(source=str(source),destination=str(ROOT),folder=folder,clip_seconds=1,start_date=(dt.date.today()+dt.timedelta(days=3)).isoformat(),timezone='UTC',**kw)

def test_real_clipping_and_dedup(source):
    b=spec(source,'real-clips'); jid=service.create(b)
    tasks.run_job.run(jid); d=db.details(jid)
    assert d['state']=='completed',d['error']
    assert len(d['clips'])==3
    assert service.create(b)==jid
    for c in d['clips']:
        p=Path(c['path']); assert p.exists() and p.parent.name==b.start_date
        v=media.probe(p); assert (v['width'],v['height'])==(1080,1920)
        assert abs(v['duration']-c['duration'])<.25 and v['audio']
    before=[c['checksum'] for c in d['clips']]
    tasks.run_job.run(jid)
    assert before==[c['checksum'] for c in db.details(jid)['clips']]

def test_two_channel_schedule_and_slots(source):
    b=spec(source,'scheduled',schedule=True,channels=[Channel(id=c,times=['12:00']) for c in ['demo-youtube','demo-second']])
    jid=service.create(b); tasks.run_job.run(jid); d=db.details(jid)
    assert d['state']=='completed',d['error']
    assert len(d['publications'])==6
    assert all(p['post_id'] and p['state']=='scheduled' for p in d['publications'])
    another=service.prepare(spec(source,'scheduled-next',schedule=True,channels=b.channels))
    old={(p['channel'],p['scheduled']) for p in d['publications']}
    assert not old.intersection((p['channel'],p['scheduled']) for p in another['publications'])
    tasks.reconcile(jid)
    assert all(p['state']=='scheduled' for p in db.details(jid)['publications'])

def test_uncertain_submission_never_blindly_retried(source,monkeypatch):
    b=spec(source,'uncertain',schedule=True,channels=[Channel(id='demo-youtube',times=['17:00'])])
    jid=service.create(b); calls=[]
    def fail(self,payload): calls.append(payload); raise PostizError('network timeout')
    monkeypatch.setattr(Postiz,'create',fail)
    tasks.run_job.run(jid)
    assert db.job(jid)['state']=='failed'
    assert db.details(jid)['publications'][0]['state']=='unknown'
    db.update(jid,state='queued'); tasks.run_job.run(jid)
    assert len(calls)==1

def test_auth_paths_and_cross_site():
    with TestClient(app) as c:
        assert c.get('/api/jobs').status_code==401
        assert c.post('/api/login',json={'password':'test-password'}).status_code==200
        assert c.get('/api/jobs').status_code==200
        assert c.get('/api/files',params={'path':'/etc'}).status_code==400
        assert c.post('/api/logout',json={},headers={'origin':'https://evil.example'}).status_code==403
        assert c.get('/api/jobs',headers={'host':'evil.example'}).status_code==403

def test_paused_batch_does_not_process(source):
    jid=service.create(spec(source,'paused')); db.update(jid,state='paused',requested='pause')
    tasks.run_job.run(jid)
    assert not Path(db.job(jid)['output']).exists()

def test_changed_source_refused(source):
    copy=ROOT/'changed.mp4'; copy.write_bytes(source.read_bytes())
    jid=service.create(spec(copy,'changed-output')); copy.write_bytes(copy.read_bytes()+b'changed')
    tasks.run_job.run(jid)
    assert 'changed' in db.job(jid)['error']

def test_dst_and_invalid_template(source):
    slots=list(service.candidate_slots('2026-11-01',['01:30'],'America/New_York'))
    assert not slots[0].startswith('2026-11-01')
    with pytest.raises(ValueError,match='Title'):
        service.prepare(spec(source,'bad-template',schedule=True,title='{invalid}',channels=[Channel(id='demo-youtube',times=['20:00'])]))

def test_foreign_output_is_preserved(source):
    jid=service.create(spec(source,'foreign')); out=Path(db.job(jid)['output']); out.mkdir(); marker=out/'keep.txt'; marker.write_text('keep')
    tasks.run_job.run(jid)
    assert db.job(jid)['state']=='failed' and marker.read_text()=='keep'

def test_preview_change_requires_review(source):
    b=spec(source,'reviewed',schedule=True,channels=[Channel(id='demo-youtube',times=['22:00'])])
    plan=service.prepare(b)
    service.create(spec(source,'other-booking',schedule=True,channels=b.channels))
    with pytest.raises(ValueError,match='schedule changed'):
        service.create(b.model_copy(update={'preview_token':plan['preview_token']}))

def test_fit_without_audio_and_pause(source):
    silent=ROOT/'silent.mp4'
    subprocess.run(['ffmpeg','-v','error','-i',str(source),'-an','-c:v','copy',str(silent)],check=True)
    target=ROOT/'fit.mp4'
    media.encode(silent,target,0,.5,'fit',lambda:None,lambda p:None)
    assert not media.probe(target)['audio']
    def stop(): raise media.Interrupted('pause')
    with pytest.raises(media.Interrupted): media.encode(silent,ROOT/'interrupted.mp4',0,2,'crop',stop,lambda p:None)
    assert not (ROOT/'interrupted.mp4').exists()
    assert not (ROOT/'interrupted.partial.mp4').exists()

def test_explicit_resolution_and_resume_guard(source,monkeypatch):
    b=spec(source,'api-guard',schedule=True,channels=[Channel(id='demo-youtube',times=['23:00'])])
    jid=service.create(b)
    with db.connect() as c:
        c.execute("UPDATE publications SET state='unknown' WHERE job_id=?",(jid,))
    db.update(jid,state='failed')
    with TestClient(app) as c:
        c.post('/api/login',json={'password':'test-password'})
        assert c.post('/api/jobs/'+jid+'/resume',json={}).status_code==400
        assert c.post('/api/jobs',json=b.model_dump()).status_code==400
        assert c.post('/api/jobs/'+jid+'/cancel',json={}).status_code==200
        assert db.job(jid)['state']=='cancelled'
