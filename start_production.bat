@echo off
REM AAM Backup Automation V1 — Production Startup Script
REM Run via Task Scheduler: "At system startup" with 30-second delay
REM Starts: Prefect API + Backup Scheduler + Dashboard UI

cd /d C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1

echo Starting Prefect API server...
start "PrefectServer" cmd /c "prefect server start"

echo Starting Dashboard UI...
start "DashboardUI" cmd /c "uv run python ui.py"

echo Waiting 30 seconds for Prefect API to be ready...
timeout /t 30 /nobreak >nul

echo Starting backup scheduler...
uv run python serve.py
pause
