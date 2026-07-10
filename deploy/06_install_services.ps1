# =======================================================================
# Self-Elevate to Administrator
# =======================================================================
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

# =======================================================================
# AAM Backup Automation V1 - SERVICE INSTALLER (PowerShell)
# Run as Administrator. Re-runnable on every upgrade/config change.
# =======================================================================

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue" # Prevent NativeCommandError from NSSM

# -- Resolve paths ----------------------------------------------------
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectDir = Split-Path -Parent $ScriptDir
$DeployDir  = Join-Path $ProjectDir "deploy"
$BinDir     = Join-Path $DeployDir "bin"

$NSSM = Join-Path $BinDir "nssm.exe"

# -- Find uv executable -----------------------------------------------
$UV_EXE = $null

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCmd) { $UV_EXE = $uvCmd.Source }

if (-not $UV_EXE) {
    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe"),
        "C:\Program Files\Python312\Scripts\uv.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $UV_EXE = $c; break }
    }
}

if (-not $UV_EXE) {
    Write-Host ""
    Write-Host "  ERROR: 'uv' package manager not found." -ForegroundColor Red
    Write-Host '  Install it: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Write-Host ""
    exit 1
}

# -- Read runtime_dir from config.yaml via Python ---------------------
$ConfigFile   = Join-Path $ProjectDir "config.yaml"
$DefaultRuntime = "C:\BackupAgent"
$ReadConfigPy = Join-Path $DeployDir "read_config.py"

function Read-ConfigValue {
    param([string]$Key, [string]$Default)
    try {
        $orig = Get-Location
        Set-Location $ProjectDir
        $result = & $UV_EXE run --quiet python $ReadConfigPy $ConfigFile $Key --default $Default 2>$null
        Set-Location $orig
        if ($LASTEXITCODE -eq 0 -and $result) { return $result }
    } catch {
        if ($orig) { Set-Location $orig }
    }
    return $Default
}

$RuntimeDir = Read-ConfigValue "paths.runtime_dir" $DefaultRuntime
$BackupRoot = $RuntimeDir
$LogDir     = Join-Path $BackupRoot "logs"
$PrefectHome = Join-Path $BackupRoot ".prefect"

$SVC_SERVER   = "AamPrefectServer"
$SVC_AGENT    = "AamBackupAgent"
$SVC_WATCHDOG = "AamWatchdog"

# -- Validate Google Cloud SDK ----------------------------------------
$GcloudCmd = Get-Command gcloud -ErrorAction SilentlyContinue
if (-not $GcloudCmd) {
    $GcloudCmd = Join-Path $BinDir "google-cloud-sdk\bin\gcloud.cmd"
}
if (-not $GcloudCmd -and -not (Test-Path $GcloudCmd)) {
    Write-Host ""
    Write-Host "  ERROR: Google Cloud SDK not found in deploy\bin or PATH." -ForegroundColor Red
    Write-Host "  Please run 03_setup_system.bat first to download and set up the SDK."
    Write-Host ""
    exit 1
}

# -- Validate rclone --------------------------------------------------
$RcloneExe = $null
$candidate = Join-Path $BinDir "rclone.exe"
if (Test-Path $candidate) { $RcloneExe = $candidate }

if (-not $RcloneExe) {
    $RcloneExe = Get-Command rclone -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
}

if (-not $RcloneExe) {
    Write-Host ""
    Write-Host "  ERROR: 'rclone' executable not found in deploy\bin or system PATH." -ForegroundColor Red
    Write-Host "  Download rclone from: https://rclone.org/downloads/"
    Write-Host "  Place rclone.exe in:  $BinDir"
    Write-Host ""
    exit 1
}

# -- Validate NSSM ----------------------------------------------------
if (-not (Test-Path $NSSM)) {
    Write-Host ""
    Write-Host "  ERROR: NSSM not found at $NSSM" -ForegroundColor Red
    Write-Host "  Download from https://nssm.cc/download and place nssm.exe in deploy\bin\"
    Write-Host ""
    exit 1
}

# -- Validate project -------------------------------------------------
$LaunchPy = Join-Path $ProjectDir "launch.py"
if (-not (Test-Path $LaunchPy)) {
    Write-Host ""
    Write-Host "  ERROR: launch.py not found in $ProjectDir" -ForegroundColor Red
    Write-Host "  Ensure this script is in the 'deploy' folder of the project."
    Write-Host ""
    exit 1
}

# -- Create required directories ---------------------------------------
if (-not (Test-Path $LogDir))     { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
if (-not (Test-Path $PrefectHome)) { New-Item -ItemType Directory -Path $PrefectHome -Force | Out-Null }

Write-Host ""
Write-Host "==================================================================="
Write-Host "  AAM Backup Automation V1 - SERVICE INSTALLER"
Write-Host "==================================================================="
Write-Host "  NSSM:         $NSSM"
Write-Host "  uv:           $UV_EXE"
Write-Host "  Project:      $ProjectDir"
Write-Host "  Logs:         $LogDir"
Write-Host "  Prefect home: $PrefectHome"
Write-Host "==================================================================="

# -- Stop and remove old services -------------------------------------
Write-Host ""
Write-Host "[setup] Stopping any running AAM services..."
& $NSSM stop $SVC_WATCHDOG 2>$null
& $NSSM stop $SVC_AGENT 2>$null
& $NSSM stop $SVC_SERVER 2>$null

& $NSSM remove $SVC_WATCHDOG confirm 2>$null
& $NSSM remove $SVC_AGENT confirm 2>$null
& $NSSM remove $SVC_SERVER confirm 2>$null

Start-Sleep -Seconds 3

# ====================================================================
# SERVICE 1: AamPrefectServer
# ====================================================================
Write-Host ""
Write-Host "[setup] Installing $SVC_SERVER..."

& $NSSM install $SVC_SERVER $UV_EXE
& $NSSM set $SVC_SERVER AppParameters "run prefect server start --host 127.0.0.1 --port 4200"
& $NSSM set $SVC_SERVER AppDirectory $ProjectDir
& $NSSM set $SVC_SERVER DisplayName "AAM Prefect Server"
& $NSSM set $SVC_SERVER Description "Prefect 3 API server for AAM Backup Automation V1"
& $NSSM set $SVC_SERVER Start SERVICE_AUTO_START

# Stdout + stderr to same log file (append mode), 10 MB rotation
& $NSSM set $SVC_SERVER AppStdout "$LogDir\prefect_svc.log"
& $NSSM set $SVC_SERVER AppStderr "$LogDir\prefect_svc.log"
& $NSSM set $SVC_SERVER AppStdoutCreationDisposition 4
& $NSSM set $SVC_SERVER AppStderrCreationDisposition 4
& $NSSM set $SVC_SERVER AppRotateFiles 1
& $NSSM set $SVC_SERVER AppRotateOnline 1
& $NSSM set $SVC_SERVER AppRotateBytes 10485760

# Graceful shutdown timeouts
& $NSSM set $SVC_SERVER AppStopMethodSkip 0
& $NSSM set $SVC_SERVER AppStopMethodConsole 15000
& $NSSM set $SVC_SERVER AppStopMethodWindow 15000
& $NSSM set $SVC_SERVER AppStopMethodThreads 15000

& $NSSM set $SVC_SERVER AppRestartDelay 30000
& $NSSM set $SVC_SERVER AppEnvironmentExtra `
  "PREFECT_HOME=$PrefectHome" `
  "PREFECT_API_URL=http://127.0.0.1:4200/api" `
  "PREFECT_API_DATABASE_CONNECTION_TIMEOUT=60.0" `
  "PREFECT_SERVER_DATABASE_TIMEOUT=60.0" `
  "PREFECT_SERVER_DATABASE_SQLALCHEMY_POOL_TIMEOUT=60.0" `
  "PREFECT_SERVER_ANALYTICS_ENABLED=False"

sc.exe failure $SVC_SERVER reset= 86400 actions= restart/30000/restart/60000/restart/60000 2>$null
sc.exe failureflag $SVC_SERVER 1 2>$null

Write-Host "[OK]   $SVC_SERVER installed."


# ====================================================================
# SERVICE 2: AamBackupAgent
# ====================================================================
Write-Host ""
Write-Host "[setup] Installing $SVC_AGENT..."

& $NSSM install $SVC_AGENT $UV_EXE
& $NSSM set $SVC_AGENT AppParameters "run python launch.py"
& $NSSM set $SVC_AGENT AppDirectory $ProjectDir
& $NSSM set $SVC_AGENT DisplayName "AAM Backup Agent"
& $NSSM set $SVC_AGENT Description "AAM Backup dashboard (port 8080) and Prefect scheduler"
& $NSSM set $SVC_AGENT Start SERVICE_AUTO_START

# Depends on Prefect server being up first
& $NSSM set $SVC_AGENT DependOnService $SVC_SERVER

& $NSSM set $SVC_AGENT AppStdout "$LogDir\agent_svc.log"
& $NSSM set $SVC_AGENT AppStderr "$LogDir\agent_svc.log"
& $NSSM set $SVC_AGENT AppStdoutCreationDisposition 4
& $NSSM set $SVC_AGENT AppStderrCreationDisposition 4
& $NSSM set $SVC_AGENT AppRotateFiles 1
& $NSSM set $SVC_AGENT AppRotateOnline 1
& $NSSM set $SVC_AGENT AppRotateBytes 10485760

# Graceful shutdown (reduced to 15s to bypass Prefect Ctrl+C hang)
& $NSSM set $SVC_AGENT AppStopMethodConsole 15000
& $NSSM set $SVC_AGENT AppStopMethodWindow 15000
& $NSSM set $SVC_AGENT AppStopMethodThreads 15000

& $NSSM set $SVC_AGENT AppRestartDelay 30000
& $NSSM set $SVC_AGENT AppEnvironmentExtra "PREFECT_HOME=$PrefectHome" "PREFECT_API_URL=http://127.0.0.1:4200/api" "PREFECT_API_DATABASE_CONNECTION_TIMEOUT=60.0"

sc.exe failure $SVC_AGENT reset= 86400 actions= restart/60000/restart/90000/restart/120000 2>$null
sc.exe failureflag $SVC_AGENT 1 2>$null

Write-Host "[OK]   $SVC_AGENT installed."


# ====================================================================
# SERVICE 3: AamWatchdog
# ====================================================================
Write-Host ""
Write-Host "[setup] Installing $SVC_WATCHDOG..."

& $NSSM install $SVC_WATCHDOG $UV_EXE
& $NSSM set $SVC_WATCHDOG AppParameters "run python watchdog.py"
& $NSSM set $SVC_WATCHDOG AppDirectory $ProjectDir
& $NSSM set $SVC_WATCHDOG DisplayName "AAM Backup Watchdog"
& $NSSM set $SVC_WATCHDOG Description "Monitors API health and restarts services if hung"
& $NSSM set $SVC_WATCHDOG Start SERVICE_AUTO_START

& $NSSM set $SVC_WATCHDOG AppStdout "$LogDir\watchdog_svc.log"
& $NSSM set $SVC_WATCHDOG AppStderr "$LogDir\watchdog_svc.log"
& $NSSM set $SVC_WATCHDOG AppStdoutCreationDisposition 4
& $NSSM set $SVC_WATCHDOG AppStderrCreationDisposition 4
& $NSSM set $SVC_WATCHDOG AppRotateFiles 1
& $NSSM set $SVC_WATCHDOG AppRotateOnline 1
& $NSSM set $SVC_WATCHDOG AppRotateBytes 10485760

# Graceful shutdown
& $NSSM set $SVC_WATCHDOG AppStopMethodConsole 15000
& $NSSM set $SVC_WATCHDOG AppStopMethodWindow 15000
& $NSSM set $SVC_WATCHDOG AppStopMethodThreads 15000

& $NSSM set $SVC_WATCHDOG AppRestartDelay 15000
& $NSSM set $SVC_WATCHDOG AppEnvironmentExtra "PREFECT_HOME=$PrefectHome" "PREFECT_API_URL=http://127.0.0.1:4200/api" "PREFECT_API_DATABASE_CONNECTION_TIMEOUT=60.0"

sc.exe failure $SVC_WATCHDOG reset= 86400 actions= restart/15000/restart/30000/restart/30000 2>$null
sc.exe failureflag $SVC_WATCHDOG 1 2>$null

Write-Host "[OK]   $SVC_WATCHDOG installed."


# ====================================================================
# Start all services in dependency order
# ====================================================================
Write-Host ""
Write-Host "[setup] Starting $SVC_SERVER..."
net start $SVC_SERVER
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: Failed to start $SVC_SERVER. This service must be running" -ForegroundColor Red
    Write-Host "  before the others can start." -ForegroundColor Red
}

Write-Host "[setup] Waiting 15 seconds for Prefect API to initialize..."
Start-Sleep -Seconds 15

Write-Host "[setup] Starting $SVC_AGENT..."
net start $SVC_AGENT
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARNING: $SVC_AGENT failed to start. It will retry automatically." -ForegroundColor Yellow
}

Write-Host "[setup] Starting $SVC_WATCHDOG..."
net start $SVC_WATCHDOG
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARNING: $SVC_WATCHDOG failed to start. It will retry automatically." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==================================================================="
Write-Host "  INSTALLATION COMPLETE"
Write-Host "==================================================================="
Write-Host "  Services:    Open services.msc to verify status"
Write-Host "  Prefect UI:  http://localhost:4200"
Write-Host "  Dashboard:   http://localhost:8080"
Write-Host "  Logs:        $LogDir"
Write-Host "==================================================================="
Write-Host ""
