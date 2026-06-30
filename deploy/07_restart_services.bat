@echo off
:: =======================================================================
:: Restart Services for AAM Backup Automation
:: Run this as Administrator after updating config.yaml
:: =======================================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click 07_restart_services.bat ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

:: -- Resolve project directory ----------------------------------------
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"

:: -- Read runtime_dir from config.yaml via Python ---------------------
set "CONFIG_FILE=%PROJECT_DIR%\config.yaml"
set "DEFAULT_RUNTIME=C:\BackupAgent"

:: Find uv
set "UV_EXE="
for /f "delims=" %%I in ('where uv 2^>nul') do (
    set "UV_EXE=%%I"
    goto :uv_found
)
:uv_found
if "%UV_EXE%"=="" if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if "%UV_EXE%"=="" if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
if "%UV_EXE%"=="" if exist "C:\Program Files\Python312\Scripts\uv.exe" set "UV_EXE=C:\Program Files\Python312\Scripts\uv.exe"

set "RUNTIME_DIR="
if not "%UV_EXE%"=="" (
    cd /d "%PROJECT_DIR%"
    for /f "delims=" %%R in ('"%UV_EXE%" run --quiet python "%PROJECT_DIR%\deploy\read_config.py" "%CONFIG_FILE%" paths.runtime_dir --default "%DEFAULT_RUNTIME%" 2^>nul') do set "RUNTIME_DIR=%%R"
) else (
    for /f "delims=" %%R in ('python "%PROJECT_DIR%\deploy\read_config.py" "%CONFIG_FILE%" paths.runtime_dir --default "%DEFAULT_RUNTIME%" 2^>nul') do set "RUNTIME_DIR=%%R"
)
if "%RUNTIME_DIR%"=="" set "RUNTIME_DIR=%DEFAULT_RUNTIME%"

set "LOG_DIR=%RUNTIME_DIR%\logs"

echo.
echo ===================================================
echo  AAM Backup - Restarting Services
echo ===================================================
echo.
echo  Stopping all AAM services...
net stop AamWatchdog 2>nul
net stop AamBackupAgent 2>nul
net stop AamPrefectServer 2>nul

echo.
echo  Starting AamPrefectServer (API server)...
net start AamPrefectServer
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: AamPrefectServer failed to start.
    echo  This service must be running before the others can start.
    echo.
    echo  Troubleshooting:
    echo    1. Check if port 4200 is already in use:
    echo       netstat -ano | findstr :4200
    echo    2. Check the log for errors:
    echo       %LOG_DIR%\prefect_svc.log
    echo    3. Verify config.yaml is valid:
    echo       deploy\05_test_config.bat
    echo.
    pause
    exit /b 1
)

echo  Waiting 10 seconds for Prefect API to initialize...
timeout /t 10 /nobreak >nul

echo  Starting AamBackupAgent...
net start AamBackupAgent
if %errorlevel% neq 0 (
    echo.
    echo  WARNING: AamBackupAgent failed to start.
    echo  The agent will retry automatically every 60 seconds.
    echo  If it keeps failing, check the log:
    echo    %LOG_DIR%\agent_svc.log
) else (
    echo  OK - AamBackupAgent started.
)

echo  Starting AamWatchdog...
net start AamWatchdog
if %errorlevel% neq 0 (
    echo.
    echo  WARNING: AamWatchdog failed to start.
    echo  The watchdog will retry automatically every 15 seconds.
    echo  If it keeps failing, check the log:
    echo    %LOG_DIR%\watchdog_svc.log
) else (
    echo  OK - AamWatchdog started.
)

echo.
echo ===================================================
echo  SERVICES RESTARTED
echo ===================================================
echo.
echo  Services:
    echo    AamPrefectServer   - API server
    echo    AamBackupAgent     - Backup dashboard + scheduler
    echo    AamWatchdog        - Health monitor
echo.
echo  Verify status:  services.msc
echo  Prefect UI:     http://localhost:4200
echo  Dashboard:      http://localhost:8080
echo  Logs:           %LOG_DIR%
echo.
echo  If services fail to start, check the logs above
echo  and ensure config.yaml is valid (run 05_test_config.bat).
echo.
pause
