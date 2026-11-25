# Shadow Trial & Deploy Checklist (`rclone-engine`, 2026-11-25)

Reference: `docs/briefs/BRIEF_WS-F_shadow_trial_deploy.md`, `docs/PLAN_MASTER_PARALLEL_2026-11-25.md`.
**Every box/NAS step below is owner-gated (OD1, OD3, OD10).** Never run during production windows (`20:30–22:30`, `01:30–03:00`, `05:45–06:30`, Sunday `07:45–08:30` IST) or while `C:\BackupAgent\backup.lock` exists.

---

## Step A — Read-Only Dry-Run & Source/NAS Audit (F2, after G1 + OD3)

- Owner Go Date / Sign-off: `____________________`
- Code copy used: `C:\AAMLivebox\code_rclone` (never `C:\AAMBackup`)
- Run as: `.\aambackup`

Checklist:
- [ ] Run `tools/shadow/run_step.py --step plan-ro` (`--dry-run` against real mirror)
- [ ] Run `rclone check --one-way --size-only` against real mirror (read-only)
- [ ] Record source facts: file count, total bytes, paths > 260, read-only files (`dir /a:r /s`), case-only duplicates, trailing dot/space names
- [ ] Record NAS mirror read-only file count (feeds **OD1**: `0` → nothing; `> 0` → owner decides one-time `attrib -R /S`)
- [ ] Record wall times for `rclone-lan-plan`, `rclone-lan-check`, `rclone-lan-hash` (size `lan.rclone_measure_timeout_seconds`, `lan.subprocess_timeout_seconds`, `lan.verify_timeout_seconds`, `lan.arrival_hash_timeout_seconds`, `budget.lan_post_reserve_minutes` ≥ 3× measured)
- [ ] Save evidence to `agent_runs/shadow_A.md`

---

## Step B — Shadow Folder Sync & Functional Checks (F3, after G5 + OD3)

- Owner Go Date / Sign-off: `____________________`
- Target: `<share>\AAM_SHADOW_TEST\FY26-27` and `<share>\AAM_SHADOW_TEST_versions\` (never real mirror)

Checklist:
- [ ] First full sync + peak `--check-first` RSS (`memory.csv`)
- [ ] No-change rerun (expect `0` transfers, `0` deletes)
- [ ] 300 renames (expect moves with `0` deletes)
- [ ] 20 deletes + 20 replaced files (expect versions folder populated with equal MD5)
- [ ] Read-only destination files replaced/deleted, long paths > 260, case-only rename (`--fix-case` comparison)
- [ ] Locked source file test (`LAN_PARTIAL`, `deletes_held=True`) + `--retries 2` vs `3` timing
- [ ] Versions purge with `0`-day test retention on `<share>\AAM_SHADOW_TEST_versions\` only
- [ ] NAS mtime precision probe
- [ ] Save evidence to `agent_runs/shadow_B.md`

---

## Cable Pull — Outage & S6 Stall Watchdog (F4, owner at NAS)

- Owner Go Date / Sign-off: `____________________`
- [ ] Pull NAS network ~2 min mid-sync, then replug → expect `LAN_PARTIAL` / `LAN_FAILED` retryable, `0` deletions, next run `LAN_COMPLETE` with no `.partial` left
- [ ] Hold outage > S6 `stall_timeout_seconds` → expect S6 tree-kill within threshold + 1 poll, next run `LAN_COMPLETE`
- [ ] Save evidence to `agent_runs/shadow_cable.md`

---

## Go / No-Go Criteria (G6 / F5)

- [ ] Step A: 0 unexplained planned copies/deletes vs robocopy mirror
- [ ] Step B: no-change 0/0, renames = moves with 0 deletes, versions MD5 equal, arrival verified, long paths OK, OD1 resolved
- [ ] Cable pull: 0 deletions on outage, never COMPLETE on interrupted run, S6 fires per OD2, next run COMPLETE
- [ ] Timeouts ≥ 3× measured durations; extrapolated peak memory < 50% server RAM
- [ ] G4 (`rclone-rc1`) and G5 (harness verdict) passed

Owner Go/No-Go Decision (`GO` / `NO-GO`) & Date: `____________________`

---

## Deploy & Rollback (F6, weekday `09:00–17:00` IST after 02:01 cloud terminal, OD10)

1. Pre-checks: `aam-backup` `active_slots == 0`, no `C:\BackupAgent\backup.lock`, no running python/rclone/robocopy; note `git -C C:\AAMBackup log -1` (`dc50265`).
2. Backup `C:\BackupAgent\manifest.db` and `C:\AAMBackup\config.yaml` with SHA-256 recorded.
3. Stop `AamWatchdog`, then `AamBackupAgent`.
4. Fetch `rclone-rc1` bundle over ssh and checkout `--detach rclone-rc1`.
5. Update `config.yaml`: remove deprecated keys, add `lan.versions_*`, set measured timeouts/reserve, apply OD1 `attrib -R /S` if approved, validate with `load_config()` as `aambackup`.
6. Run `deploy/04_check_readiness.ps1 -ServiceAccount .\aambackup`.
7. Start `AamBackupAgent`, then `AamWatchdog`; verify `/health` and deployment schedules.
8. Watch first night (21:01 LAN, 02:01 cloud) and 7 nights (**G7**).

**Rollback**: Stop `AamWatchdog` and `AamBackupAgent`, `git checkout --detach dc50265`, restore `config.yaml.bak-before-rclone-<stamp>` (and `manifest.db` only if schema `user_version` changed), restart services, verify `/health`.

---

## 8. Shadow Trial & Post-`rclone-rc1` Observations (E1–E7)

| ID | Topic | What to check / record during shadow trial |
|---|---|---|
| **E1** | Dry-run + size-check + hash-check wall time | Measure `rclone-lan-plan`, `rclone-lan-check`, and `rclone-lan-hash` against the real mirror and verify `lan.rclone_measure_timeout_seconds`, `lan.verify_timeout_seconds`, and `lan.arrival_hash_timeout_seconds` are >= 3x measured. |
| **E2** | Budget post-reserve margin | Confirm `budget.lan_post_reserve_minutes * 60` >= measured check + measured hash + `wol.shutdown_verify_timeout_seconds` with margin. |
| **E3** | `--track-renames` vs NAS | Verify folder/file renames on the source produce `move to` rather than delete+copy against the real Samba share, and confirm `--fix-case` necessity. |
| **E4** | Read-only files on NAS | Count any read-only attributes on the NAS mirror (read-only check, no attribute changes) to confirm rclone replacement/deletion behavior. |
| **E5** | Retry count (`lan.retry_count = 2`) | Observe a locked-file night (e.g. open Tally/PDF) and confirm retry wall time stays well within the night budget. |
| **E6** | Versions folder growth (`AAM_versions`) | Verify `AAM_versions\<YYYY-MM-DD_HHmm>_<FY>\` space accounting and `rclone purge` retention behavior after 91 days (or test retention window). |
| **E7** | rclone version pin (`tools.rclone_expected_version`) | Confirm `deploy/bin/rclone.exe` on the box reports `v1.74.x` and `health_check_task` passes cleanly without triggering `AAM-SYS-04`. |
