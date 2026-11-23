# Deploy log - trial box, 2026-10-19 (fresh install of the MVP-hardening release)

Written while the owner was away. No password, key or secret is in this file (the repository is public).
Owner instruction (2026-10-19): back up logs, database, old config and key to a safe place on the box;
wipe; install from scratch; verify and fix; use the owner's chosen password for the dashboard AND for
Advanced; document everything; push to git when finished.

## 1. Before anything was touched

* Release gate: unit suite 2354 passed / 19 skipped; full audit lab 242 passed / 3 skipped / 1 failed;
  the one failure (`SecretText` could not be deep-copied by `model_copy(deep=True)`) was a real bug found by
  the lab, fixed (e6e80f9) and re-run green. Independent code review (read-only agent) ran in parallel,
  see section 5.
* Box state: `AamPrefectServer`, `AamBackupAgent` (runs as `.\aambackup`), `AamWatchdog` running, no backup
  tool running; code was 3 commits behind (6adcfb2); 1347 GB free on `C:` (`D:` has 0 GB free).

## 2. Backup (on the box, `C:\AAM_backup_20261019`, 15 MB, verified)

| What | Where in the backup | Check |
|---|---|---|
| Database (consistent online copy, agent was running) | `runtime\manifest.db` | SQLite `.backup`, 9 tables, 184 run-history rows, integrity `ok` |
| Run logs + service logs + `logs.zip` + the `pre-v110/v111` folders | `runtime\logs`, `runtime\run_logs`, ... | 325 files, 13,895,553 bytes - identical to the source |
| Old config + its 2026-10-15 backup copy | `config\` | SHA-256 recorded |
| Google service-account key (from `paths.gcs_key_path`) and `deploy\keys` | `key\` | both copies have the same hash |

The copy is on the same disk as the data. For protection against a disk failure copy the folder to another
machine (it contains the Google key and the old config with the mail password: keep it private).

## 3. Wipe and fresh install

1. Services stopped (watchdog first); no `robocopy`/`rclone` running.
2. Old code moved to `C:\AAMBackup_old_20261019`, `C:\BackupAgent` emptied.
3. `git clone --branch mvp-hardening`, checked out the release commit; key restored to
   `deploy\keys`; the old config carried over **minus** `dashboard.api_key` and `dashboard.auth_enabled`.
4. `04_check_readiness.ps1` (creates `.venv` with `uv sync --frozen --no-dev --link-mode copy`).
5. Passwords: the owner's chosen password was set as BOTH the dashboard and the Advanced password
   (the in-app rule "the two must differ" was bypassed on purpose, hashes written through the normal
   locked config store). The mail password in config.yaml was re-saved so it is stored encrypted (DPAPI).
6. `bind_address` changed from `127.0.0.1` to `0.0.0.0` so office PCs can open the dashboard; the installer
   opened the firewall for **LocalSubnet only**. To undo: set `dashboard.bind_address: "127.0.0.1"`.
7. Windows Update Active Hours set to 20:00-08:00 (see issue 3 below).
8. `06_install_services.ps1 -KeepServiceAccount`: the agent service was updated in place, **its account
   (`.\aambackup`) and the password Windows holds for it were not touched**; Prefect server and watchdog were
   reinstalled (LocalSystem). Firewall rule for 8080 created.
9. Old install folder deleted after the new one was verified. Older leftovers from earlier sessions
   (`C:\AAMBackup_v113`, `C:\AAMBackup.rollback-eef781f`) were left alone.

## 4. Problems found on the box and fixed (all in git, with tests)

| # | Problem | Fix |
|---|---|---|
| 1 | `04_check_readiness.ps1` crashed under StrictMode when the optional Windows Update policy value `NoAutoUpdate` was absent | read it through `PSObject.Properties` (170c566) |
| 2 | Carrying the config over with PowerShell 5.1 `Get-Content`/`Set-Content` re-encoded the UTF-8 comment characters, YAML refused the file | the carry-over is done byte-safely (UTF-8, no BOM); noted for the runbook |
| 3 | The "Active Hours" registry key the project used (`...\UX\ISM\ActiveHours`) **does not exist on Windows**; the real values are `...\UX\Settings\ActiveHoursStart/End` and the policy `SetActiveHours/ActiveHoursStart/End`. The old window (12:00-06:00 = 18 h) was also invalid on Server 2016 (max 12 h) | `03_setup_system.bat` and the readiness check now use the real keys and 20:00-08:00 (b24347e) |
| 4 | Installer: `icacls` rejects `.\aambackup` ("No mapping between account names and security IDs") | grants use `COMPUTERNAME\aambackup` (78519bb); a first version of that fix broke PowerShell parsing (a `\n` in a comment) and was corrected in f24f911 |
| 5 | After the install `AamBackupAgent` crash-looped: `ImportError: DLL load failed while importing _rust: Access is denied`. `uv sync --link-mode copy` had copied `cryptography\_rust.pyd` with an Administrators-only, non-inherited ACL | readiness now runs `icacls .venv /reset /T /C /Q` after `uv sync` (958d164) |
| 6 | Config backups made just before the mail password was encrypted still held the old plain password | the three such backups in `C:\AAMBackup` were deleted; the original is only in the private backup folder |

## 5. Independent code review (read-only) - findings and what was done

Fixed in 10d3054: (1) an upgraded server's old `dashboard.api_key` / `auth_enabled` crash-looped the services -> now
dropped with a warning (`DEPRECATED_KEYS`); (3) logout left Advanced unlocked -> logout locks it; (5) the typed mail
password reached a temp copy in plain text -> encrypted there too; (6) an undecryptable mail password was
invisible -> RED dashboard item; (7) PBKDF2 and the notify call blocked the dashboard's event loop -> thread pool;
(2) FY archive with a DPAPI Google key -> refuses clearly (S4 still open); (4) `11_reset_dashboard_passwords.ps1`
re-grants the agent account on the rewritten config.yaml; plus small leftovers (unused import, stale docstring, literal
default, `-KeepServiceAccount` refuses built-in accounts).
Not changed, on purpose: (8) the Google key sits in the process environment only while the temporary rclone config
exists (acceptable, documented); (9) `weekly_cron` is expected to fire on Monday (the report covers the last full Mon-Mon week);
(10) the "last 30 days" label ends yesterday although the window runs to now.
Reviewer's note kept: DPAPI machine scope means any local administrator or process on the server can decrypt; it
protects copies of the files, not the running server.

## 6. Verification on the box

* Readiness: all checks passed (two warnings: Defender exclusions; the service-account probe was not run because
  the account password is not known to this session).
* Services: AamPrefectServer, AamBackupAgent (as `.\aambackup`, checked with Windows), AamWatchdog all Running;
  six deployments registered; next runs: LAN 21:00 IST today, cloud 02:00 IST, weekly report Mon 08:00 IST,
  monthly 1 Nov 08:30, rollover check daily, audit Sundays.
* Dashboard from another PC (`http://10.10.150.170:8080`): `/health` answers; `/login` 200; wrong password -> redirect with
  error; correct password -> session; `/status` without session 401, with session 200; Advanced login 200;
  settings list never shows the mail password (`is_set` only).
* Real runs started from the dashboard: **cloud `CLOUD_NO_CHANGES_COMPLETE`** (12 s), **LAN `LAN_COMPLETE`** (1 s,
  0 files: the test NAS already holds the data). The dashboard attention list is empty.
* **Test email sent through the real mail server** using the DPAPI-decrypted mail password ("Test email sent").
* `/health` returned 503 right after the install only because the fresh database had no successful run yet; after the
  two runs the attention list is empty.

## 7. Things the owner should know / do

* **The weekly report at 08:00 IST today will say "action needed ... no backup ran" for 12-18 Oct**: the database was
  wiped as requested, so the report period has no runs. That is the new "empty period still reports" behaviour working,
  not a real failure. The old history is in `C:\AAM_backup_20261019\runtime\manifest.db`.
* Password: the dashboard and Advanced use the same password (your choice). Changing either later in
  Settings > Advanced > Passwords will insist that the two differ.
* The `aambackup` Windows account password was NOT changed (this session cannot change it on the NAS PC). If you want it
  changed, do it on both machines and re-run `06_install_services.ps1 -ServiceAccount .\aambackup` (it asks for the password).
* Not done (needs you or a decision): the real **NAS shutdown test** (only with you next to the NAS); G3 (send the ~60
  preview emails to your inbox: `python tools\preview_emails.py --config C:\AAMBackup\config.yaml --to <you>`);
  S4/S5 (move the Google key to the encrypted form and the archive step off `gcloud`); the M5 scenario list and the
  seven truthful nights; changing the `aambackup` password on both machines.
* To go back: stop the three services, restore `C:\AAM_backup_20261019\config\config.yaml` and `runtime\manifest.db`, and
  install the previous commit (6adcfb2) - but the old code cannot read the new config keys.
