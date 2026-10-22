# Shared helpers for 04_check_readiness.ps1 and 06_install_services.ps1 (dot-source: . "$PSScriptRoot\_common.ps1").

function Assert-Administrator {
    # No self-elevation: a script that re-opens itself in a new window loses its output when it ends, so the
    # red lines are never seen. Refuse with the one thing to do.
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Write-Host ""
        Write-Host "  ERROR: this script must run in an ADMINISTRATOR PowerShell." -ForegroundColor Red
        Write-Host "  Start menu > type PowerShell > right-click > Run as administrator, then run it again."
        Write-Host ""
        exit 1
    }
}

function Set-InstallEnvironment([string]$RuntimeDir) {
    # Must run before the first `uv` call: a shell opened before 03_setup_system.bat does not see its machine-wide
    # variables, and uv would then put Python (and .venv's base) into THIS administrator's profile, which the
    # service account cannot read.
    if (-not $env:UV_PYTHON_INSTALL_DIR) { $env:UV_PYTHON_INSTALL_DIR = Join-Path $env:ProgramData "uv\python" }
    if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $env:ProgramData "uv\cache" }
    if ($RuntimeDir) { $env:PREFECT_HOME = Join-Path $RuntimeDir ".prefect" }
}

function Get-AclAccount([string]$Account) {
    # icacls does not resolve dot-backslash-name: use COMPUTERNAME-backslash-name (found on the trial box)
    if ($Account.StartsWith(".\")) { return "$env:COMPUTERNAME\" + $Account.Substring(2) }
    return $Account
}

function Grant-AgentRights([string]$RuntimeDir, [string]$ProjectDir, [string]$ConfigFile, [string]$ServiceAccount) {
    # The agent writes the DB, logs, run logs, lock and Prefect home under the runtime folder, and (FY rollover)
    # config.yaml + its backup next to it. /T also covers files that already exist (an upgrade: logs and the DB were
    # created by LocalSystem). Used by BOTH 04 (before the probe runs as the account) and 06, so the readiness
    # check and the install can never disagree about a right. Returns $true when everything was granted.
    if (-not (Test-Path $RuntimeDir)) { New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null }
    $acl = Get-AclAccount $ServiceAccount
    icacls $RuntimeDir /grant "${acl}:(OI)(CI)M" /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    icacls $ProjectDir /grant "${acl}:M" | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    icacls $ConfigFile /grant "${acl}:M" | Out-Null
    return ($LASTEXITCODE -eq 0)
}
