@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: AAM Backup Automation V1 — THE ULTIMATE NSSM SERVICE INSTALLER
:: Run as Administrator from any directory.
::
:: Features:
::   - Auto-detects project root directory based on script location
::   - Auto-detects `uv.exe` path from system PATH or common locations
::   - Auto-downloads NSSM if missing (using uv and download_nssm.py)
::   - Cleans up orphaned prefect/python processes before reinstalling
::   - Sets explicit NSSM shutdown timeouts to prevent STOP_PENDING errors
::   - Sets up resilient restart delays and log rotations for 3 services
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

:: ── Dynamic Path Resolution ─────────────────────────────────────────
:: Get the directory of the current script (e.g. C:\AAM_BACKUP_V1\deploy)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

:: 3. Configure Windows Long Path Support
echo [INFO] Enabling Windows Long Path support (MaxPath > 260 chars)...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [OK] Long paths enabled in Registry.
) else (
    echo [WARN] Failed to enable Long Paths. You may need to enable it via Group Policy.
)

:: Resolve the parent directory as the project root (e.g. C:\AAM_BACKUP_V1)
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"

set NSSM=%PROJECT_DIR%\deploy\bin\nssm.exe
set BACKUP_ROOT=C:\BackupAgent
set LOG_DIR=%BACKUP_ROOT%\logs
set PREFECT_HOME=%BACKUP_ROOT%\.prefect

set SVC_SERVER=AamPrefectServer
set SVC_AGENT=AamBackupAgent
set SVC_WATCHDOG=AamWatchdog

:: ── Find uv executable dynamically ──────────────────────────────────
set "UV_EXE="
for /f "delims=" %%I in ('where uv 2^>nul') do (
    set "UV_EXE=%%I"
    goto :uv_found
)
:uv_found

if "%UV_EXE%"=="" (
    if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
)
if "%UV_EXE%"=="" (
    if exist "C:\Program Files\Python312\Scripts\uv.exe" set "UV_EXE=C:\Program Files\Python312\Scripts\uv.exe"
)

if "%UV_EXE%"=="" (
    echo.
    echo  ERROR: 'uv' package manager not found.
    echo  Please install it: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo.
    pause
    exit /b 1
)

:: ── Guard: Auto-download NSSM if missing ─────────────────────────────
if not exist "%NSSM%" (
    echo.
    echo [setup] NSSM not found at %NSSM%. Attempting to auto-download...
    cd /d "%PROJECT_DIR%"
    "%UV_EXE%" run python "%PROJECT_DIR%\deploy\download_nssm.py"
    if not exist "%NSSM%" (
        echo  ERROR: Failed to automatically download NSSM.
        pause
        exit /b 1
    )
    echo [setup] NSSM successfully downloaded.
)

:: ── Guard: project directory validation ──────────────────────────────
if not exist "%PROJECT_DIR%\launch.py" (
    echo.
    echo  ERROR: launch.py not found in %PROJECT_DIR%.
    echo  Ensure this script is in the 'deploy' folder of the project.
    echo.
    pause
    exit /b 1
)

:: ── Create required directories ──────────────────────────────────────
if not exist "%LOG_DIR%"      mkdir "%LOG_DIR%"
if not exist "%PREFECT_HOME%" mkdir "%PREFECT_HOME%"

echo.
echo ===================================================================
echo   AAM Backup Automation V1 — ULTIMATE INSTALLER
echo ===================================================================
echo   NSSM:         %NSSM%
echo   uv:           %UV_EXE%
echo   Project:      %PROJECT_DIR%
echo   Logs:         %LOG_DIR%
echo   Prefect home: %PREFECT_HOME%
echo ===================================================================

:: ── Remove old services (clean reinstall) ────────────────────────────
echo.
echo [setup] Stopping and removing any existing services...
"%NSSM%" stop  %SVC_WATCHDOG% 2>nul
"%NSSM%" stop  %SVC_AGENT%   2>nul
"%NSSM%" stop  %SVC_SERVER%  2>nul

echo [setup] Killing any orphaned prefect subprocesses...
taskkill /F /IM prefect.exe /T 2>nul

"%NSSM%" remove %SVC_WATCHDOG% confirm 2>nul
"%NSSM%" remove %SVC_AGENT%   confirm 2>nul
"%NSSM%" remove %SVC_SERVER%  confirm 2>nul

:: Small pause to let Windows SCM release file handles completely
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

:: NSSM graceful shutdown settings
"%NSSM%" set %SVC_SERVER% AppStopMethodSkip     0
"%NSSM%" set %SVC_SERVER% AppStopMethodConsole  15000
"%NSSM%" set %SVC_SERVER% AppStopMethodWindow   15000
"%NSSM%" set %SVC_SERVER% AppStopMethodThreads  15000

:: Restart delay after crash: 30 seconds
"%NSSM%" set %SVC_SERVER% AppRestartDelay       30000

:: Fixed Prefect home
"%NSSM%" set %SVC_SERVER% AppEnvironmentExtra   "PREFECT_HOME=%PREFECT_HOME%" "PREFECT_API_URL=http://127.0.0.1:4200/api"

:: Windows SCM recovery actions
sc failure %SVC_SERVER% reset= 86400 actions= restart/30000/restart/60000/restart/60000 >nul
sc failureflag %SVC_SERVER% 1 >nul

echo [setup] %SVC_SERVER% installed successfully.


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

:: Depend on Prefect server being Running first
"%NSSM%" set %SVC_AGENT% DependOnService        %SVC_SERVER%

"%NSSM%" set %SVC_AGENT% AppStdout              "%LOG_DIR%\agent_svc.log"
"%NSSM%" set %SVC_AGENT% AppStderr              "%LOG_DIR%\agent_svc.log"
"%NSSM%" set %SVC_AGENT% AppStdoutCreationDisposition 4
"%NSSM%" set %SVC_AGENT% AppStderrCreationDisposition 4
"%NSSM%" set %SVC_AGENT% AppRotateFiles         1
"%NSSM%" set %SVC_AGENT% AppRotateOnline        1
"%NSSM%" set %SVC_AGENT% AppRotateBytes         10485760

:: Graceful shutdown - allowing sub-processes (rclone) to finish closing
"%NSSM%" set %SVC_AGENT% AppStopMethodConsole   30000
"%NSSM%" set %SVC_AGENT% AppStopMethodWindow    30000
"%NSSM%" set %SVC_AGENT% AppStopMethodThreads   30000

"%NSSM%" set %SVC_AGENT% AppRestartDelay        30000
"%NSSM%" set %SVC_AGENT% AppEnvironmentExtra    "PREFECT_HOME=%PREFECT_HOME%" "PREFECT_API_URL=http://127.0.0.1:4200/api"

sc failure %SVC_AGENT% reset= 86400 actions= restart/60000/restart/90000/restart/120000 >nul
sc failureflag %SVC_AGENT% 1 >nul

echo [setup] %SVC_AGENT% installed successfully.


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
"%NSSM%" set %SVC_WATCHDOG% AppEnvironmentExtra    "PREFECT_HOME=%PREFECT_HOME%" "PREFECT_API_URL=http://127.0.0.1:4200/api"

sc failure %SVC_WATCHDOG% reset= 86400 actions= restart/15000/restart/30000/restart/30000 >nul
sc failureflag %SVC_WATCHDOG% 1 >nul

echo [setup] %SVC_WATCHDOG% installed successfully.


:: ════════════════════════════════════════════════════════════════════
:: Start all services
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
echo   ULTIMATE INSTALLATION COMPLETE
echo ===================================================================
echo   Services:    Open services.msc to verify status
echo   Prefect UI:  http://localhost:4200
echo   Dashboard:   http://localhost:8080
echo   Logs:        %LOG_DIR%
echo ===================================================================
echo.
echo   NOTE: If LAN backup fails with access denied, the services
echo   must run as a local/domain user with network share access.
echo   Configure this via services.msc -^> Log On tab.
echo.
pause
