import contextlib, json, sqlite3, time
from .settings import DATA
DB = DATA / 'jobs.sqlite3'
@contextlib.contextmanager
def connect():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback(); raise
    finally: c.close()
def init():
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, config TEXT NOT NULL,
          output TEXT UNIQUE NOT NULL, state TEXT NOT NULL, requested TEXT NOT NULL DEFAULT '',
          progress REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
          updated REAL NOT NULL, mode TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'Waiting for worker');
        CREATE TABLE IF NOT EXISTS clips (
          id TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), part INTEGER, start REAL, duration REAL,
          path TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', checksum TEXT,
          media TEXT, UNIQUE(job_id,part));
        CREATE TABLE IF NOT EXISTS publications (
          id TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), clip_id TEXT REFERENCES clips(id),
          channel TEXT NOT NULL, scheduled TEXT NOT NULL, title TEXT, description TEXT, settings TEXT,
          state TEXT NOT NULL DEFAULT 'planned', post_id TEXT, receipt TEXT, url TEXT, error TEXT DEFAULT '');
        CREATE UNIQUE INDEX IF NOT EXISTS reserved_slots ON publications(channel,scheduled)
          WHERE state NOT IN ('cancelled');
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, job_id TEXT, at REAL, message TEXT);
        CREATE TABLE IF NOT EXISTS rate_limit (id INTEGER PRIMARY KEY, next_at REAL);
        INSERT OR IGNORE INTO rate_limit VALUES(1,0);
        CREATE TABLE IF NOT EXISTS mock_posts (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        ''')
def job(jid):
    with connect() as c:
        row=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
    if not row: raise ValueError('Batch not found')
    return dict(row)
def update(jid, **fields):
    allowed={'state','requested','progress','error','stage'}
    if not set(fields)<=allowed: raise ValueError('Invalid job field')
    fields['updated']=time.time()
    with connect() as c:
        c.execute('UPDATE jobs SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?',(*fields.values(),jid))
def event(jid, message):
    with connect() as c: c.execute('INSERT INTO events(job_id,at,message) VALUES(?,?,?)',(jid,time.time(),message))
def details(jid):
    data=job(jid); data['config']=json.loads(data['config'])
    with connect() as c:
        data['clips']=[dict(r) for r in c.execute('SELECT * FROM clips WHERE job_id=? ORDER BY part',(jid,))]
        data['publications']=[dict(r) for r in c.execute('SELECT * FROM publications WHERE job_id=? ORDER BY scheduled,channel',(jid,))]
        data['events']=[dict(r) for r in c.execute('SELECT at,message FROM events WHERE job_id=? ORDER BY id DESC LIMIT 30',(jid,))]
    return data
init()
