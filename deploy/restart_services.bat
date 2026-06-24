@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: Restart Services for AAM Backup Automation
:: Run this as Administrator after updating config.yaml
:: ═══════════════════════════════════════════════════════════════════════

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click restart_services.bat ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

:: ── Resolve project directory ────────────────────────────────────────
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"

:: ── Read runtime_dir from config.yaml via Python ─────────────────────
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

echo [INFO] Stopping all AAM Backup services...
net stop AamWatchdog 2>nul
net stop AamBackupAgent 2>nul
net stop AamPrefectServer 2>nul

echo.
echo [INFO] Restarting AAM Backup services...
net start AamPrefectServer
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: AamPrefectServer failed to start.
    echo  Dependent services cannot start without it.
    echo  Check: %LOG_DIR%\prefect_svc.log
    pause
    exit /b 1
)
echo [INFO] Waiting 10 seconds for Prefect API to spin up...
timeout /t 10 /nobreak >nul
net start AamBackupAgent
net start AamWatchdog

echo.
echo ===================================================
echo  SERVICES RESTARTED SUCCESSFULLY
echo ===================================================
pause
