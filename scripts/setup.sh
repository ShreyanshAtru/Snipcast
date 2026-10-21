#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Detect Operating System
OS="unix"
case "${OSTYPE:-}" in
  msys*|cygwin*|win32*) OS="windows" ;;
  darwin*)             OS="macos" ;;
  linux*)              OS="linux" ;;
  *)
    if [[ "$OSTYPE" == "" && -n "${COMSPEC:-}" ]]; then
      OS="windows"
    fi
    ;;
esac

echo "Detected OS: $OS"

# Check required system CLI tools
for program in node npm ffmpeg ffprobe; do
  if ! command -v "$program" >/dev/null 2>&1; then
    if [ "$OS" = "macos" ]; then
      echo "Missing $program. On macOS: brew install $program"
    elif [ "$OS" = "windows" ]; then
      echo "Missing $program. On Windows: winget install Gyan.FFmpeg / OpenJS.NodeJS"
    else
      echo "Missing $program. On Linux: sudo apt update && sudo apt install -y ffmpeg nodejs npm"
    fi
    exit 1
  fi
done

# Python executable resolution (python3 on Unix, python or python3 on Windows)
PYTHON_CMD="python3"
if ! command -v python3 >/dev/null 2>&1; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
  else
    if [ "$OS" = "macos" ]; then
      echo "Missing python3. On macOS: brew install python@3.12"
    elif [ "$OS" = "windows" ]; then
      echo "Missing python. On Windows: winget install Python.Python.3.12"
    else
      echo "Missing python3. On Linux: sudo apt install -y python3 python3-venv"
    fi
    exit 1
  fi
fi

$PYTHON_CMD -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
node -e 'if(Number(process.versions.node.split(".")[0])<20) throw Error("Node 20+ required")'

# Create Python virtual environment if not present
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment in .venv..."
  $PYTHON_CMD -m venv .venv
fi

# Locate python inside virtualenv (.venv/bin on Unix, .venv/Scripts on Windows)
if [ -f ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
elif [ -f ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
else
  echo "Could not find python in .venv"
  exit 1
fi

echo "Installing backend dependencies..."
$VENV_PY -m pip install --upgrade pip
$VENV_PY -m pip install -r requirements.txt

echo "Building web frontend..."
npm ci --prefix web
npm run build --prefix web

# Generate .env configuration if not present
$VENV_PY - <<'PY'
import os, secrets
from pathlib import Path
p = Path('.env')
if not p.exists():
    example = Path('.env.example')
    text = example.read_text() if example.exists() else ""
    text = text.replace('APP_PASSWORD=', 'APP_PASSWORD=' + secrets.token_urlsafe(18))
    text = text.replace('SESSION_SECRET=', 'SESSION_SECRET=' + secrets.token_hex(32))
    home_dir = str(Path.home()).replace('\\', '/')
    text = text.replace('/Users/shreyanshjain', home_dir)
    p.write_text(text)
    try:
        p.chmod(0o600)
    except OSError:
        pass
print('Ready. Find your workspace password in .env (APP_PASSWORD).')
print('Start Docker Desktop (or Redis), then run: bash scripts/start.sh (or .\\scripts\\start.ps1 on Windows)')
PY
