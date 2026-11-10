# Start script for native Windows PowerShell
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $ScriptDir "..")

$venvPy = Join-Path (Get-Location) ".venv\Scripts\python.exe"

$missing = @()
if (-not (Test-Path $venvPy)) { $missing += ".venv (Virtual environment not found at $venvPy)" }
if (-not (Test-Path ".env")) { $missing += ".env (Environment file missing)" }
if (-not (Test-Path "web\dist")) { $missing += "web\dist (Frontend build files missing)" }

if ($missing.Count -gt 0) {
    Write-Host "Missing setup prerequisites:" -ForegroundColor Red
    foreach ($m in $missing) {
        Write-Host "  - $m" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Error "Please run .\scripts\setup.ps1 first to install dependencies and configure the environment."
    exit 1
}

# Verify Redis or start via docker compose
$redisCheck = $false
try {
    $redisOutput = & $venvPy -c "from app.settings import REDIS_URL; import redis; print(redis.Redis.from_url(REDIS_URL,socket_connect_timeout=1,socket_timeout=1).ping())" 2>$null
    $redisCheck = ($LASTEXITCODE -eq 0 -and ($redisOutput | Select-Object -Last 1) -eq "True")
} catch {
    # An unavailable Redis server is expected here; try starting the Compose service.
    $redisCheck = $false
}
if (-not $redisCheck) {
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        # Distinguish an unavailable Docker Desktop engine from a Compose failure.
        # In particular, redis:7-alpine needs Docker's Linux container engine.
        $dockerInfo = & docker info --format '{{.OSType}}' 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $dockerInfo) {
            throw "Docker is installed but its engine is unavailable. Start Docker Desktop, wait for it to finish starting, and make sure Linux containers are enabled, then rerun .\scripts\start.ps1."
        }
        if (($dockerInfo | Select-Object -Last 1).Trim() -ne "linux") {
            throw "Docker is running in Windows-container mode. Switch Docker Desktop to Linux containers (redis:7-alpine requires Linux), then rerun .\scripts\start.ps1."
        }

        Write-Host "Starting Redis container via docker compose..."
        docker compose up -d --wait
        if ($LASTEXITCODE -ne 0) { throw "Docker is running, but Redis did not start. Check Docker Desktop and run 'docker compose up -d --wait' to see the detailed error." }
        $redisOutput = & $venvPy -c "from app.settings import REDIS_URL; import redis; print(redis.Redis.from_url(REDIS_URL,socket_connect_timeout=2,socket_timeout=2).ping())" 2>$null
        if ($LASTEXITCODE -ne 0 -or ($redisOutput | Select-Object -Last 1) -ne "True") {
            throw "Redis is still unreachable at REDIS_URL after starting Docker Compose. Check REDIS_URL in .env."
        }
    } else {
        Write-Error "Redis is not reachable at REDIS_URL and Docker is not installed. Start Redis or Docker Desktop."
        exit 1
    }
}

# Test port availability
$listener = $null
try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 8787)
    $listener.Start()
} catch [System.Net.Sockets.SocketException] {
    throw "Port 8787 is already in use. Close the process using that port, then rerun the script."
} finally {
    if ($null -ne $listener) { $listener.Stop() }
}

if (-not (Test-Path "data")) {
    New-Item -ItemType Directory -Path "data" | Out-Null
}

# Initialize database
& $venvPy -c 'from app import db'
if ($LASTEXITCODE -ne 0) { throw "Database initialization failed. Check your Python dependencies and .env configuration." }

# Fail early with an actionable message if the web stack has an incompatible or
# incomplete install (for example, a mismatched Pydantic binary in .venv).
& $venvPy -c 'import fastapi, pydantic, pydantic_core; print(1)'
if ($LASTEXITCODE -ne 0) {
    throw "FastAPI/Pydantic imports failed in .venv. Reinstall the locked dependencies with: .\.venv\Scripts\python.exe -m pip install --force-reinstall -r requirements.txt"
}

Write-Host "Starting Celery worker, Celery beat dispatcher, and FastAPI backend..."

$workerProc = $null
$dispatcherProc = $null
$apiProc = $null
try {
    $workerProc = Start-Process -FilePath $venvPy -ArgumentList "-m celery -A app.tasks worker --pool=solo --concurrency=1 --loglevel=INFO" -WorkingDirectory (Get-Location).Path -RedirectStandardOutput "data\worker.log" -RedirectStandardError "data\worker-error.log" -PassThru -NoNewWindow -ErrorAction Stop
    $dispatcherProc = Start-Process -FilePath $venvPy -ArgumentList "-m celery -A app.tasks beat --loglevel=INFO --schedule=data\celerybeat" -WorkingDirectory (Get-Location).Path -RedirectStandardOutput "data\dispatcher.log" -RedirectStandardError "data\dispatcher-error.log" -PassThru -NoNewWindow -ErrorAction Stop
    $apiProc = Start-Process -FilePath $venvPy -ArgumentList "-m uvicorn app.main:app --host 127.0.0.1 --port 8787" -WorkingDirectory (Get-Location).Path -RedirectStandardOutput "data\api.log" -RedirectStandardError "data\api-error.log" -PassThru -NoNewWindow -ErrorAction Stop
} catch {
    foreach ($proc in @($workerProc, $dispatcherProc, $apiProc)) {
        if ($null -ne $proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
    throw "Could not start a Clipdesk service: $($_.Exception.Message)"
}

Write-Host "Open http://127.0.0.1:8787 in your browser."
Write-Host "Press Ctrl+C to stop all services."

try {
    while ($true) {
        if ($workerProc.HasExited -or $dispatcherProc.HasExited -or $apiProc.HasExited) {
            $failed = @()
            if ($workerProc.HasExited) { $failed += "worker (exit $($workerProc.ExitCode))" }
            if ($dispatcherProc.HasExited) { $failed += "dispatcher (exit $($dispatcherProc.ExitCode))" }
            if ($apiProc.HasExited) { $failed += "API (exit $($apiProc.ExitCode))" }
            Write-Warning "Service exited: $($failed -join ', '). Check data\worker-error.log, data\dispatcher-error.log, and data\api-error.log."
            foreach ($log in @("data\worker-error.log", "data\dispatcher-error.log", "data\api-error.log")) {
                if (Test-Path $log) {
                    Write-Host "--- $log (last 30 lines) ---"
                    Get-Content $log -Tail 30
                }
            }
            break
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Write-Host "Stopping background services..."
    foreach ($proc in @($workerProc, $dispatcherProc, $apiProc)) {
        if ($null -ne $proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

