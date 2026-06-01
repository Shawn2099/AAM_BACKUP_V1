@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: AAM Backup Automation V1 — NSSM Service Installer (3 services)
:: Run as Administrator from any directory.
::
:: Creates two Windows Services:
::   AamPrefectServer  — prefect server start (API + scheduler backend)
::   AamBackupAgent    — launch.py (dashboard UI + Prefect serve())
::
:: Both services:
::   - Start automatically at boot (before user login)
::   - Restart automatically on crash (30s first, 60s subsequent)
::   - Capture stdout+stderr to C:\BackupAgent\logs\ with 10MB rotation
::   - Run under LocalSystem with fixed PREFECT_HOME so DB path never changes
:: ═══════════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion

:: ── Configuration ────────────────────────────────────────────────────
set PROJECT_DIR=C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1
set NSSM=%PROJECT_DIR%\deploy\bin\nssm.exe
set UV_EXE=C:\Program Files\Python312\Scripts\uv.exe
set BACKUP_ROOT=C:\BackupAgent
set LOG_DIR=%BACKUP_ROOT%\logs
set PREFECT_HOME=%BACKUP_ROOT%\.prefect

set SVC_SERVER=AamPrefectServer
set SVC_AGENT=AamBackupAgent
set SVC_WATCHDOG=AamWatchdog

:: ── Guard: must run as Administrator ────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click install_services.bat ^> "Run as administrator"
    echo.
    exit /b 1
)

:: ── Guard: NSSM must exist in bundle ────────────────────────────────
if not exist "%NSSM%" (
    echo.
    echo  ERROR: Bundled NSSM not found at %NSSM%
    echo  Please ensure deploy\bin\nssm.exe exists in the repository.
    echo.
    exit /b 1
)

:: ── Guard: uv must exist ─────────────────────────────────────────────
if not exist "%UV_EXE%" (
    echo.
    echo  ERROR: uv not found at %UV_EXE%
    echo  Check your uv installation and update UV_EXE in this script.
    echo.
    exit /b 1
)

:: ── Guard: project directory must exist ──────────────────────────────
if not exist "%PROJECT_DIR%\launch.py" (
    echo.
    echo  ERROR: Project not found at %PROJECT_DIR%
    echo  Update PROJECT_DIR in this script to match your installation path.
    echo.
    exit /b 1
)

:: ── Create required directories ──────────────────────────────────────
if not exist "%LOG_DIR%"      mkdir "%LOG_DIR%"
if not exist "%PREFECT_HOME%" mkdir "%PREFECT_HOME%"

echo.
echo ===================================================================
echo   AAM Backup Automation V1 — Service Installation
echo ===================================================================
echo   NSSM:         %NSSM%
echo   uv:           %UV_EXE%
echo   Project:      %PROJECT_DIR%
echo   Logs:         %LOG_DIR%
echo   Prefect home: %PREFECT_HOME%
echo ===================================================================

:: ── Remove old services if they exist (idempotent reinstall) ─────────
echo.
echo [setup] Removing any existing services (clean reinstall)...
"%NSSM%" stop  %SVC_WATCHDOG% 2>nul
"%NSSM%" stop  %SVC_AGENT%   2>nul
"%NSSM%" stop  %SVC_SERVER%  2>nul
"%NSSM%" remove %SVC_WATCHDOG% confirm 2>nul
"%NSSM%" remove %SVC_AGENT%   confirm 2>nul
"%NSSM%" remove %SVC_SERVER%  confirm 2>nul
:: Small pause to let SCM release handles
timeout /t 3 /nobreak >nul


:: ════════════════════════════════════════════════════════════════════
:: SERVICE 1: AamPrefectServer
:: Runs: uv run prefect server start
:: ════════════════════════════════════════════════════════════════════
echo.
echo [setup] Installing %SVC_SERVER%...

"%NSSM%" install %SVC_SERVER% "%UV_EXE%"
"%NSSM%" set %SVC_SERVER% AppParameters         "run prefect server start"
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

:: Restart delay after crash: NSSM-level (immediate kill + delay before restart)
"%NSSM%" set %SVC_SERVER% AppRestartDelay       30000

:: Fixed Prefect home — DB path never changes regardless of service account
"%NSSM%" set %SVC_SERVER% AppEnvironmentExtra   "PREFECT_HOME=%PREFECT_HOME%" "PREFECT_API_URL=http://127.0.0.1:4200/api" "PREFECT_SERVER_API_PORT=4200"

:: Windows SCM recovery actions (belt-and-suspenders alongside NSSM)
:: Reset counter after 24h. On failure: restart after 30s, 60s, 60s.
sc failure %SVC_SERVER% reset= 86400 actions= restart/30000/restart/60000/restart/60000 >nul
sc failureflag %SVC_SERVER% 1 >nul

echo [setup] %SVC_SERVER% installed.


:: ════════════════════════════════════════════════════════════════════
:: SERVICE 2: AamBackupAgent
:: Runs: uv run python launch.py  (dashboard + scheduler)
:: Depends on AamPrefectServer being Running first.
:: ════════════════════════════════════════════════════════════════════
echo.
echo [setup] Installing %SVC_AGENT%...

"%NSSM%" install %SVC_AGENT% "%UV_EXE%"
"%NSSM%" set %SVC_AGENT% AppParameters          "run python launch.py"
"%NSSM%" set %SVC_AGENT% AppDirectory           "%PROJECT_DIR%"
"%NSSM%" set %SVC_AGENT% DisplayName            "AAM Backup Agent"
"%NSSM%" set %SVC_AGENT% Description            "AAM Backup dashboard (port 8080) and Prefect scheduler"
"%NSSM%" set %SVC_AGENT% Start                  SERVICE_AUTO_START

:: Depend on Prefect server — SCM won't start this until AamPrefectServer is Running
"%NSSM%" set %SVC_AGENT% DependOnService        %SVC_SERVER%

:: Stdout + stderr → agent log, 10 MB rotation
"%NSSM%" set %SVC_AGENT% AppStdout              "%LOG_DIR%\agent_svc.log"
"%NSSM%" set %SVC_AGENT% AppStderr              "%LOG_DIR%\agent_svc.log"
"%NSSM%" set %SVC_AGENT% AppStdoutCreationDisposition 4
"%NSSM%" set %SVC_AGENT% AppStderrCreationDisposition 4
"%NSSM%" set %SVC_AGENT% AppRotateFiles         1
"%NSSM%" set %SVC_AGENT% AppRotateOnline        1
"%NSSM%" set %SVC_AGENT% AppRotateBytes         10485760

:: 30s restart delay — gives Prefect server time to be fully ready after its own restart
"%NSSM%" set %SVC_AGENT% AppRestartDelay        30000

"%NSSM%" set %SVC_AGENT% AppEnvironmentExtra    "PREFECT_HOME=%PREFECT_HOME%" "PREFECT_API_URL=http://127.0.0.1:4200/api"

sc failure %SVC_AGENT% reset= 86400 actions= restart/60000/restart/90000/restart/120000 >nul
sc failureflag %SVC_AGENT% 1 >nul

echo [setup] %SVC_AGENT% installed.


:: ════════════════════════════════════════════════════════════════════
:: SERVICE 3: AamWatchdog
:: Runs: uv run python watchdog.py
:: Polls Prefect API health every 60s. Restarts AamPrefectServer (via
:: sc stop) if the API is unresponsive for 3 consecutive checks.
:: No DependOnService — handles Prefect being unavailable gracefully.
:: ════════════════════════════════════════════════════════════════════
echo.
echo [setup] Installing %SVC_WATCHDOG%...

"%NSSM%" install %SVC_WATCHDOG% "%UV_EXE%"
"%NSSM%" set %SVC_WATCHDOG% AppParameters          "run python watchdog.py"
"%NSSM%" set %SVC_WATCHDOG% AppDirectory           "%PROJECT_DIR%"
"%NSSM%" set %SVC_WATCHDOG% DisplayName            "AAM Backup Watchdog"
"%NSSM%" set %SVC_WATCHDOG% Description            "Polls Prefect API health, restarts AamPrefectServer if hung-but-alive"
"%NSSM%" set %SVC_WATCHDOG% Start                  SERVICE_AUTO_START

"%NSSM%" set %SVC_WATCHDOG% AppStdout              "%LOG_DIR%\watchdog_svc.log"
"%NSSM%" set %SVC_WATCHDOG% AppStderr              "%LOG_DIR%\watchdog_svc.log"
"%NSSM%" set %SVC_WATCHDOG% AppStdoutCreationDisposition 4
"%NSSM%" set %SVC_WATCHDOG% AppStderrCreationDisposition 4
"%NSSM%" set %SVC_WATCHDOG% AppRotateFiles         1
"%NSSM%" set %SVC_WATCHDOG% AppRotateOnline        1
"%NSSM%" set %SVC_WATCHDOG% AppRotateBytes         10485760

:: Watchdog restart delay — short since it holds no state and boots instantly
"%NSSM%" set %SVC_WATCHDOG% AppRestartDelay        15000

"%NSSM%" set %SVC_WATCHDOG% AppEnvironmentExtra    "PREFECT_HOME=%PREFECT_HOME%" "PREFECT_API_URL=http://127.0.0.1:4200/api"

sc failure %SVC_WATCHDOG% reset= 86400 actions= restart/15000/restart/30000/restart/30000 >nul
sc failureflag %SVC_WATCHDOG% 1 >nul

echo [setup] %SVC_WATCHDOG% installed.


:: ════════════════════════════════════════════════════════════════════
:: Start all three services
:: ════════════════════════════════════════════════════════════════════
echo.
echo [setup] Starting %SVC_SERVER%...
net start %SVC_SERVER%
if %errorlevel% neq 0 (
    echo  ERROR: Failed to start %SVC_SERVER%. Check: %LOG_DIR%\prefect_svc.log
    exit /b 1
)

echo [setup] Waiting 20 seconds for Prefect API to become ready...
timeout /t 20 /nobreak >nul

echo [setup] Starting %SVC_AGENT%...
net start %SVC_AGENT%
if %errorlevel% neq 0 (
    echo  WARNING: %SVC_AGENT% failed to start on first attempt.
    echo  NSSM will retry automatically. Check: %LOG_DIR%\agent_svc.log
)

echo [setup] Starting %SVC_WATCHDOG%...
net start %SVC_WATCHDOG%
if %errorlevel% neq 0 (
    echo  WARNING: %SVC_WATCHDOG% failed to start on first attempt.
    echo  NSSM will retry automatically. Check: %LOG_DIR%\watchdog_svc.log
)

echo.
echo ===================================================================
echo   Installation complete.
echo ===================================================================
echo   Services:    Open services.msc to verify status
echo   Prefect UI:  http://localhost:4200
echo   Dashboard:   http://localhost:8080
echo   Server log:  %LOG_DIR%\prefect_svc.log
echo   Agent log:   %LOG_DIR%\agent_svc.log
echo ===================================================================
echo.
echo   NOTE: If LAN backup fails with access denied errors, the services
echo   may need to run as a domain or local account with share access
echo   instead of LocalSystem. Edit via: services.msc > Log On tab.
echo.
