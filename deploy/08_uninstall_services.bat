@echo off
:: =======================================================================
:: Uninstall Services for AAM Backup Automation
:: =======================================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click un06_install_services.ps1 ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"
set NSSM=%PROJECT_DIR%\deploy\bin\nssm.exe

echo.
echo ===================================================
echo  AAM Backup - Uninstalling Services
echo ===================================================
echo.

set "REMOVED=0"
set "NOTFOUND=0"

if not exist "%NSSM%" (
    echo  NSSM not found. Using Windows sc command as fallback...
    echo.

    sc query AamPrefectServer >nul 2>&1
    if %errorlevel% equ 0 (
        sc stop AamPrefectServer 2>nul
        sc delete AamPrefectServer
        set /a REMOVED+=1
        echo  Removed: AamPrefectServer
    ) else (
        echo  Skipped: AamPrefectServer (not installed)
        set /a NOTFOUND+=1
    )

    sc query AamBackupAgent >nul 2>&1
    if %errorlevel% equ 0 (
        sc stop AamBackupAgent 2>nul
        sc delete AamBackupAgent
        set /a REMOVED+=1
        echo  Removed: AamBackupAgent
    ) else (
        echo  Skipped: AamBackupAgent (not installed)
        set /a NOTFOUND+=1
    )

    sc query AamWatchdog >nul 2>&1
    if %errorlevel% equ 0 (
        sc stop AamWatchdog 2>nul
        sc delete AamWatchdog
        set /a REMOVED+=1
        echo  Removed: AamWatchdog
    ) else (
        echo  Skipped: AamWatchdog (not installed)
        set /a NOTFOUND+=1
    )
) else (
    echo  Using NSSM to remove services...
    echo.

    "%NSSM%" stop AamPrefectServer 2>nul
    "%NSSM%" remove AamPrefectServer confirm 2>nul
    set /a REMOVED+=1
    echo  Removed: AamPrefectServer

    "%NSSM%" stop AamBackupAgent 2>nul
    "%NSSM%" remove AamBackupAgent confirm 2>nul
    set /a REMOVED+=1
    echo  Removed: AamBackupAgent

    "%NSSM%" stop AamWatchdog 2>nul
    "%NSSM%" remove AamWatchdog confirm 2>nul
    set /a REMOVED+=1
    echo  Removed: AamWatchdog
)

echo.
echo  Killing any orphaned processes...
taskkill /F /IM prefect.exe /T 2>nul
taskkill /F /IM uv.exe /T 2>nul

echo.
echo ===================================================
echo  UNINSTALL COMPLETE
echo ===================================================
echo.
if %REMOVED% equ 0 (
    echo  No services were found to remove.
    echo  They may have already been uninstalled.
) else (
    echo  Removed %REMOVED% service(s).
)
echo.
echo  To reinstall, run:  deploy\06_install_services.ps1
echo.
pause
