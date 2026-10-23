<#
.SYNOPSIS
    One-command deployment of AAM Backup Automation on a test rig (this box),
    using the SHIPPED deploy scripts, then applies the service account.

.DESCRIPTION
    Must be run from an ADMINISTRATOR PowerShell. Idempotent - safe to re-run.

    What it does, in order:
      1. env prep  (03_setup_system.bat essentials: long paths, no auto-reboot,
                    active hours, Defender exclusions, firewall rules)
      2. gcloud    (downloads the standalone SDK and extracts it with the bundled
                    7za.exe, so 06_install_services.ps1's gcloud check passes)
      3. 04_check_readiness.ps1   (official readiness gate)
      4. 05_test_config.bat       (official config validator)
      5. 06_install_services.ps1  (official NSSM service installer)
      6. 12_configure_gcloud.ps1  (sets CLOUDSDK_PYTHON for the FY rollover)
      7. service account          (setup_service_account.ps1)
      8. starts the services and verifies them

.EXAMPLE
    # dry run: shows what it would do, changes nothing
    .\audit_lab\bootstrap_deployed.ps1 -WhatIf

    # real run, LocalSystem services (default - robocopy /ZB works out of the box)
    .\audit_lab\bootstrap_deployed.ps1

    # real run + dedicated service account (reproduces the /ZB issue, then fixes it)
    .\audit_lab\bootstrap_deployed.ps1 -ServiceAccount ".\aam-backup-svc" -CreateLocalUser
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Account the services will log on as. Default = LocalSystem (augmented - ./style means a local user).
    [string]$ServiceAccount = "LocalSystem",

    # Create a local (non-admin) user with this name - used to REPRODUCE the /ZB exit-16 issue.
    [switch]$CreateLocalUser,

    # Grant SeBackupPrivilege/SeRestorePrivilege/SeServiceLogonRight to the account.
    [switch]$GrantBackupRights,

    # Skip the 120 MB gcloud SDK download (the FY rollover archive step then no-ops).
    [switch]$SkipGcloud,

    # Do not restart services at the end (for staged testing).
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step($m) { Write-Host "`n================ $m ================" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Write-Fix($m)  { Write-Host "  [FIX]  $m" -ForegroundColor Magenta }

# ── 0. admin gate ──────────────────────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "  ELEVATION REQUIRED. Re-run from an Administrator PowerShell:" -ForegroundColor Red
    Write-Host "    Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -File `"$PSCommandPath`"'" -ForegroundColor Yellow
    exit 1
}
Write-Ok "running elevated"

# ── 1. environment prep ────────────────────────────────────────────────────────
Write-Step "1/8  Environment (long paths, reboot policy, Defender, firewall)"
$pairs = @{
    "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled"                     = 1
    "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU\NoAutoRebootWithLoggedOnUsers"= 1
    "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\ISM\ActiveHours\Start"                        = 12
    "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\ISM\ActiveHours\End"                          = 6
    "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\ISM\ActiveHours\ActiveHoursSource"             = 2
}
foreach ($k in $pairs.Keys) {
    $p = Split-Path $k -Parent; $n = Split-Path $k -Leaf
    if (-not (Test-Path $p)) { New-Item -Path $p -Force | Out-Null }
    if ((Get-ItemProperty -Path $p -Name $n -ErrorAction SilentlyContinue).$n -ne $pairs[$k]) {
        if ($PSCmdlet.ShouldProcess($k, "set $n=$($pairs[$k])")) { New-ItemProperty -Path $p -Name $n -Value $pairs[$k] -PropertyType DWord -Force | Out-Null }
    }
}
Write-Ok "registry: long paths + update active hours (12:00-06:00) set"

Add-MpPreference -ExclusionPath $Root -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionProcess "robocopy.exe","rclone.exe" -ErrorAction SilentlyContinue
foreach ($port in @(4200, 8080)) {
    if (-not (Get-NetFirewallRule -DisplayName "AAM*$port*" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "AAM Backup $port" -Direction Inbound -LocalPort $port -Protocol TCP -Action Allow -ErrorAction SilentlyContinue | Out-Null
    }
}
Write-Ok "Defender exclusions + firewall rules for 4200/8080"

# ── 2. gcloud SDK (needed by the installer's gcloud check) ────────────────────
Write-Step "2/8  Google Cloud SDK (isolated)"
$gcloudCmd = Join-Path $Root "deploy\bin\google-cloud-sdk\bin\gcloud.cmd"
if ((Test-Path $gcloudCmd) -or (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Ok "gcloud already available"
} elseif ($SkipGcloud) {
    Write-Warn "skipped - the FY rollover ARCHIVE transition will no-op"
} else {
    $zip = Join-Path $Root "deploy\bin\google-cloud-sdk.zip"
    $url = "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-windows-x86_64.zip"
    # PS 5.1 Invoke-WebRequest stalls badly on large files (the progress renderer eats
    # CPU and the connection dies). Use BITS (native on Server 2016, resumable), with
    # IWR as a fallback. Both are 2016-compatible - no curl.exe dependency.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = 'SilentlyContinue'
    $ok = $false
    try {
        Write-Host "  downloading ~120 MB via BITS (Start-BitsTransfer) ..."
        Start-BitsTransfer -Source $url -Destination $zip -Description "Google Cloud SDK" -ErrorAction Stop
        $ok = (Test-Path $zip)
    } catch {
        Write-Warn "BITS transfer failed ($($_.Exception.Message)) - falling back to Invoke-WebRequest"
    }
    if (-not $ok) {
        Write-Host "  downloading via Invoke-WebRequest ..."
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    }
    $sizeMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    if ((Get-Item $zip).Length -lt 50MB) {
        throw "download looks truncated ($sizeMb MB, expected >50 MB). Delete $zip and re-run, or use -SkipGcloud."
    }
    Write-Host "  downloaded $sizeMb MB - extracting with the bundled 7za ..."
    & (Join-Path $Root "deploy\bin\7za.exe") x -y -o"$(Join-Path $Root 'deploy\bin')" $zip | Out-Null
    if (-not (Test-Path $gcloudCmd)) { throw "gcloud.cmd still missing after extraction" }
    Write-Ok "gcloud extracted to deploy\bin\google-cloud-sdk"
}

# ── 3. official readiness gate ──────────────────────────────────────────────────
Write-Step "3/8  04_check_readiness.ps1 (official gate)"
if ($PSCmdlet.ShouldProcess("readiness gate", "run")) {
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "deploy\04_check_readiness.ps1")
}

# ── 4. official config validator ───────────────────────────────────────────────
Write-Step "4/8  05_test_config.bat (official validator)"
if ($PSCmdlet.ShouldProcess("config", "validate")) {
    & (Join-Path $Root "deploy\05_test_config.bat")
    if ($LASTEXITCODE -ne 0) { throw "config.yaml validation failed - fix it before installing services" }
    Write-Ok "config.yaml valid"
}

# ── 5. official service installer ──────────────────────────────────────────────
Write-Step "5/8  06_install_services.ps1 (official installer)"
if ($PSCmdlet.ShouldProcess("services", "install")) {
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "deploy\06_install_services.ps1")
    foreach ($s in @("AamPrefectServer", "AamBackupAgent", "AamWatchdog")) {
        $sv = Get-Service -Name $s -ErrorAction SilentlyContinue
        Write-Host ("  {0,-18} {1}" -f $s, $(if ($sv) { $sv.Status } else { "MISSING" }))
    }
}

# ── 6. gcloud python wiring ────────────────────────────────────────────────────
Write-Step "6/8  12_configure_gcloud.ps1 -RestartServices"
if ((Test-Path $gcloudCmd) -and $PSCmdlet.ShouldProcess("gcloud", "configure CLOUDSDK_PYTHON")) {
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "deploy\12_configure_gcloud.ps1") -RestartServices
}

# ── 7. service account ─────────────────────────────────────────────────────────
Write-Step "7/8  Service account: $ServiceAccount"
$svcAcct = $ServiceAccount
if ($CreateLocalUser -and $ServiceAccount -like ".\*") {
    $user = $ServiceAccount.Substring(2)
    if (-not (Get-LocalUser -Name $user -ErrorAction SilentlyContinue)) {
        Write-Fix "creating local user $user (standard, non-admin)"
        $pw = ConvertTo-SecureString ("Aam-" + [guid]::NewGuid().ToString("N").Substring(0, 12)) -AsPlainText -Force
        New-LocalUser -Name $user -Description "AAM backup service account" -Password $pw -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
        Add-LocalGroupMember -Group "Users" -Member $user
        Write-Ok "created $user (record its password from the vault/ADM)"
    } else { Write-Ok "local user $user already exists" }
    $svcAcct = ".\$user"
}
if ($svcAcct -ne "LocalSystem" -and $PSCmdlet.ShouldProcess("service account", "apply")) {
    $acctPw = Read-Host "  password for $svcAcct" -AsSecureString
    & (Join-Path $PSScriptRoot "setup_service_account.ps1") -AccountName $svcAcct -Credential (
        New-Object System.Management.Automation.PSCredential($svcAcct, $acctPw)) -Apply -SkipRestart
}

# ── 8. start + verify ──────────────────────────────────────────────────────────
Write-Step "8/8  Start services and verify"
if (-not $NoStart) {
    Start-Service -Name "AamPrefectServer" -ErrorAction SilentlyContinue
    Write-Host "  waiting 15s for the Prefect API..."
    Start-Sleep -Seconds 15
    Start-Service -Name "AamBackupAgent" -ErrorAction SilentlyContinue
    Start-Service -Name "AamWatchdog" -ErrorAction SilentlyContinue
}
foreach ($s in @("AamPrefectServer", "AamBackupAgent", "AamWatchdog")) {
    $sv = Get-Service -Name $s -ErrorAction SilentlyContinue
    $acct = (& (Join-Path $Root "deploy\bin\nssm.exe") get $s ObjectName 2>$null)
    Write-Host ("  {0,-18} {1,-9} logon={2}" -f $s, $(if ($sv) { $sv.Status } else { "MISSING" }), $acct)
}
for ($i = 0; $i -lt 12; $i++) {
    try { $null = Invoke-RestMethod "http://127.0.0.1:4200/api/health" -TimeoutSec 5; Write-Ok "Prefect API healthy"; break }
    catch { Start-Sleep -Seconds 5 }
}
try {
    $s = Invoke-RestMethod "http://127.0.0.1:8080/health" -TimeoutSec 5
    Write-Ok "Dashboard /health responds: $($s | ConvertTo-Json -Compress)"
} catch { Write-Warn "dashboard /health did not respond: $($_.Exception.Message)" }

@"

NEXT: run the deployed-version test suite (real services, real Prefect, real files):

    .\.venv\Scripts\python.exe -m pytest audit_lab/test_w7_deployed.py -q -s --timeout=1800

That suite triggers backups through the DEPLOYMENT API (exactly like the dashboard
button) and verifies the LAN copy, the real GCS objects, run_history, the Prefect
console, and the watchdog.
"@ | Write-Host
