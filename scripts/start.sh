#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Detect python & celery in virtualenv (.venv/bin on Unix, .venv/Scripts on Windows)
if [ -f ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
  VENV_CELERY=".venv/Scripts/celery.exe"
  VENV_UVICORN=".venv/Scripts/uvicorn.exe"
elif [ -f ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
  VENV_CELERY=".venv/bin/celery"
  VENV_UVICORN=".venv/bin/uvicorn"
else
  echo 'Run bash scripts/setup.sh (or .\scripts\setup.ps1) first.'
  exit 1
fi

[[ -f .env && -d web/dist ]] || { echo 'Run bash scripts/setup.sh (or .\scripts\setup.ps1) first.'; exit 1; }

# Check Redis connection or start with docker compose
if ! $VENV_PY -c 'from app.settings import REDIS_URL; import redis; redis.Redis.from_url(REDIS_URL,socket_connect_timeout=1,socket_timeout=1).ping()' >/dev/null 2>&1; then
  command -v docker >/dev/null || { echo 'Start Redis at the REDIS_URL in .env, or install and start Docker Desktop.'; exit 1; }
  docker compose up -d --wait
fi

$VENV_PY -c 'import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("127.0.0.1",8787)); s.close()'
mkdir -p data

# Initialize SQLite database before concurrent service imports
$VENV_PY -c 'from app import db'

pids=()
cleanup(){
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait || true
}
trap cleanup EXIT INT TERM

$VENV_CELERY -A app.tasks worker --pool=solo --concurrency=1 --loglevel=INFO >data/worker.log 2>&1 &
pids+=("$!")

$VENV_CELERY -A app.tasks beat --loglevel=INFO --schedule=data/celerybeat >data/dispatcher.log 2>&1 &
pids+=("$!")

$VENV_UVICORN app.main:app --host 127.0.0.1 --port 8787 &
pids+=("$!")

echo 'Open http://127.0.0.1:8787 — Ctrl+C stops the app and worker. Redis stays running.'
while true; do
  for pid in "${pids[@]}"; do
    kill -0 "$pid" 2>/dev/null || {
      echo 'A service stopped; see data/worker.log and data/dispatcher.log'
      exit 1
    }
  done
  sleep 2
done
