import hashlib, hmac, json, os, secrets, shutil, sqlite3, time
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import db, settings, media, service
from .models import Batch
from .postiz import Postiz, PostizError
from .tasks import run_job, has_lock, reconcile

app = FastAPI(title="Clipdesk local pipeline")
PASSWORD = os.getenv("APP_PASSWORD", "")
SECRET = os.getenv("SESSION_SECRET", "")


def signed(exp):
    return hmac.new(SECRET.encode(), str(exp).encode(), hashlib.sha256).hexdigest()


def valid_session(cookie):
    try:
        expiry, mac = cookie.split(".")
        return int(expiry) > time.time() and hmac.compare_digest(mac, signed(expiry))
    except (ValueError, AttributeError):
        return False


@app.middleware("http")
async def local_security(request: Request, call_next):
    if request.url.hostname not in ("localhost", "127.0.0.1", "testserver"):
        return JSONResponse({"detail": "Local host only"}, 403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("origin") not in (settings.ORIGIN, None):
            return JSONResponse({"detail": "Origin not allowed"}, 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site request blocked"}, 403)
    if request.url.path.startswith("/api/") and request.url.path not in (
        "/api/login",
        "/api/session",
    ):
        if not PASSWORD or not SECRET:
            return JSONResponse(
                {"detail": "Run scripts/setup.sh to configure local credentials"}, 503
            )
        if not valid_session(request.cookies.get("session")):
            return JSONResponse({"detail": "Sign in to your local workspace"}, 401)
    result = await call_next(request)
    result.headers["X-Content-Type-Options"] = "nosniff"
    result.headers["Referrer-Policy"] = "no-referrer"
    result.headers["Cache-Control"] = "no-store"
    return result


@app.exception_handler(ValueError)
async def bad_value(request, exc):
    return JSONResponse({"detail": str(exc)}, 400)


@app.exception_handler(PostizError)
async def upstream(request, exc):
    return JSONResponse({"detail": str(exc)}, 502)


@app.exception_handler(sqlite3.IntegrityError)
async def conflict(request, exc):
    return JSONResponse(
        {
            "detail": "A folder or posting slot was reserved concurrently. Refresh the plan."
        },
        409,
    )


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login):
    if not PASSWORD or not SECRET:
        raise HTTPException(503, "Run setup first")
    if not secrets.compare_digest(body.password, PASSWORD):
        raise HTTPException(401, "Incorrect workspace password")
    expiry = str(int(time.time() + 12 * 3600))
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "session",
        expiry + "." + signed(expiry),
        httponly=True,
        samesite="strict",
        max_age=43200,
    )
    return response


@app.get("/api/session")
def session(request: Request):
    return {
        "authenticated": bool(SECRET and valid_session(request.cookies.get("session"))),
        "configured": bool(SECRET and PASSWORD),
    }


@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response


@app.get("/api/status")
def status():
    broker = False
    try:
        import redis

        broker = bool(
            redis.Redis.from_url(
                settings.REDIS_URL, socket_connect_timeout=0.4, socket_timeout=0.4
            ).ping()
        )
    except Exception:
        pass
    return {
        "mode": settings.MODE,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "redis": broker,
        "media_root": str(settings.ALLOWED_ROOT),
        "postiz_configured": settings.MODE == "demo" or bool(settings.KEY),
    }


@app.get("/api/files")
def files(path: str = ""):
    directory = media.local_path(path or str(settings.ALLOWED_ROOT))
    if not directory.is_dir():
        raise ValueError("Not a directory")
    entries = []
    for p in directory.iterdir():
        if p.name.startswith("."):
            continue
        try:
            r = p.resolve()
            if not r.is_relative_to(settings.ALLOWED_ROOT):
                continue
            if r.is_dir() or p.suffix.lower() in (
                ".mp4",
                ".mkv",
                ".mov",
                ".webm",
                ".avi",
                ".m4v",
            ):
                entries.append(
                    {"name": p.name, "path": str(r), "directory": r.is_dir()}
                )
        except OSError:
            continue
    return {
        "path": str(directory),
        "parent": str(directory.parent) if directory != settings.ALLOWED_ROOT else None,
        "entries": sorted(
            entries, key=lambda x: (not x["directory"], x["name"].lower())
        )[:500],
    }


@app.get("/api/channels")
def channels():
    return Postiz().channels()


@app.post("/api/preview")
def preview(body: Batch):
    return service.prepare(body)


@app.post("/api/jobs")
def create(body: Batch):
    if not body.preview_token:
        raise ValueError("Preview the batch before starting")
    jid = service.create(body)
    try:
        run_job.apply_async(args=[jid], retry=False)
    except Exception:
        db.event(
            jid,
            "Broker unavailable; saved job will dispatch when Redis and worker return",
        )
    return db.details(jid)


@app.get("/api/jobs")
def jobs():
    with db.connect() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT id,output,state,stage,progress,error,created,mode FROM jobs ORDER BY created DESC LIMIT 100"
            )
        ]


@app.get("/api/jobs/{jid}")
def job(jid: str):
    return db.details(jid)


@app.post("/api/jobs/{jid}/pause")
def pause(jid: str):
    r = db.job(jid)
    if r["state"] not in ("running", "queued"):
        raise ValueError("Batch is not active")
    db.update(jid, requested="pause")
    if r["state"] == "queued":
        db.update(jid, state="paused", stage="Paused")
    return {"ok": True}


@app.post("/api/jobs/{jid}/resume")
def resume(jid: str):
    r = db.details(jid)
    if has_lock(jid):
        raise ValueError("Worker is still active; wait for it to stop")
    if r["state"] not in ("paused", "failed", "queued", "running"):
        raise ValueError("Batch cannot be resumed")
    if any(p["state"] in ("unknown", "submitting") for p in r["publications"]):
        raise ValueError("Reconcile uncertain submissions before resume")
    db.update(jid, state="queued", requested="", error="", stage="Waiting for worker")
    try:
        run_job.apply_async(args=[jid], retry=False)
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: str):
    r = db.job(jid)
    db.update(jid, requested="cancel")
    if not has_lock(jid):
        db.update(jid, state="cancelled", stage="Pending work cancelled")
        with db.connect() as c:
            c.execute(
                "UPDATE publications SET state='cancelled' WHERE job_id=? AND state='planned'",
                (jid,),
            )
    return {
        "ok": True,
        "note": "Already scheduled Postiz posts are unchanged; manage them in Postiz.",
    }


@app.post("/api/jobs/{jid}/refresh")
def refresh(jid: str):
    if has_lock(jid):
        raise ValueError("Wait for active processing before reconciliation")
    reconcile(jid)
    return db.details(jid)


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    # 1. Verify job exists
    job = db.job(jid)

    # 2. Signal worker to stop (best-effort; worker checks this flag)
    db.update(jid, requested="cancel")

    # 3. Delete all Postiz posts linked to this batch (ignore 404s)
    with db.connect() as c:
        pubs = [
            dict(r)
            for r in c.execute(
                "SELECT post_id FROM publications WHERE job_id=? AND post_id IS NOT NULL AND post_id != ''",
                (jid,),
            )
        ]
    client = Postiz()
    for pub in pubs:
        try:
            client.delete(pub["post_id"])
        except Exception:
            pass  # best-effort; don't block deletion on a Postiz failure

    # 4. Remove local output folder
    import shutil as _shutil
    output = Path(job["output"])
    if output.exists():
        marker = output / ".clipdesk-job"
        # Safety check: only delete folders owned by this batch
        if marker.is_file() and marker.read_text() == jid:
            _shutil.rmtree(output, ignore_errors=True)

    # 5. Hard-delete from DB (cascade: events, publications, clips, job)
    with db.connect() as c:
        c.execute("DELETE FROM events WHERE job_id=?", (jid,))
        c.execute("DELETE FROM publications WHERE job_id=?", (jid,))
        c.execute("DELETE FROM clips WHERE job_id=?", (jid,))
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))

    return {"ok": True}


class Resolution(BaseModel):
    post_id: str


@app.post("/api/publications/{pid}/resolve")
def resolve(pid: str, body: Resolution):
    with db.connect() as c:
        row = c.execute("SELECT * FROM publications WHERE id=?", (pid,)).fetchone()
    if not row or row["state"] not in ("unknown", "submitting"):
        raise ValueError("No uncertain submission to resolve")
    if has_lock(row["job_id"]):
        raise ValueError("Wait for worker to stop")
    from datetime import timedelta

    rows = Postiz().posts(
        service.iso(service.parse(row["scheduled"]) - timedelta(days=1)),
        service.iso(service.parse(row["scheduled"]) + timedelta(days=1)),
    )
    match = next(
        (
            r
            for r in rows
            if r["id"] == body.post_id
            and r.get("integration", {}).get("id") == row["channel"]
        ),
        None,
    )
    if not match or service.parse(match.get("publishDate", "")) != service.parse(
        row["scheduled"]
    ):
        raise ValueError("Post ID does not match the expected channel and time")
    if db.job(row["job_id"])["mode"] != settings.MODE:
        raise ValueError("Batch mode differs from server mode")
    with db.connect() as c:
        c.execute(
            "UPDATE publications SET post_id=?,state='scheduled',error='' WHERE id=?",
            (body.post_id, pid),
        )
    db.event(
        row["job_id"],
        "Operator linked uncertain submission to confirmed Postiz post " + body.post_id,
    )
    return {"ok": True}


@app.get("/api/clips/{cid}")
def clip(cid: str):
    with db.connect() as c:
        row = c.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not row or row["state"] != "ready":
        raise HTTPException(404, "Clip not ready")
    return FileResponse(media.local_path(row["path"]), media_type="video/mp4")


WEB = settings.ROOT / "web" / "dist"
if WEB.exists():
    @app.get("/create/")
    @app.get("/batches/")
    @app.get("/connections/")
    def ui_trailing_slash(request: Request):
        from fastapi.responses import RedirectResponse

        target = request.url.path.rstrip("/")
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=307)

    # Client-side pages must serve the React entry point on direct navigation.
    @app.get("/create")
    @app.get("/batches")
    @app.get("/connections")
    def ui_route():
        return FileResponse(WEB / "index.html")

    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
else:

    @app.get("/")
    def missing_build():
        return {"message": "Build the UI: cd web && npm ci && npm run build"}
