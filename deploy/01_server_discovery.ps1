
# ═══════════════════════════════════════════════════════════════════════
# Self-Elevate to Administrator
# ═══════════════════════════════════════════════════════════════════════
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

# ═══════════════════════════════════════════════════════════════════════
# AAM Backup Automation — Server Discovery (PowerShell)
#
# Runs on Windows Server 2016+ without Python
# Generates: server_discovery_report.md + server_discovery_report.json
# ═══════════════════════════════════════════════════════════════════════

Set-StrictMode -Version Latest
$ErrorActionPreference = "SilentlyContinue"

$ReportDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ReportMD  = Join-Path $ReportDir "server_discovery_report.md"
$ReportJSON = Join-Path $ReportDir "server_discovery_report.json"

Write-Host ""
Write-Host "===================================================================="
Write-Host "  AAM Backup Automation - Server Discovery"
Write-Host "===================================================================="
Write-Host "  Reports will be saved to:"
Write-Host "    $ReportMD"
Write-Host "    $ReportJSON"
Write-Host "===================================================================="
Write-Host ""

$TargetIP = Read-Host "Enter Backup Server IP to test connectivity (or press Enter to skip)"

# ── Helper: append to markdown ────────────────────────────────────────
function md($line) {
    Add-Content -Path $ReportMD -Value $line -Encoding UTF8
}

# ── Helper: JSON object builder ──────────────────────────────────────
$jsonParts = [System.Collections.Generic.List[string]]::new()

function json($line) {
    $jsonParts.Add($line)
}

# ── Initialize Reports ──────────────────────────────────────────────
"# Server Discovery Report" | Out-File -FilePath $ReportMD -Encoding UTF8
md ""
md "**Generated:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
md ""

json "{"
json "  `"generated`": `"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`","

# ── 1. System Information ──────────────────────────────────────────
Write-Host "[1/11] Gathering system information..."
md "## 1. System Information"
md ""

$hostname  = $env:COMPUTERNAME
$domain    = (Get-WmiObject Win32_ComputerSystem).Domain
$winVer    = (Get-WmiObject Win32_OperatingSystem).Caption
$buildNum  = (Get-WmiObject Win32_OperatingSystem).BuildNumber
$isAdmin   = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

md "- **Hostname:** $hostname"
md "- **Domain:** $domain"
md "- **Windows Version:** $winVer"
md "- **Build Number:** $buildNum"
md "- **Current User:** $env:USERNAME"
md "- **Admin Privileges:** $(if ($isAdmin) {'Yes'} else {'No'})"
md ""

json "  `"system`": {"
json "    `"hostname`": `"$hostname`","
json "    `"domain`": `"$domain`","
json "    `"windows_version`": `"$winVer`","
json "    `"build_number`": `"$buildNum`","
json "    `"current_user`": `"$env:USERNAME`","
json "    `"is_admin`": `"$(if ($isAdmin) {'yes'} else {'no'})`""
json "  },"

# ── 2. Storage Information ─────────────────────────────────────────
Write-Host "[2/11] Gathering storage information..."
md "## 2. Storage Information"
md ""
md "### Drives"
md ""
md "| Drive | Type | Total | Free | File System |"
md "|-------|------|-------|------|-------------|"

json "  `"drives`": ["

$driveTypeMap = @{ 2 = "Removable"; 3 = "Local"; 4 = "Network"; 5 = "CD-ROM" }
$drives = Get-WmiObject Win32_LogicalDisk | Where-Object { $_.DriveType -ne 5 }
$driveIdx = 0

foreach ($d in $drives) {
    $driveIdx++
    $type = if ($driveTypeMap.ContainsKey($d.DriveType)) { $driveTypeMap[$d.DriveType] } else { "Unknown" }
    $totalGB = [math]::Round($d.Size / 1GB, 1)
    $freeGB  = [math]::Round($d.FreeSpace / 1GB, 1)

    md "| $($d.DeviceID) | $type | $totalGB GB | $freeGB GB | $($d.FileSystem) |"

    if ($driveIdx -gt 1) { json "    ," }
    json "    {"
    json "      `"drive`": `"$($d.DeviceID)`","
    json "      `"type`": `"$type`","
    json "      `"total_gb`": $totalGB,"
    json "      `"free_gb`": $freeGB,"
    json "      `"filesystem`": `"$($d.FileSystem)`""
    json "    }"
}
json "  ],"
md ""

# ── 3. Network Information ─────────────────────────────────────────
Write-Host "[3/11] Gathering network information..."
md "## 3. Network Information"
md ""
md "### IP Configuration"
md ""
md "```"
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne "127.0.0.1" } | ForEach-Object {
    md "IP: $($_.IPAddress)/$($_.PrefixLength)  Adapter: $($_.InterfaceAlias)"
}
md "```"
md ""

$primaryIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne "127.0.0.1" } | Select-Object -First 1).IPAddress
$gateway = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue | Select-Object -First 1).NextHop

json "  `"network`": {"
json "    `"primary_ip`": `"$primaryIP`","
json "    `"default_gateway`": `"$gateway`","

# DNS
md "### DNS Resolution"
md ""
$dnsOK = Resolve-DnsName google.com -ErrorAction SilentlyContinue
if ($dnsOK) {
    md "- **DNS Resolution:** Working"
    json "    `"dns_resolution`": `"working`","
} else {
    md "- **DNS Resolution:** Failed"
    json "    `"dns_resolution`": `"failed`","
}

# Internet
md "### Internet Connectivity"
md ""
$pingOK = Test-Connection -ComputerName 8.8.8.8 -Count 1 -TimeoutSeconds 2 -Quiet
if ($pingOK) {
    md "- **Internet Access:** Available"
    json "    `"internet_access`": `"available`","
} else {
    md "- **Internet Access:** Not available or blocked"
    json "    `"internet_access`": `"blocked`","
}

md ""

# Target server test
md "### Target Backup Server Test"
md ""
if ($TargetIP) {
    md "- **Target IP:** $TargetIP"
    $pingTarget = Test-Connection -ComputerName $TargetIP -Count 1 -TimeoutSeconds 2 -Quiet
    if ($pingTarget) {
        md "- **Ping:** Successful"
        $mac = (arp -a $TargetIP | Select-String $TargetIP | Select-Object -First 1) -replace '^\s+(\S+)\s+.*', '$1'
        if ($mac) { md "- **MAC Address:** $mac" }

        # SMB port test
        $tcp = New-Object System.Net.Sockets.TcpClient
        try {
            $result = $tcp.BeginConnect($TargetIP, 445, $null, $null)
            $wait = $result.AsyncWaitHandle.WaitOne(2000, $false)
            if ($wait) { $tcp.EndConnect($result); md "- **SMB Port 445:** Open" } else { md "- **SMB Port 445:** Blocked" }
        } catch { md "- **SMB Port 445:** Blocked" }
        $tcp.Close()
    } else {
        md "- **Ping:** Failed"
    }
} else {
    md "- No target IP provided. Skipped."
}
md ""

json "    `"target_server`": { `"ip`": `"$(if ($TargetIP) {$TargetIP} else {''})`" }"
json "  },"

# ── 4. Software & Tools ────────────────────────────────────────────
Write-Host "[4/11] Checking software and tools..."
md "## 4. Software & Tools"
md ""

json "  `"software`": {"

# Python
$pyVer = python --version 2>&1 | Out-String
if ($pyVer -match "Python") {
    md "- **Python:** $($pyVer.Trim())"
    json "    `"python`": `"$($pyVer.Trim())`","
} else {
    md "- **Python:** Not installed (uv will manage it)"
    json "    `"python`": `"not installed`","
}

# uv
$uvVer = uv --version 2>&1 | Out-String
if ($uvVer -match "uv") {
    md "- **uv:** $($uvVer.Trim())"
    json "    `"uv`": `"$($uvVer.Trim())`","
} else {
    md "- **uv:** Not installed"
    json "    `"uv`": `"not installed`","
}

# rclone
$rcloneVer = rclone version 2>&1 | Select-String "rclone" | Out-String
if ($rcloneVer) {
    md "- **rclone:** $($rcloneVer.Trim())"
    json "    `"rclone`": `"$($rcloneVer.Trim())`","
} else {
    md "- **rclone:** Not installed"
    json "    `"rclone`": `"not installed`","
}

# NSSM
$nssmOK = Get-Command nssm -ErrorAction SilentlyContinue
if ($nssmOK) {
    md "- **NSSM:** Available"
    json "    `"nssm`": `"available`""
} else {
    md "- **NSSM:** Not installed (will be placed in deploy\bin\)"
    json "    `"nssm`": `"not installed`""
}

json "  },"

# ── 5. Permissions & Access ────────────────────────────────────────
Write-Host "[5/11] Checking permissions..."
md "## 5. Permissions & Access"
md ""

json "  `"permissions`": {"

# Admin check
md "- **Admin Privileges:** $(if ($isAdmin) {'Yes'} else {'No'})"

# PowerShell policy
$psPolicy = Get-ExecutionPolicy
md "- **PowerShell Execution Policy:** $psPolicy"
if ($psPolicy -eq "Restricted") {
    md "  - WARNING: Set to Restricted. Run 'Set-ExecutionPolicy RemoteSigned'"
}

# Firewall
$fwProfiles = Get-NetFirewallProfile -ErrorAction SilentlyContinue | Where-Object { $_.Enabled -eq $true }
if ($fwProfiles) {
    md "- **Windows Firewall:** Active (ports 4200, 8080 may need rules)"
    md "  - To add firewall rules, run these as Administrator:"
    md "    netsh advfirewall firewall add rule name=`"AAM Prefect 4200`" dir=in action=allow protocol=tcp localport=4200"
    md "    netsh advfirewall firewall add rule name=`"AAM Dashboard 8080`" dir=in action=allow protocol=tcp localport=8080"
    json "    `"firewall`": `"active`","
} else {
    md "- **Windows Firewall:** Inactive"
    json "    `"firewall`": `"inactive`","
}

# Long paths
$longPaths = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction SilentlyContinue
if ($longPaths -and $longPaths.LongPathsEnabled -eq 1) {
    md "- **Long Paths (>260 chars):** Enabled"
} else {
    md "- **Long Paths (>260 chars):** Disabled (deep directory backups may fail)"
    md "  - Fix: Run deploy\03_setup_system.bat, or enable via Group Policy:"
    md "    Computer Config > Admin Templates > System > Filesystem > Enable Win32 long paths"
}

json "    `"long_paths`": `"$(if ($longPaths -and $longPaths.LongPathsEnabled -eq 1) {'enabled'} else {'disabled'})`""
json "  },"

# ── 6. Existing Installation ───────────────────────────────────────
Write-Host "[6/11] Checking for existing installation..."
md "## 6. Existing Installation"
md ""

$json_install = "  `"existing_installation`": {"
$checks = @()

if (Test-Path (Join-Path $ReportDir "..\config.yaml")) {
    md "- **config.yaml:** Found"
    $checks += '"config_yaml": "found"'
} else {
    md "- **config.yaml:** Not found"
    $checks += '"config_yaml": "not found"'
}

if (Test-Path (Join-Path $ReportDir "..\logs")) {
    md "- **logs directory:** Found"
    $checks += '"logs_dir": "found"'
} else {
    md "- **logs directory:** Not found"
    $checks += '"logs_dir": "not found"'
}

if (Test-Path (Join-Path $ReportDir "..\manifest.db")) {
    md "- **manifest.db:** Found"
    $checks += '"manifest_db": "found"'
} else {
    md "- **manifest.db:** Not found (will be created on first run)"
    $checks += '"manifest_db": "not found"'
}

$json_install += ($checks -join ", ")
$json_install += " },"

json $json_install
md ""

# ── 7. Port Availability ───────────────────────────────────────────
Write-Host "[7/11] Checking port availability..."
md "## 7. Port Availability"
md ""

$json_ports = "  `"ports`": {"

$port4200 = netstat -an | Select-String ":4200 " | Select-String "LISTENING"
$port8080 = netstat -an | Select-String ":8080 " | Select-String "LISTENING"

if ($port4200) {
    md "- **Port 4200 (Prefect):** IN USE"
    $json_ports += ' "prefect_4200": "in_use"'
} else {
    md "- **Port 4200 (Prefect):** Available"
    $json_ports += ' "prefect_4200": "available"'
}

if ($port8080) {
    md "- **Port 8080 (Dashboard):** IN USE"
    $json_ports += ', "dashboard_8080": "in_use"'
} else {
    md "- **Port 8080 (Dashboard):** Available"
    $json_ports += ', "dashboard_8080": "available"'
}

$json_ports += " },"
json $json_ports
md ""

# ── 8. System Resources ───────────────────────────────────────────
Write-Host "[8/11] Checking system resources..."
md "## 8. System Resources"
md ""

$ramGB = [math]::Round((Get-WmiObject Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
$cpuName = (Get-WmiObject Win32_Processor).Name
$cpuCores = (Get-WmiObject Win32_Processor).NumberOfCores

md "- **Total RAM:** $ramGB GB"
md "- **CPU:** $cpuName"
md "- **CPU Cores:** $cpuCores"

json "  `"resources`": {"
json "    `"total_ram_gb`": $ramGB,"
json "    `"cpu`": `"$cpuName`","
json "    `"cpu_cores`": $cpuCores"
json "  },"

# ── 9. Timezone & Power ────────────────────────────────────────────
Write-Host "[9/11] Checking timezone and power settings..."
md "## 9. Timezone & Power"
md ""

$tz = (Get-TimeZone).DisplayName
md "- **Timezone:** $tz"

# Auto-updates
$noAutoUpdate = Get-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" -Name NoAutoUpdate -ErrorAction SilentlyContinue
if ($noAutoUpdate -and $noAutoUpdate.NoAutoUpdate -eq 1) {
    md "- **Auto Updates:** Disabled (good for servers)"
} else {
    md "- **Auto Updates:** Enabled (may cause unexpected reboots)"
    md "  - Fix: Run deploy\03_setup_system.bat to suppress auto-reboots"
}

# Pending reboot
$pendingReboot = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending" -ErrorAction SilentlyContinue
if ($pendingReboot) {
    md "- **Pending Reboot:** YES (reboot before deployment)"
} else {
    md "- **Pending Reboot:** No"
}

md ""

json "  `"timezone_power`": {"
json "    `"timezone`": `"$tz`""
json "  },"

# ── 10. GCS Connectivity ──────────────────────────────────────────
Write-Host "[10/11] Testing GCS connectivity..."
md "## 10. GCS Connectivity"
md ""

$gcsPing = Test-Connection -ComputerName storage.googleapis.com -Count 1 -TimeoutSeconds 2 -Quiet
if ($gcsPing) {
    md "- **storage.googleapis.com:** Reachable"
} else {
    md "- **storage.googleapis.com:** NOT reachable (cloud backups will fail)"
    md "  - Check: firewall rules, proxy settings, DNS resolution, internet connectivity"
}

# NTP
md "### NTP Accessibility"
md ""
$ntpOK = w32tm /stripchart /computer:time.windows.com /dataonly /samples:1 2>$null | Select-String ","
if ($ntpOK) {
    md "- **time.windows.com:** Reachable (UDP 123)"
} else {
    md "- **time.windows.com:** Blocked or Unreachable"
}

md ""

json "  `"gcs_connectivity`": {"
json "    `"reachable`": `"$(if ($gcsPing) {'yes'} else {'no'})`""
json "  },"

# ── 11. Windows Services ───────────────────────────────────────────
Write-Host "[11/11] Checking Windows services..."
md "## 11. Windows Services"
md ""

$criticalServices = @("LanmanServer", "LanmanWorkstation", "W32Time", "EventLog", "Schedule")
foreach ($svc in $criticalServices) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s) { md "- **$($s.Name):** $($s.Status)" }
}

md "### Existing AAM Services"
md ""
$aamServices = @("AamPrefectServer", "AamBackupAgent", "AamWatchdog")
foreach ($svc in $aamServices) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s) {
        md "- **$($s.Name):** $($s.Status)"
    } else {
        md "- **$svc:** Not installed"
    }
}

md ""
md "---"
md "*Report generated by AAM Backup Server Discovery Script (PowerShell)*"

# ── Write JSON ──────────────────────────────────────────────────────
$jsonParts.Add("}")
$jsonContent = $jsonParts -join "`n"
$jsonContent | Out-File -FilePath $ReportJSON -Encoding UTF8

Write-Host ""
Write-Host "===================================================================="
Write-Host "  Discovery Complete!"
Write-Host "===================================================================="
Write-Host ""
Write-Host "  Reports saved to:"
Write-Host "    $ReportMD"
Write-Host "    $ReportJSON"
Write-Host ""
Write-Host "  Please send both files to your deployment team."
Write-Host ""
pause
