#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../infra/postiz/upstream"
exec docker compose -p clipdesk-postiz -f docker-compose.yaml "$@"
