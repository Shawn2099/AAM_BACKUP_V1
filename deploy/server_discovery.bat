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

:: ── Initialize Reports ──────────────────────────────────────────────
echo # Server Discovery Report > "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo **Generated:** %DATE% %TIME% >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo { > "%REPORT_JSON%"
echo   "generated": "%DATE% %TIME%", >> "%REPORT_JSON%"

:: ── 1. System Information ──────────────────────────────────────────
echo [1/6] Gathering system information...

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
echo [2/6] Gathering storage information...

echo ## 2. Storage Information >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo ### Drives >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"
echo ^| Drive ^| Type ^| Total ^| Free ^| File System ^| >> "%REPORT_MD%"
echo ^|-------^|------^|-------^|------^|-------------^| >> "%REPORT_MD%"

echo   "drives": [ >> "%REPORT_JSON%"

set "DRIVE_COUNT=0"
for /f "tokens=1-5" %%a in ('wmic logicaldisk get caption^,drivetype^,size^,freespace^,filesystem /format:csv 2^>nul ^| findstr /v "Caption"') do (
    set /a DRIVE_COUNT+=1
    
    :: Drive type mapping
    set "DRIVE_TYPE=Unknown"
    if "%%b"=="2" set "DRIVE_TYPE=Removable"
    if "%%b"=="3" set "DRIVE_TYPE=Local"
    if "%%b"=="4" set "DRIVE_TYPE=Network"
    if "%%b"=="5" set "DRIVE_TYPE=CD-ROM"
    
    :: Convert bytes to GB
    set /a "TOTAL_GB=%%d / 1073741824"
    set /a "FREE_GB=%%e / 1073741824"
    
    echo ^| %%a ^| !DRIVE_TYPE! ^| !TOTAL_GB! GB ^| !FREE_GB! GB ^| %%c ^| >> "%REPORT_MD%"
    
    if !DRIVE_COUNT! GTR 1 echo , >> "%REPORT_JSON%"
    echo     { >> "%REPORT_JSON%"
    echo       "drive": "%%a", >> "%REPORT_JSON%"
    echo       "type": "!DRIVE_TYPE!", >> "%REPORT_JSON%"
    echo       "total_gb": !TOTAL_GB!, >> "%REPORT_JSON%"
    echo       "free_gb": !FREE_GB!, >> "%REPORT_JSON%"
    echo       "filesystem": "%%c" >> "%REPORT_JSON%"
    echo     } >> "%REPORT_JSON%"
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
echo [3/6] Gathering network information...

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

echo   }, >> "%REPORT_JSON%"

echo ### DNS Resolution Test >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

nslookup google.com >nul 2>&1
if %errorlevel%==0 (
    echo - **DNS Resolution:** Working >> "%REPORT_MD%"
) else (
    echo - **DNS Resolution:** Failed >> "%REPORT_MD%"
)

echo ### Internet Connectivity >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

ping -n 1 8.8.8.8 >nul 2>&1
if %errorlevel%==0 (
    echo - **Internet Access:** Available >> "%REPORT_MD%"
) else (
    echo - **Internet Access:** Not available or blocked >> "%REPORT_MD%"
)

echo. >> "%REPORT_MD%"

:: ── 4. Software & Tools ────────────────────────────────────────────
echo [4/6] Checking software and tools...

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

:: Check robocopy
robocopy /? >nul 2>&1
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
echo [5/6] Checking permissions and access...

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

:: Check common UNC paths
echo ### Network Share Access >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

set "SHARE_FOUND=0"
for %%s in (
    "\\192.168.10.10\share$"
    "\\192.168.1.1\share"
    "\\NAS\backup"
    "\\SERVER\share"
) do (
    net use %%s >nul 2>&1
    if !errorlevel!==0 (
        echo - **%%s:** Accessible >> "%REPORT_MD%"
        set "SHARE_FOUND=1"
        net use %%s /delete >nul 2>&1
    )
)

if "!SHARE_FOUND!"=="0" (
    echo - No common shares found (enter specific UNC path during config) >> "%REPORT_MD%"
)

echo   }, >> "%REPORT_JSON%"

echo. >> "%REPORT_MD%"

:: ── 6. Existing Installation ───────────────────────────────────────
echo [6/6] Checking for existing installation...

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

echo   } >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 7. Port Availability ────────────────────────────────────────────
echo [7/12] Checking port availability...

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
echo [8/12] Checking system resources...

echo ## 8. System Resources >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "resources": { >> "%REPORT_JSON%"

:: Get total RAM
for /f "tokens=2 delims==" %%a in ('wmic computersystem get totalphysicalmemory /value 2^>nul ^| find "="') do set "TOTAL_RAM=%%a"
set /a "TOTAL_RAM_GB=TOTAL_RAM / 1073741824"
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
echo [9/12] Checking timezone and power settings...

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
echo [10/12] Checking Windows features and dependencies...

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
    echo     "firewall": "inactive" >> "%REPORT_JSON%"
)

echo   } >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 11. Potential Conflicts ────────────────────────────────────────
echo [11/12] Checking for potential conflicts...

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

:: Check for antivirus
echo ### Antivirus Software >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

wmic /namespace:\\root\SecurityCenter2 path AntiVirusProduct get displayName 2>nul | findstr /v "displayName" >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=*" %%a in ('wmic /namespace:\\root\SecurityCenter2 path AntiVirusProduct get displayName 2^>nul ^| findstr /v "displayName"') do (
        if not "%%a"=="" echo - **%%a** (may interfere with backups) >> "%REPORT_MD%"
    )
) else (
    echo - No antivirus detected (or WMI not available) >> "%REPORT_MD%"
)

echo     "backup_software_checked": "yes", >> "%REPORT_JSON%"
echo     "antivirus_checked": "yes" >> "%REPORT_JSON%"

echo   }, >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 12. Event Log Errors ──────────────────────────────────────────
echo [12/12] Checking recent system errors...

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
