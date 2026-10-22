<#
.SYNOPSIS
  Reset the dashboard password and/or the Advanced password (forgotten password).
.DESCRIPTION
  Run as administrator on the server. Asks for the new password(s) (typed twice, hidden), stores
  salted hashes in config.yaml through the locked config store, and signs everyone out: the
  agent notices the changed hash on its next config refresh (dashboard.config_refresh_seconds).
  Services are not restarted. Plan E7.
.EXAMPLE
  .\11_reset_dashboard_passwords.ps1                # both
  .\11_reset_dashboard_passwords.ps1 -Which login   # only the dashboard password
#>
param(
    [ValidateSet("both", "login", "admin")][string]$Which = "both",
    [string]$ConfigFile = ""
)
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "_common.ps1")
$ConfigFile = Resolve-ConfigFile $ProjectDir $ConfigFile
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Run this script as administrator." -ForegroundColor Red
    exit 1
}
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$env:AAM_CONFIG = $ConfigFile
Push-Location $ProjectDir
& $VenvPython (Join-Path $PSScriptRoot "set_dashboard_passwords.py") --config $ConfigFile --which $Which
$code = $LASTEXITCODE
Pop-Location
# the config store replaces config.yaml atomically as Administrator: give the agent's account its
# Modify right on the new file again (Settings saves, password changes and the FY switch need it)
$agent = (Get-CimInstance Win32_Service -Filter "Name='AamBackupAgent'" -ErrorAction SilentlyContinue).StartName
if ($agent -and $agent -ne "LocalSystem") {
    $acl = if ($agent.StartsWith(".\")) { "$env:COMPUTERNAME\" + $agent.Substring(2) } else { $agent }
    icacls $ConfigFile /grant "${acl}:M" | Out-Null
}
exit $code
