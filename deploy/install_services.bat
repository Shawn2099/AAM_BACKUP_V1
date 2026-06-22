@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: AAM Backup Automation V1 — SERVICE INSTALLER
:: Run as Administrator. Re-runnable on every upgrade/config change.
::
:: PRE-REQUISITE: Run setup_system.bat ONCE before this on a fresh server.
::
:: What it does:
::   1. Validates uv and NSSM are available
::   2. Stops and removes any existing AAM services cleanly
::   3. Installs AamPrefectServer, AamBackupAgent, AamWatchdog via NSSM
::   4. Starts all services in the correct dependency order
::
:: Runtime: ~30 seconds
:: ═══════════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion

:: ── Guard: must run as Administrator ────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click install_services.bat ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

:: ── Resolve paths ────────────────────────────────────────────────────
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"

set NSSM=%PROJECT_DIR%\deploy\bin\nssm.exe
set BACKUP_ROOT=C:\BackupAgent
set LOG_DIR=%BACKUP_ROOT%\logs
set PREFECT_HOME=%BACKUP_ROOT%\.prefect

set SVC_SERVER=AamPrefectServer
set SVC_AGENT=AamBackupAgent
set SVC_WATCHDOG=AamWatchdog

:: ── Warn if setup_system.bat has not been run ─────────────────────
set "GCLOUD_CMD=%PROJECT_DIR%\deploy\bin\google-cloud-sdk\bin\gcloud.cmd"
if not exist "%GCLOUD_CMD%" (
    echo.
    echo [WARN] Google Cloud SDK not found in deploy\bin.
    echo [WARN] FY rollover archive transition will not work.
    echo [WARN] Run setup_system.bat first if this is a fresh server.
    echo.
)

:: ── Validate rclone ──────────────────────────────────────────────────
set "RCLONE_EXE="
if exist "%PROJECT_DIR%\deploy\bin\rclone.exe" set "RCLONE_EXE=%PROJECT_DIR%\deploy\bin\rclone.exe"
if "%RCLONE_EXE%"=="" (
    for /f "delims=" %%I in ('where rclone 2^>nul') do (
        set "RCLONE_EXE=%%I"
    )
)
if "%RCLONE_EXE%"=="" (
    echo.
    echo  ERROR: 'rclone' executable not found in deploy\bin or system PATH.
    echo  Please place rclone.exe in deploy\bin\ per the Deployment Guide.
    echo.
    pause
    exit /b 1
)


:: ── Find uv executable ───────────────────────────────────────────────
set "UV_EXE="
for /f "delims=" %%I in ('where uv 2^>nul') do (
    set "UV_EXE=%%I"
    goto :uv_found
)
:uv_found

if "%UV_EXE%"=="" (
    if exist "%USERPROFILE%\.local\bin\uv.exe"        set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
)
if "%UV_EXE%"=="" (
    if exist "%USERPROFILE%\.cargo\bin\uv.exe"        set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
)
if "%UV_EXE%"=="" (
    if exist "C:\Program Files\Python312\Scripts\uv.exe" set "UV_EXE=C:\Program Files\Python312\Scripts\uv.exe"
)

if "%UV_EXE%"=="" (
    echo.
    echo  ERROR: 'uv' package manager not found.
    echo  Install it: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo.
    pause
    exit /b 1
)

:: ── Auto-download NSSM if missing ────────────────────────────────────
if not exist "%NSSM%" (
    echo [setup] NSSM not found. Attempting to auto-download...
    cd /d "%PROJECT_DIR%"
    "%UV_EXE%" run python "%PROJECT_DIR%\deploy\download_nssm.py"
    if not exist "%NSSM%" (
        echo  ERROR: Failed to download NSSM.
        pause
        exit /b 1
    )
    echo [OK]   NSSM downloaded.
)

:: ── Validate project ─────────────────────────────────────────────────
if not exist "%PROJECT_DIR%\launch.py" (
    echo.
    echo  ERROR: launch.py not found in %PROJECT_DIR%.
    echo  Ensure this script is in the 'deploy' folder of the project.
    echo.
    pause
    exit /b 1
)

:: ── Create required directories ───────────────────────────────────────
if not exist "%LOG_DIR%"      mkdir "%LOG_DIR%"
if not exist "%PREFECT_HOME%" mkdir "%PREFECT_HOME%"

echo.
echo ===================================================================
echo   AAM Backup Automation V1 — SERVICE INSTALLER
echo ===================================================================
echo   NSSM:         %NSSM%
echo   uv:           %UV_EXE%
echo   Project:      %PROJECT_DIR%
echo   Logs:         %LOG_DIR%
echo   Prefect home: %PREFECT_HOME%
echo ===================================================================

:: ── Stop and remove old services ─────────────────────────────────────
echo.
echo [setup] Stopping any running AAM services...
taskkill /F /IM python.exe /T 2>nul
taskkill /F /IM prefect.exe /T 2>nul

"%NSSM%" stop  %SVC_WATCHDOG% 2>nul
"%NSSM%" stop  %SVC_AGENT%   2>nul
"%NSSM%" stop  %SVC_SERVER%  2>nul

"%NSSM%" remove %SVC_WATCHDOG% confirm 2>nul
"%NSSM%" remove %SVC_AGENT%   confirm 2>nul
"%NSSM%" remove %SVC_SERVER%  confirm 2>nul

:: Small pause to let Windows SCM release file handles
timeout /t 3 /nobreak >nul

:: ════════════════════════════════════════════════════════════════════
:: SERVICE 1: AamPrefectServer
:: ════════════════════════════════════════════════════════════════════
echo.
echo [setup] Installing %SVC_SERVER%...

"%NSSM%" install %SVC_SERVER% "%UV_EXE%"
"%NSSM%" set %SVC_SERVER% AppParameters         "run prefect server start --host 127.0.0.1 --port 4200"
"%NSSM%" set %SVC_SERVER% AppDirectory          "%PROJECT_DIR%"
"%NSSM%" set %SVC_SERVER% DisplayName           "AAM Prefect Server"
"%NSSM%" set %SVC_SERVER% Description           "Prefect 3 API server for AAM Backup Automation V1"
"%NSSM%" set %SVC_SERVER% Start                 SERVICE_AUTO_START

:: Stdout + stderr → same log file (append mode), 10 MB rotation
"%NSSM%" set %SVC_SERVER% AppStdout             "%LOG_DIR%\prefect_svc.log"
"%NSSM%" set %SVC_SERVER% AppStderr             "%LOG_DIR%\prefect_svc.log"
"%NSSM%" set %SVC_SERVER% AppStdoutCreationDisposition 4
"%NSSM%" set %SVC_SERVER% AppStderrCreationDisposition 4
"%NSSM%" set %SVC_SERVER% AppRotateFiles        1
"%NSSM%" set %SVC_SERVER% AppRotateOnline       1
"%NSSM%" set %SVC_SERVER% AppRotateBytes        10485760

:: Graceful shutdown timeouts
"%NSSM%" set %SVC_SERVER% AppStopMethodSkip     0
"%NSSM%" set %SVC_SERVER% AppStopMethodConsole  15000
"%NSSM%" set %SVC_SERVER% AppStopMethodWindow   15000
"%NSSM%" set %SVC_SERVER% AppStopMethodThreads  15000

"%NSSM%" set %SVC_SERVER% AppRestartDelay       30000
"%NSSM%" set %SVC_SERVER% AppEnvironmentExtra   "PREFECT_HOME=%PREFECT_HOME%" "PREFECT_API_URL=http://127.0.0.1:4200/api" "PREFECT_API_DATABASE_CONNECTION_TIMEOUT=60.0"

sc failure %SVC_SERVER% reset= 86400 actions= restart/30000/restart/60000/restart/60000 >nul
sc failureflag %SVC_SERVER% 1 >nul

echo [OK]   %SVC_SERVER% installed.


:: ════════════════════════════════════════════════════════════════════
:: SERVICE 2: AamBackupAgent
:: ════════════════════════════════════════════════════════════════════
echo.
echo [setup] Installing %SVC_AGENT%...

"%NSSM%" install %SVC_AGENT% "%UV_EXE%"
"%NSSM%" set %SVC_AGENT% AppParameters          "run python launch.py"
"%NSSM%" set %SVC_AGENT% AppDirectory           "%PROJECT_DIR%"
"%NSSM%" set %SVC_AGENT% DisplayName            "AAM Backup Agent"
"%NSSM%" set %SVC_AGENT% Description            "AAM Backup dashboard (port 8080) and Prefect scheduler"
"%NSSM%" set %SVC_AGENT% Start                  SERVICE_AUTO_START

:: Depends on Prefect server being up first
"%NSSM%" set %SVC_AGENT% DependOnService        %SVC_SERVER%

"%NSSM%" set %SVC_AGENT% AppStdout              "%LOG_DIR%\agent_svc.log"
"%NSSM%" set %SVC_AGENT% AppStderr              "%LOG_DIR%\agent_svc.log"
"%NSSM%" set %SVC_AGENT% AppStdoutCreationDisposition 4
"%NSSM%" set %SVC_AGENT% AppStderrCreationDisposition 4
"%NSSM%" set %SVC_AGENT% AppRotateFiles         1
"%NSSM%" set %SVC_AGENT% AppRotateOnline        1
"%NSSM%" set %SVC_AGENT% AppRotateBytes         10485760

:: Graceful shutdown — allow rclone/robocopy subprocesses to finish
"%NSSM%" set %SVC_AGENT% AppStopMethodConsole   120000
"%NSSM%" set %SVC_AGENT% AppStopMethodWindow    120000
"%NSSM%" set %SVC_AGENT% AppStopMethodThreads   120000

"%NSSM%" set %SVC_AGENT% AppRestartDelay        30000
"%NSSM%" set %SVC_AGENT% AppEnvironmentExtra    "PREFECT_HOME=%PREFECT_HOME%" "PREFECT_API_URL=http://127.0.0.1:4200/api" "PREFECT_API_DATABASE_CONNECTION_TIMEOUT=60.0"

sc failure %SVC_AGENT% reset= 86400 actions= restart/60000/restart/90000/restart/120000 >nul
sc failureflag %SVC_AGENT% 1 >nul

echo [OK]   %SVC_AGENT% installed.


:: ════════════════════════════════════════════════════════════════════
:: SERVICE 3: AamWatchdog
:: ════════════════════════════════════════════════════════════════════
echo.
echo [setup] Installing %SVC_WATCHDOG%...

"%NSSM%" install %SVC_WATCHDOG% "%UV_EXE%"
"%NSSM%" set %SVC_WATCHDOG% AppParameters          "run python watchdog.py"
"%NSSM%" set %SVC_WATCHDOG% AppDirectory           "%PROJECT_DIR%"
"%NSSM%" set %SVC_WATCHDOG% DisplayName            "AAM Backup Watchdog"
"%NSSM%" set %SVC_WATCHDOG% Description            "Monitors API health and restarts services if hung"
"%NSSM%" set %SVC_WATCHDOG% Start                  SERVICE_AUTO_START

"%NSSM%" set %SVC_WATCHDOG% AppStdout              "%LOG_DIR%\watchdog_svc.log"
"%NSSM%" set %SVC_WATCHDOG% AppStderr              "%LOG_DIR%\watchdog_svc.log"
"%NSSM%" set %SVC_WATCHDOG% AppStdoutCreationDisposition 4
"%NSSM%" set %SVC_WATCHDOG% AppStderrCreationDisposition 4
"%NSSM%" set %SVC_WATCHDOG% AppRotateFiles         1
"%NSSM%" set %SVC_WATCHDOG% AppRotateOnline        1
"%NSSM%" set %SVC_WATCHDOG% AppRotateBytes         10485760

"%NSSM%" set %SVC_WATCHDOG% AppRestartDelay        15000
"%NSSM%" set %SVC_WATCHDOG% AppEnvironmentExtra    "PREFECT_HOME=%PREFECT_HOME%" "PREFECT_API_URL=http://127.0.0.1:4200/api" "PREFECT_API_DATABASE_CONNECTION_TIMEOUT=60.0"

sc failure %SVC_WATCHDOG% reset= 86400 actions= restart/15000/restart/30000/restart/30000 >nul
sc failureflag %SVC_WATCHDOG% 1 >nul

echo [OK]   %SVC_WATCHDOG% installed.


:: ════════════════════════════════════════════════════════════════════
:: Start all services in dependency order
:: ════════════════════════════════════════════════════════════════════
echo.
echo [setup] Starting %SVC_SERVER%...
net start %SVC_SERVER%
if %errorlevel% neq 0 (
    echo  ERROR: Failed to start %SVC_SERVER%. Check: %LOG_DIR%\prefect_svc.log
)

echo [setup] Waiting 15 seconds for Prefect API to initialize...
timeout /t 15 /nobreak >nul

echo [setup] Starting %SVC_AGENT%...
net start %SVC_AGENT%
if %errorlevel% neq 0 (
    echo  WARNING: %SVC_AGENT% failed to start. It will retry automatically.
)

echo [setup] Starting %SVC_WATCHDOG%...
net start %SVC_WATCHDOG%
if %errorlevel% neq 0 (
    echo  WARNING: %SVC_WATCHDOG% failed to start. It will retry automatically.
)

echo.
echo ===================================================================
echo   INSTALLATION COMPLETE
echo ===================================================================
echo   Services:    Open services.msc to verify status
echo   Prefect UI:  http://localhost:4200
echo   Dashboard:   http://localhost:8080
echo   Logs:        %LOG_DIR%
echo ===================================================================
echo.
echo   NOTE: If LAN backup fails with access denied, the services
echo   must run as a domain user with network share access.
echo   Configure via services.msc -^> Log On tab.
echo.
pause
