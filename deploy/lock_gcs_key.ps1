# Usage:  lock_gcs_key.ps1 -KeyPath <file> [-Account <service account>]
#   Only SYSTEM, Administrators and the service account (read only) can open the Google key.
#   Everyone else (Users, Everyone, Authenticated Users) loses access. Re-runnable.
#   When the key sits in deploy\keys (the default) the folder is locked the same way, so a new
#   key dropped there starts out locked and nobody else can add or replace a file in it.
#   Exit 0 = locked (or no key file yet); exit 1 = the key was NOT changed or could not be locked.
param(
    [Parameter(Mandatory = $true)][string]$KeyPath,
    [string]$Account = ""
)

$ErrorActionPreference = "Continue"

function Resolve-AclAccount([string]$Name) {
    # icacls does not resolve dot-backslash-name: use COMPUTERNAME-backslash-name
    if ($Name.StartsWith(".\")) { return "$env:COMPUTERNAME\" + $Name.Substring(2) }
    return $Name
}

function Lock-Path([string]$Path, [string]$AccountName, [bool]$IsFolder) {
    # SIDs, not names: S-1-5-18 = SYSTEM, S-1-5-32-544 = Administrators (any Windows language)
    $full  = if ($IsFolder) { "(OI)(CI)(F)" } else { "(F)" }
    $read  = if ($IsFolder) { "(OI)(CI)(RX)" } else { "(R)" }
    # Grant first, cut inheritance after: a failed grant never leaves the file without access.
    $grants = @("*S-1-5-18:$full", "*S-1-5-32-544:$full")
    if ($AccountName) { $grants += ((Resolve-AclAccount $AccountName) + ":$read") }
    foreach ($g in $grants) {
        icacls $Path /grant:r $g /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { return "could not grant $g on $Path" }
    }
    icacls $Path /inheritance:r /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { return "could not remove inherited access on $Path" }
    # also entries added directly (not inherited): Users, Everyone, Authenticated Users
    icacls $Path /remove:g "*S-1-5-32-545" "*S-1-1-0" "*S-1-5-11" /C /Q | Out-Null
    $left = (icacls $Path | Out-String)
    foreach ($who in @("BUILTIN\Users", "Everyone", "NT AUTHORITY\Authenticated Users")) {
        if ($left -match [regex]::Escape($who)) { return "$who can still reach $Path" }
    }
    return $null
}

if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    Write-Host "[WARN] Google key not found at $KeyPath - nothing to lock (cloud backups need it)" -ForegroundColor Yellow
    exit 0
}

$problem = Lock-Path $KeyPath $Account $false
if ($problem) {
    Write-Host "  ERROR: Google key not locked: $problem" -ForegroundColor Red
    exit 1
}
$who = if ($Account) { "SYSTEM, Administrators and $Account (read)" } else { "SYSTEM and Administrators" }
Write-Host "[OK]   Google key locked to $who : $KeyPath"

$keysDir = Join-Path (Split-Path -Parent $PSScriptRoot) "deploy\keys"
$parent  = Split-Path -Parent (Resolve-Path -LiteralPath $KeyPath).Path
if ($parent.TrimEnd('\') -ieq $keysDir.TrimEnd('\')) {
    $problem = Lock-Path $parent $Account $true
    if ($problem) {
        Write-Host "  ERROR: keys folder not locked: $problem" -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK]   keys folder locked the same way: $parent"
}
exit 0
