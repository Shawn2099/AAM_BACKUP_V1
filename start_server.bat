@echo off
REM AAM Backup Automation V1 — Prefect API Server
REM Runs as a separate Task Scheduler entry.
REM Starts at system startup with 10-second delay.
REM Runs indefinitely — Windows restarts on failure.

cd /d "%~dp0"
set PATH=C:\Program Files\Python312\Scripts;%PATH%
set PREFECT_API_URL=http://127.0.0.1:4200/api
set PREFECT_SERVER_API_PORT=4200

uv run prefect server start
