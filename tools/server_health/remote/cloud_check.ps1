param(
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [Parameter(Mandatory = $true)][string]$Bucket,
    [Parameter(Mandatory = $true)][string]$Prefix,
    [Parameter(Mandatory = $true)][string]$KeyPath,
    [Parameter(Mandatory = $true)][string]$ProjectNumber,
    [Parameter(Mandatory = $true)][string]$Location,
    [Parameter(Mandatory = $true)][string]$StorageClass,
    [Parameter(Mandatory = $true)][string]$RcloneExe,
    [string]$CanaryName = ".AAM_TARGET_MOUNTED",
    [int]$Checkers = 8
)

# Read-only cloud verification. Emits one line prefixed with ##CLOUD_JSON##.
$ErrorActionPreference = "Continue"
$cfgPath = Join-Path $env:TEMP ("aam_gcs_" + [guid]::NewGuid().ToString("N") + ".conf")
$diffPath = Join-Path $env:TEMP ("aam_gcs_diff_" + [guid]::NewGuid().ToString("N") + ".txt")
$remote = "aam_gcs:$Bucket/$Prefix"
$key = $KeyPath.Replace("\", "/")

$conf = @"
[aam_gcs]
type = google cloud storage
service_account_file = $key
project_number = $ProjectNumber
object_acl =
bucket_acl =
bucket_policy_only = true
location = $Location
storage_class = $StorageClass
"@
Set-Content -Path $cfgPath -Value $conf -Encoding ASCII

try {
    $remoteCount = $null
    $remoteBytes = $null
    $sizeRaw = & $RcloneExe --config $cfgPath size $remote --exclude $CanaryName --json 2>$null
    if ($LASTEXITCODE -eq 0) {
        try {
            $sizeObj = ($sizeRaw | Out-String) | ConvertFrom-Json
            $remoteCount = [int64]$sizeObj.count
            $remoteBytes = [int64]$sizeObj.bytes
        } catch { }
    }

    $checkOut = & $RcloneExe --config $cfgPath check $SourceDir $remote `
        --one-way --exclude $CanaryName --checkers $Checkers --retries 3 `
        --combined $diffPath 2>&1
    $checkExit = $LASTEXITCODE

    $equal = 0; $added = 0; $removed = 0; $modified = 0; $errors = 0
    if (Test-Path $diffPath) {
        foreach ($line in Get-Content $diffPath) {
            if (-not $line) { continue }
            switch ($line.Substring(0, 1)) {
                "=" { $equal++ }
                "+" { $added++ }
                "-" { $removed++ }
                "*" { $modified++ }
                "!" { $errors++ }
            }
        }
    }

    $verdict = if ($checkExit -eq 0 -and $added -eq 0 -and $removed -eq 0 -and $modified -eq 0 -and $errors -eq 0) {
        "MATCH"
    } elseif ($checkExit -eq 1) {
        "DIFFERENCES"
    } else {
        "ERROR"
    }

    $tail = ($checkOut | Select-Object -Last 6) -join " | "
    $result = [pscustomobject]@{
        verdict        = $verdict
        check_exit     = $checkExit
        remote_count   = $remoteCount
        remote_bytes   = $remoteBytes
        equal          = $equal
        added          = $added
        removed        = $removed
        modified       = $modified
        errors         = $errors
        remote         = $remote
        rclone_tail    = $tail
    }
    "##CLOUD_JSON##" + ($result | ConvertTo-Json -Compress -Depth 5)
    if ($verdict -eq "MATCH") { exit 0 } elseif ($verdict -eq "DIFFERENCES") { exit 1 } else { exit 2 }
}
finally {
    Remove-Item $cfgPath, $diffPath -Force -ErrorAction SilentlyContinue
}
