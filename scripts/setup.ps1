# Setup script for native Windows PowerShell
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $ScriptDir "..")

Write-Host "Running Clipdesk setup for Windows..."

# Check required binaries
$required = @("node", "npm", "ffmpeg", "ffprobe")
foreach ($prog in $required) {
    if (-not (Get-Command $prog -ErrorAction SilentlyContinue)) {
        Write-Error "Missing '$prog'. On Windows, install using winget: winget install Gyan.FFmpeg / OpenJS.NodeJS"
        exit 1
    }
}

# Resolve Python binary
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    $py = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $py) {
    Write-Error "Missing Python. On Windows: winget install Python.Python.3.12"
    exit 1
}

# Check Python version
& $py.Source -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
& node -e 'if(Number(process.versions.node.split(".")[0])<20) throw Error("Node 20+ required")'

# Create virtual environment if missing
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment in .venv..."
    & $py.Source -m venv .venv
}

$venvPy = Join-Path (Get-Location) ".venv\Scripts\python.exe"

Write-Host "Installing Python dependencies..."
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt

Write-Host "Building web frontend..."
npm ci --prefix web
npm run build --prefix web

# Generate .env configuration if not present
& $venvPy -c @"
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
print('Ready. Find your workspace password in .env (APP_PASSWORD).')
print('Start Docker Desktop (or Redis), then run: .\\scripts\\start.ps1')
"@

