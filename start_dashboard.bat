@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=.
echo Starting AAM Backup Dashboard UI on http://127.0.0.1:8080 ...
echo Press Ctrl+C to stop.
.\.venv\Scripts\python.exe -m uvicorn ui:app --host 127.0.0.1 --port 8080 --log-level info
pause
