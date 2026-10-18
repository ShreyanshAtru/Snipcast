import json, os, sys, time
from pathlib import Path
from celery import Celery
from . import db, settings, media
from .postiz import Postiz, PostizError, payload, receipt_id
from .service import export_manifest, parse
import datetime as dt

if sys.platform == 'win32':
    import msvcrt
    def _lock_file(f):
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except (OSError, IOError):
            return False

    def _unlock_file(f):
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except (OSError, IOError):
            pass
else:
    import fcntl
    def _lock_file(f):
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False

    def _unlock_file(f):
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except (OSError, IOError):
            pass

celery=Celery('clipdesk',broker=settings.REDIS_URL)
celery.conf.update(task_serializer='json',accept_content=['json'],task_acks_late=True,
    task_reject_on_worker_lost=True,worker_prefetch_multiplier=1,broker_connection_retry_on_startup=True,
    broker_transport_options={'visibility_timeout':86400},task_ignore_result=True,
    beat_schedule={'dispatch-saved-jobs':{'task':'app.tasks.dispatch','schedule':10.0}})

def has_lock(jid):
    db.job(jid)
    lock_file = settings.DATA / (jid + '.lock')
    with lock_file.open('a+') as f:
        f.seek(0)
        if _lock_file(f):
            _unlock_file(f)
            return False
        return True

def checkpoint(jid):
    r=db.job(jid)
    if r['requested']: raise media.Interrupted(r['requested'])

def retry_safe(jid,fn):
    for attempt in range(3):
        checkpoint(jid)
        try: return fn()
        except PostizError as e:
            if e.status not in (None,429,500,502,503,504) or attempt==2: raise
            db.event(jid,'Temporary connection failure; retrying read/upload only')
            for _ in range(2**attempt*2): checkpoint(jid); time.sleep(1)

@celery.task(name='app.tasks.dispatch')
def dispatch():
    # Database acts as a durable outbox. A failed broker submission leaves jobs queued.
    with db.connect() as c: ids=[r['id'] for r in c.execute("SELECT id FROM jobs WHERE state='queued'")]
    for jid in ids: run_job.delay(jid)

@celery.task(name='app.tasks.run_job')
def run_job(jid):
    lock_file = settings.DATA / (jid + '.lock')
    with lock_file.open('a+') as lock:
        lock.seek(0)
        if not _lock_file(lock):
            return
        try:
            job=db.job(jid)
            if job['state'] not in ('queued','running'): return
            if job['mode']!=settings.MODE: raise ValueError('Batch mode differs from server mode; never replay demo batches live')
            config=json.loads(job['config']); source=media.local_path(config['source'])
            if [source.stat().st_size,source.stat().st_mtime_ns]!=config['source_stat']: raise ValueError('Source file changed since planning')
            checkpoint(jid)
            output=media.local_path(job['output'],False); 
            if output.exists():
                marker=output/'.clipdesk-job'
                if not marker.is_file() or marker.read_text()!=jid: raise ValueError('Output folder is not owned by this batch')
            else:
                output.mkdir(); (output/'.clipdesk-job').write_text(jid)
            db.update(jid,state='running',error='',stage='Clipping')
            data=db.details(jid); total=len(data['clips'])
            for i,clip in enumerate(data['clips']):
                checkpoint(jid); path=media.local_path(clip['path'],False)
                if clip['state']=='ready' and path.exists() and media.checksum(path)==clip['checksum']: continue
                path.parent.mkdir(parents=True, exist_ok=True)
                digest=media.encode(source,path,clip['start'],clip['duration'],config['framing'],lambda:checkpoint(jid),lambda p:db.update(jid,progress=(i+p)/total*.7,stage=f'Clipping {i+1} of {total}'))
                with db.connect() as c: c.execute("UPDATE clips SET state='ready',checksum=? WHERE id=?",(digest,clip['id']))
                db.event(jid,f'Clip {clip["part"]} ready'); export_manifest(jid)
            db.update(jid,progress=.7,stage='Checking publication slots')
            client=Postiz(check=lambda:checkpoint(jid)); pubs=db.details(jid)['publications']
            if pubs:
                valid={r['id'] for r in retry_safe(jid,client.channels)}
                if not {p['channel'] for p in pubs}<=valid: raise ValueError('A selected channel is disconnected')
                remote=retry_safe(jid,lambda:client.posts(min(p['scheduled'] for p in pubs),max(p['scheduled'] for p in pubs)))
                known={p['post_id'] for p in pubs if p['post_id']}
                occupied={(r.get('integration',{}).get('id'),r.get('publishDate')) for r in remote if r.get('state') in ('QUEUE','PUBLISHED') and r.get('id') not in known and r.get('publishDate')}
                for pub in pubs:
                    if pub['state'] in ('scheduled','published','publish_failed','cancelled'): continue
                    if pub['state'] in ('submitting','unknown'): raise ValueError('Uncertain submission needs reconciliation before retry')
                    if parse(pub['scheduled'])<=dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=5): raise ValueError('A reserved posting slot is too close or past. Cancel pending work and create a new schedule.')
                    if any(ch==pub['channel'] and parse(t)==parse(pub['scheduled']) for ch,t in occupied): raise ValueError('A posting slot was taken directly in Postiz; resolve calendar conflict')
            for n,pub in enumerate(pubs):
                checkpoint(jid)
                if pub['state'] in ('scheduled','published','publish_failed','cancelled'): continue
                with db.connect() as c: clip=dict(c.execute('SELECT * FROM clips WHERE id=?',(pub['clip_id'],)).fetchone())
                db.update(jid,stage=f'Uploading and scheduling {n+1} of {len(pubs)}',progress=.7+.3*n/len(pubs))
                if not clip['media']:
                    reference=retry_safe(jid,lambda:client.upload(clip['path']))
                    with db.connect() as c: c.execute('UPDATE clips SET media=? WHERE id=?',(json.dumps(reference),clip['id']))
                else: reference=json.loads(clip['media'])
                checkpoint(jid)
                with db.connect() as c: c.execute("UPDATE publications SET state='submitting' WHERE id=?",(pub['id'],))
                try:
                    receipt=client.create(payload(pub,reference)); pid=receipt_id(receipt)
                    if not pid: raise PostizError('Postiz receipt missing post ID; reconcile before retry')
                    with db.connect() as c: c.execute("UPDATE publications SET state='scheduled',post_id=?,receipt=? WHERE id=?",(pid,json.dumps(receipt),pub['id']))
                except Exception:
                    with db.connect() as c: c.execute("UPDATE publications SET state='unknown',error='Submission result uncertain; do not retry blindly' WHERE id=?",(pub['id'],))
                    raise
                db.event(jid,f'Scheduled part {clip["part"]} to {pub["channel"]}'); export_manifest(jid)
            db.update(jid,state='completed',progress=1,stage='Scheduled in Postiz' if pubs else 'Clips ready')
            db.event(jid,'Batch completed. Scheduled does not mean published.')
        except media.Interrupted as e:
            action=str(e); db.update(jid,state='cancelled' if action=='cancel' else 'paused',stage='Cancelled pending work' if action=='cancel' else 'Paused')
            if action=='cancel':
                with db.connect() as c: c.execute("UPDATE publications SET state='cancelled' WHERE job_id=? AND state='planned'",(jid,))
            db.event(jid,'Stopped local work; already scheduled posts remain in Postiz.')
        except Exception as e:
            db.update(jid,state='failed',error=str(e),stage='Needs attention'); db.event(jid,str(e))
        finally:
            export_manifest(jid)
            _unlock_file(lock)

def reconcile(jid):
    job=db.details(jid)
    if job['mode']!=settings.MODE: raise ValueError('Cannot reconcile a batch from another mode')
    pubs=job['publications']
    if not pubs: return
    remote=Postiz().posts(min(p['scheduled'] for p in pubs),max(p['scheduled'] for p in pubs))
    for p in pubs:
        matches=[r for r in remote if r.get('id')==p['post_id']] if p['post_id'] else []
        # Unknown submissions are deliberately not inferred from text/time alone.
        if len(matches)!=1: continue
        r=matches[0]; state={'PUBLISHED':'published','ERROR':'publish_failed','QUEUE':'scheduled'}.get(r.get('state'))
        if state:
            with db.connect() as c: c.execute('UPDATE publications SET state=?,url=?,error=? WHERE id=?',(state,r.get('releaseURL'),str(r.get('error') or ''),p['id']))
    export_manifest(jid)
