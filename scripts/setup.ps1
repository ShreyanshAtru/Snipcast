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

# Generate .env configuration if not present. Write a temporary Python file so
# Windows PowerShell 5.1 does not reinterpret newlines in a native -c argument.
if (-not (Test-Path ".env")) {
    $envGenerator = Join-Path $env:TEMP "clipdesk-create-env.py"
    @'
import secrets
from pathlib import Path

p = Path(".env")
example = Path(".env.example")
text = example.read_text(encoding="utf-8") if example.exists() else ""
text = text.replace("APP_PASSWORD=", "APP_PASSWORD=" + secrets.token_urlsafe(18), 1)
text = text.replace("SESSION_SECRET=", "SESSION_SECRET=" + secrets.token_hex(32), 1)
home_dir = str(Path.home()).replace("\\", "/")
text = text.replace("/Users/shreyanshjain", home_dir)
p.write_text(text, encoding="utf-8")
'@ | Set-Content -Path $envGenerator -Encoding UTF8
    try {
        & $venvPy $envGenerator
        if ($LASTEXITCODE -ne 0) { throw "Failed to generate .env." }
    } finally {
        Remove-Item $envGenerator -ErrorAction SilentlyContinue
    }
}

# Replace the sample Mac media root when setup is run on another machine. Keep
# a user-configured MEDIA_ROOT unchanged on later setup runs.
if (Test-Path ".env") {
    $mediaRoot = (& $venvPy -c "from pathlib import Path; print(str(Path.home()).replace('\\', '/'))").Trim()
    $envContent = Get-Content ".env" -Raw
    $envContent = $envContent.Replace("MEDIA_ROOT=/Users/shreyanshjain", "MEDIA_ROOT=$mediaRoot")
    Set-Content -Path ".env" -Value $envContent -NoNewline -Encoding UTF8
}

Write-Host "Ready. Find your workspace password in .env (APP_PASSWORD)."
Write-Host "Start Docker Desktop (or Redis), then run: .\scripts\start.ps1"

