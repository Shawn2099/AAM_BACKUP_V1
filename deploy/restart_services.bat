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

echo [INFO] Stopping all AAM Backup services...
net stop AamWatchdog 2>nul
net stop AamBackupAgent 2>nul
net stop AamPrefectServer 2>nul

echo.
echo [INFO] Restarting AAM Backup services...
net start AamPrefectServer
echo [INFO] Waiting 10 seconds for Prefect API to spin up...
timeout /t 10 /nobreak >nul
net start AamBackupAgent
net start AamWatchdog

echo.
echo ===================================================
echo  SERVICES RESTARTED SUCCESSFULLY
echo ===================================================
pause
