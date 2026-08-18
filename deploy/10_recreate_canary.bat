@echo off
:: =======================================================================
:: Recreate the .AAM_TARGET_MOUNTED canary file in the LAN destination
:: usage:  10_recreate_canary.bat <destination-UNC> [canary-filename]
:: example:  10_recreate_canary.bat \\10.0.0.5\Backups\FY25-26
::
:: G11: the preflight (core/lan_preflight.py) refuses to run robocopy /MIR
:: against a destination without this canary, to prevent mirroring into an
:: unmounted/empty share. This script is the documented, one-step recovery
:: for the "Canary file ... missing" preflight failure.
:: =======================================================================
setlocal

set DEST=%~1
set NAME=%~2
if "%NAME%"=="" set NAME=.AAM_TARGET_MOUNTED

if "%DEST%"=="" (
    echo.
    echo  ERROR: destination path required.
    echo.
    echo  Usage:  %~nx0 ^<destination-UNC^> [canary-filename]
    echo  Example: %~nx0 \\NAS\Backups\FY25-26
    echo.
    exit /b 1
)

if exist "%DEST%\%NAME%" (
    echo [OK]   Canary already present: %DEST%\%NAME%
    exit /b 0
)

if not exist "%DEST%\" (
    echo.
    echo  ERROR: destination does not exist or is not reachable: %DEST%
    echo  Check that the FY share is mounted before re-creating the canary.
    echo  (A missing canary on a share that should hold backup data usually
    echo   means the share or its mount was touched manually - investigate
    echo   before continuing.)
    echo.
    exit /b 1
)

> "%DEST%\%NAME%" type nul
if %ERRORLEVEL% equ 0 (
    echo [OK]   Canary created: %DEST%\%NAME%
    echo        The next LAN preflight will pass. Re-run the LAN pipeline if
    echo        the backup was aborted because of the missing canary.
    exit /b 0
) else (
    echo.
    echo  ERROR: could not create %DEST%\%NAME%
    echo  Check share permissions for the account running this script.
    echo.
    exit /b 1
)
endlocal
