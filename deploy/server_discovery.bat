@echo off
setlocal enabledelayedexpansion

:: ============================================================================
:: AAM Backup Automation - Server Discovery Script
:: 
:: Runs on Windows Server 2016+ without Python
:: Generates: server_discovery_report.md + server_discovery_report.json
:: ============================================================================

set "REPORT_DIR=%~dp0"
set "REPORT_MD=%REPORT_DIR%server_discovery_report.md"
set "REPORT_JSON=%REPORT_DIR%server_discovery_report.json"

echo.
echo ====================================================================
echo   AAM Backup Automation - Server Discovery
echo ====================================================================
echo   This script will gather system information for deployment.
echo   Reports will be saved to:
echo     %REPORT_MD%
echo     %REPORT_JSON%
echo ====================================================================
echo.

set "TARGET_IP="
set /p TARGET_IP="Enter Backup Server IP to test connectivity (or press Enter to skip): "
echo.

:: ── Initialize Reports ──────────────────────────────────────────────
echo # Server Discovery Report > "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo **Generated:** %DATE% %TIME% >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo { > "%REPORT_JSON%"
echo   "generated": "%DATE% %TIME%", >> "%REPORT_JSON%"

:: ── 1. System Information ──────────────────────────────────────────
echo [1/18] Gathering system information...

echo ## 1. System Information >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

:: Get hostname
for /f "tokens=2 delims==" %%a in ('wmic computersystem get name /value 2^>nul ^| find "="') do set "HOSTNAME=%%a"
echo - **Hostname:** %HOSTNAME% >> "%REPORT_MD%"

:: Get domain/workgroup
for /f "tokens=2 delims==" %%a in ('wmic computersystem get domain /value 2^>nul ^| find "="') do set "DOMAIN=%%a"
echo - **Domain:** %DOMAIN% >> "%REPORT_MD%"

:: Get Windows version
for /f "tokens=2 delims==" %%a in ('wmic os get caption /value 2^>nul ^| find "="') do set "WIN_VER=%%a"
echo - **Windows Version:** %WIN_VER% >> "%REPORT_MD%"

:: Get build number
for /f "tokens=2 delims==" %%a in ('wmic os get buildnumber /value 2^>nul ^| find "="') do set "BUILD_NUM=%%a"
echo - **Build Number:** %BUILD_NUM% >> "%REPORT_MD%"

:: Get current user
echo - **Current User:** %USERNAME% >> "%REPORT_MD%"

:: Check admin privileges
net session >nul 2>&1
if %errorlevel%==0 (
    echo - **Admin Privileges:** Yes >> "%REPORT_MD%"
    set "IS_ADMIN=Yes"
) else (
    echo - **Admin Privileges:** No >> "%REPORT_MD%"
    set "IS_ADMIN=No"
)

echo. >> "%REPORT_MD%"

echo   "system": { >> "%REPORT_JSON%"
echo     "hostname": "%HOSTNAME%", >> "%REPORT_JSON%"
echo     "domain": "%DOMAIN%", >> "%REPORT_JSON%"
echo     "windows_version": "%WIN_VER%", >> "%REPORT_JSON%"
echo     "build_number": "%BUILD_NUM%", >> "%REPORT_JSON%"
echo     "current_user": "%USERNAME%", >> "%REPORT_JSON%"
echo     "is_admin": "%IS_ADMIN%" >> "%REPORT_JSON%"
echo   }, >> "%REPORT_JSON%"

:: ── 2. Storage Information ─────────────────────────────────────────
echo [2/18] Gathering storage information...

echo ## 2. Storage Information >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo ### Drives >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo ^| Drive ^| Type ^| Total ^| Free ^| File System ^| >> "%REPORT_MD%"
echo ^|-------^|------^|-------^|------^|-------------^| >> "%REPORT_MD%"

echo   "drives": [ >> "%REPORT_JSON%"

set "DRIVE_COUNT=0"
:: wmic CSV output column order: Node,Caption,DriveType,FileSystem,FreeSpace,Size
:: tokens: %%a=Node %%b=Caption %%c=DriveType %%d=FileSystem %%e=FreeSpace %%f=Size
for /f "tokens=1-6 delims=," %%a in ('wmic logicaldisk get caption^,drivetype^,filesystem^,freespace^,size /format:csv 2^>nul ^| findstr /v "Caption" ^| findstr /v "^$"') do (
    if not "%%b"=="" (
    set /a DRIVE_COUNT+=1

    :: Drive type mapping (%%c = DriveType)
    set "DRIVE_TYPE=Unknown"
    if "%%c"=="2" set "DRIVE_TYPE=Removable"
    if "%%c"=="3" set "DRIVE_TYPE=Local"
    if "%%c"=="4" set "DRIVE_TYPE=Network"
    if "%%c"=="5" set "DRIVE_TYPE=CD-ROM"

    :: Convert bytes to GB using PowerShell to avoid 32-bit set/a overflow
    :: %%e=FreeSpace bytes, %%f=Size bytes
    for /f %%G in ('powershell -NoProfile -Command "[math]::Round([decimal]'%%f' / 1073741824, 1)" 2^>nul') do set "TOTAL_GB=%%G"
    for /f %%H in ('powershell -NoProfile -Command "[math]::Round([decimal]'%%e' / 1073741824, 1)" 2^>nul') do set "FREE_GB=%%H"
    if "!TOTAL_GB!"=="" set "TOTAL_GB=0"
    if "!FREE_GB!"=="" set "FREE_GB=0"

    echo ^| %%b ^| !DRIVE_TYPE! ^| !TOTAL_GB! GB ^| !FREE_GB! GB ^| %%d ^| >> "%REPORT_MD%"

    if !DRIVE_COUNT! GTR 1 echo , >> "%REPORT_JSON%"
    echo     { >> "%REPORT_JSON%"
    echo       "drive": "%%b", >> "%REPORT_JSON%"
    echo       "type": "!DRIVE_TYPE!", >> "%REPORT_JSON%"
    echo       "total_gb": !TOTAL_GB!, >> "%REPORT_JSON%"
    echo       "free_gb": !FREE_GB!, >> "%REPORT_JSON%"
    echo       "filesystem": "%%d" >> "%REPORT_JSON%"
    echo     } >> "%REPORT_JSON%"
    )
)

echo   ], >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: Check common source drives
echo ### Source Drive Check >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

for %%d in (C D E F G H) do (
    if exist "%%d:\" (
        echo - **%%d:\** exists >> "%REPORT_MD%"
        
        :: Check for common backup source folders
        if exist "%%d:\SOURCE" echo   - Found: %%d:\SOURCE >> "%REPORT_MD%"
        if exist "%%d:\DATA" echo   - Found: %%d:\DATA >> "%REPORT_MD%"
        if exist "%%d:\BACKUP" echo   - Found: %%d:\BACKUP >> "%REPORT_MD%"
        if exist "%%d:\Documents" echo   - Found: %%d:\Documents >> "%REPORT_MD%"
    )
)

echo. >> "%REPORT_MD%"

:: ── 3. Network Information ─────────────────────────────────────────
echo [3/18] Gathering network information...

echo ## 3. Network Information >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo ### IP Configuration >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo '>> "%REPORT_MD%"
ipconfig | findstr /i "IPv4 Subnet Gateway DNS" >> "%REPORT_MD%"
echo '>> "%REPORT_MD%"

echo   "network": { >> "%REPORT_JSON%"

:: Get primary IP
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4" ^| findstr /v "127.0.0.1"') do (
    set "PRIMARY_IP=%%a"
    set "PRIMARY_IP=!PRIMARY_IP: =!"
    goto :got_ip
)
:got_ip
echo     "primary_ip": "!PRIMARY_IP!", >> "%REPORT_JSON%"

:: Get default gateway
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "Default Gateway" ^| findstr /v ":"') do (
    set "GATEWAY=%%a"
    set "GATEWAY=!GATEWAY: =!"
    goto :got_gw
)
:got_gw
echo     "default_gateway": "!GATEWAY!", >> "%REPORT_JSON%"

echo ### Wake-on-LAN (WoL) Status >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
set "WOL_FOUND=0"
set "WOL_STATUS=none"
set "WOL_ADAPTER="

for /f "tokens=*" %%a in ('powershell -Command "try { Get-NetAdapterPowerManagement -ErrorAction Stop | Where-Object { $_.WakeOnMagicPacket -eq 'Enabled' } | Select-Object -ExpandProperty Name } catch { Write-Output 'ERROR' }" 2^>nul') do (
    if "%%a"=="ERROR" (
        echo - **WoL Check:** Could not determine ^(requires PowerShell 5.1+^) >> "%REPORT_MD%"
        set "WOL_STATUS=unknown"
        set "WOL_FOUND=-1"
    ) else if not "%%a"=="" (
        echo - **Adapter [%%a]:** Wake on Magic Packet is ENABLED >> "%REPORT_MD%"
        set "WOL_STATUS=enabled"
        set "WOL_ADAPTER=%%a"
        set "WOL_FOUND=1"
    )
)

if "!WOL_FOUND!"=="0" (
    echo - **WoL Check:** No adapters found with Wake on Magic Packet enabled >> "%REPORT_MD%"
    set "WOL_STATUS=disabled"
)
echo. >> "%REPORT_MD%"

echo     "wol_status": "!WOL_STATUS!", >> "%REPORT_JSON%"
if "!WOL_ADAPTER!"=="" (
    echo     "wol_adapter": null, >> "%REPORT_JSON%"
) else (
    echo     "wol_adapter": "!WOL_ADAPTER!", >> "%REPORT_JSON%"
)

echo ### DNS Resolution Test >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

nslookup google.com >nul 2>&1
if %errorlevel%==0 (
    echo - **DNS Resolution:** Working >> "%REPORT_MD%"
    echo     "dns_resolution": "working", >> "%REPORT_JSON%"
) else (
    echo - **DNS Resolution:** Failed >> "%REPORT_MD%"
    echo     "dns_resolution": "failed", >> "%REPORT_JSON%"
)

echo ### Internet Connectivity >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

:: Use ping with timeout (2 seconds) to avoid hanging
ping -n 1 -w 2000 8.8.8.8 >nul 2>&1
if %errorlevel%==0 (
    echo - **Internet Access:** Available >> "%REPORT_MD%"
    echo     "internet_access": "available", >> "%REPORT_JSON%"
) else (
    echo - **Internet Access:** Not available or blocked >> "%REPORT_MD%"
    echo     "internet_access": "blocked", >> "%REPORT_JSON%"
)

echo. >> "%REPORT_MD%"

echo ### Local Network Devices (ARP Cache) >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo Identifying local network devices for potential WoL targets: >> "%REPORT_MD%"
echo ^`^`^` >> "%REPORT_MD%"
arp -a >> "%REPORT_MD%"
echo ^`^`^` >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo ### Target Backup Server Test >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo     "target_server": { >> "%REPORT_JSON%"
if not "!TARGET_IP!"=="" (
    echo - **Target IP:** !TARGET_IP! >> "%REPORT_MD%"
    echo       "ip": "!TARGET_IP!", >> "%REPORT_JSON%"
    
    :: Ping test
    ping -n 1 -w 2000 !TARGET_IP! >nul 2>&1
    if !errorlevel!==0 (
        echo - **Ping:** Successful >> "%REPORT_MD%"
        echo       "ping": "successful", >> "%REPORT_JSON%"
        
        :: ARP test for MAC address (useful for WoL)
        set "TARGET_MAC="
        for /f "tokens=2" %%m in ('arp -a !TARGET_IP! 2^>nul ^| findstr /i "!TARGET_IP!"') do set "TARGET_MAC=%%m"
        if not "!TARGET_MAC!"=="" (
            echo - **MAC Address:** !TARGET_MAC! ^(can be used for WoL^) >> "%REPORT_MD%"
            echo       "mac_address": "!TARGET_MAC!", >> "%REPORT_JSON%"
        ) else (
            echo - **MAC Address:** Could not resolve via ARP >> "%REPORT_MD%"
            echo       "mac_address": null, >> "%REPORT_JSON%"
        )
        
        :: Test SMB Port 445
        powershell -Command "try { $tcp = New-Object System.Net.Sockets.TcpClient; $result = $tcp.BeginConnect('!TARGET_IP!', 445, $null, $null); $wait = $result.AsyncWaitHandle.WaitOne(2000, $false); if ($wait) { $tcp.EndConnect($result); $tcp.Close(); Write-Output 'True' } else { $tcp.Close(); Write-Output 'False' } } catch { Write-Output 'False' }" 2^>nul | findstr "True" >nul 2>&1
        if !errorlevel!==0 (
            echo - **SMB Port 445:** Open ^(File sharing reachable^) >> "%REPORT_MD%"
            echo       "smb_port_445": "open" >> "%REPORT_JSON%"
        ) else (
            echo - **SMB Port 445:** Blocked or Unreachable >> "%REPORT_MD%"
            echo       "smb_port_445": "blocked" >> "%REPORT_JSON%"
        )
    ) else (
        echo - **Ping:** Failed ^(Host unreachable or ICMP blocked^) >> "%REPORT_MD%"
        echo       "ping": "failed", >> "%REPORT_JSON%"
        echo       "mac_address": null, >> "%REPORT_JSON%"
        echo       "smb_port_445": "unknown" >> "%REPORT_JSON%"
    )
) else (
    echo - No target IP provided. Skipped. >> "%REPORT_MD%"
    echo       "ip": null >> "%REPORT_JSON%"
)
echo     } >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

echo   }, >> "%REPORT_JSON%"

:: ── 4. Software & Tools ────────────────────────────────────────────
echo [4/18] Checking software and tools...

echo ## 4. Software & Tools >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "software": { >> "%REPORT_JSON%"

:: Check Python
echo ### Python >> "%REPORT_MD%"
python --version >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
    echo - **Python:** !PY_VER! >> "%REPORT_MD%"
    echo     "python": "!PY_VER!", >> "%REPORT_JSON%"
) else (
    echo - **Python:** Not installed >> "%REPORT_MD%"
    echo     "python": "not installed", >> "%REPORT_JSON%"
)

:: Check Python3
python3 --version >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=*" %%v in ('python3 --version 2^>^&1') do set "PY3_VER=%%v"
    echo - **Python3:** !PY3_VER! >> "%REPORT_MD%"
)

:: Check uv
uv --version >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=*" %%v in ('uv --version 2^>^&1') do set "UV_VER=%%v"
    echo - **uv:** !UV_VER! >> "%REPORT_MD%"
    echo     "uv": "!UV_VER!", >> "%REPORT_JSON%"
) else (
    echo - **uv:** Not installed >> "%REPORT_MD%"
    echo     "uv": "not installed", >> "%REPORT_JSON%"
)

:: Check pip
pip --version >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=*" %%v in ('pip --version 2^>^&1') do set "PIP_VER=%%v"
    echo - **pip:** !PIP_VER! >> "%REPORT_MD%"
    echo     "pip": "!PIP_VER!", >> "%REPORT_JSON%"
) else (
    echo - **pip:** Not installed >> "%REPORT_MD%"
    echo     "pip": "not installed", >> "%REPORT_JSON%"
)

:: Check rclone
rclone version >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=*" %%v in ('rclone version 2^>^&1 ^| findstr "rclone"') do set "RCLONE_VER=%%v"
    echo - **rclone:** !RCLONE_VER! >> "%REPORT_MD%"
    echo     "rclone": "!RCLONE_VER!", >> "%REPORT_JSON%"
) else (
    echo - **rclone:** Not installed >> "%REPORT_MD%"
    echo     "rclone": "not installed", >> "%REPORT_JSON%"
)

:: Check robocopy — note: robocopy /? returns exit code 16, so use 'where' instead
where robocopy >nul 2>&1
if %errorlevel%==0 (
    echo - **robocopy:** Available >> "%REPORT_MD%"
    echo     "robocopy": "available", >> "%REPORT_JSON%"
) else (
    echo - **robocopy:** Not available >> "%REPORT_MD%"
    echo     "robocopy": "not available", >> "%REPORT_JSON%"
)

:: Check NSSM
nssm version >nul 2>&1
if %errorlevel%==0 (
    echo - **NSSM:** Available >> "%REPORT_MD%"
    echo     "nssm": "available" >> "%REPORT_JSON%"
) else (
    echo - **NSSM:** Not installed >> "%REPORT_MD%"
    echo     "nssm": "not installed" >> "%REPORT_JSON%"
)

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 5. Permissions & Access ────────────────────────────────────────
echo [5/18] Checking permissions and access...

echo ## 5. Permissions & Access >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "permissions": { >> "%REPORT_JSON%"

:: Check service install permissions
sc query type= service state= all >nul 2>&1
if %errorlevel%==0 (
    echo - **Service Query:** Allowed >> "%REPORT_MD%"
    echo     "service_query": "allowed", >> "%REPORT_JSON%"
) else (
    echo - **Service Query:** Denied >> "%REPORT_MD%"
    echo     "service_query": "denied", >> "%REPORT_JSON%"
)

:: Check if can create services
sc query AamBackupAgent >nul 2>&1
if %errorlevel%==0 (
    echo - **Existing Service (AamBackupAgent):** Found >> "%REPORT_MD%"
    echo     "existing_service": "found", >> "%REPORT_JSON%"
) else (
    echo - **Existing Service (AamBackupAgent):** Not found >> "%REPORT_MD%"
    echo     "existing_service": "not found", >> "%REPORT_JSON%"
)

:: Network Share Access check removed - performing random net use /delete on a live server is risky.
echo ### Network Share Access >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo - Skipped automated share test to ensure zero impact on live network connections. >> "%REPORT_MD%"

echo ### Local Administrators >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo The following users can be used to run the backup service (AamBackupAgent) >> "%REPORT_MD%"
echo to ensure access to UNC paths: >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "administrators": [ >> "%REPORT_JSON%"
set "FIRST_ADMIN=1"
for /f "tokens=*" %%a in ('net localgroup administrators ^| findstr /v "Alias Name Comment Members \-\-\- The command completed"') do (
    if not "%%a"=="" (
        echo - %%a >> "%REPORT_MD%"
        if "!FIRST_ADMIN!"=="1" (
            set "FIRST_ADMIN=0"
        ) else (
            echo , >> "%REPORT_JSON%"
        )
        echo     "%%a" >> "%REPORT_JSON%"
    )
)
echo   ] >> "%REPORT_JSON%"

echo ### PowerShell Execution Policy >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

for /f "tokens=*" %%a in ('powershell -noprofile -command "Get-ExecutionPolicy"') do set "PS_EXEC_POLICY=%%a"
echo - **Execution Policy:** %PS_EXEC_POLICY% >> "%REPORT_MD%"
echo     ,"powershell_execution_policy": "%PS_EXEC_POLICY%" >> "%REPORT_JSON%"

if /I "%PS_EXEC_POLICY%"=="Restricted" (
    echo   - WARNING: Set to Restricted. PowerShell scripts will fail. Run 'Set-ExecutionPolicy RemoteSigned' >> "%REPORT_MD%"
)

echo ### User Account Control (UAC) >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v EnableLUA >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=3" %%a in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v EnableLUA 2^>nul ^| findstr "EnableLUA"') do set "UAC_ENABLED=%%a"
    if "!UAC_ENABLED!"=="0x1" (
        echo - **UAC Status:** Enabled ^(Strict mode may require explicit admin elevation^) >> "%REPORT_MD%"
        echo     ,"uac_enabled": "yes" >> "%REPORT_JSON%"
    ) else (
        echo - **UAC Status:** Disabled >> "%REPORT_MD%"
        echo     ,"uac_enabled": "no" >> "%REPORT_JSON%"
    )
) else (
    echo - **UAC Status:** Could not determine >> "%REPORT_MD%"
    echo     ,"uac_enabled": "unknown" >> "%REPORT_JSON%"
)

echo   }, >> "%REPORT_JSON%"

echo. >> "%REPORT_MD%"

:: ── 6. Existing Installation ───────────────────────────────────────
echo [6/18] Checking for existing installation...

echo ## 6. Existing Installation >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "existing_installation": { >> "%REPORT_JSON%"

:: Check for config.yaml
if exist "%~dp0..\config.yaml" (
    echo - **config.yaml:** Found >> "%REPORT_MD%"
    echo     "config_yaml": "found", >> "%REPORT_JSON%"
) else (
    echo - **config.yaml:** Not found >> "%REPORT_MD%"
    echo     "config_yaml": "not found", >> "%REPORT_JSON%"
)

:: Check for logs directory
if exist "%~dp0..\logs" (
    echo - **logs directory:** Found >> "%REPORT_MD%"
    echo     "logs_dir": "found", >> "%REPORT_JSON%"
    
    :: Count log files
    set "LOG_COUNT=0"
    for %%f in ("%~dp0..\logs\*.log") do set /a LOG_COUNT+=1
    echo   - Log files: !LOG_COUNT! >> "%REPORT_MD%"
) else (
    echo - **logs directory:** Not found >> "%REPORT_MD%"
    echo     "logs_dir": "not found", >> "%REPORT_JSON%"
)

:: Check for .prefect directory
if exist "%~dp0..\.prefect" (
    echo - **.prefect directory:** Found >> "%REPORT_MD%"
    echo     "prefect_dir": "found", >> "%REPORT_JSON%"
) else (
    echo - **.prefect directory:** Not found >> "%REPORT_MD%"
    echo     "prefect_dir": "not found", >> "%REPORT_JSON%"
)

:: Check for database
if exist "%~dp0..\manifest.db" (
    echo - **manifest.db:** Found >> "%REPORT_MD%"
    echo     "manifest_db": "found", >> "%REPORT_JSON%"
) else (
    echo - **manifest.db:** Not found >> "%REPORT_MD%"
    echo     "manifest_db": "not found", >> "%REPORT_JSON%"
)

:: Check for GCS key
if exist "%~dp0..\*.json" (
    echo - **GCS key file:** Found >> "%REPORT_MD%"
    echo     "gcs_key": "found" >> "%REPORT_JSON%"
) else (
    echo - **GCS key file:** Not found >> "%REPORT_MD%"
    echo     "gcs_key": "not found" >> "%REPORT_JSON%"
)

:: Note the trailing comma — more JSON sections follow
echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 16. Connectivity Tests ────────────────────────────────────────
echo [16/18] Testing connectivity to AAM services...

echo ## 16. Connectivity Tests >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "connectivity": { >> "%REPORT_JSON%"

echo ### Proxy Settings >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

set "PROXY_ENABLED=no"
set "PROXY_SERVER="

reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=3" %%a in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable 2^>nul ^| findstr "ProxyEnable"') do (
        if "%%a"=="0x1" set "PROXY_ENABLED=yes"
    )
)

if "!PROXY_ENABLED!"=="yes" (
    for /f "tokens=3" %%a in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer 2^>nul ^| findstr "ProxyServer"') do set "PROXY_SERVER=%%a"
    echo - **System Proxy:** Enabled ^(!PROXY_SERVER!^) >> "%REPORT_MD%"
    echo     "proxy_enabled": "yes", >> "%REPORT_JSON%"
    echo     "proxy_server": "!PROXY_SERVER!", >> "%REPORT_JSON%"
) else (
    echo - **System Proxy:** Not configured >> "%REPORT_MD%"
    echo     "proxy_enabled": "no", >> "%REPORT_JSON%"
)

if defined HTTP_PROXY (
    echo - **HTTP_PROXY (Env):** %HTTP_PROXY% >> "%REPORT_MD%"
    echo     "env_http_proxy": "%HTTP_PROXY%", >> "%REPORT_JSON%"
)
if defined HTTPS_PROXY (
    echo - **HTTPS_PROXY (Env):** %HTTPS_PROXY% >> "%REPORT_MD%"
    echo     "env_https_proxy": "%HTTPS_PROXY%", >> "%REPORT_JSON%"
)

echo. >> "%REPORT_MD%"

:: Test Google Cloud Storage connectivity
echo ### Google Cloud Storage >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

:: Use ping with timeout (2 seconds) to avoid hanging
ping -n 1 -w 2000 storage.googleapis.com >nul 2>&1
if %errorlevel%==0 (
    echo - **storage.googleapis.com:** Reachable >> "%REPORT_MD%"
    echo     "gcs_reachable": "yes", >> "%REPORT_JSON%"
) else (
    echo - **storage.googleapis.com:** NOT reachable (cloud backups will fail) >> "%REPORT_MD%"
    echo     "gcs_reachable": "no", >> "%REPORT_JSON%"
)

:: Test Time Skew against Google
echo ### Time Skew Check >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

set "SKEW_STATUS=ERROR"
set "SKEW_DIFF=0"
for /f "tokens=1,2 delims=|" %%a in ('powershell -Command "try { $res = Invoke-WebRequest -Uri 'http://google.com' -Method Head -UseBasicParsing -TimeoutSec 5; $dateStr = $res.Headers['Date']; $googleTime = [DateTime]::ParseExact($dateStr, 'r', [System.Globalization.CultureInfo]::InvariantCulture).ToUniversalTime(); $localTime = [DateTime]::UtcNow; $diff = [math]::Round([math]::Abs(($googleTime - $localTime).TotalSeconds)); if ($diff -gt 180) { Write-Output \"FAIL|$diff\" } else { Write-Output \"PASS|$diff\" } } catch { Write-Output 'ERROR|0' }" 2^>nul') do (
    set "SKEW_STATUS=%%a"
    set "SKEW_DIFF=%%b"
)

if "!SKEW_STATUS!"=="PASS" (
    echo - **Time Skew:** OK (difference: !SKEW_DIFF! seconds) >> "%REPORT_MD%"
    echo     "time_skew": "ok", >> "%REPORT_JSON%"
    echo     "time_skew_seconds": !SKEW_DIFF!, >> "%REPORT_JSON%"
) else if "!SKEW_STATUS!"=="FAIL" (
    echo - **Time Skew:** FAILED (difference: !SKEW_DIFF! seconds) - RUN w32tm /resync >> "%REPORT_MD%"
    echo     "time_skew": "failed", >> "%REPORT_JSON%"
    echo     "time_skew_seconds": !SKEW_DIFF!, >> "%REPORT_JSON%"
) else (
    echo - **Time Skew:** Could not verify against google.com >> "%REPORT_MD%"
    echo     "time_skew": "error", >> "%REPORT_JSON%"
    echo     "time_skew_seconds": null, >> "%REPORT_JSON%"
)


:: Test NTP Accessibility
echo ### NTP Accessibility >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo Testing NTP (UDP 123) against common time servers (may take a few seconds)... >> "%REPORT_MD%"

:: Test time.windows.com
w32tm /stripchart /computer:time.windows.com /dataonly /samples:1 2>nul | findstr /R /C:", [+\-]" >nul 2>&1
if %errorlevel%==0 (
    echo - **time.windows.com:** Reachable (UDP 123 Open) >> "%REPORT_MD%"
    echo     "ntp_windows": "open", >> "%REPORT_JSON%"
) else (
    echo - **time.windows.com:** Blocked or Unreachable >> "%REPORT_MD%"
    echo     "ntp_windows": "blocked", >> "%REPORT_JSON%"
)

:: Test pool.ntp.org
w32tm /stripchart /computer:pool.ntp.org /dataonly /samples:1 2>nul | findstr /R /C:", [+\-]" >nul 2>&1
if %errorlevel%==0 (
    echo - **pool.ntp.org:** Reachable (UDP 123 Open) >> "%REPORT_MD%"
    echo     "ntp_pool": "open", >> "%REPORT_JSON%"
) else (
    echo - **pool.ntp.org:** Blocked or Unreachable >> "%REPORT_MD%"
    echo     "ntp_pool": "blocked", >> "%REPORT_JSON%"
)

echo. >> "%REPORT_MD%"

:: Test SMTP connectivity (common ports)
echo ### SMTP Connectivity >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

:: Use PowerShell with timeout to avoid hanging
:: Note: Test-NetConnection is available on Server 2016+ (PowerShell 5.1)
echo Testing SMTP ports (may take 30-60 seconds if blocked)... >> "%REPORT_MD%"

:: Test port 587 (TLS) - with 5 second timeout
powershell -Command "try { $tcp = New-Object System.Net.Sockets.TcpClient; $result = $tcp.BeginConnect('smtp.gmail.com', 587, $null, $null); $wait = $result.AsyncWaitHandle.WaitOne(5000, $false); if ($wait) { $tcp.EndConnect($result); $tcp.Close(); Write-Output 'True' } else { $tcp.Close(); Write-Output 'False' } } catch { Write-Output 'False' }" 2>nul | findstr "True" >nul 2>&1
if %errorlevel%==0 (
    echo - **Port 587 (SMTP TLS):** Open >> "%REPORT_MD%"
    echo     "smtp_587": "open", >> "%REPORT_JSON%"
) else (
    echo - **Port 587 (SMTP TLS):** Blocked or unreachable >> "%REPORT_MD%"
    echo     "smtp_587": "blocked", >> "%REPORT_JSON%"
)

:: Test port 465 (SSL) - with 5 second timeout
powershell -Command "try { $tcp = New-Object System.Net.Sockets.TcpClient; $result = $tcp.BeginConnect('smtp.gmail.com', 465, $null, $null); $wait = $result.AsyncWaitHandle.WaitOne(5000, $false); if ($wait) { $tcp.EndConnect($result); $tcp.Close(); Write-Output 'True' } else { $tcp.Close(); Write-Output 'False' } } catch { Write-Output 'False' }" 2>nul | findstr "True" >nul 2>&1
if %errorlevel%==0 (
    echo - **Port 465 (SMTP SSL):** Open >> "%REPORT_MD%"
    echo     "smtp_465": "open", >> "%REPORT_JSON%"
) else (
    echo - **Port 465 (SMTP SSL):** Blocked or unreachable >> "%REPORT_MD%"
    echo     "smtp_465": "blocked", >> "%REPORT_JSON%"
)

:: Test port 25 (Plain) - with 5 second timeout
powershell -Command "try { $tcp = New-Object System.Net.Sockets.TcpClient; $result = $tcp.BeginConnect('smtp.gmail.com', 25, $null, $null); $wait = $result.AsyncWaitHandle.WaitOne(5000, $false); if ($wait) { $tcp.EndConnect($result); $tcp.Close(); Write-Output 'True' } else { $tcp.Close(); Write-Output 'False' } } catch { Write-Output 'False' }" 2>nul | findstr "True" >nul 2>&1
if %errorlevel%==0 (
    echo - **Port 25 (SMTP Plain):** Open >> "%REPORT_MD%"
    echo     "smtp_25": "open" >> "%REPORT_JSON%"
) else (
    echo - **Port 25 (SMTP Plain):** Blocked or unreachable >> "%REPORT_MD%"
    echo     "smtp_25": "blocked" >> "%REPORT_JSON%"
)

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 17. Pending Reboot & Windows Update ────────────────────────────
echo [17/18] Checking pending reboot and Windows Update...

echo ## 17. Pending Reboot & Windows Update >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "reboot_update": { >> "%REPORT_JSON%"

:: Check for pending reboot
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending" >nul 2>&1
if %errorlevel%==0 (
    echo - **Pending Reboot:** YES (reboot before deployment) >> "%REPORT_MD%"
    echo     "pending_reboot": "yes", >> "%REPORT_JSON%"
) else (
    echo - **Pending Reboot:** No >> "%REPORT_MD%"
    echo     "pending_reboot": "no", >> "%REPORT_JSON%"
)

:: Check for pending Windows Update
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired" >nul 2>&1
if %errorlevel%==0 (
    echo - **Windows Update Reboot:** Required >> "%REPORT_MD%"
    echo     "update_reboot": "required", >> "%REPORT_JSON%"
) else (
    echo - **Windows Update Reboot:** Not required >> "%REPORT_MD%"
    echo     "update_reboot": "not_required", >> "%REPORT_JSON%"
)

:: Check last update time
echo ### Recent Updates >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

wmic qfe list brief /format:table 2>nul | findstr /v "Description" | findstr /v "^$" >nul 2>&1
if %errorlevel%==0 (
    echo - **Recent Updates:** Retrieved (see JSON) >> "%REPORT_MD%"
    
    echo     "recent_updates": [ >> "%REPORT_JSON%"
    set "FIRST=1"
    for /f "tokens=1,2,3" %%a in ('wmic qfe list brief /format:table 2^>nul ^| findstr /v "Description" ^| findstr /v "^$"') do (
        if not "%%a"=="" (
            if "!FIRST!"=="1" (
                set "FIRST=0"
            ) else (
                echo , >> "%REPORT_JSON%"
            )
            echo       {"hotfix": "%%a", "installed": "%%b %%c"} >> "%REPORT_JSON%"
        )
    )
    echo     ] >> "%REPORT_JSON%"
) else (
    echo - **Recent Updates:** Could not retrieve >> "%REPORT_MD%"
    echo     "recent_updates": [] >> "%REPORT_JSON%"
)

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 18. Final Summary & Recommendations ───────────────────────────
echo [18/18] Generating final summary...

echo ## 18. Final Summary & Recommendations >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "summary": { >> "%REPORT_JSON%"

set "READY=Yes"
set "ISSUES="
set "WARNINGS="

:: Critical checks
if not exist "%~dp0..\config.yaml" (
    set "READY=No"
    set "ISSUES=!ISSUES!- Missing config.yaml\n"
)

if "!IS_ADMIN!"=="No" (
    set "READY=No"
    set "ISSUES=!ISSUES!- Not running as administrator\n"
)

:: Check for pending reboot
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending" >nul 2>&1
if %errorlevel%==0 (
    set "WARNINGS=!WARNINGS!- Pending reboot detected\n"
)

:: Check GCS connectivity
ping -n 1 -w 2000 storage.googleapis.com >nul 2>&1
if not %errorlevel%==0 (
    set "READY=No"
    set "ISSUES=!ISSUES!- Cannot reach Google Cloud Storage\n"
)

:: Check port availability
netstat -an | findstr ":4200 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    set "WARNINGS=!WARNINGS!- Port 4200 already in use\n"
)

netstat -an | findstr ":8080 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    set "WARNINGS=!WARNINGS!- Port 8080 already in use\n"
)

:: Output summary
if "!READY!"=="Yes" (
    echo ### Deployment Readiness: READY >> "%REPORT_MD%"
    echo     "ready": "yes", >> "%REPORT_JSON%"
) else (
    echo ### Deployment Readiness: ISSUES FOUND >> "%REPORT_MD%"
    echo     "ready": "no", >> "%REPORT_JSON%"
    echo. >> "%REPORT_MD%"
    echo **Critical Issues:** >> "%REPORT_MD%"
    echo !ISSUES! >> "%REPORT_MD%"
)

if not "!WARNINGS!"=="" (
    echo. >> "%REPORT_MD%"
    echo **Warnings:** >> "%REPORT_MD%"
    echo !WARNINGS! >> "%REPORT_MD%"
    echo     "warnings": "!WARNINGS!", >> "%REPORT_JSON%"
) else (
    echo     "warnings": "none", >> "%REPORT_JSON%"
)

echo. >> "%REPORT_MD%"
echo ### Next Steps >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo 1. Send both reports to deployment team >> "%REPORT_MD%"
echo 2. Team will generate config.yaml from collected data >> "%REPORT_MD%"
echo 3. Schedule deployment window >> "%REPORT_MD%"
echo 4. Run install_services.bat as Administrator >> "%REPORT_MD%"

echo     "issues": "!ISSUES!", >> "%REPORT_JSON%"
echo     "next_steps": "Send reports to deployment team" >> "%REPORT_JSON%"

:: Trailing comma — services and other sections follow
echo   }, >> "%REPORT_JSON%"

:: ── 13. Windows Services Status ────────────────────────────────────
echo [13/18] Checking Windows services...

echo ## 13. Windows Services Status >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "services": { >> "%REPORT_JSON%"

:: Check critical services
echo ### Critical Services >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

for %%s in (LanmanServer LanmanWorkstation W32Time EventLog Schedule Spooler wuauserv) do (
    sc query %%s >nul 2>&1
    if !errorlevel!==0 (
        for /f "tokens=3 delims=: " %%a in ('sc query %%s ^| findstr "STATE"') do (
            echo - **%%s:** %%a >> "%REPORT_MD%"
        )
    )
)

echo     "critical_services_checked": "yes", >> "%REPORT_JSON%"

:: Check for services that might interfere
echo ### Potential Interference >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

set "INTERFERENCE_FOUND=0"
for %%s in (WSearch Spooler SQLSERVERAGENT MSSQLSERVER) do (
    sc query %%s >nul 2>&1
    if !errorlevel!==0 (
        for /f "tokens=3 delims=: " %%a in ('sc query %%s ^| findstr "STATE"') do (
            echo - **%%s:** %%a (may use resources) >> "%REPORT_MD%"
            set "INTERFERENCE_FOUND=1"
        )
    )
)

if "!INTERFERENCE_FOUND!"=="0" (
    echo - No interfering services detected >> "%REPORT_MD%"
)

echo     "interference_checked": "yes", >> "%REPORT_JSON%"

echo ### VSS (Volume Shadow Copy) Health >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

sc query vss >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=3 delims=: " %%a in ('sc query vss ^| findstr "STATE"') do set "VSS_STATE=%%a"
    echo - **VSS Service State:** !VSS_STATE! >> "%REPORT_MD%"
    echo     "vss_service": "!VSS_STATE!", >> "%REPORT_JSON%"
) else (
    echo - **VSS Service:** Not found or inaccessible >> "%REPORT_MD%"
    echo     "vss_service": "unknown", >> "%REPORT_JSON%"
)

:: Check for VSS writer errors
echo - Checking VSS Writers... >> "%REPORT_MD%"
vssadmin list writers 2>nul | findstr /i "error state" | findstr /v /c:"No error" >nul 2>&1
if %errorlevel%==0 (
    echo - **VSS Writers:** ERRORS DETECTED (backups of locked files may fail) >> "%REPORT_MD%"
    echo     "vss_writers_healthy": "no" >> "%REPORT_JSON%"
    echo ^`^`^` >> "%REPORT_MD%"
    vssadmin list writers | findstr /B /C:"Writer name:" /C:"   State:" /C:"   Last error:" >> "%REPORT_MD%"
    echo ^`^`^` >> "%REPORT_MD%"
) else (
    echo - **VSS Writers:** All writers stable/healthy >> "%REPORT_MD%"
    echo     "vss_writers_healthy": "yes" >> "%REPORT_JSON%"
)

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 14. Installed Software ────────────────────────────────────────
echo [14/18] Checking installed software...

echo ## 14. Installed Software >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "installed_software": { >> "%REPORT_JSON%"

:: Get installed programs
echo ### Key Software >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

wmic product get name,version /format:csv 2>nul | findstr /v "Name" | findstr /v "^$" >nul 2>&1
if %errorlevel%==0 (
    echo - **Installed Programs:** Retrieved (see JSON) >> "%REPORT_MD%"
    
    echo     "programs": [ >> "%REPORT_JSON%"
    set "FIRST=1"
    for /f "tokens=2,3 delims=," %%a in ('wmic product get name^,version /format:csv 2^>nul ^| findstr /v "Name" ^| findstr /v "^$"') do (
        if not "%%a"=="" (
            if "!FIRST!"=="1" (
                set "FIRST=0"
            ) else (
                echo , >> "%REPORT_JSON%"
            )
            echo       {"name": "%%a", "version": "%%b"} >> "%REPORT_JSON%"
        )
    )
    echo     ], >> "%REPORT_JSON%"
) else (
    echo - **Installed Programs:** Could not retrieve >> "%REPORT_MD%"
    echo     "programs": [], >> "%REPORT_JSON%"
)

:: Check for Python installations
echo ### Python Installations >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

where python >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=*" %%p in ('where python 2^>nul') do (
        echo - **Python found at:** %%p >> "%REPORT_MD%"
    )
)

echo     "python_locations_checked": "yes" >> "%REPORT_JSON%"

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 15. Environment & System Variables ─────────────────────────────
echo [15/18] Checking environment variables...

echo ## 15. Environment & System Variables >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "environment": { >> "%REPORT_JSON%"

:: Check PATH for relevant tools
echo ### PATH Check >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo - **PATH entries:** >> "%REPORT_MD%"
echo '>> "%REPORT_MD%"
echo %PATH% | findstr /i "python rclone nssm uv" >> "%REPORT_MD%"
echo '>> "%REPORT_MD%"

echo     "path_checked": "yes", >> "%REPORT_JSON%"

:: Check for relevant environment variables
echo ### Relevant Environment Variables >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

if defined PYTHON_HOME (
    echo - **PYTHON_HOME:** %PYTHON_HOME% >> "%REPORT_MD%"
    echo     "python_home": "%PYTHON_HOME%", >> "%REPORT_JSON%"
) else (
    echo     "python_home": "not set", >> "%REPORT_JSON%"
)

if defined PYTHONPATH (
    echo - **PYTHONPATH:** %PYTHONPATH% >> "%REPORT_MD%"
    echo     "pythonpath": "%PYTHONPATH%", >> "%REPORT_JSON%"
) else (
    echo     "pythonpath": "not set", >> "%REPORT_JSON%"
)

if defined UV_HOME (
    echo - **UV_HOME:** %UV_HOME% >> "%REPORT_MD%"
    echo     "uv_home": "%UV_HOME%", >> "%REPORT_JSON%"
) else (
    echo     "uv_home": "not set", >> "%REPORT_JSON%"
)

if defined PREFECT_HOME (
    echo - **PREFECT_HOME:** %PREFECT_HOME% >> "%REPORT_MD%"
    echo     "prefect_home": "%PREFECT_HOME%", >> "%REPORT_JSON%"
) else (
    echo     "prefect_home": "not set", >> "%REPORT_JSON%"
)

if defined RCLONE_CONFIG (
    echo - **RCLONE_CONFIG:** %RCLONE_CONFIG% >> "%REPORT_MD%"
    echo     "rclone_config": "%RCLONE_CONFIG%", >> "%REPORT_JSON%"
) else (
    echo     "rclone_config": "not set", >> "%REPORT_JSON%"
)

:: Check TEMP/TMP paths
echo ### Temp Paths >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo - **TEMP:** %TEMP% >> "%REPORT_MD%"
echo - **TMP:** %TMP% >> "%REPORT_MD%"

echo     "temp_path": "%TEMP%", >> "%REPORT_JSON%"
echo     "tmp_path": "%TMP%" >> "%REPORT_JSON%"

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 7. Port Availability ────────────────────────────────────────────
echo [7/18] Checking port availability...

echo ## 7. Port Availability >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "ports": { >> "%REPORT_JSON%"

:: Check port 4200 (Prefect Server)
netstat -an | findstr ":4200 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo - **Port 4200 (Prefect):** IN USE >> "%REPORT_MD%"
    echo     "prefect_4200": "in_use", >> "%REPORT_JSON%"
) else (
    echo - **Port 4200 (Prefect):** Available >> "%REPORT_MD%"
    echo     "prefect_4200": "available", >> "%REPORT_JSON%"
)

:: Check port 8080 (Dashboard)
netstat -an | findstr ":8080 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo - **Port 8080 (Dashboard):** IN USE >> "%REPORT_MD%"
    echo     "dashboard_8080": "in_use" >> "%REPORT_JSON%"
) else (
    echo - **Port 8080 (Dashboard):** Available >> "%REPORT_MD%"
    echo     "dashboard_8080": "available" >> "%REPORT_JSON%"
)

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 8. System Resources ───────────────────────────────────────────
echo [8/18] Checking system resources...

echo ## 8. System Resources >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "resources": { >> "%REPORT_JSON%"

:: Get total RAM — use PowerShell to avoid 32-bit set/a overflow (e.g. 8 GB = 8589934592 overflows)
for /f %%a in ('powershell -NoProfile -Command "[math]::Round((Get-WmiObject Win32_ComputerSystem).TotalPhysicalMemory / 1073741824, 1)" 2^>nul') do set "TOTAL_RAM_GB=%%a"
if "!TOTAL_RAM_GB!"=="" set "TOTAL_RAM_GB=0"
echo - **Total RAM:** !TOTAL_RAM_GB! GB >> "%REPORT_MD%"
echo     "total_ram_gb": !TOTAL_RAM_GB!, >> "%REPORT_JSON%"

:: Get CPU info
for /f "tokens=2 delims==" %%a in ('wmic cpu get name /value 2^>nul ^| find "="') do set "CPU_NAME=%%a"
echo - **CPU:** %CPU_NAME% >> "%REPORT_MD%"
echo     "cpu": "%CPU_NAME%", >> "%REPORT_JSON%"

:: Get CPU cores
for /f "tokens=2 delims==" %%a in ('wmic cpu get numberofcores /value 2^>nul ^| find "="') do set "CPU_CORES=%%a"
echo - **CPU Cores:** %CPU_CORES% >> "%REPORT_MD%"
echo     "cpu_cores": %CPU_CORES%, >> "%REPORT_JSON%"

:: Get uptime
for /f "tokens=1" %%a in ('wmic os get lastbootuptime /value 2^>nul ^| find "="') do set "BOOT_TIME=%%a"
echo - **Last Boot:** %BOOT_TIME% >> "%REPORT_MD%"
echo     "last_boot": "%BOOT_TIME%" >> "%REPORT_JSON%"

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 9. Timezone & Power ────────────────────────────────────────────
echo [9/18] Checking timezone and power settings...

echo ## 9. Timezone & Power >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "timezone_power": { >> "%REPORT_JSON%"

:: Get timezone
for /f "tokens=*" %%a in ('tzutil /g') do set "TIMEZONE=%%a"
echo - **Timezone:** %TIMEZONE% >> "%REPORT_MD%"
echo     "timezone": "%TIMEZONE%", >> "%REPORT_JSON%"

:: Check if auto-updates enabled
reg query "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoUpdate >nul 2>&1
if %errorlevel%==0 (
    echo - **Auto Updates:** Disabled (good for servers) >> "%REPORT_MD%"
    echo     "auto_updates": "disabled", >> "%REPORT_JSON%"
) else (
    echo - **Auto Updates:** Enabled (may cause unexpected reboots) >> "%REPORT_MD%"
    echo     "auto_updates": "enabled", >> "%REPORT_JSON%"
)

:: Check sleep/hibernate settings
powercfg /getactivescheme >nul 2>&1
if %errorlevel%==0 (
    echo - **Power Plan:** Configured >> "%REPORT_MD%"
    echo     "power_plan": "configured" >> "%REPORT_JSON%"
) else (
    echo - **Power Plan:** Could not determine >> "%REPORT_MD%"
    echo     "power_plan": "unknown" >> "%REPORT_JSON%"
)

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 10. Windows Features & Dependencies ────────────────────────────
echo [10/18] Checking Windows features and dependencies...

echo ## 10. Windows Features & Dependencies >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "dependencies": { >> "%REPORT_JSON%"

:: Check .NET Framework
reg query "HKLM\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full" /v Release >nul 2>&1
if %errorlevel%==0 (
    echo - **.NET Framework 4.x:** Installed >> "%REPORT_MD%"
    echo     "dotnet": "installed", >> "%REPORT_JSON%"
) else (
    echo - **.NET Framework 4.x:** Not found >> "%REPORT_MD%"
    echo     "dotnet": "not found", >> "%REPORT_JSON%"
)

:: Check Visual C++ Redistributable
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Version >nul 2>&1
if %errorlevel%==0 (
    echo - **Visual C++ 2015-2022:** Installed >> "%REPORT_MD%"
    echo     "vc_redist": "installed", >> "%REPORT_JSON%"
) else (
    echo - **Visual C++ 2015-2022:** Not found (may be needed) >> "%REPORT_MD%"
    echo     "vc_redist": "not found", >> "%REPORT_JSON%"
)

:: Check SMB client
sc query mrxsmb >nul 2>&1
if %errorlevel%==0 (
    echo - **SMB Client:** Available >> "%REPORT_MD%"
    echo     "smb_client": "available", >> "%REPORT_JSON%"
) else (
    echo - **SMB Client:** Not available >> "%REPORT_MD%"
    echo     "smb_client": "not available", >> "%REPORT_JSON%"
)

:: Check Windows Firewall
netsh advfirewall show allprofiles state | findstr /i "ON" >nul 2>&1
if %errorlevel%==0 (
    echo - **Windows Firewall:** Active (ports 4200, 8080 may need rules) >> "%REPORT_MD%"
    echo     "firewall": "active", >> "%REPORT_JSON%"
) else (
    echo - **Windows Firewall:** Inactive >> "%REPORT_MD%"
    echo     "firewall": "inactive", >> "%REPORT_JSON%"
)

echo ### Long Path Support (MAX_PATH) >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

reg query "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=3" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled 2^>nul ^| findstr "LongPathsEnabled"') do set "LONG_PATHS=%%a"
    if "!LONG_PATHS!"=="0x1" (
        echo - **Long Paths (>260 chars):** Enabled >> "%REPORT_MD%"
        echo     "long_paths_enabled": "yes" >> "%REPORT_JSON%"
    ) else (
        echo - **Long Paths (>260 chars):** Disabled (Deep directory backups may fail) >> "%REPORT_MD%"
        echo     "long_paths_enabled": "no" >> "%REPORT_JSON%"
    )
) else (
    echo - **Long Paths (>260 chars):** Key not found (Disabled by default) >> "%REPORT_MD%"
    echo     "long_paths_enabled": "no" >> "%REPORT_JSON%"
)

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 11. Potential Conflicts ────────────────────────────────────────
echo [11/18] Checking for potential conflicts...

echo ## 11. Potential Conflicts >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "conflicts": { >> "%REPORT_JSON%"

:: Check for other backup software
echo ### Backup Software >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

set "BACKUP_SW_FOUND=0"

:: Check for common backup services
for %%s in (AcronisAgent VeeamBackupAgent CarboniteService CrashPlanService MozyBackup) do (
    sc query %%s >nul 2>&1
    if !errorlevel!==0 (
        echo - **%%s:** Running (may conflict) >> "%REPORT_MD%"
        set "BACKUP_SW_FOUND=1"
    )
)

if "!BACKUP_SW_FOUND!"=="0" (
    echo - No other backup software detected >> "%REPORT_MD%"
)

:: Windows Defender / Anti-Malware Detailed Check
echo ### Antivirus ^& Windows Defender >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo     "backup_software_checked": "yes", >> "%REPORT_JSON%"

set "AV_FOUND=0"
:: Check Windows Defender via PowerShell (Standard on Windows Server 2016+)
powershell -Command "if (Get-Command Get-MpComputerStatus -ErrorAction SilentlyContinue) { $status = Get-MpComputerStatus; if ($status) { Write-Output \"RTP:$($status.RealTimeProtectionEnabled)\" } else { Write-Output 'NONE' } } else { Write-Output 'NONE' }" 2>nul > "%TEMP%\wd_status.txt"

for /f "tokens=1,2 delims=:" %%a in (%TEMP%\wd_status.txt) do (
    if "%%a"=="RTP" (
        set "AV_FOUND=1"
        if "%%b"=="True" (
            echo - **Windows Defender:** Active (Real-Time Protection is ON) >> "%REPORT_MD%"
            echo     "defender_active": "yes", >> "%REPORT_JSON%"
            echo   - WARNING: Real-Time Protection can significantly slow down backups or lock files. >> "%REPORT_MD%"
        ) else (
            echo - **Windows Defender:** Installed (Real-Time Protection is OFF) >> "%REPORT_MD%"
            echo     "defender_active": "no", >> "%REPORT_JSON%"
        )
        
        :: Check exclusions
        echo - **Defender Exclusions:** >> "%REPORT_MD%"
        set "HAS_EXC=0"
        for /f "tokens=*" %%e in ('powershell -Command "(Get-MpPreference).ExclusionPath" 2^>nul') do (
            if not "%%e"=="" (
                echo   - %%e >> "%REPORT_MD%"
                set "HAS_EXC=1"
            )
        )
        if "!HAS_EXC!"=="0" echo   - None configured >> "%REPORT_MD%"
    )
)
del "%TEMP%\wd_status.txt" 2>nul

:: Fallback / Third-Party AV Check via Services
set "THIRD_PARTY_AV_FOUND=no"
for %%s in (AVP SepMasterService McShield SAVAdminService SavService sophossps) do (
    sc query %%s >nul 2>&1
    if !errorlevel!==0 (
        echo - **Third-Party AV Service Found:** %%s (may interfere with backups) >> "%REPORT_MD%"
        set "AV_FOUND=1"
        set "THIRD_PARTY_AV_FOUND=yes"
    )
)

if "!AV_FOUND!"=="0" (
    echo - No antivirus or Defender active/detected. >> "%REPORT_MD%"
    echo     "antivirus_detected": "no" >> "%REPORT_JSON%"
) else (
    echo     "antivirus_detected": "yes", >> "%REPORT_JSON%"
    echo     "third_party_av": "!THIRD_PARTY_AV_FOUND!" >> "%REPORT_JSON%"
)

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 12. Event Log Errors ──────────────────────────────────────────
echo [12/18] Checking recent system errors...

echo ## 12. Recent System Errors >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "recent_errors": { >> "%REPORT_JSON%"

:: Check for critical errors in last 24 hours
wevtutil qe System /c:5 /rd:true /f:text 2>nul | findstr /i "Critical Error" >nul 2>&1
if %errorlevel%==0 (
    echo - **Recent Errors Found:** Yes (check Event Viewer) >> "%REPORT_MD%"
    echo     "has_errors": "yes" >> "%REPORT_JSON%"
) else (
    echo - **Recent Errors:** None found >> "%REPORT_MD%"
    echo     "has_errors": "no" >> "%REPORT_JSON%"
)

echo   } >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── Summary ────────────────────────────────────────────────────────
echo ## Summary >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo ### Deployment Readiness >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

set "READY=Yes"
set "ISSUES="

if not exist "%~dp0..\config.yaml" (
    set "READY=No"
    set "ISSUES=!ISSUES!- Missing config.yaml\n"
)

if "!IS_ADMIN!"=="No" (
    set "READY=No"
    set "ISSUES=!ISSUES!- Not running as administrator\n"
)

if "!READY!"=="Yes" (
    echo **Status:** Ready for deployment >> "%REPORT_MD%"
) else (
    echo **Status:** Issues found >> "%REPORT_MD%"
    echo. >> "%REPORT_MD%"
    echo ### Issues to Resolve: >> "%REPORT_MD%"
    echo !ISSUES! >> "%REPORT_MD%"
)

echo. >> "%REPORT_MD%"
echo --- >> "%REPORT_MD%"
echo *Report generated by AAM Backup Discovery Script* >> "%REPORT_MD%"

echo } >> "%REPORT_JSON%"

:: ── Done ────────────────────────────────────────────────────────────
echo.
echo ====================================================================
echo   Discovery Complete!
echo ====================================================================
echo.
echo   Reports saved to:
echo     %REPORT_MD%
echo     %REPORT_JSON%
echo.
echo   Please send both files to your deployment team.
echo.
pause
