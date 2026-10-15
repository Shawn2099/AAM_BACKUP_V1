# Deployment runbook — Windows Server 2016 (by hand, no AI agent needed)

For the person sitting at the server. Every step is a command you type or a
window you click; each says what you should see. If a step does not show what
it says, **stop** and do not continue — nothing is changed until step 6.

Verified 2026-10-15 on the trial server (Windows Server 2016, PowerShell
5.1.14393): all `.ps1` scripts parse on its own PowerShell, every program the
`.bat` files call exists there, and the cmdlets used (`Get-LocalUser`,
`Start-Process -Credential`, `Get-Credential`) are available.

| Placeholder | Meaning | Example |
|---|---|---|
| `C:\AAMBackup` | where the code lives | (trial server) |
| `C:\BackupAgent` | runtime folder (`paths.runtime_dir`) | |
| `aambackup` | the backup service account | your choice |

---

## 0. Before the day (owner / IT)

1. **Service account on the server** — `lusrmgr.msc` → Users → New User:
   name `aambackup`, strong password, *Password never expires* ticked,
   *User must change password* unticked.
2. **Same account on the NAS** — same name, **same password**, with *write*
   (modify) on the backup share. (The server is in a WORKGROUP: Windows logs on
   to the NAS with this name + password. This is the fix for the "Access is
   denied" nights.)
3. **Backup right** (only if `lan.use_backup_mode: true`, the default) —
   `secpol.msc` → Local Policies → User Rights Assignment →
   *Back up files and directories* → Add → `aambackup`.
4. **NAS remote shutdown** — on the NAS, allow `aambackup` to shut it down
   remotely ("Force shutdown from a remote system") and allow it through its
   firewall. This cannot be tested without shutting the NAS down; the first
   real LAN backup reports it (dashboard: *NAS shutdown not verified* if not).
5. Pick a quiet time: no backup running (LAN 21:00, cloud 02:00 after this
   update). Check: Task Manager shows no `robocopy.exe` / `rclone.exe`.

## 1. Open an Administrator PowerShell

Start menu → type `PowerShell` → right-click → *Run as administrator*.
Then:

```powershell
cd C:\AAMBackup
```

## 2. Save anything changed on this server (never lose it)

```powershell
git status
```

If it lists modified files (on the trial server: `core/integrity.py`, `ui.py`,
`uv.lock`), save them twice — a patch file and a git stash:

```powershell
git diff --output=C:\BackupAgent\box_local_edits_2026-10-15.patch
git stash push -m "box-local edits before mvp-hardening"
git status
```

You should now see *nothing to commit, working tree clean*. Keep the patch
file; send it to the developer to check whether any of it is still needed.

## 3. Get the new version

```powershell
git fetch origin
git checkout mvp-hardening
git pull
git log -1 --oneline
```

You should see the latest commit of `mvp-hardening` (as on GitHub).

## 4. Update config.yaml for this version

Open `C:\AAMBackup\config.yaml` in Notepad. In the `schedule:` section set:

```yaml
  lan_cron: "0 21 * * *"
  cloud_cron: "0 2 * * *"
  monthly_cron: "30 8 1 * *"
  audit_cron: "0 10 * * SUN"
```

(The new version refuses a schedule where one backup could still be running
when the next one starts — LAN 21:00 / cloud 22:00 is refused.) Every other
setting is optional; `config.example.yaml` documents all of them.

## 5. Readiness check (changes nothing on the services)

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\04_check_readiness.ps1 -ServiceAccount .\aambackup
```

It asks for the `aambackup` password, then runs the checks **as that
account**. Wanted result: the last line is green
`>>> SUCCESS: All readiness checks passed!`. Every `[FAIL]` line names the
fix. Typical ones:

| `[FAIL]` says | Fix |
|---|---|
| `as .\aambackup: NAS write ... Access is denied` | step 0.2 (same name + password on the NAS, write on the share) |
| `as .\aambackup: backup privilege ...` | step 0.3, then sign out/in is NOT needed - just re-run |
| `Config validation ... schedule does not fit the run budgets` | step 4 (the message shows which two jobs collide) |
| `Base Python location ... inside a user profile` | re-run `deploy\03_setup_system.bat` (installs Python to `%ProgramData%`), then this step |
| `the probe could not run as the account` | wrong password, or the account cannot read `C:\AAMBackup` |

`[SKIP] NAS remote shutdown` is expected (see step 0.4).

## 6. Install the services

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\06_install_services.ps1 -ServiceAccount .\aambackup
```

It asks for the password again. It first re-checks `config.yaml` and stops
**without touching the services** if it is not valid. At the end you should
see `INSTALLATION COMPLETE`.

## 7. Check it is running

1. `services.msc`: *AAM Prefect Server*, *AAM Backup Agent*, *AAM Backup
   Watchdog* = **Running**; *AAM Backup Agent* "Log On As" = `.\aambackup`.
2. Browser on the server: `http://localhost:8080` — the dashboard opens; the
   banner may be red until the first backups run (that is truthful).
3. `http://localhost:4200` — Prefect shows the deployments `backup-cloud`,
   `backup-lan`, `integrity-audit`, reports, `rollover-check`.
4. `C:\BackupAgent\logs\watchdog_app.log` — recent lines, no CRITICAL.

## 8. First backups

- Run `backup-lan` once from Prefect (*Run* → *Quick run*) and watch the
  dashboard: expected `LAN_COMPLETE`, then *NAS shutdown verified*.
- **First full cloud upload**: either let it spread over several nights
  (each night `CLOUD_PARTIAL` "continues next run" until done), or run
  `backup-cloud` once by hand with parameter `budget_hours` = e.g. `48` over a
  weekend (Prefect → deployment → *Run* → *Custom run* → parameters).

## 9. If something goes wrong — go back to the old version

```powershell
cd C:\AAMBackup
git checkout production
git stash pop
powershell -ExecutionPolicy Bypass -File .\deploy\06_install_services.ps1
```

(`git stash pop` restores the server's own edits from step 2; the old
installer reinstalls the old services as LocalSystem.) Put the old schedule
back in `config.yaml` if you changed it.

## 10. Acceptance (plan WP17) — tick when seen

- [ ] Readiness all green as `aambackup`
- [ ] LAN backup `LAN_COMPLETE` with real WoL wake (note the wake time in
      `run_logs` → set `wol.wake_timeout_seconds` to about 2x it)
- [ ] NAS shutdown *verified* on the dashboard
- [ ] Cloud backup complete (or PARTIAL nights until the first upload is done)
- [ ] Forced failure (e.g. unplug NAS cable during LAN): exactly **one** alert
      email, dashboard red
- [ ] Blocked run: dashboard card → approve with the typed phrase → one run
- [ ] Weekly + monthly report emails received
- [ ] Integrity audit `VERIFIED`; with internet blocked → *audit could not run*
      (not "missing files")
- [ ] `sc stop AamBackupAgent` stops within ~3 min (no STOP_PENDING) and
      `backup.lock` is gone
- [ ] Restore test: `deploy\09_restore_from_gcs.bat` restores a sample folder;
      files open
- [ ] 7 nights in a row with truthful results (dashboard vs run_logs)
- [ ] Windows Update reboot window decided (see SESSION_HANDOFF.md)
