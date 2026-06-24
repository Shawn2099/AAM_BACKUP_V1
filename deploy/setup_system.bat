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
set "GCLOUD_ZIP=%PROJECT_DIR%\deploy\bin\google-cloud-sdk.zip"

echo.
echo ===================================================================
echo   AAM Backup Automation V1 — SYSTEM SETUP
echo ===================================================================
echo   Project:  %PROJECT_DIR%
echo ===================================================================

:: ════════════════════════════════════════════════════════════════════
:: STEP 0: Install uv (Python package and runtime manager)
:: uv is the ONLY external dependency. It auto-downloads and manages
:: the correct Python version — no manual Python install needed.
:: ════════════════════════════════════════════════════════════════════
echo.
echo [0/3] Checking uv package manager...

:: Check all known install locations
set "UV_EXE="
for /f "delims=" %%I in ('where uv 2^>nul') do (
    set "UV_EXE=%%I"
    goto :uv_check_done
)
:: uv v0.4+ default install location
if exist "%USERPROFILE%\.local\bin\uv.exe"        set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if "%UV_EXE%"=="" if exist "%USERPROFILE%\.cargo\bin\uv.exe"        set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
if "%UV_EXE%"=="" if exist "C:\Program Files\Python312\Scripts\uv.exe" set "UV_EXE=C:\Program Files\Python312\Scripts\uv.exe"
:uv_check_done

if not "%UV_EXE%"=="" (
    echo [OK]   uv found: %UV_EXE%
    goto :uv_done
)

:: uv not found — download and install it
echo [....] uv not found. Downloading and installing uv...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing | Invoke-Expression"

:: Re-check known paths after install (new shell session needed for PATH,
:: so we explicitly check the paths the installer uses)
if exist "%USERPROFILE%\.local\bin\uv.exe"        set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if "%UV_EXE%"=="" if exist "%USERPROFILE%\.cargo\bin\uv.exe"        set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
if "%UV_EXE%"=="" for /f "delims=" %%I in ('where uv 2^>nul') do set "UV_EXE=%%I"

if "%UV_EXE%"=="" (
    echo [ERROR] uv installation failed. Cannot continue.
    echo [ERROR] Install manually: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo [ERROR] Then re-run setup_system.bat.
    pause
    exit /b 1
)
echo [OK]   uv installed: %UV_EXE%

:: Pre-cache Python 3.12 so first backup run is instant
echo [....] Pre-caching Python 3.12 via uv ^(one-time download^)...
"%UV_EXE%" python install 3.12 >nul 2>&1
echo [OK]   Python 3.12 ready.

:uv_done


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

:: Instruct user to manually extract to avoid Antivirus hang
echo.
echo [*]    ACTION REQUIRED: Antivirus makes auto-extraction very slow.
echo [*]    Please manually extract the downloaded file:
echo [*]    %GCLOUD_ZIP%
echo [*]    Extract it so the folder is: %PROJECT_DIR%\deploy\bin\google-cloud-sdk
echo.
echo [....] Opening the folder for you now...
explorer "%PROJECT_DIR%\deploy\bin"

:gcloud_done

echo.
echo ===================================================================
echo   SYSTEM SETUP COMPLETE
echo ===================================================================
echo   Next step: Run install_services.bat to install the 3 services.
echo ===================================================================
echo.
pause
