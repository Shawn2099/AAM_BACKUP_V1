# Daily seed churn in `_seed_test` (trial box)

Keeps the backup source drive changing every day without anyone touching it: a few new files,
a few rewritten, a few deleted, so each nightly run has something to report and `/MIR` copies
are exercised instead of "no changes" every time.

## Files

| File | What it does |
| --- | --- |
| `tools/seed_test_files.py` | writes the files (formats, sizes, manifest) - manual use |
| `tools/seed_daily.py` | one day of churn: random add/replace/delete, capped and floored |
| `tools/install_seed_daily_task.ps1` | registers/checks/runs/removes the Windows scheduled task |
| `tests/test_seed_daily.py` | cap/floor rules, real churn, dry-run, log line |

Only `<source_drive>\_seed_test` is ever written. Real client files are never read, changed or
deleted; `--delete` only removes files listed in `_seed_test\seed_manifest.csv`.

## Installed on the trial box (2026-10-27)

Task **`AAM Seed Test Files`**, runs daily at **12:00** as **SYSTEM** (no password, no desktop
session - `Start-Process -Credential` fails on this box with `0xC0000142`):

```
C:\AAMBackup\.venv\Scripts\python.exe C:\AAMBackup\tools\seed_daily.py
  --dest E:\FY26-27 --add 5 --replace 3 --delete 2 --max-total 200 --log C:\AAMBackup\logs\seed_daily.log
```

Counts are maxima, picked at random each day (a quiet day can add 1 and delete 0). The folder
never exceeds 200 files and never drops below 10, so it stays small forever. One line per day
goes to `C:\AAMBackup\logs\seed_daily.log`:

```
2026-10-27 01:45:54 added 4, replaced 1, deleted 2 (1.0 MB), 107 seeded files in E:\FY26-27\_seed_test
```

## Commands (on the box, elevated)

```powershell
# what is registered, when it runs, how the last run went, last 5 log lines
powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMBackup\tools\install_seed_daily_task.ps1 -Status

# run it right now (useful before a manual backup test)
powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMBackup\tools\install_seed_daily_task.ps1 -Run

# change time or counts, e.g. 09:30, up to 3 new / 2 replaced / 1 deleted
powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMBackup\tools\install_seed_daily_task.ps1 -Time 09:30 -Add 3 -Replace 2 -Delete 1

# stop it (seeded files stay; remove E:\FY26-27\_seed_test to clean up)
powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMBackup\tools\install_seed_daily_task.ps1 -Unregister
```

Manual/one-off: `python tools/seed_daily.py --dest E:\FY26-27 --dry-run` prints the plan and
writes nothing.

## Deploying the change to another box

No scp on the trial box - pipe files in (base64 in a script, `python -` over ssh), same as every
other deploy; then run the installer once. The task keeps working across reboots (it is a
scheduled task, not a service, and needs no logon).
