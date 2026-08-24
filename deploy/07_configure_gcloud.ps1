# P3-GCP: configure CLOUDSDK_PYTHON for the bundled Google Cloud SDK.
#
# Evidence (REP/FY-10 ledger rows): the bundled deploy/bin/google-cloud-sdk
# gcloud.cmd needs a Python interpreter; without CLOUDSDK_PYTHON every ARCHIVE
# transition silently no-ops. Machine-scope env vars are only inherited by
# services AFTER they restart, so this script ends with an optional service
# restart step (-RestartServices).
#
# Idempotent: re-running detects the correct value and skips writes.
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy\07_configure_gcloud.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\07_configure_gcloud.ps1 -RestartServices

param(
    [switch]$RestartServices
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython  = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Error "venv python not found at $venvPython - adjust the path for this deployment"
}

$current = [Environment]::GetEnvironmentVariable("CLOUDSDK_PYTHON", "Machine")
if ($current -eq $venvPython) {
    Write-Host "[gcloud] CLOUDSDK_PYTHON already correct: $venvPython"
} else {
    [Environment]::SetEnvironmentVariable("CLOUDSDK_PYTHON", $venvPython, "Machine")
    Write-Host "[gcloud] CLOUDSDK_PYTHON set (Machine): $venvPython (was: '$current')"
}

# Also set Process scope so the current session can use gcloud immediately.
$env:CLOUDSDK_PYTHON = $venvPython

if ($RestartServices) {
    # Machine-scope variables are only inherited AFTER a service restart.
    # NSSM-managed services: stop is picked up as failure -> auto-restart,
    # but we start them explicitly to avoid depending on recovery timing.
    foreach ($svc in @("AamPrefectServer", "AamBackupAgent", "AamWatchdog")) {
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        if ($s) {
            Write-Host "[gcloud] restarting $svc ..."
            Stop-Service -Name $svc -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 3
            Start-Service -Name $svc
            Write-Host "[gcloud] $svc restarted"
        } else {
            Write-Host "[gcloud] service $svc not installed - skipping"
        }
    }
} else {
    Write-Host "[gcloud] NOTE: restart AamBackupAgent/AamWatchdog/AamPrefectServer (or pass -RestartServices) so services inherit the variable."
}

Write-Host "[gcloud] done."
