# Validation and acceptance

## Completed in the build workspace

- 11 backend tests passed using Python 3.12 and real FFmpeg/ffprobe.
- Generated a 2.3-second test video; verified three clips, correct 1080×1920 dimensions, approximate segment durations, preserved audio and date subfolders.
- Verified fit/padding mode with a silent video and safe cleanup of interrupted encoding.
- Verified demo scheduling to two channels, reserved-slot collision avoidance, reconciliation and duplicate batch handling.
- Simulated an uncertain Postiz creation failure; verified that resume never repeats the creation call.
- Verified authentication, blocked outside-root paths, cross-origin rejection, source mutation detection, pause, cancellation and foreign output-folder preservation.
- Verified that changed schedules require a fresh preview before submission.
- TypeScript compilation and Vite production build passed.
- Shell setup/start scripts passed Bash syntax checks.
- Production UI was served over HTTP; login cookies and authenticated channel requests passed a live local server smoke test.

One test-library deprecation warning is emitted by Starlette's httpx TestClient adapter; tests pass. No real account credentials are in this package.

## Not validated here

- Docker Desktop and the complete upstream Postiz stack could not be launched in this environment.
- Actual Celery-to-Redis process delivery needs the local acceptance check below. Tests execute the task body directly, including real encoding and persistent state.
- No live upload or social publication was performed.
- Browser automation could not start because the Chromium download failed; UI compilation is verified, but visual/browser interaction testing remains local.
- macOS execution and Apple Silicon image compatibility require testing on your machine.

## Mac acceptance check

1. Run setup/start. Confirm Redis shows available and `data/worker.log` shows the worker ready.
2. In demo mode, run a small video with 1-second clips and two demo channels. Confirm progress reaches completed, MP4s play, and each clip has two scheduled destinations.
3. Run a longer clipping batch; pause, wait until paused, restart the app, then resume. Confirm completed clips are reused.
4. Set up Postiz, connect one YouTube test account and set Clipdesk to live mode.
5. Schedule one private clip at least an hour ahead. Confirm the media and post exist in Postiz, then confirm actual publication and use Refresh Postiz status.
6. Test a second channel and check timezone/date interpretation before submitting a long batch.
7. Keep the Mac/Postiz awake through publication. Do not treat “scheduled” as “published”.

Only after these checks should the team increase batch size or prepare a hosted multi-user version.
