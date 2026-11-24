<#
Start a lane on the trial box DETACHED (it survives the ssh session closing) and read its state later.
Copy to C:\AAMLivebox\box_run.ps1 and call it as a file (no inline quoting, no bash heredocs).

  start :  powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMLivebox\box_run.ps1 -Action start -Name a2026 `
             -RunArgs "--lane A --stamp a2026 --days 728 --start 2026-10-01 --allow-dev-path ..."
           Writes <Name>_run.ps1, launches it through WMI (Win32_Process.Create: outside the ssh job object, same user),
           returns at once. Files in C:\AAMLivebox: <Name>_out.txt, <Name>_err.txt, <Name>_exit.txt, <Name>.pid.
  status:  powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMLivebox\box_run.ps1 -Action status -Name a2026
           Prints running/finished, exit code, last output lines and the newest evidence\progress.json
           (pass -Lane <lane name> to pick the progress file of that lane; otherwise the newest one).

Untested on the box when written: first start a trivial run (e.g. -RunArgs "--help") and check status.
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet("start", "status")][string]$Action,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9_-]+$')][string]$Name,
    [string]$RunArgs = "",
    [string]$Lane = "",
    [string]$Root = "C:\AAMLivebox",
    [string]$Python = "C:\AAMBackup\.venv\Scripts\python.exe",
    [int]$Tail = 15
)
$ErrorActionPreference = "Stop"
$out = Join-Path $Root "${Name}_out.txt"
$err = Join-Path $Root "${Name}_err.txt"
$exitf = Join-Path $Root "${Name}_exit.txt"
$pidf = Join-Path $Root "${Name}.pid"
$runner = Join-Path $Root "${Name}_run.ps1"

if ($Action -eq "start") {
    if ((Test-Path $pidf) -and -not (Test-Path $exitf)) {
        $old = [int](Get-Content $pidf -TotalCount 1)
        if (Get-Process -Id $old -ErrorAction SilentlyContinue) { throw "run '$Name' is already running (pid $old)" }
    }
    Remove-Item $out, $err, $exitf, $pidf -ErrorAction SilentlyContinue
    $harness = Join-Path $Root "harness"
    # the run script: run the driver, keep stdout/stderr, record the exit code last
    $body = @(
        '$ErrorActionPreference = "Continue"',
        "Set-Location '$harness'",
        "`$p = Start-Process -FilePath '$Python' -ArgumentList '-X utf8 -m tools.livebox.run_lane $RunArgs' -NoNewWindow -Wait -PassThru -RedirectStandardOutput '$out' -RedirectStandardError '$err'",
        "`$p.ExitCode | Set-Content -Path '$exitf'"
    )
    Set-Content -Path $runner -Value $body -Encoding ASCII
    $cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`""
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; CurrentDirectory = $harness }
    if ($r.ReturnValue -ne 0) { throw "Win32_Process.Create failed, code $($r.ReturnValue)" }
    Set-Content -Path $pidf -Value $r.ProcessId
    "started '$Name' pid $($r.ProcessId); check with: -Action status -Name $Name"
    return
}

# status
$state = "not started (no pid file)"
$procId = $null
if (Test-Path $pidf) {
    $procId = [int](Get-Content $pidf -TotalCount 1)
    if (Test-Path $exitf) { $state = "finished" }
    elseif (Get-Process -Id $procId -ErrorAction SilentlyContinue) { $state = "running" }
    else { $state = "DIED (process gone, no exit file)" }
}
"state: $state   pid: $procId"
if (Test-Path $exitf) { "exit code: $((Get-Content $exitf -TotalCount 1))" }
foreach ($f in @($out, $err)) {
    if (Test-Path $f) { "--- last $Tail lines of $(Split-Path $f -Leaf) ---"; Get-Content $f -Tail $Tail }
}
$cand = Get-ChildItem -Path (Join-Path $Root "*\*\evidence\progress.json") -ErrorAction SilentlyContinue |
    Where-Object { (-not $Lane) -or ($_.Directory.Parent.Name -eq $Lane) } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($cand) { "--- progress.json ($($cand.FullName), updated $($cand.LastWriteTime)) ---"; Get-Content $cand.FullName -Raw }
else { "no progress.json yet" }
