# Clipdesk — local video-to-YouTube pipeline

A React UI, FastAPI server, Celery worker and Redis queue. FFmpeg clips on your computer; the worker uploads clips to Postiz and creates dated posts. Postiz owns publishing and social OAuth. SQLite records jobs, reservations and receipts. No LLM key is required.

**This is a local, single-workspace MVP.** It supports multiple YouTube channels in the connected Postiz workspace. Instagram and hosted multi-user accounts are not implemented. It starts in demo mode: actual FFmpeg clipping, simulated Postiz uploads and schedules. Nothing is published until you configure live mode and explicitly submit a scheduling batch.

## 1. Install on your Mac

Extract `video-pipeline.zip` under `/Users/shreyanshjain`. The project folder should be `/Users/shreyanshjain/video-pipeline`. Install Docker Desktop and start it. With Homebrew installed:

```bash
brew install python@3.12 node ffmpeg
cd /Users/shreyanshjain/video-pipeline
bash scripts/setup.sh
bash scripts/start.sh
```

The setup script creates a dedicated Python `.venv`, installs dependencies, builds the UI, and creates `.env` with random local credentials. FFmpeg is the system executable; it is not cloned or compiled. Node 20+ and Python 3.11+ are required. Setup does not overwrite an existing `.env`.

Open **http://127.0.0.1:8787**. Sign in using `APP_PASSWORD` from the project's `.env`. Use this exact host to match the configured browser origin.

The start script starts the dedicated Redis Docker service when needed, then the API, a single Celery worker and a periodic dispatcher. Logs are in `data/worker.log` and `data/dispatcher.log`. Ctrl+C stops these local processes; Redis remains running. Run `docker compose stop` to stop that Redis service. Do not run two start scripts at once.

If you already operate Redis, change `REDIS_URL` in `.env` before starting. Port 6381 is used to avoid Postiz's separate Redis service.

## 2. Test without a Postiz account

1. Choose a source video using Browse or enter its absolute path.
2. Choose an existing output directory and a **new** folder name.
3. Choose clip length (59 seconds by default), centre crop or fit with black padding.
4. Set the title template, description and static hashtags.
5. Choose **Clip only**, or **Clip + schedule** and the demo YouTube channels.
6. Set the start date, daily times, timezone, visibility and audience. Preview the complete schedule, then start.
7. Open My batches to track progress and open completed clips.

Files are encoded to 1080×1920 H.264/AAC MP4. The last shorter clip is retained. Clips are organized as `chosen-folder/YYYY-MM-DD/clip_0001.mp4`. With scheduling, the folder date is the first selected channel's planned date for that clip; `manifest.json` records every channel's exact date. Clip-only batches use the specified start date. A clip is encoded once and reused across channels.

Paths must be inside `MEDIA_ROOT` (your home directory by default). For an external disk, set MEDIA_ROOT to a dedicated directory on that disk and keep both input and output within it. The UI browses files on the **API computer**, not arbitrary files on a remote browser's computer.

## 3. Set up Postiz separately

Postiz is needed for real publication, but not demo testing. If you already have a reachable Postiz instance, skip its installation and configure the API connection below.

For a fresh local installation:

```bash
cd /Users/shreyanshjain/video-pipeline
bash scripts/setup-postiz.sh
```

This fetches the official `gitroomhq/postiz-docker-compose` repository at commit `dd4969e5e694cd009619a0d53cff14c21104580b` into `infra/postiz/upstream`. It generates a JWT secret and database password, removes global container/network names, binds the public port to loopback, and raises the local API limit to 1000 requests/hour for batch processing. Existing checkouts and database volumes are preserved. The upstream Postiz image uses `latest`; pin its digest after your acceptance test before production use.

Edit `infra/postiz/upstream/docker-compose.yaml` and fill in:

```yaml
YOUTUBE_CLIENT_ID: 'your-google-oauth-client-id'
YOUTUBE_CLIENT_SECRET: 'your-google-oauth-client-secret'
```

Then:

```bash
bash scripts/postiz.sh up -d
bash scripts/postiz.sh ps
```

Open **http://localhost:4007**, register your workspace, and connect your YouTube channels. Use Postiz's Google callback URL exactly in the Google Cloud OAuth web client; for this local base URL the provider documentation specifies `http://localhost:4007/integrations/social/youtube`. Add the Google account as a test user while the OAuth app is in Testing, and enable YouTube Data API v3. A changed base URL requires matching OAuth settings. Credentials belong in Postiz, not browser code.

For startup errors:

```bash
bash scripts/postiz.sh logs --tail=100 postiz postiz-postgres temporal
```

Do not delete database volumes to fix a password mismatch. Existing database passwords are not changed simply by changing a Compose environment value. Keep the original credentials or deliberately migrate them. After editing OAuth values, run `bash scripts/postiz.sh up -d` to recreate affected containers; a separate restart is unnecessary.

The helper prepares the current upstream stack, including Postgres, Redis and Temporal. Docker/Postiz could not be launched in the build environment; validate this step on your Mac. Port 4007 must be free. The script does not reuse or modify an unrelated Postiz installation.

## 4. Connect Clipdesk to Postiz

In Postiz, get your workspace public API key from its settings. Edit Clipdesk's root `.env`:

```dotenv
POSTIZ_MODE=live
POSTIZ_BASE_URL=http://127.0.0.1:4007/api/public/v1
POSTIZ_API_KEY=your-postiz-api-key
POSTIZ_MIN_INTERVAL_SECONDS=4
```

Stop and restart `scripts/start.sh`. Go to Connections → Refresh channels. Create a **new** live batch, preview its schedule, then start with one short clip and private visibility. Demo batches cannot be replayed as live batches.

If using another Postiz instance, set its public API base URL. Remote instances require HTTPS. Set request pacing to match its limit: the bundled instance uses 1000/hour with 4 seconds between requests; a 30/hour instance needs at least 125 seconds. The limiter is shared across this local app's processes, but cannot account for other API clients.

The worker sends actual MP4 bytes to `POST /upload`, saves the returned media reference, then creates a `POST /posts` schedule with the channel ID, title, description, hashtags and UTC time. Postiz never needs access to your original local path. Google OAuth secrets are separate from the Postiz API key.

Keep the worker running until uploads and scheduling finish. Once scheduled, Postiz must remain online to publish; if it runs on the same Mac, keep that Mac awake. Google Testing authorization may need reconnection after seven days. A successful API schedule is **not** proof of publication; use Refresh Postiz status and check Postiz for platform errors.

## Recovery and boundaries

- Jobs and posting slots are saved transactionally before broker dispatch. The dispatcher picks up saved queued jobs every ten seconds.
- Checksums prevent needless re-encoding on resume. The source's size and modification timestamp must remain unchanged.
- Pause stops local processing at a checkpoint; an in-flight HTTP call may finish first. Resume retains completed work. After an abrupt worker crash, use Resume once the old process has stopped.
- Cancel stops pending work and releases unsubmitted reservations. **Already scheduled posts remain scheduled in Postiz.** Manage/delete them in Postiz.
- Ambiguous network results during post creation are marked `unknown`. They are never blindly retried. Inspect the Postiz calendar and use Link existing post with a verified Postiz post ID. If no post was created, cancel pending work, inspect/remove any existing schedules, and create a new batch/folder. Keep the old batch for audit.
- Slots are checked when previewing, reserving and before submitting. Postiz does not provide an atomic cross-client reservation here; another scheduler could still race after the check. Use one scheduling owner per channel when possible.
- Past slots, slots less than ten minutes away at planning, and ambiguous/nonexistent DST times are skipped. If clipping takes too long and a reserved time becomes unsafe, the batch stops for a new schedule instead of publishing immediately.
- Existing output folders are refused. Low disk space, disconnected channels, changed files and provider errors surface in the batch details.
- Batch limit: 500 clips, 20 channels, videos shorter than 24 hours. Fixed cuts do not detect scenes, track faces, transcribe, or generate captions.
- The local password is not team authentication. Do not expose this server publicly. No per-user isolation, distributed file storage, billing or upload size policy is implemented.

## Code map

- `app/main.py`: authenticated local API and static UI
- `app/service.py`: planning, timezone slots, reservations, manifests
- `app/tasks.py`: durable queue dispatch, FFmpeg jobs, uploads and reconciliation
- `app/media.py`: FFmpeg/ffprobe subprocess wrapper and validation
- `app/postiz.py`: public API adapter and demo mode
- `app/db.py`: SQLite state and posting reservations
- `web/src/`: React UI
- `scripts/`: Mac setup/start and upstream Postiz helpers
- `compose.yaml`: independent Redis for Clipdesk
- `legacy/`: earlier CLI prototype, retained for reference; not used by the app

FFmpeg runs as a subprocess so crashes and long encoding work stay outside the API process. Celery is the background processor; Postiz retains its own internal scheduling implementation. We do not fork either upstream project.

For UI development use `APP_ORIGIN=http://127.0.0.1:5173` for the API and `npm run dev --prefix web`; the Vite proxy forwards API requests. Restore the origin to port 8787 for the built UI.

## Tests

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
npm run build --prefix web
```

Tests use generated video and demo posts; they never contact YouTube. See `docs/VALIDATION.md` for performed checks and outstanding live acceptance tests.

## Later deployment

Retain the local media worker/agent if files stay on users' Macs. Replace SQLite with Postgres and local paths with authenticated upload/object storage or explicit agent assignments for a hosted product. Add real users, workspace-scoped Postiz credentials, authorization, tenant queue quotas, audit logs and secret management. FFmpeg/Celery require a persistent worker host; a frontend deployment alone does not run this pipeline. Instagram needs its own validated provider payload and account setup, rather than reusing YouTube settings.

## References

- https://github.com/gitroomhq/postiz-docker-compose
- https://docs.postiz.com/public-api/integrations/list
- https://docs.postiz.com/public-api/upload
- https://docs.postiz.com/public-api/posts/create
- https://docs.postiz.com/public-api/posts/list
- https://docs.postiz.com/providers/youtube
- https://docs.celeryq.dev/en/stable/userguide/tasks.html

Use footage you have permission to publish. Adding cuts or hashtags does not grant distribution rights.
