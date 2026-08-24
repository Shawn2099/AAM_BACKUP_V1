@echo off
:: One-time Google Cloud login for AAM Backup setup.
:: A browser window will open - sign in with the Google account
:: that owns the aam-backup-2026 project.
title AAM - Google Cloud Login
"C:\AAM_BACKUP_V1\deploy\bin\google-cloud-sdk\bin\gcloud.cmd" auth login
echo.
echo Login complete. You can close this window.
pause
