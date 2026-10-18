# Start script for native Windows PowerShell
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $ScriptDir "..")

$venvPy = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$venvCelery = Join-Path (Get-Location) ".venv\Scripts\celery.exe"
$venvUvicorn = Join-Path (Get-Location) ".venv\Scripts\uvicorn.exe"

if (-not (Test-Path $venvPy) -or -not (Test-Path ".env") -or -not (Test-Path "web\dist")) {
    Write-Error "Please run .\scripts\setup.ps1 first."
    exit 1
}

# Verify Redis or start via docker compose
$redisCheck = & $venvPy -c "from app.settings import REDIS_URL; import redis; print(redis.Redis.from_url(REDIS_URL,socket_connect_timeout=1,socket_timeout=1).ping())" 2>$null
if ($redisCheck -ne "True") {
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        Write-Host "Starting Redis container via docker compose..."
        docker compose up -d --wait
    } else {
        Write-Error "Redis is not reachable at REDIS_URL and Docker is not installed. Start Redis or Docker Desktop."
        exit 1
    }
}

# Test port availability
& $venvPy -c 'import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("127.0.0.1",8787)); s.close()'

if (-not (Test-Path "data")) {
    New-Item -ItemType Directory -Path "data" | Out-Null
}

# Initialize database
& $venvPy -c 'from app import db'

Write-Host "Starting Celery worker, Celery beat dispatcher, and FastAPI backend..."

$workerProc = Start-Process -FilePath $venvCelery -ArgumentList "-A app.tasks worker --pool=solo --concurrency=1 --loglevel=INFO" -RedirectStandardOutput "data\worker.log" -RedirectStandardError "data\worker.log" -PassThru -NoNewWindow
$dispatcherProc = Start-Process -FilePath $venvCelery -ArgumentList "-A app.tasks beat --loglevel=INFO --schedule=data\celerybeat" -RedirectStandardOutput "data\dispatcher.log" -RedirectStandardError "data\dispatcher.log" -PassThru -NoNewWindow
$apiProc = Start-Process -FilePath $venvUvicorn -ArgumentList "app.main:app --host 127.0.0.1 --port 8787" -PassThru -NoNewWindow

Write-Host "Open http://127.0.0.1:8787 in your browser."
Write-Host "Press Ctrl+C to stop all services."

try {
    while ($true) {
        if ($workerProc.HasExited -or $dispatcherProc.HasExited -or $apiProc.HasExited) {
            Write-Warning "One of the background services terminated. Check data\worker.log and data\dispatcher.log."
            break
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Write-Host "Stopping background services..."
    Stop-Process -Id $workerProc.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $dispatcherProc.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $apiProc.Id -Force -ErrorAction SilentlyContinue
}

