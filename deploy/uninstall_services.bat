@echo off
:: ═══════════════════════════════════════════════════════════════
:: AAM Backup Automation V1 — NSSM Service Uninstaller
:: Run as Administrator.
:: Stops and completely removes both AAM services.
:: ═══════════════════════════════════════════════════════════════

setlocal

set PROJECT_DIR=C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1
set NSSM=%PROJECT_DIR%\deploy\bin\nssm.exe
set SVC_SERVER=AamPrefectServer
set SVC_AGENT=AamBackupAgent
set SVC_WATCHDOG=AamWatchdog

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Must run as Administrator.
    exit /b 1
)

echo.
echo ===================================================================
echo   AAM Backup Automation V1 — Service Removal
echo ===================================================================

echo.
echo [uninstall] Stopping %SVC_WATCHDOG% (monitor first)...
"%NSSM%" stop %SVC_WATCHDOG% 2>nul
net stop %SVC_WATCHDOG% 2>nul

echo [uninstall] Stopping %SVC_AGENT%...
"%NSSM%" stop %SVC_AGENT% 2>nul
net stop %SVC_AGENT% 2>nul

echo [uninstall] Stopping %SVC_SERVER%...
"%NSSM%" stop %SVC_SERVER% 2>nul
net stop %SVC_SERVER% 2>nul

timeout /t 3 /nobreak >nul

echo [uninstall] Removing %SVC_WATCHDOG%...
"%NSSM%" remove %SVC_WATCHDOG% confirm 2>nul
sc delete %SVC_WATCHDOG% >nul 2>&1

echo [uninstall] Removing %SVC_AGENT%...
"%NSSM%" remove %SVC_AGENT% confirm 2>nul
sc delete %SVC_AGENT% >nul 2>&1

echo [uninstall] Removing %SVC_SERVER%...
"%NSSM%" remove %SVC_SERVER% confirm 2>nul
sc delete %SVC_SERVER% >nul 2>&1

echo.
echo ===================================================================
echo   Done. Both services have been removed.
echo   Logs remain at C:\BackupAgent\logs\ — delete manually if needed.
echo ===================================================================
echo.
