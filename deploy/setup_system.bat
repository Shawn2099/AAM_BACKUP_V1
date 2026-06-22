@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: AAM Backup Automation V1 — SYSTEM SETUP (run ONCE per server)
::
:: Run this BEFORE install_services.bat on a fresh server.
:: It only needs to be run once. All changes are idempotent.
::
:: What it does:
::   1. Enables Windows Long Path support (>260 chars)
::   2. Suppresses Windows Update auto-reboots during backup windows
::   3. Downloads and extracts the isolated Google Cloud SDK to deploy/bin
::      (required for FY rollover archive transition on April 1)
::
:: Runtime: ~1-2 minutes (most time is SDK download on first run)
:: ═══════════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion

:: ── Guard: must run as Administrator ────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click setup_system.bat ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

:: ── Resolve paths ────────────────────────────────────────────────────
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"

set "SEVENZIP=%PROJECT_DIR%\deploy\bin\7za.exe"
set "GCLOUD_CMD=%PROJECT_DIR%\deploy\bin\google-cloud-sdk\bin\gcloud.cmd"

echo.
echo ===================================================================
echo   AAM Backup Automation V1 — SYSTEM SETUP
echo ===================================================================
echo   Project:  %PROJECT_DIR%
echo ===================================================================

:: ════════════════════════════════════════════════════════════════════
:: STEP 1: Windows Long Path Support
:: Files with paths >260 chars are silently skipped by robocopy and
:: rclone without this. Essential for deep accounting folder trees.
:: ════════════════════════════════════════════════════════════════════
echo.
echo [1/3] Enabling Windows Long Path support ^(paths ^> 260 chars^)...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [OK]   Long paths enabled.
) else (
    echo [WARN] Long paths: registry write failed. Enable via Group Policy if needed.
)

:: ════════════════════════════════════════════════════════════════════
:: STEP 2: Suppress Windows Update Auto-Reboots
:: Prevents Windows from rebooting the server mid-backup at 1 AM or
:: 6 PM without warning. Updates still install — only auto-reboots
:: are blocked until an Administrator manually approves.
:: ════════════════════════════════════════════════════════════════════
echo.
echo [2/3] Suppressing Windows Update automatic reboots...
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [OK]   Auto-reboot suppressed. Server will NOT reboot automatically after updates.
) else (
    echo [WARN] Auto-reboot suppression failed. Configure via Group Policy if needed.
)

:: ════════════════════════════════════════════════════════════════════
:: STEP 3: Isolated Google Cloud SDK
:: Downloads the standalone SDK zip and extracts it to deploy/bin
:: using the bundled 7za.exe (much faster than Expand-Archive).
:: Completely isolated from system — immune to Windows Updates and
:: gcloud SDK version changes on the server.
:: ════════════════════════════════════════════════════════════════════
echo.
echo [3/3] Checking isolated Google Cloud SDK...

if exist "%GCLOUD_CMD%" (
    echo [OK]   SDK already present: %GCLOUD_CMD%
    echo [OK]   Skipping download.
    goto :gcloud_done
)

echo [....] SDK not found. Downloading standalone archive ^(~120MB^)...
echo [....] This only happens ONCE. Subsequent runs skip this step.
echo.

:: Verify 7za.exe is available for fast extraction
if not exist "%SEVENZIP%" (
    echo [WARN] 7za.exe not found at %SEVENZIP%.
    echo [WARN] Falling back to Expand-Archive (will be slower ~10 min).
    set USE_SEVENZIP=0
) else (
    echo [OK]   Using 7za.exe for fast extraction.
    set USE_SEVENZIP=1
)

:: Download
set "GCLOUD_ZIP=%TEMP%\google-cloud-sdk.zip"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-windows-x86_64.zip' -OutFile '%GCLOUD_ZIP%' -UseBasicParsing"

if not exist "%GCLOUD_ZIP%" (
    echo [WARN] Download failed. FY rollover archive transition will be skipped on April 1.
    echo [WARN] Re-run setup_system.bat when internet is available.
    goto :gcloud_done
)

:: Extract
echo.
echo [....] Extracting SDK to deploy\bin\google-cloud-sdk...
if %USE_SEVENZIP% equ 1 (
    "%SEVENZIP%" x "%GCLOUD_ZIP%" -o"%PROJECT_DIR%\deploy\bin" -y >nul
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%GCLOUD_ZIP%' -DestinationPath '%PROJECT_DIR%\deploy\bin' -Force"
)

:: Cleanup zip immediately
del /f /q "%GCLOUD_ZIP%" 2>nul

:: Verify
if exist "%GCLOUD_CMD%" (
    echo [OK]   Google Cloud SDK extracted successfully.
    echo [OK]   Location: %GCLOUD_CMD%
) else (
    echo [WARN] Extraction failed. gcloud.cmd not found at expected path.
    echo [WARN] FY rollover archive transition will be skipped on April 1.
)

:gcloud_done

echo.
echo ===================================================================
echo   SYSTEM SETUP COMPLETE
echo ===================================================================
echo   You can now run install_services.bat to install the services.
echo ===================================================================
echo.
pause
