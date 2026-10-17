#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env && -x .venv/bin/python && -d web/dist ]] || { echo 'Run bash scripts/setup.sh first'; exit 1; }
if ! .venv/bin/python -c 'from app.settings import REDIS_URL; import redis; redis.Redis.from_url(REDIS_URL,socket_connect_timeout=1,socket_timeout=1).ping()' >/dev/null 2>&1; then
  command -v docker >/dev/null || { echo 'Start Redis at the REDIS_URL in .env, or install and start Docker Desktop.'; exit 1; }
  docker compose up -d --wait
fi
.venv/bin/python -c 'import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("127.0.0.1",8787)); s.close()'
mkdir -p data
# Initialize SQLite (including WAL mode) before concurrent service imports.
.venv/bin/python -c 'from app import db'
pids=()
cleanup(){ trap - EXIT INT TERM; for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done; wait || true; }
trap cleanup EXIT INT TERM
.venv/bin/celery -A app.tasks worker --pool=solo --concurrency=1 --loglevel=INFO >data/worker.log 2>&1 &
pids+=("$!")
.venv/bin/celery -A app.tasks beat --loglevel=INFO --schedule=data/celerybeat >data/dispatcher.log 2>&1 &
pids+=("$!")
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8787 &
pids+=("$!")
echo 'Open http://127.0.0.1:8787 — Ctrl+C stops the app and worker. Redis stays running.'
while true; do
  for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null || { echo 'A service stopped; see data/worker.log and data/dispatcher.log'; exit 1; }; done
  sleep 2
done
