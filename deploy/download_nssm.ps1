# AAM Backup Automation V1 — NSSM Downloader
# Downloads NSSM 2.24 (win64) to C:\BackupAgent\tools\nssm.exe
# Run: powershell -ExecutionPolicy Bypass -File deploy\download_nssm.ps1

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$NssmVersion  = "2.24"
$NssmUrl      = "https://nssm.cc/release/nssm-$NssmVersion.zip"
$ToolsDir     = "C:\BackupAgent\tools"
$ZipPath      = "$env:TEMP\nssm-$NssmVersion.zip"
$ExtractDir   = "$env:TEMP\nssm-extract-$NssmVersion"
$NssmDest     = "$ToolsDir\nssm.exe"

Write-Host ""
Write-Host "==================================================="
Write-Host "  AAM Backup — NSSM Downloader"
Write-Host "==================================================="
Write-Host ""

# ── Create tools directory ────────────────────────────────────
if (-not (Test-Path $ToolsDir)) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    Write-Host "[nssm] Created tools directory: $ToolsDir"
}

# ── Download ──────────────────────────────────────────────────
Write-Host "[nssm] Downloading NSSM $NssmVersion from $NssmUrl ..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $NssmUrl -OutFile $ZipPath -UseBasicParsing
Write-Host "[nssm] Download complete: $ZipPath"

# ── Extract ───────────────────────────────────────────────────
if (Test-Path $ExtractDir) { Remove-Item $ExtractDir -Recurse -Force }
Write-Host "[nssm] Extracting..."
Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force

# ── Copy win64 binary ─────────────────────────────────────────
$NssmSrc = Join-Path $ExtractDir "nssm-$NssmVersion\win64\nssm.exe"
if (-not (Test-Path $NssmSrc)) {
    Write-Error "[nssm] ERROR: Could not find win64 binary at $NssmSrc"
    exit 1
}
Copy-Item -Path $NssmSrc -Destination $NssmDest -Force
Write-Host "[nssm] Installed: $NssmDest"

# ── Verify ────────────────────────────────────────────────────
$ver = & $NssmDest version 2>&1 | Select-Object -First 1
Write-Host "[nssm] Verified: $ver"

# ── Cleanup ───────────────────────────────────────────────────
Remove-Item $ZipPath    -Force
Remove-Item $ExtractDir -Recurse -Force
Write-Host "[nssm] Cleaned up temp files"

Write-Host ""
Write-Host "Done. NSSM is ready at: $NssmDest"
Write-Host "Next step: Run deploy\install_services.bat as Administrator"
Write-Host ""
