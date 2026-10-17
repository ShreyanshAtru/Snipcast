#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v git >/dev/null
command -v docker >/dev/null || { echo 'Install and start Docker Desktop first.'; exit 1; }
[[ -x .venv/bin/python ]] || { echo 'Run scripts/setup.sh first'; exit 1; }
if [[ -e infra/postiz/upstream ]]; then
  echo 'Existing Postiz checkout found; preserving configuration and data.'
else
  git clone https://github.com/gitroomhq/postiz-docker-compose.git infra/postiz/upstream
  git -C infra/postiz/upstream checkout dd4969e5e694cd009619a0d53cff14c21104580b
  .venv/bin/python scripts/configure_postiz.py
fi
echo 'Postiz files prepared in infra/postiz/upstream.'
echo 'Edit YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in docker-compose.yaml there.'
echo 'Then run: bash scripts/postiz.sh up -d'
echo 'Open http://localhost:4007 after the services become healthy.'
