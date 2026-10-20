@echo off
:: =======================================================================
:: AAM Backup Automation V1 - SYSTEM SETUP (run ONCE per server)
::
:: Run this BEFORE 06_install_services.ps1 on a fresh server.
:: All steps are idempotent - safe to re-run if needed.
::
:: What it does:
::   0. Installs uv (Python runtime and package manager)
::   1. Enables Windows Long Path support (>260 chars)
::   2. Suppresses Windows Update auto-reboots during backup windows
::   3. Adds Microsoft Defender exclusions for project dir and transfer processes
::   4. Opens Windows Firewall ports 4200 (Prefect) and 8080 (Dashboard)
::   5. Downloads the isolated Google Cloud SDK to deploy/bin
::
:: Runtime: ~1-2 minutes (most time is SDK download + extraction, first run only)
:: =======================================================================

setlocal EnableDelayedExpansion

:: -- Guard: must run as Administrator ------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click 03_setup_system.bat ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

:: -- Resolve paths -------------------------------------------------
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
echo   AAM Backup Automation V1 - SYSTEM SETUP
echo ===================================================================
echo   Project:  %PROJECT_DIR%
echo ===================================================================

:: ====================================================================
:: STEP 0: Install uv (Python package and runtime manager)
:: uv is the ONLY external dependency. It auto-downloads and manages
:: the correct Python version - no manual Python install needed.
:: ====================================================================
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

:: uv not found - download and install it
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
    echo [ERROR] Then re-run 03_setup_system.bat.
    pause
    exit /b 1
)
echo [OK]   uv installed: %UV_EXE%

:uv_done

:: Python 3.12 goes to %ProgramData%\uv\python - NOT into this admin's user
:: profile: the backup service account (plan WP16) must be able to read the
:: interpreter the project's .venv is built on. Set machine-wide so the later
:: `uv sync --frozen` (04_check_readiness.ps1) uses the same place.
if exist "C:\Program Files\Python312\python.exe" (
    echo [OK]   Python 3.12 installed for all users: C:\Program Files\Python312
    goto :python_done
)
set "UV_PYTHON_INSTALL_DIR=%ProgramData%\uv\python"
setx UV_PYTHON_INSTALL_DIR "%ProgramData%\uv\python" /M >nul
echo [....] Installing Python 3.12 via uv into %UV_PYTHON_INSTALL_DIR% ^(one-time download^)...
"%UV_EXE%" python install 3.12 >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.12 install failed - check internet access, then re-run.
    pause
    exit /b 1
)
echo [OK]   Python 3.12 ready ^(readable by the service account^).
:python_done


:: ====================================================================
:: STEP 1: Windows Long Path Support
:: Files with paths >260 chars are silently skipped by robocopy and
:: rclone without this. Essential for deep accounting folder trees.
:: ====================================================================
echo.
echo [1/3] Enabling Windows Long Path support ^(paths ^> 260 chars^)...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [OK]   Long paths enabled.
) else (
    echo [WARN] Long paths: registry write failed. Enable manually:
    echo        Group Policy: Computer Config ^> Admin Templates ^> System ^> Filesystem
    echo        Enable "Win32 long paths" and reboot.
)

:: ====================================================================
:: STEP 2: Suppress Windows Update Auto-Reboots
:: Prevents Windows from rebooting the server mid-backup at 1 AM or
:: 6 PM without warning. Updates still install - only auto-reboots
:: are blocked until an Administrator manually approves.
:: ====================================================================
echo.
echo [2/3] Suppressing Windows Update automatic reboots...
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [OK]   Auto-reboot suppressed. Server will NOT reboot automatically after updates.
) else (
    echo [WARN] Auto-reboot suppression failed. Enable manually:
    echo        Group Policy: Computer Config ^> Admin Templates ^> Windows Components
    echo        ^> Windows Update ^> "No auto-restart with logged on users"
)

:: G9: the policy above only applies while a user is LOGGED ON - a headless
:: backup server is almost never logged on, so it does not protect this box.
:: Windows Update Active Hours do: reboots are forbidden inside them. Windows Server 2016
:: allows at most 12 hours (Microsoft "waas-restart"): a longer window such as 12:00-06:00 (18 h)
:: is INVALID and ignored (plan H2). Protected = 20:00 to next 08:00 (12 h), which covers the
:: nightly LAN and cloud backups; a pending reboot can happen 08:00-20:00.
:: The real values (checked on the trial box, plan H2): the Settings value
:: HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings ActiveHoursStart/End, and the policy
:: HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate SetActiveHours/ActiveHoursStart/End.
:: (The older key ...\UX\ISM\ActiveHours does not exist on Windows.)
reg add "HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings" /v ActiveHoursStart /t REG_DWORD /d 20 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings" /v ActiveHoursEnd /t REG_DWORD /d 8 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" /v SetActiveHours /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" /v ActiveHoursStart /t REG_DWORD /d 20 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" /v ActiveHoursEnd /t REG_DWORD /d 8 /f >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [OK]   Update Active Hours set to 20:00-08:00 (12 h, the maximum). Reboots can only happen 08:00-20:00.
) else (
    echo [WARN] Active Hours registry write failed. Set manually:
    echo        HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings
    echo        ActiveHoursStart=20  ActiveHoursEnd=8
)

:: ====================================================================
:: STEP 3: Microsoft Defender Antivirus Exclusion
:: Prevents Defender from scanning the massive .venv Python folder during
:: execution, and excludes transfer processes (robocopy, rclone) from active
:: monitoring to prevent CPU throttling and file locking.
:: ====================================================================
echo.
echo [3/5] Adding Microsoft Defender exclusions for project directory and transfer processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-MpPreference -ExclusionPath '%PROJECT_DIR%' -ErrorAction SilentlyContinue; Add-MpPreference -ExclusionProcess 'robocopy.exe', 'rclone.exe' -ErrorAction SilentlyContinue"
if %ERRORLEVEL% equ 0 (
    echo [OK]   Antivirus exclusions added for: %PROJECT_DIR% and transfer processes.
) else (
    echo [WARN] Defender exclusion failed or another AV is in use.
    echo        If using third-party AV, manually exclude folder:
    echo        %PROJECT_DIR%
    echo        And processes: robocopy.exe, rclone.exe
)

:: ====================================================================
:: STEP 4: Windows Firewall
:: This script opens NO port. 06_install_services.ps1 owns the firewall: it
:: opens only the dashboard port, scoped to dashboard.firewall_remote_addresses
:: (default LocalSubnet). The Prefect port 4200 has no login and is never
:: opened (the server listens on 127.0.0.1 only). A rule created here for any
:: address would undo that scope whenever this script is run again (A5).
:: ====================================================================
echo.
echo [4/5] Firewall: nothing to do here - 06_install_services.ps1 opens the dashboard port (office network only).
echo [OK]   No firewall rule created by this script.

:: ====================================================================
:: STEP 5: Isolated Google Cloud SDK
:: Downloads the standalone SDK zip and extracts it to deploy/bin
:: using the bundled 7za.exe (much faster than Expand-Archive).
:: Completely isolated from system - immune to Windows Updates and
:: global gcloud SDK version changes.
:: ====================================================================
echo.
echo [5/5] Checking Google Cloud SDK...

:: 1. Check if gcloud is already in PATH
where gcloud >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [OK]   'gcloud' found in system PATH. Skipping isolated SDK download.
    goto :gcloud_done
)

:: 2. Check if isolated SDK is already extracted
if exist "%GCLOUD_CMD%" (
    echo [OK]   Isolated SDK already present at %GCLOUD_CMD%.
    goto :gcloud_done
)

:: 3. Check if isolated SDK zip is already downloaded but not extracted
if exist "%GCLOUD_ZIP%" (
    echo [OK]   SDK archive already downloaded at %GCLOUD_ZIP%.
    goto :extract_instructions
)

echo [....] SDK not found. Downloading standalone archive ^(~120MB^)...
echo [....] This only happens ONCE. Subsequent runs skip this step.
echo.

:: Download - TLS 1.2 forced inside the PowerShell command itself
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-windows-x86_64.zip' -OutFile '%GCLOUD_ZIP%' -UseBasicParsing"

if not exist "%GCLOUD_ZIP%" (
    echo [WARN] Download failed. Check internet connectivity.
    echo [WARN] FY rollover archive transition will be skipped on April 1.
    echo [WARN] Re-run 03_setup_system.bat when internet is available.
    goto :gcloud_done
)

echo [OK]   Download complete.

:extract_instructions
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
echo   Next step: Run 06_install_services.ps1 to install the 3 services.
echo ===================================================================
echo.
pause
