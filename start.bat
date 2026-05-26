@echo off
REM AAM Backup Automation V1 — One-click Launch
REM Starts: Prefect API + Dashboard UI + Backup Scheduler
REM Single Ctrl+C stops all three

cd /d C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1
uv run python launch.py
pause
