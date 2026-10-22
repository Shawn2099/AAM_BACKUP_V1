<#
.SYNOPSIS
  Run the audit lab (real robocopy / rclone against a sandboxed rig) and write a short report.
.DESCRIPTION
  About 60 minutes. Uses a CLEAN Prefect home (the PC's own ~/.prefect database was migrated by another
  Prefect version and makes the lab hang). Does not touch the server or any real backup data.
  Do not edit files in this folder while it runs (the lab imports the working tree).
  Results: agent_runs\audit_lab_<timestamp>\  ->  REPORT.md (read this), lab.log, junit.xml
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\run_audit_lab.ps1
#>
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Stamp = Get-Date -Format "yyyyMMdd_HHmm"
$Out = Join-Path $Repo "agent_runs\audit_lab_$Stamp"
New-Item -ItemType Directory -Force $Out | Out-Null
$PrefectHome = Join-Path $env:TEMP "aam_lab_prefect_$Stamp"
New-Item -ItemType Directory -Force $PrefectHome | Out-Null
$env:PREFECT_HOME = $PrefectHome
$Py = Join-Path $Repo ".venv\Scripts\python.exe"
Push-Location $Repo
try {
    Write-Host "Audit lab started $(Get-Date -Format 'HH:mm'). Results: $Out"
    # PowerShell 5.1 turns any stderr line of a native program into a terminating error under "Stop"
    # (the lab's logger writes to stderr), so run it with "Continue" and judge by the exit code.
    $ErrorActionPreference = "Continue"
    & $Py -m pytest audit_lab --ignore=audit_lab/test_w7_deployed.py -q -s --timeout=1800 `
        -p no:cacheprovider -o addopts="" --junitxml (Join-Path $Out "junit.xml") *> (Join-Path $Out "lab.log")
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    & $Py (Join-Path $PSScriptRoot "lab_report.py") (Join-Path $Out "junit.xml") (Join-Path $Out "REPORT.md") $code
    Write-Host (Get-Content (Join-Path $Out "REPORT.md") -Raw)
}
finally {
    Pop-Location
    Remove-Item -Recurse -Force $PrefectHome -ErrorAction SilentlyContinue
}
