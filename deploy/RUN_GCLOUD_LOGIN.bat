@echo off
:: One-time Google Cloud login for AAM Backup setup.
:: A browser window will open - sign in with the Google account
:: that owns the Google Cloud project of this site.
:: Works from wherever the code folder is (no fixed path).
title AAM - Google Cloud Login
set "GCLOUD=%~dp0bin\google-cloud-sdk\bin\gcloud.cmd"
if not exist "%GCLOUD%" (
    echo ERROR: %GCLOUD% not found. Run 03_setup_system.bat first - it unpacks the Google Cloud SDK.
    pause
    exit /b 1
)
"%GCLOUD%" auth login
echo.
echo Login complete. You can close this window.
pause
