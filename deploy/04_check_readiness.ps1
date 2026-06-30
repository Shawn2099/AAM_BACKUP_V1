
# =======================================================================
# Self-Elevate to Administrator
# =======================================================================
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue" # Prevent NativeCommandError from external tools

$passed = $true
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectDir = Split-Path -Parent $ScriptDir
$BinDir = Join-Path $ScriptDir "bin"

function Check-Item($Name, $Status, $IsError) {
    if ($IsError) {
        Write-Host "[FAIL] " -NoNewline -ForegroundColor Red
        Write-Host "$($Name): $Status"
        $script:passed = $false
    } else {
        Write-Host "[OK]   " -NoNewline -ForegroundColor Green
        Write-Host "$($Name): $Status"
    }
}

Write-Host "===================================================================="
Write-Host "  AAM Backup Automation - READINESS GATE"
Write-Host "===================================================================="
Write-Host ""

# 1. Powershell Version
if ($PSVersionTable.PSVersion.Major -ge 5) {
    Check-Item "PowerShell" "v$($PSVersionTable.PSVersion.ToString())" $false
} else {
    Check-Item "PowerShell" "v$($PSVersionTable.PSVersion.ToString()) (Requires 5.1+)" $true
}

# 2. Check uv
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if ($uvCmd) { $uvCmd.Source } else { $null }
if (-not $uvPath) {
    $c = @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe", "C:\Program Files\Python312\Scripts\uv.exe")
    foreach ($p in $c) { if (Test-Path $p) { $uvPath = $p; break } }
}
if ($uvPath) {
    $uvVer = & $uvPath --version 2>&1
    Check-Item "uv" "$uvVer ($uvPath)" $false

    Write-Host "       -> Bootstrapping Python via uv... " -NoNewline
    $pyVer = (& $uvPath run python --version 2>&1) -join " "
    if ($LASTEXITCODE -eq 0 -and $pyVer -match "Python") {
        Write-Host "[OK]" -ForegroundColor Green
    } else {
        Write-Host "[FAIL]" -ForegroundColor Red
        Check-Item "Python Runtime" "Failed to bootstrap Python. Check internet." $true
    }

    Write-Host "       -> Installing required libraries (uv sync)... " -NoNewline
    $sync = cmd /c "`"$uvPath`" sync --directory `"$ProjectDir`" 2>&1"
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[OK]" -ForegroundColor Green
    } else {
        Write-Host "[FAIL]" -ForegroundColor Red
        Check-Item "Dependencies" "Failed to install Python libraries. Check internet." $true
    }
} else {
    Check-Item "uv" "Not found" $true
}

# 3. Check NSSM
$nssmPath = Join-Path $BinDir "nssm.exe"
if (Test-Path $nssmPath) {
    $nssmVer = (& $nssmPath version 2>&1) -join " "
    Check-Item "NSSM" "Found ($nssmPath)" $false
} else {
    Check-Item "NSSM" "Missing at $nssmPath" $true
}

# 4. Check rclone
$rclonePath = Join-Path $BinDir "rclone.exe"
if (Test-Path $rclonePath) {
    $rcloneVer = (& $rclonePath version 2>&1 | Select-Object -First 1)
    Check-Item "rclone" "$rcloneVer ($rclonePath)" $false
} else {
    Check-Item "rclone" "Missing at $rclonePath" $true
}

# 5. Check robocopy
$roboPath = "C:\Windows\System32\robocopy.exe"
if (Test-Path $roboPath) {
    Check-Item "robocopy" "Found ($roboPath)" $false
} else {
    Check-Item "robocopy" "Missing at $roboPath" $true
}

# 6. Check gcloud
$gcloudPath = $null
$gcmd = Get-Command gcloud -ErrorAction SilentlyContinue
if ($gcmd) { $gcloudPath = $gcmd.Source }
if (-not $gcloudPath) {
    $iso = Join-Path $BinDir "google-cloud-sdk\bin\gcloud.cmd"
    if (Test-Path $iso) { $gcloudPath = $iso }
}
if ($gcloudPath) {
    # Using path instead of calling --version because gcloud init can be very slow
    Check-Item "gcloud" "Found ($gcloudPath)" $false
} else {
    Check-Item "gcloud" "Not found in PATH or isolated bin" $true
}

# 7. Check Registry - Long Paths
$lp = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction SilentlyContinue
if ($lp -and $lp.LongPathsEnabled -eq 1) {
    Check-Item "Long Paths" "Enabled" $false
} else {
    Check-Item "Long Paths" "Disabled (Run 03_setup_system.bat)" $true
}

# 8. Check Registry - Auto Reboot
$ar = Get-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" -Name NoAutoRebootWithLoggedOnUsers -ErrorAction SilentlyContinue
if ($ar -and $ar.NoAutoRebootWithLoggedOnUsers -eq 1) {
    Check-Item "Auto-Reboot" "Suppressed" $false
} else {
    Check-Item "Auto-Reboot" "Not suppressed (Run 03_setup_system.bat)" $true
}

# 9. Check Ports
$p4200 = netstat -an | Select-String ":4200 " | Select-String "LISTENING"
if ($p4200) { Check-Item "Port 4200" "In use! (Must be free for Prefect)" $true } else { Check-Item "Port 4200" "Available" $false }

$p8080 = netstat -an | Select-String ":8080 " | Select-String "LISTENING"
if ($p8080) { Check-Item "Port 8080" "In use! (Must be free for Dashboard)" $true } else { Check-Item "Port 8080" "Available" $false }

# 10. Pending Reboot Check
$reboot1 = Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"
$reboot2 = Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"
if ($reboot1 -or $reboot2) {
    Check-Item "Pending Reboot" "YES (Restart server before continuing!)" $true
} else {
    Check-Item "Pending Reboot" "None" $false
}

# 11. Critical Windows Services
$w32time = Get-Service W32Time -ErrorAction SilentlyContinue
if ($w32time -and $w32time.Status -eq "Running") {
    Check-Item "W32Time Service" "Running" $false
} else {
    Check-Item "W32Time Service" "Stopped (Critical for file locking)" $true
}

$lanman = Get-Service LanmanWorkstation -ErrorAction SilentlyContinue
if ($lanman -and $lanman.Status -eq "Running") {
    Check-Item "SMB Client" "Running" $false
} else {
    Check-Item "SMB Client" "Stopped (Critical for LAN backups)" $true
}

# 12. Antivirus Check (Warning only)
$wd = Get-Service WinDefend -ErrorAction SilentlyContinue
if ($wd -and $wd.Status -eq "Running") {
    Write-Host "[WARN] " -NoNewline -ForegroundColor Yellow
    Write-Host "Windows Defender is running. Ensure exclusions are set (run 03_setup_system.bat)."
    Write-Host "       Exclusions needed: project folder, robocopy.exe, rclone.exe"
}

# 13. Directory Write-Access
$testFile = Join-Path $ProjectDir ".write_test.tmp"
try {
    "test" | Out-File -FilePath $testFile -Encoding UTF8 -ErrorAction Stop
    Remove-Item $testFile -Force -ErrorAction SilentlyContinue
    Check-Item "Directory Permissions" "Write access confirmed" $false
} catch {
    Check-Item "Directory Permissions" "ACCESS DENIED! Move folder out of protected dirs." $true
}

# 14. Config & Keys
$cfg = Join-Path $ProjectDir "config.yaml"
if (Test-Path $cfg) { Check-Item "config.yaml" "Found" $false } else { Check-Item "config.yaml" "Missing in project root" $true }

$key = Join-Path $ScriptDir "keys\aam-gcs-key.json"
if (Test-Path $key) {
    try {
        $keyJson = Get-Content $key -Raw | ConvertFrom-Json -ErrorAction Stop
        if ($keyJson.type -eq "service_account") {
            Check-Item "GCS Key" "Valid Service Account JSON" $false
        } else {
            Check-Item "GCS Key" "Invalid JSON (missing 'type: service_account')" $true
        }
    } catch {
        Check-Item "GCS Key" "Corrupt or invalid JSON syntax" $true
    }
} else {
    Write-Host "[WARN] " -NoNewline -ForegroundColor Yellow
    Write-Host "GCS Key missing at $key (Required for cloud backups)"
}

Write-Host ""
if ($passed) {
    Write-Host ">>> SUCCESS: All readiness checks passed! You may proceed to 05_test_config.bat." -ForegroundColor Green
} else {
    Write-Host ">>> ERROR: One or more checks failed. Fix the red items above before proceeding." -ForegroundColor Red
}
Write-Host ""
# pause
