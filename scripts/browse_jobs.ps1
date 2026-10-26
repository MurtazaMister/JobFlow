# One-click launcher for the local JobFlow dashboard (used by the "Browse Jobs"
# desktop shortcut). Brings up Docker + the jobflow-postgres container, starts
# the web server in the background if it isn't already running, then opens the
# feed in Brave's "mmister 2003" profile.

# Not "Stop": Windows PowerShell turns a native command's stderr (e.g. `docker
# info` while Docker is down) into a terminating error under it, even when
# redirected. Failures are checked explicitly via $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$Root      = Split-Path -Parent $PSScriptRoot
$Url       = "http://localhost:8080"
$Brave     = "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
$Profile   = "Profile 8"   # Brave's folder for the "mmister 2003" profile
$LogDir    = Join-Path $Root "data\output"
$Container = "jobflow-postgres"

Add-Type -AssemblyName PresentationFramework
function Fail($msg) {
    [System.Windows.MessageBox]::Show($msg, "Browse Jobs", "OK", "Error") | Out-Null
    exit 1
}

function Test-Health {
    # 127.0.0.1, not localhost: PowerShell tries ::1 first and the server is
    # IPv4-only, which can eat the whole timeout.
    try { (Invoke-WebRequest "http://127.0.0.1:8080/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 }
    catch { $false }
}

function Test-Docker {
    docker info --format "{{.ServerVersion}}" *> $null
    return $LASTEXITCODE -eq 0
}

if (-not (Test-Health)) {
    # 1) Docker engine (the database lives in a container).
    if (-not (Test-Docker)) {
        $dd = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
        if (-not (Test-Path $dd)) { Fail "Docker Desktop is not installed." }
        $deadline = (Get-Date).AddSeconds(180)
        $nextLaunch = Get-Date
        while (-not (Test-Docker)) {
            if ((Get-Date) -gt $deadline) { Fail "Docker Desktop didn't start within 3 minutes." }
            # (Re)launch whenever Docker Desktop isn't running: a launch made
            # while a previous instance is still shutting down just exits.
            if ((Get-Date) -ge $nextLaunch -and -not (Get-Process "Docker Desktop" -ErrorAction SilentlyContinue)) {
                Start-Process $dd
                $nextLaunch = (Get-Date).AddSeconds(15)
            }
            Start-Sleep -Seconds 3
        }
    }

    # 2) Database container.
    docker start $Container *> $null
    if ($LASTEXITCODE -ne 0) { Fail "Couldn't start the '$Container' Docker container." }

    # 3) Web server, hidden, logging to data\output\web.log.
    New-Item -ItemType Directory -Force $LogDir | Out-Null
    $env:PYTHONUTF8 = "1"
    Start-Process -FilePath (Join-Path $Root ".venv\Scripts\jobflow.exe") `
        -ArgumentList "web", "--port", "8080" `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "web.log") `
        -RedirectStandardError (Join-Path $LogDir "web.err.log")

    $deadline = (Get-Date).AddSeconds(90)
    while (-not (Test-Health)) {
        if ((Get-Date) -gt $deadline) {
            Fail "JobFlow didn't start. See $LogDir\web.err.log for details."
        }
        Start-Sleep -Seconds 1
    }
}

# 4) Open the feed in the right Brave profile.
Start-Process -FilePath $Brave -ArgumentList "--profile-directory=`"$Profile`"", $Url
