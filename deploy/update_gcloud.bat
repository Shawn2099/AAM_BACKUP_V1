@echo off
:: =======================================================================
:: Update Isolated gcloud SDK (deploy/bin/google-cloud-sdk)
:: Run this periodically or when archive transitions fail with API errors.
:: =======================================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click update_gcloud.bat ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

set "SCRIPT_DIR=%~dp0"
set "GCLOUD=%SCRIPT_DIR%bin\google-cloud-sdk\bin\gcloud.cmd"

if not exist "%GCLOUD%" (
    echo.
    echo  ERROR: Isolated gcloud SDK not found at:
    echo    %GCLOUD%
    echo.
    echo  Run 03_setup_system.bat to install it first.
    pause
    exit /b 1
)

echo.
echo ===================================================
echo  Updating isolated gcloud SDK...
echo ===================================================
echo.

"%GCLOUD%" components update

if %errorlevel% neq 0 (
    echo.
    echo  WARNING: Update may have failed. Check the output above.
    echo  If API errors persist, try re-installing:
    echo    deploy\03_setup_system.bat
) else (
    echo.
    echo  gcloud SDK updated successfully.
    "%GCLOUD%" version
)

pause
