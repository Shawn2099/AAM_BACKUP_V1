# Usage:  06_install_services.ps1 [-ServiceAccount .\aambackup]
#   -ServiceAccount  local account AamBackupAgent runs as (plan §10 item 2: in a
#                    WORKGROUP, LocalSystem has no usable credentials on the NAS).
#                    The password is asked for interactively, never passed on the
#                    command line. Omit it to keep LocalSystem (NOT for production).
#   -KeepServiceAccount  upgrade without the account password: AamBackupAgent keeps the account and
#                    the password Windows already holds for it (the service is updated in place, not
#                    removed). Fails if the agent does not run as a dedicated account yet.
#   Run 04_check_readiness.ps1 -ServiceAccount <same> first: it proves the account
#   can reach the NAS, read the source and write the runtime folder.
param(
    [string]$ServiceAccount = "",
    [string]$PasswordFile = "",
    [switch]$KeepServiceAccount
)

. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Definition) "_common.ps1")
Assert-Administrator

# -PasswordFile (unattended install): a file holding the password as a DPAPI
# secure string (ConvertFrom-SecureString) made by THIS Windows user on THIS
# machine - nobody else can decrypt it. It is deleted as soon as it is read.
function Read-PasswordFile([string]$Path) {
    try {
        # Set-Content ends the file with a line break, which ConvertTo-SecureString
        # rejects ("Input string was not in a correct format") - trim it
        $sec = (Get-Content -Path $Path -Raw -ErrorAction Stop).Trim() | ConvertTo-SecureString -ErrorAction Stop
    } finally {
        Remove-Item -Path $Path -Force -ErrorAction SilentlyContinue
    }
    return $sec
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

# (uv is NOT needed here: 04_check_readiness.ps1 creates .venv with
#  `uv sync --frozen`; the services then run .venv's Python directly.)

# -- Read runtime_dir from config.yaml via Python ---------------------
$ConfigFile   = Join-Path $ProjectDir "config.yaml"
$DefaultRuntime = "C:\BackupAgent"
$ReadConfigPy = Join-Path $DeployDir "read_config.py"

function Read-ConfigValue {
    param([string]$Key, [string]$Default)
    try {
        $orig = Get-Location
        Set-Location $ProjectDir
        $result = & $VenvPython $ReadConfigPy $ConfigFile $Key --default $Default 2>$null
        Set-Location $orig
        if ($LASTEXITCODE -eq 0 -and $result) { return $result }
    } catch {
        if ($orig) { Set-Location $orig }
    }
    return $Default
}

# WP16: services start the project's venv Python directly - no `uv run` at boot,
# so a boot never syncs packages from the internet (Sophos) and never depends on
# uv.exe living in an installer's user profile. uv is only used to install.
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host ""
    Write-Host "  ERROR: $VenvPython not found." -ForegroundColor Red
    Write-Host "  Run 04_check_readiness.ps1 first (it runs 'uv sync --frozen')."
    Write-Host ""
    exit 1
}

$RuntimeDir = Read-ConfigValue "paths.runtime_dir" $DefaultRuntime
Set-InstallEnvironment $RuntimeDir
$BackupRoot = $RuntimeDir
$LogDir     = Join-Path $BackupRoot "logs"
$PrefectHome = Join-Path $BackupRoot ".prefect"

$SVC_SERVER   = "AamPrefectServer"
$SVC_AGENT    = "AamBackupAgent"
$SVC_WATCHDOG = "AamWatchdog"

# (the Google Cloud SDK is checked by 04_check_readiness.ps1, which must be green before this script)

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

# -- Validate the config BEFORE touching the services (WP14/WP16) -------
# Strict validation incl. the schedule/run-budget check: a config the new code
# refuses would otherwise crash-loop all three services after install.
$env:AAM_CONFIG = $ConfigFile
Push-Location $ProjectDir
$cfgCheck = & $VenvPython -c "from models.config import load_config; load_config(r'$ConfigFile')" 2>&1
$cfgExit = $LASTEXITCODE
Pop-Location
if ($cfgExit -ne 0) {
    Write-Host ""
    Write-Host "  ERROR: $ConfigFile is not valid for this version:" -ForegroundColor Red
    $cfgCheck | Select-Object -Last 15 | ForEach-Object { Write-Host "    $_" }
    Write-Host "  Fix config.yaml, then re-run this installer. Services were NOT changed."
    Write-Host ""
    exit 1
}

# The dashboard address for the firewall rule is read NOW, before any service is touched: a failure here
# used to happen after the services were installed, with no firewall rule, so the office could not reach it.
Push-Location $ProjectDir
$dashInfo = & $VenvPython -c "from models.config import load_config; d=load_config(r'$ConfigFile').dashboard; print(d.bind_address); print(d.port); print(','.join(d.firewall_remote_addresses))"
$dashExit = $LASTEXITCODE
Pop-Location
if ($dashExit -ne 0 -or @($dashInfo).Count -lt 3) {
    Write-Host "  ERROR: could not read the dashboard settings from $ConfigFile. Services were NOT changed." -ForegroundColor Red
    exit 1
}
$dashBind = $dashInfo[0]; $dashPort = $dashInfo[1]; $dashRemote = $dashInfo[2] -split ","

# -- Dashboard passwords (plan E4) ------------------------------------
# The dashboard opens only when BOTH passwords exist (fail closed). Ask here, at the server
# console, for the ones that have no hash yet; they are typed twice, hidden, and stored as
# salted hashes. Later changes: Settings > Advanced > Passwords, or 11_reset_dashboard_passwords.ps1.
Push-Location $ProjectDir
& $VenvPython (Join-Path $ProjectDir "deploy\set_dashboard_passwords.py") --config $ConfigFile --only-missing
$pwExit = $LASTEXITCODE
Pop-Location
if ($pwExit -ne 0) {
    Write-Host "  ERROR: the dashboard passwords were not set. Re-run this installer. Services were NOT changed." -ForegroundColor Red
    exit 1
}

# -- Mail password (plan S5) ------------------------------------------
# A plain notifications.smtp_password is encrypted with Windows DPAPI (machine scope: the agent,
# the watchdog and this installer can all read it; a copy of config.yaml elsewhere cannot).
# The Google key file stays plain on purpose (owner decision); lock_gcs_key.ps1 restricts it.
Push-Location $ProjectDir
& $VenvPython (Join-Path $ProjectDir "deploy\encrypt_secrets.py") --config $ConfigFile
$secExit = $LASTEXITCODE
Pop-Location
if ($secExit -ne 0) {
    Write-Host "  ERROR: the mail password was not encrypted. Re-run this installer. Services were NOT changed." -ForegroundColor Red
    exit 1
}

# -- Service account (agent only) -------------------------------------
# AamBackupAgent touches the NAS, so it runs as the dedicated account.
# AamPrefectServer (local API) and AamWatchdog (must stop/start services,
# which a plain account cannot) stay LocalSystem.
$AgentPassword = $null
if ($KeepServiceAccount) {
    $current = (Get-CimInstance Win32_Service -Filter "Name='AamBackupAgent'" -ErrorAction SilentlyContinue).StartName
    if (-not $current -or $current -match '^(LocalSystem|NT AUTHORITY\\)') {
        Write-Host "  ERROR: -KeepServiceAccount: AamBackupAgent does not run as a dedicated account ('$current')." -ForegroundColor Red
        Write-Host "  Install it once with -ServiceAccount .\<account>. Services were NOT changed." -ForegroundColor Red
        exit 1
    }
    $ServiceAccount = $current
    Write-Host "[OK]   Keeping AamBackupAgent's account $ServiceAccount (password stays with Windows)"
}
if ($ServiceAccount) {
    try {
        if (-not $KeepServiceAccount) {
            $sec = if ($PasswordFile) { Read-PasswordFile $PasswordFile } else { Read-Host -AsSecureString "Password for $ServiceAccount" }
            $AgentPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        }
    } catch {
        $AgentPassword = $null
    }
    if (-not $AgentPassword -and -not $KeepServiceAccount) {
        Write-Host "  ERROR: no password for $ServiceAccount could be read. Services were NOT changed." -ForegroundColor Red
        exit 1
    }
    # Same function 04_check_readiness.ps1 used before its probe (runtime folder, project folder, config.yaml)
    if (-not (Grant-AgentRights $RuntimeDir $ProjectDir $ConfigFile $ServiceAccount)) {
        Write-Host "  ERROR: could not give $ServiceAccount Modify on $RuntimeDir / $ConfigFile. Services were NOT changed." -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK]   $ServiceAccount granted Modify on $RuntimeDir, and on $ConfigFile + its folder"
} else {
    Write-Host "[WARN] No -ServiceAccount: AamBackupAgent runs as LocalSystem, which cannot" -ForegroundColor Yellow
    Write-Host "       authenticate to a NAS in a WORKGROUP (plan §10 item 2)." -ForegroundColor Yellow
}

# -- Google key: readable only by SYSTEM, Administrators and the service account --------------
# (the box inherited BUILTIN\Users:Read from C:\, so any local account could read the key)
$GcsKeyPath = Read-ConfigValue "paths.gcs_key_path" ""
if ($GcsKeyPath) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $DeployDir "lock_gcs_key.ps1") -KeyPath $GcsKeyPath -Account $ServiceAccount
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: the Google key could not be locked down. Services were NOT changed." -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "==================================================================="
Write-Host "  AAM Backup Automation V1 - SERVICE INSTALLER"
Write-Host "==================================================================="
Write-Host "  NSSM:         $NSSM"
Write-Host "  Python:       $VenvPython (services run it directly - no uv at boot)"
Write-Host "  Config:       $ConfigFile (AAM_CONFIG)"
Write-Host "  Agent runs as: $(if ($ServiceAccount) { $ServiceAccount } else { 'LocalSystem' })"
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
if (-not $KeepServiceAccount) {
    # (kept: removing the service would drop the account password Windows holds for it)
    & $NSSM remove $SVC_AGENT confirm 2>$null
}
& $NSSM remove $SVC_SERVER confirm 2>$null

Start-Sleep -Seconds 3

# ====================================================================
# SERVICE 1: AamPrefectServer
# ====================================================================
Write-Host ""
Write-Host "[setup] Installing $SVC_SERVER..."

& $NSSM install $SVC_SERVER $VenvPython
& $NSSM set $SVC_SERVER AppParameters "-m prefect server start --host 127.0.0.1 --port 4200"
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
# F4: online rotation DISABLED. NSSM's online-rotation pipe-reader thread
# blocks service stop (STOP_PENDING with children already exited; reproduced
# on the isolated rig with the identical nssm binary + config pattern).
# Restart-time rotation (AppRotateFiles + AppRotateBytes) is retained.
& $NSSM set $SVC_SERVER AppRotateOnline 0
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
  "PREFECT_SERVER_ANALYTICS_ENABLED=False" `
  "PREFECT_SERVER_CONCURRENCY_LEASE_STORAGE=prefect.server.concurrency.lease_storage.filesystem" `
  "AAM_CONFIG=$ConfigFile"

sc.exe failure $SVC_SERVER reset= 86400 actions= restart/30000/restart/60000/restart/60000 2>$null
sc.exe failureflag $SVC_SERVER 1 2>$null

Write-Host "[OK]   $SVC_SERVER installed."


# ====================================================================
# SERVICE 2: AamBackupAgent
# ====================================================================
Write-Host ""
Write-Host "[setup] Installing $SVC_AGENT..."

if (-not $KeepServiceAccount) {
    & $NSSM install $SVC_AGENT $VenvPython
} else {
    & $NSSM set $SVC_AGENT Application $VenvPython
}
& $NSSM set $SVC_AGENT AppParameters "launch.py"
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
# F4: online rotation DISABLED (same STOP_PENDING root cause as above).
# Restart-time rotation retained.
& $NSSM set $SVC_AGENT AppRotateOnline 0
& $NSSM set $SVC_AGENT AppRotateBytes 10485760

# Graceful shutdown (F4): up to 180s maximum grace via the Console (Ctrl+C)
# stop method so Prefect serve() can cancel in-flight runs and the flow can
# release backup.lock. Window/Threads are no-ops for headless Session 0
# console daemons, so they are skipped (Skip=6: 2=Window + 4=Threads).
# The final TerminateProcess fallback is deliberately retained (bit 8 NOT
# skipped): clean exits stop immediately, stuck trees are still reaped.
& $NSSM set $SVC_AGENT AppStopMethodConsole 180000
& $NSSM set $SVC_AGENT AppStopMethodWindow 0
& $NSSM set $SVC_AGENT AppStopMethodThreads 0
& $NSSM set $SVC_AGENT AppStopMethodSkip 6

& $NSSM set $SVC_AGENT AppRestartDelay 30000
& $NSSM set $SVC_AGENT AppEnvironmentExtra "PREFECT_HOME=$PrefectHome" "PREFECT_API_URL=http://127.0.0.1:4200/api" "PREFECT_API_DATABASE_CONNECTION_TIMEOUT=60.0" "AAM_CONFIG=$ConfigFile"
if ($ServiceAccount) {
    # NSSM grants the account "Log on as a service" when it sets ObjectName
    if (-not $KeepServiceAccount) {
        & $NSSM set $SVC_AGENT ObjectName $ServiceAccount $AgentPassword
    }
    $AgentPassword = $null
    # Read it back from Windows - never report an account that was not applied
    # (trial box 2026-10-15: a bad password left the agent on LocalSystem while
    # the old code printed [OK]).
    $actual = (Get-CimInstance Win32_Service -Filter "Name='$SVC_AGENT'").StartName
    $wanted = ($ServiceAccount -split '\\')[-1]
    if (-not $actual -or $actual -notmatch ('(^|\\)' + [regex]::Escape($wanted) + '$')) {
        Write-Host "  ERROR: $SVC_AGENT still runs as '$actual', not $ServiceAccount (wrong password?)." -ForegroundColor Red
        Write-Host "  Services were installed but NOT started. Fix the password and re-run this installer." -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK]   $SVC_AGENT runs as $actual (checked with Windows)"
}

sc.exe failure $SVC_AGENT reset= 86400 actions= restart/60000/restart/90000/restart/120000 2>$null
sc.exe failureflag $SVC_AGENT 1 2>$null

Write-Host "[OK]   $SVC_AGENT installed."


# ====================================================================
# SERVICE 3: AamWatchdog
# ====================================================================
Write-Host ""
Write-Host "[setup] Installing $SVC_WATCHDOG..."

& $NSSM install $SVC_WATCHDOG $VenvPython
& $NSSM set $SVC_WATCHDOG AppParameters "watchdog.py"
& $NSSM set $SVC_WATCHDOG AppDirectory $ProjectDir
& $NSSM set $SVC_WATCHDOG DisplayName "AAM Backup Watchdog"
& $NSSM set $SVC_WATCHDOG Description "Monitors API health and restarts services if hung"
& $NSSM set $SVC_WATCHDOG Start SERVICE_AUTO_START

& $NSSM set $SVC_WATCHDOG AppStdout "$LogDir\watchdog_svc.log"
& $NSSM set $SVC_WATCHDOG AppStderr "$LogDir\watchdog_svc.log"
& $NSSM set $SVC_WATCHDOG AppStdoutCreationDisposition 4
& $NSSM set $SVC_WATCHDOG AppStderrCreationDisposition 4
& $NSSM set $SVC_WATCHDOG AppRotateFiles 1
# F4: online rotation DISABLED (same STOP_PENDING root cause as above).
# Restart-time rotation retained.
& $NSSM set $SVC_WATCHDOG AppRotateOnline 0
& $NSSM set $SVC_WATCHDOG AppRotateBytes 10485760

# Graceful shutdown
& $NSSM set $SVC_WATCHDOG AppStopMethodConsole 15000
& $NSSM set $SVC_WATCHDOG AppStopMethodWindow 15000
& $NSSM set $SVC_WATCHDOG AppStopMethodThreads 15000

& $NSSM set $SVC_WATCHDOG AppRestartDelay 15000
& $NSSM set $SVC_WATCHDOG AppEnvironmentExtra "PREFECT_HOME=$PrefectHome" "PREFECT_API_URL=http://127.0.0.1:4200/api" "PREFECT_API_DATABASE_CONNECTION_TIMEOUT=60.0" "AAM_CONFIG=$ConfigFile"

sc.exe failure $SVC_WATCHDOG reset= 86400 actions= restart/15000/restart/30000/restart/30000 2>$null
sc.exe failureflag $SVC_WATCHDOG 1 2>$null

Write-Host "[OK]   $SVC_WATCHDOG installed."


# ====================================================================
# Start all services in dependency order
# ====================================================================
function Wait-Http([string]$Url, [int]$Seconds) {
    # any HTTP answer (even 503 "needs attention", normal before the first backup) = the server is up
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try { Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 | Out-Null; return $true }
        catch { if ($_.Exception.Response) { return $true } }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    return $false
}
$startFailures = @()

Write-Host ""
Write-Host "[setup] Starting $SVC_SERVER..."
net start $SVC_SERVER
if ($LASTEXITCODE -ne 0) { $startFailures += "$SVC_SERVER did not start" }

Write-Host "[setup] Waiting for the Prefect API (up to 3 minutes)..."
if (-not (Wait-Http "http://127.0.0.1:4200/api/health" 180)) { $startFailures += "the Prefect API did not answer on port 4200" }

Write-Host "[setup] Starting $SVC_AGENT..."
net start $SVC_AGENT
if ($LASTEXITCODE -ne 0) { $startFailures += "$SVC_AGENT did not start" }

Write-Host "[setup] Starting $SVC_WATCHDOG..."
net start $SVC_WATCHDOG
if ($LASTEXITCODE -ne 0) { $startFailures += "$SVC_WATCHDOG did not start" }

# ====================================================================
# DASHBOARD FIREWALL RULE (owner 2026-10-18: office network only)
# ====================================================================
# Opens ONLY the dashboard port, ONLY to dashboard.firewall_remote_addresses
# (default LocalSubnet). Prefect's API (4200, no login) stays on 127.0.0.1 and
# is never opened. Re-running the installer replaces the rule.
$FwRuleName = "AAM Backup Dashboard"
Get-NetFirewallRule -DisplayName $FwRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
# An older 03_setup_system.bat opened Prefect's port (4200, any address); that rule must not survive.
Get-NetFirewallRule -DisplayName "AAM Backup Prefect" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($dashBind -in @("127.0.0.1", "localhost", "::1")) {
    Write-Host "[OK]   Dashboard only on this server ($dashBind) - no firewall rule needed"
} else {
    New-NetFirewallRule -DisplayName $FwRuleName -Direction Inbound -Protocol TCP -LocalPort $dashPort -RemoteAddress $dashRemote -Action Allow -Profile Any | Out-Null
    if (-not $?) {
        Write-Host "  ERROR: could not create the firewall rule '$FwRuleName'." -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK]   Firewall: dashboard port $dashPort open to $($dashRemote -join ', ') only"
}

# -- Verify what the banner below claims (Windows' own view, not the exit codes of net start) -------
foreach ($svc in @($SVC_SERVER, $SVC_AGENT, $SVC_WATCHDOG)) {
    $st = (Get-Service $svc -ErrorAction SilentlyContinue)
    if (-not $st -or $st.Status -ne "Running") { $startFailures += "$svc is not Running" }
}
$probeHost = if ($dashBind -in @("0.0.0.0", "::", "")) { "127.0.0.1" } else { $dashBind }
Write-Host "[setup] Waiting for the dashboard on port $dashPort (up to 3 minutes)..."
if (-not (Wait-Http "http://${probeHost}:$dashPort/health" 180)) { $startFailures += "the dashboard did not answer on port $dashPort" }
if ($startFailures.Count -gt 0) {
    Write-Host ""
    Write-Host "===================================================================" -ForegroundColor Red
    Write-Host "  INSTALL FAILED - the system is NOT running" -ForegroundColor Red
    $startFailures | Select-Object -Unique | ForEach-Object { Write-Host "   - $_" -ForegroundColor Red }
    Write-Host "  Look at the logs in $LogDir (agent_svc.log, prefect_svc.log), fix the cause, run this script again."
    Write-Host "===================================================================" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "==================================================================="
Write-Host "  INSTALLATION COMPLETE (3 services running, Prefect API and dashboard answered)"
Write-Host "==================================================================="
Write-Host "  Dashboard:   http://<this server>:$dashPort  - open it from an office PC with Chrome or Edge"
Write-Host "  Prefect UI:  http://localhost:4200 (this server only)"
Write-Host "  Logs:        $LogDir"
Write-Host "==================================================================="
Write-Host ""
