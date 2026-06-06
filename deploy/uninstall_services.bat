@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: Uninstall Services for AAM Backup Automation
:: ═══════════════════════════════════════════════════════════════════════

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click uninstall_services.bat ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"
set NSSM=%PROJECT_DIR%\deploy\bin\nssm.exe

if not exist "%NSSM%" (
    echo [WARN] NSSM not found. Proceeding with sc delete...
    sc stop AamWatchdog 2>nul
    sc stop AamBackupAgent 2>nul
    sc stop AamPrefectServer 2>nul
    sc delete AamWatchdog 2>nul
    sc delete AamBackupAgent 2>nul
    sc delete AamPrefectServer 2>nul
) else (
    echo [INFO] Stopping services...
    "%NSSM%" stop AamWatchdog 2>nul
    "%NSSM%" stop AamBackupAgent 2>nul
    "%NSSM%" stop AamPrefectServer 2>nul

    echo [INFO] Removing services...
    "%NSSM%" remove AamWatchdog confirm 2>nul
    "%NSSM%" remove AamBackupAgent confirm 2>nul
    "%NSSM%" remove AamPrefectServer confirm 2>nul
)

echo [INFO] Killing any orphaned prefect.exe processes...
taskkill /F /IM prefect.exe /T 2>nul

echo.
echo ===================================================
echo  SERVICES UNINSTALLED SUCCESSFULLY
echo ===================================================
pause
