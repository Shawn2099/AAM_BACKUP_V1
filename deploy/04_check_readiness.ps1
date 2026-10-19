# Usage:  04_check_readiness.ps1 [-ServiceAccount .\aambackup]
#   With -ServiceAccount, the account's password is asked for and a probe runs
#   AS that account (deploy\probe_service_account.py): NAS write, source read,
#   runtime + config folder write, SeBackupPrivilege - the exact checks the
#   nightly preflight makes (plan WP4/WP16).
param(
    [string]$ServiceAccount = "",
    [string]$PasswordFile = ""
)

# =======================================================================
# Self-Elevate to Administrator (parameters are passed on)
# =======================================================================
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $fwd = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($ServiceAccount) { $fwd += " -ServiceAccount `"$ServiceAccount`"" }
    if ($PasswordFile) { $fwd += " -PasswordFile `"$PasswordFile`"" }
    Start-Process powershell.exe -ArgumentList $fwd -Verb RunAs
    exit
}

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

    # Managed Python outside any user profile (03_setup_system.bat sets this
    # machine-wide; a shell opened before that does not see it yet)
    if (-not $env:UV_PYTHON_INSTALL_DIR) {
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $env:ProgramData "uv\python"
    }
    # --frozen: exactly the versions in uv.lock, never a re-resolve (WP16)
    # --link-mode copy: uv otherwise HARD-LINKS the packages to its cache in this
    # admin's profile - a hard link shares that file's permissions, so the
    # service account could not read them (trial box: agent would not start).
    # Copies inherit the project folder's "Users can read" permission instead.
    Write-Host "       -> Installing required libraries (uv sync --frozen --no-dev --link-mode copy)... " -NoNewline
    $sync = cmd /c "`"$uvPath`" sync --frozen --no-dev --link-mode copy --directory `"$ProjectDir`" 2>&1"
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[OK]" -ForegroundColor Green
        # uv copies some files WITH the restrictive ACL of its cache (SYSTEM/Administrators only,
        # not inherited): the service account then gets "DLL load failed ... Access is denied" for
        # e.g. cryptography\_rust.pyd (found on the trial box 2026-10-19). Give every file in .venv
        # the ACL inherited from the project folder again.
        icacls (Join-Path $ProjectDir ".venv") /reset /T /C /Q | Out-Null
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

# 8b. Check Registry - Windows Update Active Hours (G9)
# The NoAutoReboot policy above is useless on a headless server (no logged-on
# user). Active Hours is the protection that actually works here: reboots
# must be confined to 08:00-20:00. Windows Server 2016 accepts at most 12 hours of Active Hours
# (20:00-08:00 here); an 18 h window is invalid (plan H2). Report the REAL Windows Update state:
# when the update service is disabled or a policy turns updates off (a deliberate choice on this
# server) nothing reboots the box, and Active Hours are informational.
$wu = Get-Service wuauserv -ErrorAction SilentlyContinue
$wuPolicy = Get-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" -ErrorAction SilentlyContinue
# (StrictMode: read a registry value that may not exist only through PSObject.Properties)
$wuNoAuto = if ($wuPolicy -and $wuPolicy.PSObject.Properties["NoAutoUpdate"]) { $wuPolicy.NoAutoUpdate } else { $null }
$wuOff = [bool](($wu -and $wu.StartType -eq "Disabled") -or ($wuNoAuto -eq 1))
$wuState = if (-not $wu) { "service not found" } else { "service $($wu.Status), start type $($wu.StartType)" }
if ($wuNoAuto -eq 1) { $wuState += ", policy NoAutoUpdate=1" }
$ahUx = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings" -ErrorAction SilentlyContinue
$ahPol = Get-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" -ErrorAction SilentlyContinue
$ahStart = if ($ahUx -and $ahUx.PSObject.Properties["ActiveHoursStart"]) { $ahUx.ActiveHoursStart } else { $null }
$ahEnd = if ($ahUx -and $ahUx.PSObject.Properties["ActiveHoursEnd"]) { $ahUx.ActiveHoursEnd } else { $null }
if ($ahPol -and $ahPol.PSObject.Properties["SetActiveHours"] -and $ahPol.SetActiveHours -eq 1 -and $ahPol.PSObject.Properties["ActiveHoursStart"]) {
    $ahStart = $ahPol.ActiveHoursStart; $ahEnd = $ahPol.ActiveHoursEnd      # the policy wins over the Settings value
}
if ($ahStart -eq 20 -and $ahEnd -eq 8) {
    Check-Item "Update Active Hours" "20:00-08:00 (12 h, backup nights protected); Windows Update: $wuState" $false
} elseif ($wuOff) {
    Check-Item "Update Active Hours" "Not set, but Windows Update is off ($wuState) - nothing reboots the server" $false
} else {
    Check-Item "Update Active Hours" "Missing/incorrect (need 20:00-08:00) and Windows Update is active ($wuState) - reboots could hit a backup window (Run 03_setup_system.bat)" $true
}

# 9. Check Ports
# A port held by our OWN running service is fine (re-install / upgrade); only a
# foreign program on it is a problem.
foreach ($pp in @(@(4200, "AamPrefectServer", "Prefect"), @(8080, "AamBackupAgent", "Dashboard"))) {
    $port, $svcName, $what = $pp
    $listen = netstat -an | Select-String ":$port " | Select-String "LISTENING"
    $ours = Get-Service $svcName -ErrorAction SilentlyContinue
    if (-not $listen) {
        Check-Item "Port $port" "Available" $false
    } elseif ($ours -and $ours.Status -eq "Running") {
        Check-Item "Port $port" "In use by $svcName (already installed - fine for a re-install)" $false
    } else {
        Check-Item "Port $port" "In use by another program! (Must be free for $what)" $true
    }
}

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

# 15. Python the services will run (WP16: .venv's Python directly, no uv at boot)
$venvPy = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$venvCfg = Join-Path $ProjectDir ".venv\pyvenv.cfg"
if (Test-Path $venvPy) {
    Check-Item "Project Python" "$venvPy" $false
    $homeLine = Get-Content $venvCfg -ErrorAction SilentlyContinue | Where-Object { $_ -match '^home\s*=' }
    $baseHome = if ($homeLine) { ($homeLine -split '=', 2)[1].Trim() } else { "" }
    if ($baseHome -match '^[A-Za-z]:\\Users\\') {
        # A service account cannot read another user's profile: the agent would not start.
        $msg = "base Python is inside a user profile ($baseHome). Install Python for all users " +
               "(e.g. C:\Program Files\Python312) or set UV_PYTHON_INSTALL_DIR to a shared folder, " +
               "then re-create .venv"
        Check-Item "Base Python location" $msg ([bool]$ServiceAccount)
    } else {
        Check-Item "Base Python location" "$baseHome (readable by every account)" $false
    }

    # 16. config.yaml valid for THIS code version (strict keys + schedule vs run budgets)
    Push-Location $ProjectDir
    $env:AAM_CONFIG = $cfg
    $cfgOut = & $venvPy -c "from models.config import load_config; load_config(r'$cfg')" 2>&1
    $cfgExit = $LASTEXITCODE
    Pop-Location
    if ($cfgExit -eq 0) {
        Check-Item "Config validation" "config.yaml is valid (schedule fits the run budgets)" $false
    } else {
        Check-Item "Config validation" "config.yaml is NOT valid - the services would not start:" $true
        $cfgOut | Select-Object -Last 12 | ForEach-Object { Write-Host "       $_" }
    }

    # 16b. The "open the dashboard" link in every email must work on the office PCs
    Push-Location $ProjectDir
    $linkOut = & $venvPy -c "from models.config import load_config; from core.notify import dashboard_link_problem; p = dashboard_link_problem(load_config(r'$cfg')); print(p or 'ok'); raise SystemExit(1 if p else 0)" 2>&1
    $linkExit = $LASTEXITCODE
    # plan D3: show the link emails will use and every IPv4 of this server, so the owner can confirm it
    $linkInfo = & $venvPy -c "from models.config import load_config; from core.notify import dashboard_urls; from core.netinfo import all_ipv4; c = load_config(r'$cfg'); u = dashboard_urls(c); print(u[0] + (' (or ' + u[1] + ')' if u[1] else '') + '; this server: ' + ', '.join(all_ipv4()))" 2>&1
    Pop-Location
    if ($linkExit -eq 0) {
        Check-Item "Dashboard link in emails" "$($linkInfo | Select-Object -Last 1) - works from office PCs" $false
    } else {
        Check-Item "Dashboard link in emails" "$($linkOut | Select-Object -Last 1)" $true
    }
} else {
    Check-Item "Project Python" "Missing $venvPy (uv sync --frozen failed above?)" $true
}

# 17. Service account: exists, and the probe runs AS it (plan §10 item 2)
if ($ServiceAccount) {
    $shortName = ($ServiceAccount -split '\\')[-1]
    $local = Get-LocalUser -Name $shortName -ErrorAction SilentlyContinue
    if ($local -and $local.Enabled) {
        Check-Item "Service account" "$ServiceAccount exists and is enabled" $false
    } else {
        Check-Item "Service account" "$ServiceAccount not found or disabled (create it: lusrmgr.msc)" $true
    }
    if ($local -and (Test-Path $venvPy)) {
        try {
            $cred = if ($PasswordFile) { New-Object System.Management.Automation.PSCredential($ServiceAccount, (Read-PasswordFile $PasswordFile)) }
                    else { Get-Credential -UserName $ServiceAccount -Message "Password of $ServiceAccount (probe runs as this account)" }
        } catch {
            $cred = $null
        }
        $resultFile = Join-Path $env:SystemRoot ("Temp\aam_account_probe_" + [guid]::NewGuid().ToString("N") + ".txt")
        $probe = Join-Path $ScriptDir "probe_service_account.py"
        $taskName = "AAM_readiness_probe_" + [guid]::NewGuid().ToString("N").Substring(0, 8)
        # Run the probe as a short-lived scheduled task under the account: a
        # non-interactive logon like the service's own. (Start-Process -Credential
        # fails with 0xC0000142 from SSH / any session without a desktop - trial box.)
        try {
            if (-not $cred) { throw "no password for $ServiceAccount could be read" }
            $taskUser = if ($ServiceAccount.StartsWith(".\")) { "$env:COMPUTERNAME\" + $ServiceAccount.Substring(2) } else { $ServiceAccount }
            $action = New-ScheduledTaskAction -Execute $venvPy -WorkingDirectory $ProjectDir `
                      -Argument "`"$probe`" `"$cfg`" `"$resultFile`""
            Register-ScheduledTask -TaskName $taskName -Action $action -User $taskUser `
                -Password $cred.GetNetworkCredential().Password -RunLevel Limited -ErrorAction Stop | Out-Null
            $started = Get-Date
            Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
            $deadline = $started.AddMinutes(10)
            do {
                Start-Sleep -Seconds 2
                $state = (Get-ScheduledTask -TaskName $taskName).State
                $info = Get-ScheduledTaskInfo -TaskName $taskName
            # LastRunTime has whole-second resolution: compare with a 2 s tolerance, otherwise a probe
            # that started within the same second as $started is "not run yet" until the deadline
            } while (($state -eq "Running" -or $info.LastRunTime -lt $started.AddSeconds(-2)) -and (Get-Date) -lt $deadline)
            $p = [pscustomobject]@{ ExitCode = $info.LastTaskResult }
            if (Test-Path $resultFile) {
                foreach ($line in Get-Content $resultFile) {
                    $parts = $line -split ' ', 2
                    if ($parts[0] -eq "SKIP") {
                        Write-Host "[SKIP] " -NoNewline -ForegroundColor Yellow
                        Write-Host "as $($ServiceAccount): $($parts[1])"
                    } else {
                        $msg = $parts[1]
                        if ($msg -match "PermissionError" -and $msg -match "\.venv") {
                            $msg += " -> the .venv was built with hard links to an admin-only cache: stop " +
                                    "the AAM services, delete $ProjectDir\.venv, run this check again"
                        }
                        Check-Item "as $ServiceAccount" $msg ($parts[0] -ne "OK")
                    }
                }
                Remove-Item $resultFile -Force -ErrorAction SilentlyContinue
            } else {
                Check-Item "as $ServiceAccount" ("the probe could not run as the account (exit " +
                    "$($p.ExitCode)) - wrong password, or the account cannot read the project " +
                    "folder / Python") $true
            }
        } catch {
            Check-Item "as $ServiceAccount" ("could not run the probe as the account: $_ (wrong password, " +
                "or the account lacks 'Log on as a batch job' - Backup Operators have it)") $true
        } finally {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        }
    }
} else {
    Write-Host "[WARN] " -NoNewline -ForegroundColor Yellow
    Write-Host "No -ServiceAccount given: NAS access is NOT verified. In a WORKGROUP the agent must"
    Write-Host "       run as a dedicated account that also exists (same password) on the NAS."
}

# 18. Mail password readable by LocalSystem (the watchdog sends the alert emails; plan S5).
# The agent account is covered by the probe above.
if (Test-Path $venvPy) {
    $sysResult = Join-Path $env:SystemRoot ("Temp\aam_mail_probe_" + [guid]::NewGuid().ToString("N") + ".txt")
    $sysTask = "AAM_readiness_mail_" + [guid]::NewGuid().ToString("N").Substring(0, 8)
    try {
        $action = New-ScheduledTaskAction -Execute $venvPy -WorkingDirectory $ProjectDir `
                  -Argument "`"$(Join-Path $ScriptDir 'probe_mail_secret.py')`" `"$cfg`" `"$sysResult`""
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Limited
        Register-ScheduledTask -TaskName $sysTask -Action $action -Principal $principal -ErrorAction Stop | Out-Null
        $started = Get-Date
        Start-ScheduledTask -TaskName $sysTask -ErrorAction Stop
        $deadline = $started.AddMinutes(2)
        do {
            Start-Sleep -Seconds 2
            $state = (Get-ScheduledTask -TaskName $sysTask).State
            $info = Get-ScheduledTaskInfo -TaskName $sysTask
        } while (($state -eq "Running" -or $info.LastRunTime -lt $started.AddSeconds(-2)) -and (Get-Date) -lt $deadline)
        if (Test-Path $sysResult) {
            $parts = (Get-Content $sysResult -TotalCount 1) -split ' ', 2
            Check-Item "as LocalSystem" $parts[1] ($parts[0] -ne "OK")
            Remove-Item $sysResult -Force -ErrorAction SilentlyContinue
        } else {
            Check-Item "as LocalSystem" "the mail password probe produced no result (exit $($info.LastTaskResult))" $true
        }
    } catch {
        Check-Item "as LocalSystem" "could not run the mail password probe as LocalSystem: $_" $true
    } finally {
        Unregister-ScheduledTask -TaskName $sysTask -Confirm:$false -ErrorAction SilentlyContinue
    }
}

Write-Host ""
if ($passed) {
    Write-Host ">>> SUCCESS: All readiness checks passed! You may proceed to 05_test_config.bat." -ForegroundColor Green
} else {
    Write-Host ">>> ERROR: One or more checks failed. Fix the red items above before proceeding." -ForegroundColor Red
}
Write-Host ""
# pause
