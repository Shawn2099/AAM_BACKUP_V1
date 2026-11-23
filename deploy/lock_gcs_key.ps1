# Usage:  lock_gcs_key.ps1 -KeyPath <file> [-Account <service account>]
#   Only SYSTEM, Administrators and the service account (read only) can open the Google key.
#   Everyone else (Users, Everyone, Authenticated Users) loses access. Re-runnable: the whole access
#   list is rebuilt each time, so a renamed service account leaves nothing of the old one behind.
#   When the key sits in deploy\keys (the default) the folder is locked the same way, so a new
#   key dropped there starts out locked and nobody else can add or replace a file in it.
#   Exit 0 = locked (or no key file yet); exit 1 = the key was NOT changed or could not be locked.
param(
    [Parameter(Mandatory = $true)][string]$KeyPath,
    [string]$Account = ""
)

$ErrorActionPreference = "Continue"

function Resolve-AclAccount([string]$Name) {
    # dot-backslash-name does not resolve: use COMPUTERNAME-backslash-name
    if ($Name.StartsWith(".\")) { return "$env:COMPUTERNAME\" + $Name.Substring(2) }
    return $Name
}

function Lock-Path([string]$Path, [string]$AccountName, [bool]$IsFolder) {
    # The whole access list is built fresh and written in ONE call, so nothing from before survives
    # (an earlier service account, Users, Everyone, inherited entries) and a failure before the
    # write changes nothing. SIDs, not names: S-1-5-18 = SYSTEM, S-1-5-32-544 = Administrators.
    $sec = if ($IsFolder) { New-Object System.Security.AccessControl.DirectorySecurity }
           else { New-Object System.Security.AccessControl.FileSecurity }
    $sec.SetAccessRuleProtection($true, $false)            # no inherited entries
    $full = [System.Security.AccessControl.FileSystemRights]::FullControl
    $read = if ($IsFolder) { [System.Security.AccessControl.FileSystemRights]::ReadAndExecute }
            else { [System.Security.AccessControl.FileSystemRights]::Read }
    $flags = if ($IsFolder) { "ContainerInherit, ObjectInherit" } else { "None" }
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    try {
        foreach ($s in @("S-1-5-18", "S-1-5-32-544")) {
            $sid = New-Object System.Security.Principal.SecurityIdentifier($s)
            $sec.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $full, $flags, "None", $allow)))
        }
        if ($AccountName) {
            $nt = New-Object System.Security.Principal.NTAccount((Resolve-AclAccount $AccountName))
            $sid = $nt.Translate([System.Security.Principal.SecurityIdentifier])   # throws if the account does not exist
            $sec.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $read, $flags, "None", $allow)))
        }
        $item = Get-Item -LiteralPath $Path -Force
        $item.SetAccessControl($sec)
    } catch {
        return "could not set the access list on $Path ($($_.Exception.Message))"
    }
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
