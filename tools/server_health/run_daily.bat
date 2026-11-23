@echo off
setlocal
rem Daily AAM server health check.
rem Exit codes: 0 = PASS/WARN, 1 = FAIL (unless -Strict, where WARN also returns 1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server_health.ps1" %*
exit /b %ERRORLEVEL%
