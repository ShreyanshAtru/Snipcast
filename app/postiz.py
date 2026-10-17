import json, uuid, time, os
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from . import settings, db


class PostizError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Postiz:
    def __init__(self, check=lambda: None):
        self.check = check

    def pace(self):
        interval = max(0, float(os.getenv("POSTIZ_MIN_INTERVAL_SECONDS", "4")))
        with db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            at = max(
                time.time(),
                c.execute("SELECT next_at FROM rate_limit WHERE id=1").fetchone()[0],
            )
            c.execute("UPDATE rate_limit SET next_at=? WHERE id=1", (at + interval,))
        while time.time() < at:
            self.check()
            time.sleep(min(0.4, max(0, at - time.time())))
        self.check()

    def request(self, method, route, **kw):
        u = urlsplit(settings.BASE)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.query:
            raise PostizError("Invalid Postiz URL")
        if u.scheme == "http" and u.hostname not in (
            "localhost",
            "127.0.0.1",
            "host.docker.internal",
        ):
            raise PostizError("Remote Postiz must use HTTPS")
        if not settings.KEY:
            raise PostizError("Set POSTIZ_API_KEY in .env and restart API and worker")
        self.pace()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(300, connect=10), follow_redirects=False
            ) as client:
                r = client.request(
                    method,
                    settings.BASE + route,
                    headers={"Authorization": settings.KEY},
                    **kw,
                )
            if not r.is_success:
                raise PostizError(
                    f"Postiz returned HTTP {r.status_code}", r.status_code
                )
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise PostizError(
                "Postiz connection or response failed; check address and server logs"
            ) from e

    def channels(self):
        if settings.MODE == "demo":
            return [
                {
                    "id": "demo-youtube",
                    "name": "Demo YouTube",
                    "providerIdentifier": "youtube",
                },
                {
                    "id": "demo-second",
                    "name": "Demo second channel",
                    "providerIdentifier": "youtube",
                },
            ]
        rows = self.request("GET", "/integrations")
        if isinstance(rows, dict):
            rows = rows.get("integrations", [])
        if not isinstance(rows, list):
            raise PostizError("Unsupported integration response")
        return [
            dict(
                r, providerIdentifier=r.get("providerIdentifier") or r.get("identifier")
            )
            for r in rows
            if (r.get("providerIdentifier") or r.get("identifier")) == "youtube"
            and not r.get("disabled", False)
        ]

    def posts(self, start, end):
        if settings.MODE == "demo":
            with db.connect() as c:
                rows = c.execute("SELECT * FROM mock_posts").fetchall()
            out = []
            for r in rows:
                p = json.loads(r["payload"])
                dest = p["posts"][0]
                if start <= p["date"] <= end:
                    out.append(
                        {
                            "id": r["id"],
                            "publishDate": p["date"],
                            "state": "QUEUE",
                            "integration": dest["integration"],
                            "content": dest["value"][0]["content"],
                            "settings": dest["settings"],
                        }
                    )
            return out
        result = self.request(
            "GET", "/posts", params={"startDate": start, "endDate": end}
        )
        if not isinstance(result, dict) or not isinstance(result.get("posts"), list):
            raise PostizError(
                "Unsupported post-list response; cannot safely check bookings"
            )
        return result["posts"]

    def upload(self, path):
        if settings.MODE == "demo":
            return {
                "id": "demo-" + uuid.uuid4().hex,
                "path": "https://demo.invalid/clip.mp4",
            }
        with Path(path).open("rb") as f:
            r = self.request(
                "POST", "/upload", files={"file": (Path(path).name, f, "video/mp4")}
            )
        if not isinstance(r, dict) or not r.get("id") or not r.get("path"):
            raise PostizError("Upload response missing media reference")
        return {"id": r["id"], "path": r["path"]}

    def create(self, payload):
        if settings.MODE == "demo":
            pid = "demo-" + uuid.uuid4().hex
            with db.connect() as c:
                c.execute(
                    "INSERT INTO mock_posts VALUES(?,?)", (pid, json.dumps(payload))
                )
            return [{"postId": pid}]
        return self.request("POST", "/posts", json=payload)

    def delete(self, pid):
        if settings.MODE == "demo":
            with db.connect() as c:
                c.execute("DELETE FROM mock_posts WHERE id=?", (pid,))
            return
        try:
            self.request("DELETE", "/posts/" + pid)
        except PostizError as e:
            if e.status != 404:
                raise


def receipt_id(receipt):
    rows = receipt if isinstance(receipt, list) else [receipt]
    if len(rows) == 1 and isinstance(rows[0], dict):
        return rows[0].get("postId") or rows[0].get("id")
    return None


def payload(pub, media):
    return {
        "type": "schedule",
        "date": pub["scheduled"],
        "shortLink": False,
        "tags": [],
        "posts": [
            {
                "integration": {"id": pub["channel"]},
                "value": [{"content": pub["description"], "image": [media]}],
                "settings": json.loads(pub["settings"]),
            }
        ],
    }
