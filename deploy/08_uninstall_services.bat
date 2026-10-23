@echo off
:: =======================================================================
:: AAM Backup Automation V1 — Uninstall Script
:: Performs a complete clean sweep of all services and related processes.
:: Must be run as Administrator.
:: =======================================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click this file ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"
set "AAM_PROJECT_DIR=%PROJECT_DIR%"
set NSSM=%PROJECT_DIR%\deploy\bin\nssm.exe

echo.
echo ===================================================
echo  AAM Backup - Complete Clean Sweep
echo ===================================================
echo.

:: -------------------------------------------------------
:: STEP 1: Stop Windows Services (reverse dependency order)
:: Watchdog first — prevents it from restarting stopped services
:: -------------------------------------------------------
echo  [1/4] Stopping Windows services...
echo.

if not exist "%NSSM%" (
    echo  NSSM not found. Using sc command as fallback...
    echo.

    sc query AamWatchdog >nul 2>&1
    if %errorlevel% equ 0 (
        sc stop AamWatchdog 2>nul
        timeout /t 3 /nobreak >nul
        sc delete AamWatchdog
        echo  Removed: AamWatchdog
    ) else (
        echo  Skipped: AamWatchdog ^(not installed^)
    )

    sc query AamBackupAgent >nul 2>&1
    if %errorlevel% equ 0 (
        sc stop AamBackupAgent 2>nul
        timeout /t 3 /nobreak >nul
        sc delete AamBackupAgent
        echo  Removed: AamBackupAgent
    ) else (
        echo  Skipped: AamBackupAgent ^(not installed^)
    )

    sc query AamPrefectServer >nul 2>&1
    if %errorlevel% equ 0 (
        sc stop AamPrefectServer 2>nul
        timeout /t 5 /nobreak >nul
        sc delete AamPrefectServer
        echo  Removed: AamPrefectServer
    ) else (
        echo  Skipped: AamPrefectServer ^(not installed^)
    )
) else (
    echo  Using NSSM...
    echo.

    "%NSSM%" stop AamWatchdog 2>nul
    timeout /t 3 /nobreak >nul
    "%NSSM%" remove AamWatchdog confirm 2>nul
    echo  Processed: AamWatchdog

    "%NSSM%" stop AamBackupAgent 2>nul
    timeout /t 5 /nobreak >nul
    "%NSSM%" remove AamBackupAgent confirm 2>nul
    echo  Processed: AamBackupAgent

    "%NSSM%" stop AamPrefectServer 2>nul
    timeout /t 5 /nobreak >nul
    "%NSSM%" remove AamPrefectServer confirm 2>nul
    echo  Processed: AamPrefectServer
)

echo.
echo  Waiting 5 seconds for services to fully terminate...
timeout /t 5 /nobreak >nul

:: -------------------------------------------------------
:: STEP 2: Kill all Python processes running our scripts
:: Matches any python.exe with launch.py, watchdog.py, or serve.py
:: in the command line — covers system python, dev venv, and AAMBackup venv
:: -------------------------------------------------------
echo.
echo  [2/4] Killing Python processes running AAM scripts...
echo.

powershell -NoProfile -Command ^
    "Get-WmiObject Win32_Process | Where-Object { $_.Name -like '*python*' -and ($_.CommandLine -match 'launch\.py' -or $_.CommandLine -match 'watchdog\.py' -or $_.CommandLine -match 'serve\.py') } | ForEach-Object { Write-Host ('  Killing PID ' + $_.ProcessId + ': ' + $_.Name + ' -- ' + $_.CommandLine.Substring(0, [Math]::Min(90, $_.CommandLine.Length))); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo  Python processes cleaned.

:: -------------------------------------------------------
:: STEP 3: Kill all active data-transfer processes
:: rclone.exe (cloud sync) and robocopy.exe (LAN sync)
:: -------------------------------------------------------
echo.
echo  [3/4] Killing data transfer processes...
echo.

echo  Killing OUR rclone.exe (the one in this install's deploy\bin)...
powershell -NoProfile -Command ^
    "$bin = (Join-Path $env:AAM_PROJECT_DIR 'deploy\bin') + '\'; $n = 0; Get-CimInstance Win32_Process -Filter \"Name='rclone.exe'\" | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($bin, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { $n++; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; if ($n) { Write-Host ('  Done (' + $n + ').') } else { Write-Host '  Not running.' }"

echo  Killing OUR robocopy.exe (the backup's own: it always excludes the .aam_write_probe_ files)...
powershell -NoProfile -Command ^
    "$n = 0; Get-CimInstance Win32_Process -Filter \"Name='robocopy.exe'\" | Where-Object { $_.CommandLine -like '*.aam_write_probe_*' } | ForEach-Object { $n++; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; if ($n) { Write-Host ('  Done (' + $n + ').') } else { Write-Host '  Not running.' }"

:: -------------------------------------------------------
:: STEP 4: Kill remaining launcher processes of THIS install
:: prefect.exe and uv.exe are killed LAST since they are
:: parent launchers; only those started from this install's folder
:: (another uv or prefect on the machine is not ours to stop)
:: -------------------------------------------------------
echo.
echo  [4/4] Killing launcher processes...
echo.

powershell -NoProfile -Command ^
    "$root = $env:AAM_PROJECT_DIR.TrimEnd('\') + '\'; $n = 0; Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'prefect.exe' -or $_.Name -eq 'uv.exe') -and (($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) -or ($_.CommandLine -and $_.CommandLine.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0)) } | ForEach-Object { $n++; Write-Host ('  Killing ' + $_.Name + ' PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; if (-not $n) { Write-Host '  Not running.' }"

echo.
echo ===================================================
echo  COMPLETE — AAM Backup fully removed.
echo ===================================================
echo.
echo  All AAM services unregistered.
echo  All AAM processes terminated.
echo.
echo  To reinstall: deploy\06_install_services.ps1
echo.
pause
