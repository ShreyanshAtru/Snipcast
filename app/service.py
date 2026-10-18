import datetime as dt, hashlib, json, math, shutil, time, uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from . import db, settings
from .media import local_path, probe
from .models import Batch
from .postiz import Postiz

UTC = dt.timezone.utc


def iso(value):
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)


def compute_daily_times(clips_per_day: int, window_start: str, window_end: str) -> list:
    """Return clips_per_day evenly-spaced HH:MM strings within the posting window."""
    try:
        ws = dt.time.fromisoformat(window_start)
        we = dt.time.fromisoformat(window_end)
    except ValueError:
        raise ValueError('Posting window times must be in HH:MM format')
    s = ws.hour * 60 + ws.minute
    e = we.hour * 60 + we.minute
    if e <= s:
        raise ValueError('Posting window end must be after window start')
    if clips_per_day == 1:
        return [f'{ws.hour:02d}:{ws.minute:02d}']
    return [
        f'{(s + round(i * (e - s) / (clips_per_day - 1))) // 60:02d}:'
        f'{(s + round(i * (e - s) / (clips_per_day - 1))) % 60:02d}'
        for i in range(clips_per_day)
    ]


def prepare(spec: Batch):
    source = local_path(spec.source)
    destination = local_path(spec.destination)
    if not source.is_file() or not destination.is_dir():
        raise ValueError('Choose a video file and an existing destination directory')
    try:
        ZoneInfo(spec.timezone)
        dt.date.fromisoformat(spec.start_date)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Invalid timezone or start date')

    info = probe(source)
    count = math.ceil(info['duration'] / spec.clip_seconds)
    if count > settings.MAX_CLIPS:
        raise ValueError(f'Batch exceeds {settings.MAX_CLIPS} clips')
    if not shutil.which('ffmpeg'):
        raise ValueError('FFmpeg is not installed')

    estimate = int(info['duration'] * 1_500_000)
    free = shutil.disk_usage(destination).free
    if free < max(estimate, 256 * 1024 * 1024):
        raise ValueError('Not enough free space for estimated output; choose another destination')
    if spec.schedule and not spec.channels:
        raise ValueError('Choose at least one YouTube channel')
    if len({c.id for c in spec.channels}) != len(spec.channels):
        raise ValueError('Channel selected twice')

    cfg = spec.model_dump(exclude={'preview_token'})
    cfg['source'] = str(source)
    cfg['destination'] = str(destination)
    cfg['source_stat'] = [source.stat().st_size, source.stat().st_mtime_ns]
    cfg['duration'] = info['duration']
    output = local_path(str(destination / spec.folder), False)
    cfg['output'] = str(output)
    fp = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()

    clips = [
        {
            'part': i + 1,
            'start': i * spec.clip_seconds,
            'duration': min(spec.clip_seconds, info['duration'] - i * spec.clip_seconds),
        }
        for i in range(count)
    ]

    pubs = []
    occupied = set()

    if spec.schedule:
        client = Postiz()
        valid = {c['id'] for c in client.channels()}
        if not {c.id for c in spec.channels} <= valid:
            raise ValueError('A selected YouTube channel is disconnected or unavailable')

        now = dt.datetime.now(UTC) + dt.timedelta(minutes=10)
        start = dt.datetime.combine(
            dt.date.fromisoformat(spec.start_date), dt.time(), ZoneInfo(spec.timezone)
        ).astimezone(UTC)
        end = max(start, now) + dt.timedelta(days=max(30, count * 2 + 7))

        remote = client.posts(iso(start), iso(end))
        for r in remote:
            if r.get('state') in ('QUEUE', 'PUBLISHED') and r.get('publishDate'):
                occupied.add((r.get('integration', {}).get('id'), iso(parse(r['publishDate']))))
        with db.connect() as c:
            occupied.update(
                (r['channel'], r['scheduled'])
                for r in c.execute("SELECT channel,scheduled FROM publications WHERE state!='cancelled'")
            )

        # Compute posting times for this window
        daily_times = compute_daily_times(spec.clips_per_day, spec.window_start, spec.window_end)
        tz = ZoneInfo(spec.timezone)

        # Group clips into day-sized batches
        groups = [clips[i:i + spec.clips_per_day] for i in range(0, len(clips), spec.clips_per_day)]

        for channel in spec.channels:
            day = dt.date.fromisoformat(spec.start_date)
            group_idx = 0
            day_count = 0

            while group_idx < len(groups) and day_count < 3660:
                group = groups[group_idx]
                # For the last (potentially partial) group, only use as many times as clips
                needed_times = daily_times[:len(group)]

                slots = []
                valid_day = True

                for t_str in needed_times:
                    t = dt.time.fromisoformat(t_str)
                    value = dt.datetime.combine(day, t, tz)
                    # Skip DST ambiguous / gap slots
                    if value.utcoffset() != value.replace(fold=1).utcoffset():
                        valid_day = False
                        break
                    if value.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != value.replace(tzinfo=None):
                        valid_day = False
                        break
                    slot = iso(value)
                    if parse(slot) <= now:
                        valid_day = False
                        break
                    if parse(slot) > end:
                        raise ValueError('No available slot in checked date range')
                    if (channel.id, slot) in occupied:
                        valid_day = False
                        break
                    slots.append(slot)

                if valid_day and len(slots) == len(needed_times):
                    # Commit all slots for this day-group
                    for slot in slots:
                        occupied.add((channel.id, slot))
                    for clip, slot in zip(group, slots):
                        try:
                            title = spec.title.format(video_name=source.stem, part=clip['part'])
                        except (KeyError, ValueError, IndexError, AttributeError):
                            raise ValueError('Title supports only {video_name} and {part}')
                        if not 2 <= len(title) <= 100:
                            raise ValueError('Rendered title must be 2–100 characters')
                        text = (spec.description + '\n\n' + spec.hashtags).strip()
                        if len(text) > 5000:
                            raise ValueError('Description and hashtags exceed 5000 characters')
                        pubs.append({
                            'part': clip['part'],
                            'day_group': group_idx + 1,
                            'channel': channel.id,
                            'scheduled': slot,
                            'title': title,
                            'description': text,
                            'settings': {
                                '__type': 'youtube',
                                'title': title,
                                'type': spec.visibility,
                                'selfDeclaredMadeForKids': spec.made_for_kids,
                                'tags': [],
                            },
                        })
                    group_idx += 1

                day += dt.timedelta(days=1)
                day_count += 1

            if group_idx < len(groups):
                raise ValueError('No available slot in checked date range')

    token = hashlib.sha256(json.dumps([fp, pubs], sort_keys=True).encode()).hexdigest()
    return {
        'preview_token': token,
        'config': cfg,
        'fingerprint': fp,
        'count': count,
        'clips': clips,
        'publications': pubs,
        'output': str(output),
        'video': info,
        'estimated_bytes': estimate,
        'free_bytes': free,
        'mode': settings.MODE,
    }


def create(spec):
    # prepare() is read-only. Reservations committed in a single transaction.
    plan = prepare(spec)
    jid = uuid.uuid4().hex
    output = Path(plan['output'])

    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute('SELECT id FROM jobs WHERE fingerprint=?', (plan['fingerprint'],)).fetchone()
        if existing:
            return existing['id']
        if spec.preview_token and spec.preview_token != plan['preview_token']:
            raise ValueError('The schedule changed since preview. Preview again before starting.')
        if output.exists():
            raise ValueError('Output folder already exists. Choose a new folder name or resume the existing batch.')
        c.execute(
            'INSERT INTO jobs(id,fingerprint,config,output,state,created,updated,mode) VALUES(?,?,?,?,?,?,?,?)',
            (jid, plan['fingerprint'], json.dumps(plan['config']), str(output), 'queued', time.time(), time.time(), settings.MODE),
        )
        ids = {}
        for clip in plan['clips']:
            cid = uuid.uuid4().hex
            ids[clip['part']] = cid
            # Use day_NNN/ subfolder when scheduled, flat otherwise
            day_group = next((p['day_group'] for p in plan['publications'] if p['part'] == clip['part']), None)
            if day_group is not None:
                clip_path = str(output / f'day_{day_group:03d}' / f"clip_{clip['part']:04d}.mp4")
            else:
                clip_path = str(output / f"clip_{clip['part']:04d}.mp4")
            c.execute(
                'INSERT INTO clips(id,job_id,part,start,duration,path) VALUES(?,?,?,?,?,?)',
                (cid, jid, clip['part'], clip['start'], clip['duration'], clip_path),
            )
        for p in plan['publications']:
            c.execute(
                'INSERT INTO publications(id,job_id,clip_id,channel,scheduled,title,description,settings) VALUES(?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex, jid, ids[p['part']], p['channel'], p['scheduled'], p['title'], p['description'], json.dumps(p['settings'])),
            )
    db.event(jid, 'Batch saved. Waiting for Celery worker.')
    return jid


def export_manifest(jid):
    data = db.details(jid)
    output = Path(data['output'])
    if not output.exists() or not (output / '.clipdesk-job').is_file() or (output / '.clipdesk-job').read_text() != jid:
        return
    # Atomic derived export. SQLite is authoritative, not this file.
    target = output / 'manifest.json'
    temp = output / 'manifest.tmp'
    temp.write_text(json.dumps(data, indent=2))
    temp.replace(target)
