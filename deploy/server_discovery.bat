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

echo   } >> "%REPORT_JSON%"
echo. >> "%REPORT_MD%"

:: ── 16. Connectivity Tests ────────────────────────────────────────
echo [16/18] Testing connectivity to AAM services...

echo ## 16. Connectivity Tests >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

echo   "connectivity": { >> "%REPORT_JSON%"

:: Test Google Cloud Storage connectivity
echo ### Google Cloud Storage >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

ping -n 1 storage.googleapis.com >nul 2>&1
if %errorlevel%==0 (
    echo - **storage.googleapis.com:** Reachable >> "%REPORT_MD%"
    echo     "gcs_reachable": "yes", >> "%REPORT_JSON%"
) else (
    echo - **storage.googleapis.com:** NOT reachable (cloud backups will fail) >> "%REPORT_MD%"
    echo     "gcs_reachable": "no", >> "%REPORT_JSON%"
)

:: Test SMTP connectivity (common ports)
echo ### SMTP Connectivity >> "%REPORT_MD%"
echo. >> "%REPORT_MD%"

:: Test port 587 (TLS)
powershell -Command "Test-NetConnection -ComputerName smtp.gmail.com -Port 587 -WarningAction SilentlyContinue | Select-Object TcpTestSucceeded" 2>nul | findstr "True" >nul 2>&1
if %errorlevel%==0 (
    echo - **Port 587 (SMTP TLS):** Open >> "%REPORT_MD%"
    echo     "smtp_587": "open", >> "%REPORT_JSON%"
) else (
    echo - **Port 587 (SMTP TLS):** Blocked or unreachable >> "%REPORT_MD%"
    echo     "smtp_587": "blocked", >> "%REPORT_JSON%"
)

:: Test port 465 (SSL)
powershell -Command "Test-NetConnection -ComputerName smtp.gmail.com -Port 465 -WarningAction SilentlyContinue | Select-Object TcpTestSucceeded" 2>nul | findstr "True" >nul 2>&1
if %errorlevel%==0 (
    echo - **Port 465 (SMTP SSL):** Open >> "%REPORT_MD%"
    echo     "smtp_465": "open", >> "%REPORT_JSON%"
) else (
    echo - **Port 465 (SMTP SSL):** Blocked or unreachable >> "%REPORT_MD%"
    echo     "smtp_465": "blocked", >> "%REPORT_JSON%"
)

:: Test port 25 (Plain)
powershell -Command "Test-NetConnection -ComputerName smtp.gmail.com -Port 25 -WarningAction SilentlyContinue | Select-Object TcpTestSucceeded" 2>nul | findstr "True" >nul 2>&1
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
ping -n 1 storage.googleapis.com >nul 2>&1
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

echo   } >> "%REPORT_JSON%"

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

echo     "interference_checked": "yes" >> "%REPORT_JSON%"

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

echo   } >> "%REPORT_JSON%"
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
    echo     "firewall": "inactive" >> "%REPORT_JSON%"
)

echo   } >> "%REPORT_JSON%"
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
