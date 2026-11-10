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


def format_time_tz(iso_str, tz_name='Asia/Kolkata'):
    if not iso_str:
        return ''
    try:
        dt_val = dt.datetime.fromisoformat(iso_str.replace('Z', '+00:00')).astimezone(ZoneInfo(tz_name))
        return dt_val.strftime('%d %b, %I:%M %p')
    except Exception:
        return iso_str


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


def reschedule_publications(
    jid: str,
    publication_ids: list[str] | None = None,
    start_date: str | None = None,
    clips_per_day: int | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    timezone: str | None = None,
):
    job = db.details(jid)
    if job['mode'] != settings.MODE:
        raise ValueError('Cannot reschedule a batch from another mode')

    from .tasks import has_lock, run_job
    if has_lock(jid):
        raise ValueError('Worker is currently active on this batch; wait for it to stop before rescheduling')

    cfg = job['config']
    tz_name = timezone or cfg.get('timezone', 'Asia/Kolkata')
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        raise ValueError(f'Invalid timezone: {tz_name}')

    cpd = int(clips_per_day or cfg.get('clips_per_day', 5))
    if cpd < 1:
        cpd = 1
    w_start = window_start or cfg.get('window_start', '08:00')
    w_end = window_end or cfg.get('window_end', '20:00')
    daily_times = compute_daily_times(cpd, w_start, w_end)

    now_tz = dt.datetime.now(tz)
    now_utc = dt.datetime.now(UTC)
    if not start_date:
        start_date = now_tz.date().isoformat()
    else:
        try:
            dt.date.fromisoformat(start_date)
        except ValueError:
            raise ValueError(f'Invalid start_date format: {start_date}')

    pubs = job['publications']
    clip_part_map = {c['id']: c['part'] for c in job['clips']}
    for p in pubs:
        p['part'] = clip_part_map.get(p.get('clip_id'), 0)

    if publication_ids:
        pub_set = set(publication_ids)
        target_pubs = [p for p in pubs if p['id'] in pub_set and p['state'] != 'published']
    else:
        target_pubs = [p for p in pubs if p['state'] != 'published']

    if not target_pubs:
        raise ValueError('No eligible un-published clips found to reschedule')

    target_pub_ids = {p['id'] for p in target_pubs}
    target_post_ids = {p.get('post_id') for p in target_pubs if p.get('post_id')}
    occupied = set()
    now_buffer = now_utc + dt.timedelta(minutes=5)
    start_utc = dt.datetime.combine(dt.date.fromisoformat(start_date), dt.time(), tz).astimezone(UTC)
    end_utc = max(start_utc, now_buffer) + dt.timedelta(days=max(30, len(target_pubs) * 2 + 7))

    if settings.MODE != 'demo':
        try:
            client = Postiz()
            # Clean up old Postiz posts for target publications first so they do not conflict
            for p in target_pubs:
                if p.get('post_id'):
                    try:
                        client.delete(p['post_id'])
                    except Exception:
                        pass

            active_channels = client.channels()
            active_ids = {c['id'] for c in active_channels}
            for p in target_pubs:
                if p['channel'] not in active_ids and len(active_channels) == 1:
                    p['channel'] = active_channels[0]['id']

            remote = client.posts(iso(start_utc), iso(end_utc))
            for r in remote:
                if r.get('id') in target_post_ids:
                    continue
                if r.get('state') in ('QUEUE', 'PUBLISHED') and r.get('publishDate'):
                    occupied.add((r.get('integration', {}).get('id'), iso(parse(r['publishDate']))))
        except Exception:
            pass

    with db.connect() as c:
        for r in c.execute("SELECT id, channel, scheduled FROM publications WHERE state NOT IN ('cancelled', 'publish_failed')"):
            if r['id'] not in target_pub_ids:
                occupied.add((r['channel'], r['scheduled']))

    by_channel = {}
    for p in target_pubs:
        by_channel.setdefault(p['channel'], []).append(p)

    assigned_slots = {}

    for channel_id, ch_pubs in by_channel.items():
        ch_pubs.sort(key=lambda x: x.get('part', 0))
        groups = [ch_pubs[i:i + cpd] for i in range(0, len(ch_pubs), cpd)]
        day = dt.date.fromisoformat(start_date)
        group_idx = 0
        day_count = 0

        while group_idx < len(groups) and day_count < 3660:
            group = groups[group_idx]

            # Determine daily times for this day
            if day == now_tz.date():
                earliest_today = (now_tz + dt.timedelta(minutes=6)).time()
                try:
                    ws_time = dt.time.fromisoformat(w_start)
                except Exception:
                    ws_time = earliest_today
                try:
                    we_time = dt.time.fromisoformat(w_end)
                except Exception:
                    we_time = dt.time(23, 59)

                # If window_end is earlier than window_start (e.g. 02:00 vs 01:05), or in the past:
                if we_time <= ws_time or we_time <= earliest_today:
                    we_time = dt.time(23, 59)

                eff_start = max(ws_time, earliest_today)
                if eff_start >= we_time:
                    day += dt.timedelta(days=1)
                    day_count += 1
                    continue

                day_times = compute_daily_times(
                    cpd,
                    eff_start.strftime('%H:%M'),
                    we_time.strftime('%H:%M')
                )
            else:
                try:
                    ws_time = dt.time.fromisoformat(w_start)
                    we_time = dt.time.fromisoformat(w_end)
                    if we_time <= ws_time:
                        we_time = dt.time(23, 59)
                    day_times = compute_daily_times(
                        cpd,
                        ws_time.strftime('%H:%M'),
                        we_time.strftime('%H:%M')
                    )
                except Exception:
                    day_times = daily_times

            needed_times = day_times[:len(group)]
            slots = []
            valid_day = True

            for t_str in needed_times:
                t = dt.time.fromisoformat(t_str)
                value = dt.datetime.combine(day, t, tz)
                if value.utcoffset() != value.replace(fold=1).utcoffset():
                    valid_day = False
                    break
                if value.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != value.replace(tzinfo=None):
                    valid_day = False
                    break
                slot = iso(value)
                if parse(slot) <= now_buffer:
                    valid_day = False
                    break
                if parse(slot) > end_utc:
                    raise ValueError('No available slot in checked date range')
                if (channel_id, slot) in occupied:
                    valid_day = False
                    break
                slots.append(slot)

            if valid_day and len(slots) == len(needed_times):
                for p, slot in zip(group, slots):
                    occupied.add((channel_id, slot))
                    assigned_slots[p['id']] = slot
                group_idx += 1

            day += dt.timedelta(days=1)
            day_count += 1

        if group_idx < len(groups):
            raise ValueError('Could not find enough available slots in the given date range')

    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        # Temporarily mark target publications as cancelled so UNIQUE index reserved_slots does not collide with their previous slots
        placeholders = ','.join('?' for _ in target_pub_ids)
        c.execute(f"UPDATE publications SET state='cancelled' WHERE id IN ({placeholders})", list(target_pub_ids))
        for pub_id, new_slot in assigned_slots.items():
            pub_obj = next((p for p in target_pubs if p['id'] == pub_id), None)
            new_channel = pub_obj['channel'] if pub_obj else None
            if new_channel:
                c.execute(
                    "UPDATE publications SET state='planned', scheduled=?, channel=?, post_id=NULL, receipt=NULL, url=NULL, error='' WHERE id=?",
                    (new_slot, new_channel, pub_id),
                )
            else:
                c.execute(
                    "UPDATE publications SET state='planned', scheduled=?, post_id=NULL, receipt=NULL, url=NULL, error='' WHERE id=?",
                    (new_slot, pub_id),
                )
            clip_row = c.execute("SELECT clip_id FROM publications WHERE id=?", (pub_id,)).fetchone()
            if clip_row and clip_row['clip_id']:
                c.execute("UPDATE clips SET media=NULL WHERE id=?", (clip_row['clip_id'],))

        c.execute(
            "UPDATE jobs SET state='queued', requested='', error='', stage='Waiting for worker', progress=0.7, updated=? WHERE id=?",
            (time.time(), jid),
        )

    if settings.MODE != 'demo':
        try:
            client = Postiz()
            for p in target_pubs:
                if p.get('post_id'):
                    try:
                        client.delete(p['post_id'])
                    except Exception:
                        pass
        except Exception:
            pass

    for p in target_pubs:
        slot = assigned_slots.get(p['id'])
        if slot:
            part_num = p.get('part', '?')
            time_str = format_time_tz(slot, tz_name)
            db.event(jid, f"🔄 Part {part_num}: Rescheduled ➔ {time_str}")

    db.event(jid, f"🚀 Rescheduled {len(assigned_slots)} clip(s) starting from {start_date} ({tz_name})")
    export_manifest(jid)

    try:
        run_job.apply_async(args=[jid], retry=False)
    except Exception:
        db.event(jid, "⚠️ Celery broker unavailable; batch will dispatch when worker connects")

    return db.details(jid)

