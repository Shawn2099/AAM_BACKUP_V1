# AAM server health check

Read-only daily check for the test server `innoviztawinserver2016` and both
backup destinations. It answers three questions in one run:

1. **Is the server healthy?** Services, disk, Prefect API, and both SQLite
   databases (integrity, foreign keys, run freshness, notification queue,
   watchdog heartbeat).
2. **Are the files really on the LAN?** Full MD5 comparison of the live source
   tree (`E:\FY26-27` on the server) against `C:\AAM_NAS\FY26-27` on this PC.
3. **Are the files really in Google Cloud?** `rclone check` (MD5, one-way)
   against `gs://aam-backup-demo-innovizta/FY26-27`, plus object count/bytes.

Everything is opened read-only. The only files written are under
`reports/server_health/<timestamp>/`.

## Quick start

```powershell
# full check (DB + LAN + cloud)
powershell -ExecutionPolicy Bypass -File tools\server_health\server_health.ps1

# DB/system only, skip file verification
powershell -ExecutionPolicy Bypass -File tools\server_health\server_health.ps1 -SkipFiles

# LAN only, skip cloud
powershell -ExecutionPolicy Bypass -File tools\server_health\server_health.ps1 -SkipCloud

# treat warnings as failure (for monitoring)
powershell -ExecutionPolicy Bypass -File tools\server_health\server_health.ps1 -Strict
```

Or simply run `tools\server_health\run_daily.bat`.

Exit codes: `0` = PASS or WARN, `1` = FAIL (or WARN with `-Strict`).

## What each result means

| Result | Meaning |
| --- | --- |
| `PASS` | Check passed |
| `WARN` | Needs attention but backups are not broken (stale history, extra destination objects, old failed runs, stale manifest row) |
| `FAIL` | Backup integrity or server health problem |

The file checks are content checks, not filename checks:

- LAN: every source file must exist at the same relative path with the same
  **size and MD5**.
- Cloud: `rclone check --one-way` must report 0 differences and the object
  count/bytes (canary excluded) must equal the source count/bytes.
- The mount canary `.AAM_TARGET_MOUNTED` is excluded on both legs by design.

## Requirements

On the machine that runs this script:

- OpenSSH client and the `innoviztawinserver2016` alias in `~/.ssh/config`
  (key-based login; no password prompt).
- Python 3 on `PATH` (stdlib only).
- Access to the LAN copy at `C:\AAM_NAS\FY26-27` (this PC is the NAS,
  `10.10.151.53`). Run the script as a user that can read that folder.
- `reports/server_health/` is git-ignored; reports stay local.

On the test server:

- `C:\AAMBackup\.venv\Scripts\python.exe`
- `C:\AAMBackup\deploy\bin\rclone.exe`
- `C:\AAMBackup\deploy\keys\aam-gcs-key.json` (plain service-account key)
- The scheduled services `AamBackupAgent`, `AamPrefectServer`, `AamWatchdog`

## Layout

```
tools/server_health/
  server_health.ps1          main entry point (runs all checks, writes report)
  check_config.psd1          host, paths, bucket, thresholds - edit here
  run_daily.bat              wrapper for Task Scheduler
  remote/db_health.py        manifest + Prefect DB health (runs on server)
  remote/hash_tree.py        recursive MD5 manifest (server or local)
  remote/entries_check.py    manifest.file_entries vs live source (server)
  remote/cloud_check.ps1     rclone size + rclone check against GCS (server)
  local/compare_trees.py     source TSV vs LAN TSV comparison (local)
```

## Reports

Each run creates:

```
reports/server_health/YYYY-MM-DD_HHMMSS/
  report.txt            human-readable PASS/WARN/FAIL lines
  summary.json          machine-readable summary + all check details
  db_health.json        full manifest/Prefect DB state
  entries_check.json    manifest rows vs source tree
  source_hashes.tsv     MD5 of every source file
  lan_hashes.tsv        MD5 of every LAN file
  lan_compare.json      missing/extra/size/hash differences
  cloud_check.json      rclone verdict and counts
  *.err.txt             stderr from the helpers (for troubleshooting)
```

Reports older than `-KeepReports` days (default 30) are pruned automatically.

## Daily schedule (Windows Task Scheduler)

Run once, from an elevated prompt, adjusting the path and time:

```bat
schtasks /Create /TN "AAM Server Health" /SC DAILY /ST 07:30 ^
  /TR "\"C:\Users\malliath\Desktop\aam_backup\tools\server_health\run_daily.bat\"" ^
  /RU "%USERNAME%" /F
```

The task must run as a user that can read `C:\AAM_NAS` and use the SSH key.
If you prefer the GUI: *Create Task > Daily 07:30 > Start a program >
`run_daily.bat`*. Tick "Run whether user is logged on or not" only if the
account has the SSH key and NAS access in a non-interactive session.

To view the latest result by hand:

```powershell
Get-Content (Get-ChildItem reports\server_health -Directory |
  Sort-Object Name -Descending | Select-Object -First 1).FullName\report.txt
```

## Tuning

Edit `check_config.psd1`:

- `SshHost` - SSH alias.
- `SourceDir`, `LanDestination` - backup legs.
- `GcsBucket`, `GcsPrefix`, `GcsKeyPath`, `GcsProjectNumber`,
  `GcsLocation`, `GcsStorageClass` - cloud target.
- `MaxRunAgeHours` / `MaxRunAgeHoursFail` - freshness thresholds (default
  30 h WARN / 48 h FAIL; LAN runs 21:01, cloud 02:01 IST).
- `MaxWatchdogAgeMinutes` - heartbeat threshold (default 15 min).

## Notes

- The script never repairs anything. A `FAIL` on `Files source -> LAN` or
  `Files source -> GCS` means the next scheduled run should be investigated;
  the companion `.tsv`/`.json` artifacts list the exact paths.
- A stale `file_entries` row (`Manifest stale rows` WARN) is bookkeeping only:
  the row refers to a file deleted from the source. It does not affect the
  actual copy. The check lists the paths.
- Cloud `rclone check` uses GCS `md5Hash` metadata, which is computed by GCS
  on upload. It is a content check, not just a size check.
