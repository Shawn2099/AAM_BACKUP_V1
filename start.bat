@echo off
REM AAM Backup Automation V1 — Production Startup Script
REM Runs from Task Scheduler ("At system startup", delay 30s)
REM or manually. Starts: Prefect API + Dashboard UI + Backup Scheduler

cd /d "%~dp0"
set PATH=C:\Program Files\Python312\Scripts;%PATH%
uv run python launch.py
