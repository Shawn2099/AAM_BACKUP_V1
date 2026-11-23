@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=.
echo ===================================================
echo Running AAM Backup Automated Test Suites
echo ===================================================
.\.venv\Scripts\pytest.exe tests/test_config.py tests/test_lan_manifest.py tests/test_manifest.py tests/test_report.py tests/test_hashing.py tests/test_flow_status_semantics.py %*
pause
