@echo off
REM AAM Backup Automation V1 — Dashboard + Scheduler
REM Runs as a separate Task Scheduler entry (after Prefect server is ready).
REM Starts at system startup with 60-second delay.

cd /d "%~dp0"
set PATH=C:\Program Files\Python312\Scripts;%PATH%
set PREFECT_API_URL=http://127.0.0.1:4200/api

uv run python launch.py
