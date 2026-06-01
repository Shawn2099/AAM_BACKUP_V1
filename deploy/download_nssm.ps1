# AAM Backup Automation V1 — NSSM Downloader (PowerShell Wrapper)
# Runs the robust Python engine to download and configure NSSM 2.24
# Run: powershell -ExecutionPolicy Bypass -File deploy\download_nssm.ps1

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

# Execute Python engine directly
uv run python deploy/download_nssm.py
