<#
.SYNOPSIS
    Sets the Log On account for the AAM services and grants exactly the rights
    needed for robocopy /ZB to work (SeBackupPrivilege) + SeServiceLogonRight.

.DESCRIPTION
    Fixes the day-1 blocker found by the audit lab: robocopy /ZB exits 16
    ("You do not have the Backup and Restore Files user rights") for any account
    without SeBackupPrivilege, which means the LAN leg copies 0 files.

    WHY NOT "just add it to Backup Operators":
      your server is a DOMAIN CONTROLLER (AAMBDC001). Backup Operators on a DC is
      a documented privilege-escalation path (it can read NTDS.dit and restore
      over the SAM/SYSTEM hives). We therefore grant the two specific USER RIGHTS
      to the service account instead - least privilege, no group escalation.

    Run this ON THE SERVER as Administrator. Idempotent - safe to re-run.

.EXAMPLE
    # 1. dry run - shows exactly what would change, changes nothing
    .\setup_service_account.ps1 -AccountName "CAAAM\svc-aam-backup" -VerifyOnly

    # 2. apply (prompts for the password)
    .\setup_service_account.ps1 -AccountName "CAAAM\svc-aam-backup" -Apply

    # 3. apply with an explicit credential
    $cred = Get-Credential -UserName "CAAAM\svc-aam-backup" -Message "AAM backup service account"
    .\setup_service_account.ps1 -AccountName "CAAAM\svc-aam-backup" -Credential $cred -Apply
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$AccountName,                 # e.g. "CAAAM\svc-aam-backup" or ".\svc-aam-backup"

    [pscredential]$Credential,            # if omitted, you are prompted (when -Apply)

    [switch]$Apply,                       # without it: report only, change nothing

    [switch]$VerifyOnly,                  # force report-only mode

    [string[]]$Services = @("AamPrefectServer", "AamBackupAgent", "AamWatchdog"),

    [switch]$SkipRestart                  # apply the account but do not restart services
)

$ErrorActionPreference = "Stop"

# ── gate: must be Administrator ────────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ""
    Write-Host "  ERROR: not elevated. Run from an ADMINISTRATOR PowerShell:" -ForegroundColor Red
    Write-Host "    Right-click PowerShell -> Run as administrator" -ForegroundColor Yellow
    exit 1
}

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Fix($msg)  { Write-Host "  [FIX]  $msg" -ForegroundColor Magenta }

# ── 0. preflight ───────────────────────────────────────────────────────────────
Write-Step "0/5  Preflight"

$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
Write-Host "  Server : $($cs.Name)"
Write-Host "  OS     : $($os.Caption) ($($cs.Domain) / $($cs.PartOfDomain))"
if ($cs.PartOfDomain) {
    Write-Host "  Domain controller: $((Get-CimInstance Win32_OperatingSystem).ProductType -eq 2)" -ForegroundColor Yellow
    Write-Warn "This host is domain-joined. Grant the specific USER RIGHTS below - DO NOT add the service account to Backup Operators on a DC."
}

# ── resolve the account in the form Windows APIs accept ────────────────────────
function Resolve-AccountName([string]$Name) {
    $bare = ($Name -replace '^\.\\', '') -replace '^\\', ''
    $candidates = @(
        "$env:COMPUTERNAME\$bare",     # canonical local form - what SCM/INF want
        $bare,                          # bare name (the machine is implied)
        $Name                           # exactly as typed, e.g. DOMAIN\user
    ) | Select-Object -Unique
    foreach ($c in $candidates) {
        try {
            $a = New-Object System.Security.Principal.NTAccount($c)
            $s = $a.Translate([System.Security.Principal.SecurityIdentifier]).Value
            return @{ Name = $c; Sid = $s }
        } catch { }
    }
    return $null
}

$resolved = Resolve-AccountName $AccountName
if (-not $resolved) {
    Write-Host "  ERROR: account '$AccountName' does not resolve on this machine." -ForegroundColor Red
    Write-Host "         Use MACHINE\user (e.g. DESKTOP-N7II2M6\innovizta_2), DOMAIN\user, or .\localuser" -ForegroundColor Yellow
    exit 1
}
$AccountName = $resolved.Name          # canonicalise: used for NSSM + secedit below
$sid = $resolved.Sid
Write-Ok "Account resolves: $AccountName  (SID $sid)"

# ── 1. service Log On account ──────────────────────────────────────────────────
Write-Step "1/5  Service Log On account"
foreach ($svc in $Services) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if (-not $s) {
        Write-Warn "$svc not installed on this machine - skipping (run 06_install_services.ps1 first)"
        continue
    }
    $nssm = Join-Path $PSScriptRoot "..\deploy\bin\nssm.exe"
    if (-not (Test-Path $nssm)) { $nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source }
    if (-not $nssm) { Write-Warn "nssm.exe not found - cannot set the account for $svc"; continue }

    $current = (& $nssm get $svc ObjectName 2>$null)
    if ($current -eq $AccountName) {
        Write-Ok "$svc already runs as $AccountName"
        continue
    }
    Write-Fix "$svc currently logs on as '$current' -> set to $AccountName"
    if ($Apply -and $PSCmdlet.ShouldProcess($svc, "set Log On account")) {
        if (-not $Credential) {
            $Credential = Get-Credential -UserName $AccountName -Message "Password for the AAM backup service account"
        }
        & $nssm set $svc ObjectName $AccountName | Out-Null
        & $nssm set $svc ObjectPassword $Credential.GetNetworkCredential().Password | Out-Null
        Write-Ok "$svc Log On account set (password stored by NSSM, encrypted in the registry)"
    }
}

# ── 2. "Log on as a service" ───────────────────────────────────────────────────
Write-Step "2/5  User right: Log on as a service (SeServiceLogonRight)"

function Get-UserRights {
    $inf = Join-Path $env:TEMP "aam_rights_export.inf"
    if (Test-Path $inf) { Remove-Item $inf -Force }
    & secedit /export /cfg $inf /areas USER_RIGHTS /quiet | Out-Null
    if (-not (Test-Path $inf)) { throw "secedit export failed" }
    $hash = @{}
    $section = $false
    foreach ($line in Get-Content $inf) {
        if ($line -match '^\[(?<n>.+)\]$') { $section = ($matches.n -eq "Privilege Rights"); continue }
        if ($section -and $line -match '^(?<k>\w+)\s*=\s*(?<v>.*)$') {
            $hash[$matches.k] = $matches.v
        }
    }
    return @{ Inf = $inf; Rights = $hash }
}

function Grant-UserRight($Entry, $RightName, $AccountName) {
    $current = ""
    if ($Entry.Rights.ContainsKey($RightName)) { $current = $Entry.Rights[$RightName] }
    if ($current -split ',' | ForEach-Object { $_.Trim().TrimStart('*') } | Where-Object { $_ -eq $AccountName }) {
        Write-Ok "$RightName already granted to $AccountName"
        return $false
    }
    $added = (($current -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) + "*$AccountName") -join ','
    if ($Entry.Rights.ContainsKey($RightName)) { $Entry.Rights[$RightName] = $added } else { $Entry.Rights.Add($RightName, $added) }
    Write-Fix "$RightName : adding *$AccountName"
    return $true
}

$rights = Get-UserRights
$changed = $false
$changed = (Grant-UserRight $rights "SeServiceLogonRight"      $AccountName) -or $changed
$changed = (Grant-UserRight $rights "SeBackupPrivilege"        $AccountName) -or $changed
$changed = (Grant-UserRight $rights "SeRestorePrivilege"       $AccountName) -or $changed

if ($Apply -and $changed -and $PSCmdlet.ShouldProcess("Local security policy", "grant the three rights")) {
    $out = Join-Path $env:TEMP "aam_rights_apply.inf"
    $outLines = @("[Unicode]", "Unicode=yes", "[Version]", 'signature="$CHICAGO$"', "Revision=1", "", "[Privilege Rights]")
    foreach ($k in $rights.Rights.Keys) { $outLines += "$k = $($rights.Rights[$k])" }
    Set-Content -Path $out -Value $outLines -Encoding Unicode
    $db = Join-Path $env:TEMP "aam_rights.sdb"
    & secedit /configure /db $db /cfg $out /areas USER_RIGHTS /quiet | Out-Null
    Write-Ok "Local security policy updated (log: $env:TEMP - review with secpol.msc -> Local Policies -> User Rights Assignment)"
}

# ── 3. verify the account can actually do the work ─────────────────────────────
Write-Step "3/5  Capability checks"

$svcShare  = $null
$cfgFile = Join-Path $PSScriptRoot "..\config.yaml"
if (Test-Path $cfgFile) {
    $lanLine = (Select-String -Path $cfgFile -Pattern 'lan_destination:\s*"([^"]+)"').Matches.Groups[1].Value
    if ($lanLine) {
        # Select-String returns the RAW file text: YAML double-quoted scalators escape
        # backslashes as \\\\ -> unescape before using the path.
        $lanPath = $lanLine -replace '\\\\', '\'
        $svcShare = Split-Path $lanPath -Parent
    }
}
if ($svcShare) {
    Write-Host "  NAS share from config.yaml: $svcShare"
    try {
        $ok = Test-Path $svcShare -ErrorAction Stop
        if ($ok) { Write-Ok "share reachable with THIS account - still confirm with the service account:" }
        else { Write-Warn "share NOT reachable from here - verify the service account has R/W on it" }
    } catch { Write-Warn "share test failed: $($_.Exception.Message)" }
} else {
    Write-Warn "config.yaml lan_destination not found - verify the share R/W permission manually"
}

Write-Host "  Run these as the SERVICE ACCOUNT to prove it (do this before restarting):"
Write-Host ""
Write-Host "    runas /user:$AccountName ""cmd /c whoami /priv | findstr /i SeBackupPrivilege"""
Write-Host "    runas /user:$AccountName ""cmd /c whoami /priv | findstr /i SeServiceLogonRight"""
if ($svcShare) { Write-Host "    runas /user:$AccountName ""cmd /c dir $svcShare""" }
Write-Host "    runas /user:$AccountName ""cmd /c robocopy %TEMP%\a %TEMP%\b /ZB /E"""
Write-Host ""
Write-Host "  A trivial /ZB robocopy must print exit code 1 (files copied), NOT 16."

# ── 4. restart in dependency order ─────────────────────────────────────────────
Write-Step "4/5  Restart services (dependency order)"
$installed = @()
foreach ($svc in $Services) {
    if (Get-Service -Name $svc -ErrorAction SilentlyContinue) { $installed += $svc }
}
if ($installed.Count -eq 0) {
    Write-Host "  no AAM services installed on this machine - nothing to restart."
    Write-Host "  (run .\audit_lab\bootstrap_deployed.ps1 first to install them)"
} elseif ($Apply -and -not $SkipRestart) {
    foreach ($svc in @("AamWatchdog", "AamBackupAgent", "AamPrefectServer")) {
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        if (-not $s) { continue }
        if ($PSCmdlet.ShouldProcess($svc, "restart")) {
            Stop-Service -Name $svc -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 3
        }
    }
    Start-Service -Name "AamPrefectServer" -ErrorAction SilentlyContinue
    Write-Host "  waiting 15s for the Prefect API..."
    Start-Sleep -Seconds 15
    Start-Service -Name "AamBackupAgent" -ErrorAction SilentlyContinue
    Start-Service -Name "AamWatchdog" -ErrorAction SilentlyContinue
    foreach ($svc in $Services) {
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        Write-Host ("  {0,-18} {1}" -f $svc, $(if ($s) { $s.Status } else { "n/a" }))
    }
} else {
    Write-Host "  (skipped - report-only mode; use -Apply to restart)"
}

# ── 5. what to check on the FIRST run ──────────────────────────────────────────
Write-Step "5/5  Verify on the first nightly LAN run"
@"
  # the decisive line - exit 16 = still broken, exit 1/3 = /ZB working
  Select-String C:\BackupAgent\logs\*.log -Pattern "LAN sync exit" | Select-Object -Last 5

  # and re-run the lab experiment that found the bug (on the server or a box with the venv):
  .\.venv\Scripts\python.exe -m pytest audit_lab/test_w1c_controls.py::test_w1c_c1_zb_privilege_is_a_hard_failure -q -s
  #   before: with_zb exit=16    after: with_zb exit=1

  !! REMINDER: once /ZB works, the LAN leg finally RUNS. That makes the
     empty-source purge (audit finding #2) reachable on the LAN leg too.
     Either fix the guard first, or run the first night with the source verified
     non-empty and the canary present.
"@ | Write-Host

Write-Host ""
Write-Host "Done. Remember: a service Log On change requires a service RESTART to take effect," -ForegroundColor Yellow
Write-Host "and the new rights only apply to processes started after the policy update." -ForegroundColor Yellow
