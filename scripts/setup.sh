#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
for program in python3 node npm ffmpeg ffprobe; do
  command -v "$program" >/dev/null || { echo "Missing $program. On macOS: brew install python@3.12 node ffmpeg"; exit 1; }
done
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
node -e 'if(Number(process.versions.node.split(".")[0])<20) throw Error("Node 20+ required")'
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci --prefix web
npm run build --prefix web
.venv/bin/python - <<'PY'
import os,secrets
from pathlib import Path
p=Path('.env')
if not p.exists():
    text=Path('.env.example').read_text().replace('APP_PASSWORD=','APP_PASSWORD='+secrets.token_urlsafe(18)).replace('SESSION_SECRET=','SESSION_SECRET='+secrets.token_hex(32)).replace('/Users/shreyanshjain',str(Path.home()))
    p.write_text(text); p.chmod(0o600)
print('Ready. Find your workspace password in .env (APP_PASSWORD).')
print('Start Docker Desktop, then run: bash scripts/start.sh')
PY
