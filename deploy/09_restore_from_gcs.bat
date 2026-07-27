@echo off
:: =======================================================================
:: AAM Backup Automation V1 - DISASTER RECOVERY & DATA RESTORE TOOL (v2)
::
:: Safely restores backup data from Google Cloud Storage (GCS) to a
:: local folder. Includes pre-flight checks, confirmation, logging,
:: and optional post-restore verification.
::
:: REQUIREMENTS: Run as Administrator for full path access.
:: =======================================================================

setlocal EnableDelayedExpansion

:: -- Self-Elevate to Administrator ---------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click 09_restore_from_gcs.bat ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

:: -- Resolve project root from script location --------------------------
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"

:: -- Core paths ---------------------------------------------------------
set "RCLONE=%SCRIPT_DIR%\bin\rclone.exe"
set "KEY_FILE=%SCRIPT_DIR%\keys\aam-gcs-key.json"
set "CONFIG_YAML=%PROJECT_DIR%\config.yaml"
set "READ_CONFIG=%SCRIPT_DIR%\read_config.py"

:: -- Find uv (consistent with all other scripts) ------------------------
set "UV_EXE="
for /f "delims=" %%I in ('where uv 2^>nul') do (
    set "UV_EXE=%%I"
    goto :uv_found
)
:uv_found
if "%UV_EXE%"=="" if exist "%USERPROFILE%\.local\bin\uv.exe"        set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if "%UV_EXE%"=="" if exist "%USERPROFILE%\.cargo\bin\uv.exe"        set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
if "%UV_EXE%"=="" if exist "C:\Program Files\Python312\Scripts\uv.exe" set "UV_EXE=C:\Program Files\Python312\Scripts\uv.exe"

:: -- Read bucket name from config.yaml (uv-managed Python env) ----------
set "BUCKET_NAME=aam-cloudbackup"
if not "%UV_EXE%"=="" (
    if exist "%READ_CONFIG%" if exist "%CONFIG_YAML%" (
        cd /d "%PROJECT_DIR%"
        for /f "delims=" %%I in ('"%UV_EXE%" run --quiet python "%READ_CONFIG%" "%CONFIG_YAML%" cloud.bucket --default aam-cloudbackup 2^>nul') do (
            set "BUCKET_NAME=%%I"
        )
    )
)

:: -- Logging setup -------------------------------------------------------
set "DEFAULT_RUNTIME=C:\BackupAgent"
set "RUNTIME_DIR=%DEFAULT_RUNTIME%"
if not "%UV_EXE%"=="" (
    if exist "%READ_CONFIG%" if exist "%CONFIG_YAML%" (
        for /f "delims=" %%R in ('"%UV_EXE%" run --quiet python "%READ_CONFIG%" "%CONFIG_YAML%" paths.runtime_dir --default "%DEFAULT_RUNTIME%" 2^>nul') do (
            set "RUNTIME_DIR=%%R"
        )
    )
)
set "LOG_DIR=%RUNTIME_DIR%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1

:: Timestamped log file per restore session
for /f "tokens=2 delims==" %%I in ('wmic os get LocalDateTime /value') do set "DT=%%I"
set "TIMESTAMP=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%_%DT:~8,2%-%DT:~10,2%"
set "LOG_FILE=%LOG_DIR%\restore_%TIMESTAMP%.log"

:: -- Banner --------------------------------------------------------------
echo.
echo ====================================================================
echo   AAM BACKUP AUTOMATION - DISASTER RECOVERY RESTORE TOOL v2
echo ====================================================================
echo   Logs saved to: %LOG_FILE%
echo ====================================================================
echo.
call :LOG "=== AAM Disaster Recovery Restore Started at %TIMESTAMP% ==="

:: -- PRE-FLIGHT CHECKS --------------------------------------------------
echo [1/4] Running pre-flight checks...
echo.

:: Check rclone binary
if not exist "%RCLONE%" (
    echo [FAIL] rclone.exe missing at: %RCLONE%
    echo        Download from https://rclone.org/downloads/ and place in deploy\bin\
    call :LOG "[FAIL] rclone.exe missing at: %RCLONE%"
    pause
    exit /b 1
)
echo [OK]   rclone.exe found.
call :LOG "[OK] rclone.exe found."

:: Check GCS Service Account Key
if not exist "%KEY_FILE%" (
    echo [FAIL] GCS Service Account Key missing at: %KEY_FILE%
    echo        Place the aam-gcs-key.json file in deploy\keys\ and retry.
    call :LOG "[FAIL] GCS Key missing: %KEY_FILE%"
    pause
    exit /b 1
)

:: Validate that the key is a valid JSON service account file
powershell -NoProfile -Command "(Get-Content '%KEY_FILE%' -Raw | ConvertFrom-Json -ErrorAction SilentlyContinue).type -eq 'service_account'" 2>nul | findstr /i "true" >nul 2>&1
if %errorlevel% neq 0 (
    echo [FAIL] GCS Key is not a valid service account JSON: %KEY_FILE%
    call :LOG "[FAIL] GCS Key invalid or corrupt: %KEY_FILE%"
    pause
    exit /b 1
)
echo [OK]   GCS Service Account Key is valid.
call :LOG "[OK] GCS Key valid."

:: Test GCS connectivity (fast ls check — timeout 30s)
echo [....] Testing GCS connectivity...
"%RCLONE%" lsd ":gcs:%BUCKET_NAME%" --gcs-service-account-file="%KEY_FILE%" --contimeout=30s --timeout=30s >nul 2>&1
if %errorlevel% neq 0 (
    echo [FAIL] Cannot connect to GCS bucket gs://%BUCKET_NAME%
    echo        Check internet connectivity or Service Account permissions.
    call :LOG "[FAIL] GCS connectivity test failed for bucket: %BUCKET_NAME%"
    pause
    exit /b 1
)
echo [OK]   GCS bucket accessible: gs://%BUCKET_NAME%
call :LOG "[OK] GCS connectivity confirmed: gs://%BUCKET_NAME%"

echo.

:: -- USER INPUT ---------------------------------------------------------
echo [2/4] Restore Parameters
echo.
echo        GCS Bucket: gs://%BUCKET_NAME%
echo.
set /p GCS_SUBPATH="        Enter GCS folder to restore (e.g. FY25-26, leave blank for FULL bucket): "
set /p LOCAL_DEST="        Enter local destination path (e.g. D:\RESTORED_DATA):  "
echo.

:: Validate destination not empty
if "!LOCAL_DEST!"=="" (
    echo [FAIL] Local destination path cannot be empty.
    call :LOG "[FAIL] User provided empty destination path."
    pause
    exit /b 1
)

:: Build remote path
if "!GCS_SUBPATH!"=="" (
    set "REMOTE_PATH=:gcs:%BUCKET_NAME%"
    set "RESTORE_LABEL=FULL BUCKET"
) else (
    set "REMOTE_PATH=:gcs:%BUCKET_NAME%/!GCS_SUBPATH!"
    set "RESTORE_LABEL=!GCS_SUBPATH!"
)

:: -- SAFETY CONFIRMATION ------------------------------------------------
echo [3/4] Restore Confirmation
echo.
echo ====================================================================
echo   [!] RESTORE SUMMARY - PLEASE REVIEW BEFORE PROCEEDING
echo.
echo   GCS Source:  gs://%BUCKET_NAME%/!GCS_SUBPATH!
echo   Local Dest:  !LOCAL_DEST!
echo.
echo   NOTE: rclone COPY mode is used. Existing files at the destination
echo   will NOT be deleted. Only missing or newer files will be written.
echo ====================================================================
echo.
set /p CONFIRM="   Type YES to start the restore (or any other key to cancel): "
echo.
if /i not "!CONFIRM!"=="YES" (
    echo  Restore cancelled by user.
    call :LOG "Restore cancelled by user at confirmation step."
    pause
    exit /b 0
)

:: Create destination directory if needed
if not exist "!LOCAL_DEST!" (
    echo [....] Creating destination folder: !LOCAL_DEST!
    mkdir "!LOCAL_DEST!" >nul 2>&1
    if %errorlevel% neq 0 (
        echo [FAIL] Cannot create destination folder. Check path and permissions.
        call :LOG "[FAIL] mkdir failed: !LOCAL_DEST!"
        pause
        exit /b 1
    )
)

:: -- RESTORE OPERATION --------------------------------------------------
echo [4/4] Restore in progress...
echo.
call :LOG "Restore started: Source=!REMOTE_PATH!  Dest=!LOCAL_DEST!"

"%RCLONE%" copy "!REMOTE_PATH!" "!LOCAL_DEST!" ^
    --gcs-service-account-file="%KEY_FILE%" ^
    --progress ^
    --transfers=4 ^
    --checkers=8 ^
    --buffer-size=64M ^
    --retries=5 ^
    --low-level-retries=10 ^
    --stats=30s ^
    --log-file="%LOG_FILE%" ^
    --log-level=INFO

set "RCLONE_EXIT=%errorlevel%"

echo.
echo ====================================================================
if %RCLONE_EXIT% equ 0 (
    echo [OK] Restore completed successfully.
    echo      Files restored to: !LOCAL_DEST!
    echo      Full log: %LOG_FILE%
    call :LOG "[OK] Restore completed successfully. Dest=!LOCAL_DEST!"
) else (
    echo [WARN] Restore finished with errors (exit code: %RCLONE_EXIT%).
    echo        Some files may not have transferred. Review full log:
    echo        %LOG_FILE%
    echo        Common causes: network interruption, permission error on a
    echo        specific file, or a file that was open during upload.
    echo        Re-run this script to resume - rclone will skip files that
    echo        already transferred successfully.
    call :LOG "[WARN] Restore finished with exit code %RCLONE_EXIT%. Review log."
)
echo ====================================================================
echo.
call :LOG "=== Restore session ended ==="
pause
exit /b %RCLONE_EXIT%

:: -- LOG HELPER ---------------------------------------------------------
:LOG
echo %~1 >> "%LOG_FILE%" 2>nul
goto :eof
