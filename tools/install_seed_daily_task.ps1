<#
.SYNOPSIS
    Register (or check, run, remove) the daily seed-churn task on this box.

.DESCRIPTION
    Runs tools/seed_daily.py once a day as SYSTEM so the source drive always gains a few
    files, has a few replaced and a few removed before the next backup run. Writes one
    line per day to the log. Nothing outside <Dest>\_seed_test is touched.

    The task needs no password (SYSTEM) and no interactive session, which is why it is a
    scheduled task and not a start-up script (python from SSH/Start-Process -Credential
    fails on this box with 0xC0000142, see deploy/04_check_readiness.ps1).

.PARAMETER Dest
    Folder that holds (or will hold) _seed_test - normally paths.source_drive, E:\FY26-27.

.PARAMETER Time
    24h local time for the daily run. Keep it well before the 21:01 LAN run.

.EXAMPLE
    # register / update with the defaults
    powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMBackup\tools\install_seed_daily_task.ps1

.EXAMPLE
    # 09:30, quieter day: up to 3 new, 2 replaced, 1 deleted
    powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMBackup\tools\install_seed_daily_task.ps1 -Time 09:30 -Add 3 -Replace 2 -Delete 1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMBackup\tools\install_seed_daily_task.ps1 -Status
    powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMBackup\tools\install_seed_daily_task.ps1 -Run
    powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMBackup\tools\install_seed_daily_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Dest      = 'E:\FY26-27',
    [string]$Time      = '12:00',
    [string]$TaskName  = 'AAM Seed Test Files',
    [string]$Python    = 'C:\AAMBackup\.venv\Scripts\python.exe',
    [string]$Script    = 'C:\AAMBackup\tools\seed_daily.py',
    [string]$Log       = 'C:\AAMBackup\logs\seed_daily.log',
    [ValidateRange(0, 50)][int]$Add     = 5,
    [ValidateRange(0, 50)][int]$Replace = 3,
    [ValidateRange(0, 50)][int]$Delete  = 2,
    [ValidateRange(1, 5000)][int]$MaxTotal = 200,
    [switch]$Run,
    [switch]$Status,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

function Quote-Arg([string]$Value) {
    if ($Value -match '\s') { return '"' + $Value + '"' }
    return $Value
}

function Show-Task {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Task '$TaskName' is not registered." -ForegroundColor Yellow
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Task        : $TaskName ($($task.State))"
    Write-Host "Runs        : $($task.Actions[0].Execute) $($task.Actions[0].Arguments)"
    Write-Host "Next run    : $($info.NextRunTime)"
    Write-Host "Last run    : $($info.LastRunTime)  result 0x$('{0:X}' -f $info.LastTaskResult)"
    Write-Host "Missed runs : $($info.NumberOfMissedRuns)"
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($Status) {
    Show-Task
    if (Test-Path $Log) {
        Write-Host ''
        Write-Host "Last log lines ($Log):"
        Get-Content -Path $Log -Tail 5
    }
    return
}

if (-not $isAdmin) { throw 'Run this from an elevated PowerShell (Register-ScheduledTask as SYSTEM needs admin).' }

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed task '$TaskName'. Seeded files under $Dest\_seed_test were kept." -ForegroundColor Green
    } else {
        Write-Host "Task '$TaskName' is not registered." -ForegroundColor Yellow
    }
    return
}

if ($Run) {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) { throw "Task '$TaskName' is not registered - run this script once without -Run first." }
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8
    Show-Task
    if (Test-Path $Log) { Get-Content -Path $Log -Tail 3 }
    return
}

# --- register / update -------------------------------------------------------------------
if (-not (Test-Path $Python)) { throw "python not found: $Python" }
if (-not (Test-Path $Script)) { throw "seed script not found: $Script (deploy tools/seed_daily.py first)" }
if (-not (Test-Path $Dest))   { throw "destination folder not found: $Dest" }
if ($Time -notmatch '^\d{1,2}:\d{2}$') { throw "Time must look like 12:00, got '$Time'" }

$argLine = @(
    (Quote-Arg $Script)
    '--dest', (Quote-Arg $Dest)
    '--add', $Add
    '--replace', $Replace
    '--delete', $Delete
    '--max-total', $MaxTotal
    '--log', (Quote-Arg $Log)
) -join ' '

$action = New-ScheduledTaskAction -Execute $Python -Argument $argLine -WorkingDirectory (Split-Path -Parent $Python)
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Force `
    -Description 'Daily add/replace/delete of a few files in the _seed_test folder so each backup run has changes to report.' | Out-Null

Write-Host "Registered '$TaskName' - daily at $Time as SYSTEM." -ForegroundColor Green
Write-Host "  $Python $argLine"
Show-Task
