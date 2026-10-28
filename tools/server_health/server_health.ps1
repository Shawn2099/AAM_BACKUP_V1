[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$ReportRoot,
    [int]$KeepReports = 30,
    [switch]$SkipFiles,
    [switch]$SkipCloud,
    [switch]$Strict
)

$scriptDir = Split-Path -Parent $PSCommandPath
if (-not $ConfigPath) { $ConfigPath = Join-Path $scriptDir 'check_config.psd1' }
if (-not $ReportRoot) { $ReportRoot = Join-Path $scriptDir '..\..\reports\server_health' }
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$ReportRoot = [IO.Path]::GetFullPath($ReportRoot)

# AAM server health check - read-only.
# Verifies: services, disk, Prefect API, manifest DB, Prefect DB, run
# freshness, notification queue, and (optionally) byte-level copy integrity
# from the source tree to the LAN and GCS destinations.

$ErrorActionPreference = 'Stop'
$script:Results = New-Object System.Collections.Generic.List[object]
$script:Config = Import-PowerShellDataFile -Path $ConfigPath

function Add-Result {
    param([string]$Name, [string]$Status, [string]$Detail)
    if (-not $Status) { $Status = 'WARN' }
    $Status = $Status.ToUpperInvariant()
    $script:Results.Add([pscustomobject]@{ Name = $Name; Status = $Status; Detail = $Detail })
}

function Write-ReportLine {
    param([string]$Text)
    Add-Content -Path $script:ReportFile -Value $Text -Encoding UTF8
}

function Invoke-RemotePowerShell {
    param([string]$ScriptText, [string]$OutFile, [string]$ErrFile)
    $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($ScriptText))
    $ErrorActionPreference = 'Continue'
    & ssh -o ConnectTimeout=20 -o BatchMode=yes $script:Config.SshHost "powershell -NoProfile -EncodedCommand $b64" 1> $OutFile 2> $ErrFile
    return $LASTEXITCODE
}

function Invoke-RemotePython {
    param([string]$ScriptPath, [string]$Arguments, [string]$OutFile, [string]$ErrFile)
    $ErrorActionPreference = 'Continue'
    Get-Content -Raw -Path $ScriptPath |
        & ssh -o ConnectTimeout=20 -o BatchMode=yes $script:Config.SshHost "$($script:Config.RemotePython) - $Arguments" 1> $OutFile 2> $ErrFile
    return $LASTEXITCODE
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    try { return (Get-Content -Raw -Path $Path | ConvertFrom-Json) } catch { return $null }
}

function Find-MarkedJson {
    param([string]$Path, [string]$Marker)
    if (-not (Test-Path $Path)) { return $null }
    $line = Get-Content -Path $Path |
        Where-Object { $_ -like "$Marker*" } |
        Select-Object -Last 1
    if (-not $line) { return $null }
    try { return ($line.Substring($Marker.Length) | ConvertFrom-Json) } catch { return $null }
}

# ---------------------------------------------------------------- setup
$ReportRoot = [IO.Path]::GetFullPath($ReportRoot)
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$script:ReportDir = Join-Path $ReportRoot $stamp
New-Item -ItemType Directory -Force -Path $script:ReportDir | Out-Null
$script:ReportFile = Join-Path $script:ReportDir 'report.txt'
$timer = [Diagnostics.Stopwatch]::StartNew()
$cfg = $script:Config

# Prune old reports
if ($KeepReports -gt 0 -and (Test-Path $ReportRoot)) {
    $cutoff = (Get-Date).AddDays(-$KeepReports)
    Get-ChildItem -Path $ReportRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}_\d{6}$' -and $_.LastWriteTime -lt $cutoff } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

Write-ReportLine "AAM backup server health check"
Write-ReportLine "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')"
Write-ReportLine "Host:      $($cfg.SshHost)"
Write-ReportLine ""

# ---------------------------------------------------------------- 1. SSH
$sysOut = Join-Path $script:ReportDir 'sysinfo.txt'
$sysErr = Join-Path $script:ReportDir 'sysinfo.err.txt'
$sshScript = @'
$ErrorActionPreference = "Continue"
$services = @(Get-Service AamBackupAgent, AamPrefectServer, AamWatchdog -ErrorAction SilentlyContinue |
    ForEach-Object { [pscustomobject]@{ name = $_.Name; status = [string]$_.Status; start_type = [string]$_.StartType } })
$disk = Get-PSDrive C
$health = $null
try { $health = (Invoke-WebRequest "http://127.0.0.1:4200/api/health" -UseBasicParsing -TimeoutSec 10).Content } catch { }
$result = [pscustomobject]@{
    hostname         = $env:COMPUTERNAME
    user             = $env:USERNAME
    server_time      = (Get-Date).ToString("o")
    disk_free_gb     = [math]::Round($disk.Free / 1GB, 1)
    disk_used_gb     = [math]::Round($disk.Used / 1GB, 1)
    prefect_health   = "$health"
    services         = $services
}
"##SYS_JSON##" + ($result | ConvertTo-Json -Compress -Depth 6)
'@

$sshExit = Invoke-RemotePowerShell -ScriptText $sshScript -OutFile $sysOut -ErrFile $sysErr
$sys = Find-MarkedJson -Path $sysOut -Marker '##SYS_JSON##'

if ($sshExit -ne 0 -or -not $sys) {
    Add-Result 'SSH connectivity' 'FAIL' "cannot reach $($cfg.SshHost) (exit $sshExit)"
    Write-ReportLine "[FAIL] SSH connectivity - see sysinfo.err.txt"
    $timer.Stop()
    $failed = $true
} else {
    Add-Result 'SSH connectivity' 'PASS' "$($sys.hostname) as $($sys.user), server time $($sys.server_time)"
    Write-ReportLine "[PASS] SSH connectivity - $($sys.hostname) as $($sys.user)"

    $required = @('AamBackupAgent', 'AamPrefectServer', 'AamWatchdog')
    $bad = @($sys.services | Where-Object { $_.name -in $required -and $_.status -ne 'Running' })
    $missing = @($required | Where-Object { $n = $_; -not ($sys.services | Where-Object { $_.name -eq $n }) })
    if ($bad.Count -or $missing.Count) {
        Add-Result 'Windows services' 'FAIL' ("not running: " + (($bad | ForEach-Object { "$($_.name)=$($_.status)" }) -join ', ') + $(if ($missing.Count) { " missing: $($missing -join ', ')" } else { '' }))
    } else {
        Add-Result 'Windows services' 'PASS' 'AamBackupAgent, AamPrefectServer, AamWatchdog all Running'
    }

    if ([double]$sys.disk_free_gb -lt 10) {
        Add-Result 'Free disk space' 'FAIL' "$($sys.disk_free_gb) GB free on C:"
    } elseif ([double]$sys.disk_free_gb -lt 25) {
        Add-Result 'Free disk space' 'WARN' "$($sys.disk_free_gb) GB free on C:"
    } else {
        Add-Result 'Free disk space' 'PASS' "$($sys.disk_free_gb) GB free on C:"
    }

    if ("$($sys.prefect_health)".Trim().ToLower() -eq 'true') {
        Add-Result 'Prefect API health' 'PASS' 'http://127.0.0.1:4200/api/health -> true'
    } else {
        Add-Result 'Prefect API health' 'FAIL' "unexpected response: '$($sys.prefect_health)'"
    }
    $failed = $false
}

# ---------------------------------------------------------------- 2. DBs
$dbJsonPath = Join-Path $script:ReportDir 'db_health.json'
$dbErrPath = Join-Path $script:ReportDir 'db_health.err.txt'
$db = $null
if (-not $failed) {
    $dbArgs = "$($cfg.ManifestDb) $($cfg.PrefectDb) $($cfg.MaxRunAgeHours) $($cfg.MaxRunAgeHoursFail) $($cfg.MaxWatchdogAgeMinutes)"
    $dbExit = Invoke-RemotePython -ScriptPath (Join-Path $scriptDir 'remote\db_health.py') `
        -Arguments $dbArgs -OutFile $dbJsonPath -ErrFile $dbErrPath
    $db = Read-JsonFile -Path $dbJsonPath
    if ($dbExit -ne 0 -or -not $db) {
        Add-Result 'DB health script' 'FAIL' "db_health.py failed (exit $dbExit)"
    } else {
        foreach ($check in $db.checks) {
            Add-Result $check.name $check.status $check.detail
        }
    }
}

# ---------------------------------------------------------------- 3. Files
$srcTsv = Join-Path $script:ReportDir 'source_hashes.tsv'
$lanTsv = Join-Path $script:ReportDir 'lan_hashes.tsv'
$srcErrPath = Join-Path $script:ReportDir 'source_hash.err.txt'
$lanErrPath = Join-Path $script:ReportDir 'lan_hash.err.txt'
$compareJsonPath = Join-Path $script:ReportDir 'lan_compare.json'
$cloudJsonPath = Join-Path $script:ReportDir 'cloud_check.json'
$entriesJsonPath = Join-Path $script:ReportDir 'entries_check.json'

$srcSummary = $null
$lanSummary = $null
$filesOk = $false

if (-not $failed -and -not $SkipFiles) {
    # 3a. source tree on the server
    $srcExit = Invoke-RemotePython -ScriptPath (Join-Path $scriptDir 'remote\hash_tree.py') `
        -Arguments "`"$($cfg.SourceDir)`"" -OutFile $srcTsv -ErrFile $srcErrPath
    $srcSummary = Find-MarkedJson -Path $srcTsv -Marker '##SUMMARY##'
    if ($srcExit -ne 0 -or -not $srcSummary -or @($srcSummary.errors).Count -gt 0) {
        Add-Result 'Source tree readable' 'FAIL' "hash_tree.py failed on $($cfg.SourceDir) (exit $srcExit)"
    } else {
        Add-Result 'Source tree readable' 'PASS' "$($srcSummary.count) files, $($srcSummary.bytes) bytes"
        $filesOk = $true
    }

    # 3b. LAN destination on this machine
    if ($filesOk) {
        if (-not (Test-Path $cfg.LanDestination)) {
            Add-Result 'LAN destination reachable' 'FAIL' "$($cfg.LanDestination) not found on this machine"
        } else {
            $ErrorActionPreference = 'Continue'
            & $cfg.LocalPython (Join-Path $scriptDir 'remote\hash_tree.py') $cfg.LanDestination 1> $lanTsv 2> $lanErrPath
            $lanExit = $LASTEXITCODE
            $ErrorActionPreference = 'Stop'
            $lanSummary = Find-MarkedJson -Path $lanTsv -Marker '##SUMMARY##'
            if ($lanExit -ne 0 -or -not $lanSummary -or @($lanSummary.errors).Count -gt 0) {
                Add-Result 'LAN destination readable' 'FAIL' "hash_tree.py failed on $($cfg.LanDestination) (exit $lanExit)"
                $filesOk = $false
            } else {
                Add-Result 'LAN destination readable' 'PASS' "$($lanSummary.count) files, $($lanSummary.bytes) bytes"
            }
        }
    }

    # 3c. compare source vs LAN
    if ($filesOk) {
        $ErrorActionPreference = 'Continue'
        & $cfg.LocalPython (Join-Path $scriptDir 'local\compare_trees.py') `
            --source $srcTsv --dest $lanTsv --source-label $cfg.SourceDir `
            --dest-label $cfg.LanDestination --json $compareJsonPath 1> (Join-Path $script:ReportDir 'lan_compare.txt') 2>&1
        $ErrorActionPreference = 'Stop'
        $cmp = Read-JsonFile -Path $compareJsonPath
        if (-not $cmp) {
            Add-Result 'Files source -> LAN' 'FAIL' 'comparison did not produce a result'
        } elseif ($cmp.status -eq 'PASS') {
            Add-Result 'Files source -> LAN' 'PASS' "$($cmp.source_files) files, $($cmp.source_bytes) bytes, identical MD5"
        } else {
            Add-Result 'Files source -> LAN' 'FAIL' ("missing={0} extra={1} size_diff={2} hash_diff={3}" -f `
                $cmp.missing_at_destination, $cmp.extra_at_destination, $cmp.size_mismatch, $cmp.hash_mismatch)
        }
    }

    # 3d. manifest rows vs live source
    if (-not $failed) {
        $entriesArgs = "$($cfg.ManifestDb) `"$($cfg.SourceDir)`" $($cfg.CanaryName)"
        $entriesExit = Invoke-RemotePython -ScriptPath (Join-Path $scriptDir 'remote\entries_check.py') `
            -Arguments $entriesArgs -OutFile $entriesJsonPath -ErrFile (Join-Path $script:ReportDir 'entries_check.err.txt')
        $entries = Read-JsonFile -Path $entriesJsonPath
        if ($entriesExit -ne 0 -or -not $entries) {
            Add-Result 'Manifest vs source' 'FAIL' "entries_check.py failed (exit $entriesExit)"
        } else {
            foreach ($check in $entries.checks) {
                Add-Result $check.name $check.status $check.detail
            }
        }
    }
}

# ---------------------------------------------------------------- 4. Cloud
$cloud = $null
if (-not $failed -and -not $SkipFiles -and -not $SkipCloud) {
    $body = Get-Content -Raw -Path (Join-Path $scriptDir 'remote\cloud_check.ps1')
    $call = "& {`n$body`n} -SourceDir '$($cfg.SourceDir)' -Bucket '$($cfg.GcsBucket)' " +
        "-Prefix '$($cfg.GcsPrefix)' -KeyPath '$($cfg.GcsKeyPath)' " +
        "-ProjectNumber '$($cfg.GcsProjectNumber)' -Location '$($cfg.GcsLocation)' " +
        "-StorageClass '$($cfg.GcsStorageClass)' -RcloneExe '$($cfg.RcloneExe)' " +
        "-CanaryName '$($cfg.CanaryName)' -Checkers 8"
    $cloudOut = Join-Path $script:ReportDir 'cloud_check.raw.txt'
    $cloudErr = Join-Path $script:ReportDir 'cloud_check.err.txt'
    $cloudExit = Invoke-RemotePowerShell -ScriptText $call -OutFile $cloudOut -ErrFile $cloudErr
    $cloud = Find-MarkedJson -Path $cloudOut -Marker '##CLOUD_JSON##'
    if (-not $cloud) {
        Add-Result 'Files source -> GCS' 'FAIL' "rclone check produced no result (exit $cloudExit)"
    } else {
        $cloud | ConvertTo-Json -Depth 6 | Set-Content -Path $cloudJsonPath -Encoding UTF8
        if ($cloud.verdict -eq 'MATCH') {
            $detail = "$($cloud.equal) files matched by MD5, 0 differences"
            if ($null -ne $cloud.remote_count -and $null -ne $srcSummary) {
                $detail += " (GCS: $($cloud.remote_count) objects / $($cloud.remote_bytes) bytes)"
                if ([int64]$cloud.remote_count -ne [int64]$srcSummary.count -or [int64]$cloud.remote_bytes -ne [int64]$srcSummary.bytes) {
                    $detail += " COUNT/BYTE MISMATCH vs source"
                    Add-Result 'Files source -> GCS' 'WARN' $detail
                    $cloud = $null
                }
            }
            if ($cloud) { Add-Result 'Files source -> GCS' 'PASS' $detail }
        } elseif ($cloud.verdict -eq 'DIFFERENCES') {
            Add-Result 'Files source -> GCS' 'FAIL' ("missing={0} modified={1} errors={2} (rclone exit {3})" -f `
                $cloud.added, $cloud.modified, $cloud.errors, $cloud.check_exit)
        } else {
            Add-Result 'Files source -> GCS' 'FAIL' "rclone check error (exit $($cloud.check_exit)): $($cloud.rclone_tail)"
        }
    }
}

# ---------------------------------------------------------------- report
$timer.Stop()
$passCount = @($script:Results | Where-Object { $_.Status -eq 'PASS' }).Count
$warnCount = @($script:Results | Where-Object { $_.Status -eq 'WARN' }).Count
$failCount = @($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count
$overall = if ($failCount -gt 0) { 'FAIL' } elseif ($warnCount -gt 0) { 'WARN' } else { 'PASS' }

Write-ReportLine ""
Write-ReportLine ("Overall: {0}  ({1} pass, {2} warn, {3} fail)  in {4:N1}s" -f $overall, $passCount, $warnCount, $failCount, $timer.Elapsed.TotalSeconds)
Write-ReportLine ""
Write-ReportLine "Checks"
Write-ReportLine "------"
foreach ($r in $script:Results) {
    $line = "[{0}] {1} - {2}" -f $r.Status, $r.Name, $r.Detail
    Write-ReportLine $line
}
Write-ReportLine ""
Write-ReportLine "Artifacts in $script:ReportDir"

$summary = [pscustomobject]@{
    generated_at   = (Get-Date).ToString('o')
    host           = $cfg.SshHost
    overall        = $overall
    pass_count     = $passCount
    warn_count     = $warnCount
    fail_count     = $failCount
    duration_s     = [math]::Round($timer.Elapsed.TotalSeconds, 1)
    report_dir     = $script:ReportDir
    results        = $script:Results
    source_summary = $srcSummary
    lan_summary    = $lanSummary
    cloud          = $cloud
    db             = $db
}
$summary | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $script:ReportDir 'summary.json') -Encoding UTF8

# console
Write-Host ""
Write-Host ("AAM server health: {0}  ({1} pass / {2} warn / {3} fail)" -f $overall, $passCount, $warnCount, $failCount) `
    -ForegroundColor $(if ($overall -eq 'FAIL') { 'Red' } elseif ($overall -eq 'WARN') { 'Yellow' } else { 'Green' })
foreach ($r in $script:Results) {
    $color = switch ($r.Status) { 'PASS' { 'Green' } 'WARN' { 'Yellow' } default { 'Red' } }
    Write-Host ("  [{0}] {1} - {2}" -f $r.Status, $r.Name, $r.Detail) -ForegroundColor $color
}
Write-Host "  report: $script:ReportDir" -ForegroundColor DarkGray

if ($failCount -gt 0) { exit 1 }
if ($Strict -and $warnCount -gt 0) { exit 1 }
exit 0
