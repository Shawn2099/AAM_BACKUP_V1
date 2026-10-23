# Shared helpers for 04_check_readiness.ps1, 06_install_services.ps1 and 11_reset_dashboard_passwords.ps1
# (dot-source: . "$PSScriptRoot\_common.ps1").

# Where config.yaml lives on a FRESH install. It is NOT inside the code folder: 03_setup_system.bat locks that folder
# (the SYSTEM services run code from it), and the backup account must be able to create, replace and delete files
# next to config.yaml (Settings saves, the yearly FY switch, backup copies, the lock file).
$script:FreshConfigFile = "C:\BackupAgent\config\config.yaml"

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

function Resolve-ConfigFile([string]$ProjectDir, [string]$Explicit = "") {
    # explicit -ConfigFile, else an existing install's config.yaml in the project folder (the trial server keeps
    # it there), else the fresh-install location.
    if ($Explicit) { return $Explicit }
    $legacy = Join-Path $ProjectDir "config.yaml"
    if (Test-Path $legacy) { return $legacy }
    return $script:FreshConfigFile
}

function Set-InstallEnvironment([string]$RuntimeDir) {
    # Must run before the first `uv` call: a shell opened before 03_setup_system.bat does not see its machine-wide
    # variables, and uv would then put Python (and .venv's base) into THIS administrator's profile, which the
    # service account cannot read.
    if (-not $env:UV_PYTHON_INSTALL_DIR) { $env:UV_PYTHON_INSTALL_DIR = Join-Path $env:ProgramData "uv\python" }
    if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $env:ProgramData "uv\cache" }
    if ($RuntimeDir) { $env:PREFECT_HOME = Join-Path $RuntimeDir ".prefect" }
}

function Get-ServiceAccountState([string]$Account) {
    # Does the service account exist and is it enabled? Works for a local account (.\name) AND a domain account
    # (DOMAIN\name). A domain controller has no local accounts at all (Get-LocalUser / New-LocalUser do not exist
    # there), so the account must be DOMAIN\name. Returns @{ Exists; Enabled; Note }.
    $parts = $Account -split '\'
    $short = $parts[-1]
    $realm = if ($parts.Count -gt 1) { $parts[0] } else { "." }
    $isDc = ((Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).DomainRole -ge 4)
    $isLocal = ($realm -eq "." -or $realm -ieq $env:COMPUTERNAME)
    if ($isDc -and $isLocal) {
        return @{ Exists = $false; Enabled = $false
                  Note = "this server is a domain controller: it has no local accounts - use a DOMAIN\name account (for example -ServiceAccount CAAAM\aambackup)" }
    }
    try {
        Add-Type -AssemblyName System.DirectoryServices.AccountManagement -ErrorAction Stop
        $type = if ($isLocal) { [System.DirectoryServices.AccountManagement.ContextType]::Machine } else { [System.DirectoryServices.AccountManagement.ContextType]::Domain }
        $ctx = if ($isLocal) { New-Object System.DirectoryServices.AccountManagement.PrincipalContext($type) } else { New-Object System.DirectoryServices.AccountManagement.PrincipalContext($type, $realm) }
        $user = [System.DirectoryServices.AccountManagement.UserPrincipal]::FindByIdentity($ctx, [System.DirectoryServices.AccountManagement.IdentityType]::SamAccountName, $short)
    } catch {
        return @{ Exists = $false; Enabled = $false; Note = "could not look the account up: $($_.Exception.Message)" }
    }
    if (-not $user) { return @{ Exists = $false; Enabled = $false; Note = "not found" } }
    return @{ Exists = $true; Enabled = ($user.Enabled -ne $false); Note = "" }
}

function Get-AclAccount([string]$Account) {
    # icacls does not resolve dot-backslash-name: use COMPUTERNAME-backslash-name (found on the trial box)
    if ($Account.StartsWith(".\")) { return "$env:COMPUTERNAME\" + $Account.Substring(2) }
    return $Account
}

function Grant-AgentRights([string]$RuntimeDir, [string]$ProjectDir, [string]$ConfigFile, [string]$ServiceAccount) {
    # The agent writes the DB, logs, run logs, lock and Prefect home under the runtime folder, and (FY rollover,
    # Settings) config.yaml + its backup + temp/lock files in config.yaml's folder. /T also covers files that already
    # exist (an upgrade: logs and the DB were created by LocalSystem). Used by BOTH 04 (before the probe runs as the
    # account) and 06, so the readiness check and the install can never disagree about a right.
    # Returns $true when everything was granted.
    if (-not (Test-Path $RuntimeDir)) { New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null }
    $acl = Get-AclAccount $ServiceAccount
    icacls $RuntimeDir /grant "${acl}:(OI)(CI)M" /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    $configDir = Split-Path -Parent $ConfigFile
    if (-not (Test-Path $configDir)) { New-Item -ItemType Directory -Path $configDir -Force | Out-Null }
    if ($configDir.TrimEnd('\') -ieq $ProjectDir.TrimEnd('\')) {
        # legacy layout (the trial server): config.yaml sits in the code folder, so the account needs Modify on the
        # folder itself, not only on the file
        icacls $ProjectDir /grant "${acl}:M" | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
        icacls $ConfigFile /grant "${acl}:M" | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    # fresh layout: its own folder, inherited by every file the account creates there (create / replace / delete);
    # the code folder stays read-only for the account
    icacls $configDir /grant "${acl}:(OI)(CI)M" /T /C /Q | Out-Null
    return ($LASTEXITCODE -eq 0)
}
