@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: Test config.yaml for syntax and validation errors before restarting
:: ═══════════════════════════════════════════════════════════════════════

set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "PROJECT_DIR=%%~fI"

:: Find uv
set "UV_EXE="
for /f "delims=" %%I in ('where uv 2^>nul') do (
    set "UV_EXE=%%I"
    goto :uv_found
)
:uv_found

if "%UV_EXE%"=="" (
    if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
)
if "%UV_EXE%"=="" (
    if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
)
if "%UV_EXE%"=="" (
    if exist "C:\Program Files\Python312\Scripts\uv.exe" set "UV_EXE=C:\Program Files\Python312\Scripts\uv.exe"
)

if "%UV_EXE%"=="" (
    echo ERROR: 'uv' package manager not found.
    pause
    exit /b 1
)

cd /d "%PROJECT_DIR%"
"%UV_EXE%" run python "%SCRIPT_DIR%\test_config.py"
pause
