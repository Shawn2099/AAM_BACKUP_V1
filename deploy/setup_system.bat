@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: AAM Backup Automation V1 — SYSTEM SETUP (run ONCE per server)
::
:: Run this BEFORE install_services.bat on a fresh server.
:: All steps are idempotent — safe to re-run if needed.
::
:: What it does:
::   1. Enables Windows Long Path support (>260 chars)
::   2. Suppresses Windows Update auto-reboots during backup windows
::   3. Downloads and extracts the isolated Google Cloud SDK to deploy/bin
::      (required for FY rollover archive transition on April 1)
::
:: Runtime: ~1-2 minutes (most time is SDK download + extraction, first run only)
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

:: ── Resolve paths ─────────────────────────────────────────────────────
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"

set "SEVENZIP=%PROJECT_DIR%\deploy\bin\7za.exe"
set "GCLOUD_CMD=%PROJECT_DIR%\deploy\bin\google-cloud-sdk\bin\gcloud.cmd"

:: Set zip path HERE (outside all if-blocks) so %GCLOUD_ZIP% expands correctly
:: inside nested blocks with EnableDelayedExpansion.
set "GCLOUD_ZIP=%TEMP%\google-cloud-sdk.zip"

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
:: global gcloud SDK version changes.
:: ════════════════════════════════════════════════════════════════════
echo.
echo [3/3] Checking isolated Google Cloud SDK...

if exist "%GCLOUD_CMD%" (
    echo [OK]   SDK already present. Skipping download.
    echo [OK]   %GCLOUD_CMD%
    goto :gcloud_done
)

echo [....] SDK not found. Downloading standalone archive ^(~120MB^)...
echo [....] This only happens ONCE. Subsequent runs skip this step.
echo.

:: Download — TLS 1.2 forced inside the PowerShell command itself
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-windows-x86_64.zip' -OutFile '%GCLOUD_ZIP%' -UseBasicParsing"

if not exist "%GCLOUD_ZIP%" (
    echo [WARN] Download failed. Check internet connectivity.
    echo [WARN] FY rollover archive transition will be skipped on April 1.
    echo [WARN] Re-run setup_system.bat when internet is available.
    goto :gcloud_done
)

echo [OK]   Download complete.

:: Extract — use bundled 7za.exe (fast) or fall back to Expand-Archive (slow)
echo [....] Extracting SDK to deploy\bin\google-cloud-sdk...
if exist "%SEVENZIP%" (
    echo [OK]   Using 7za.exe for fast extraction...
    "%SEVENZIP%" x "%GCLOUD_ZIP%" -o"%PROJECT_DIR%\deploy\bin" -y >nul
) else (
    echo [WARN] 7za.exe not found. Falling back to Expand-Archive ^(~10 min^)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Expand-Archive -Path '%GCLOUD_ZIP%' -DestinationPath '%PROJECT_DIR%\deploy\bin' -Force"
)

:: Delete zip immediately to free disk space
del /f /q "%GCLOUD_ZIP%" 2>nul

:: Verify extraction succeeded
if exist "%GCLOUD_CMD%" (
    echo [OK]   Google Cloud SDK extracted successfully.
    echo [OK]   Location: %GCLOUD_CMD%
) else (
    echo [WARN] Extraction completed but gcloud.cmd not found at expected path:
    echo [WARN]   %GCLOUD_CMD%
    echo [WARN] FY rollover archive transition will be skipped on April 1.
)

:gcloud_done

echo.
echo ===================================================================
echo   SYSTEM SETUP COMPLETE
echo ===================================================================
echo   Next step: Run install_services.bat to install the 3 services.
echo ===================================================================
echo.
pause
