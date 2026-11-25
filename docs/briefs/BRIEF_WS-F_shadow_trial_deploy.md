# Brief WS-F: shadow trial on the real box/NAS, go/no-go, deploy and rollback

Paste this whole file to a fresh agent. Master plan: `docs/PLAN_MASTER_PARALLEL_2026-11-25.md`. **Every box/NAS step below is owner-gated (OD3, OD10).**

## Context
Product: nightly backup automation for an accounting firm. Trial box `ssh innoviztawinserver2016` runs `baseline-v1-overlap`
at `dc50265` in `C:\AAMBackup` (runtime `C:\BackupAgent`), robocopy LAN mirror to the real NAS `10.10.151.53` (share path in
the box `config.yaml`, `lan.destination`), source `E:\FY26-27` (~703 files / 96 MB test data), schedule LAN 21:01, cloud
02:01, rollover 06:00, audit Sun 08:00, weekly Mon 08:00, monthly 1st 08:30 IST. `AamBackupAgent` runs as `.\aambackup`; the
NAS share is access-denied for the ssh user, so NAS steps must run as `aambackup`. The new build `rclone-engine` replaces
robocopy with `rclone sync` (P1 plan §2.1 flags; Phase 1b `--backup-dir` versions folder; copy-only on G3 block). Risks to
measure (P1 plan §6 R1-R4, R9): SMB long paths, NAS mtime precision vs `lan.modify_window_seconds` (2 s), case-only renames,
`System Volume Information`, `--track-renames` hashing over SMB, `--check-first` memory at scale, read-only files (OD1),
metadata loss (ACLs/attributes, accepted), timeouts.

## Rules (owner, binding)
- No box or NAS action without the owner's written go for that step and date. Never power off the NAS or this PC; no reboot/shutdown; never stop a running backup; tell the owner before anything that touches backup data.
- Never write into the real mirror folder before the deploy (step A is read-only: `--dry-run`, and `rclone check`). Step B writes only to a NEW test folder `<share>\AAM_SHADOW_TEST\` and `<share>\AAM_SHADOW_TEST_versions\`, never inside the mirror.
- Avoid production windows: no NAS step 20:30-22:30, 01:30-03:00, 05:45-06:30, Sunday 07:45-08:30 IST, and never while `C:\BackupAgent\backup.lock` exists.
- Never print secrets. Remote PowerShell: `;` not `&&`, `[Console]::OutputEncoding=[Text.Encoding]::UTF8`; no scp, pipe files over ssh; Git Bash mangles backslashes (write scripts with the Write tool).
- `tools/shadow` code is test-first (Haiku tests, Sonnet code); trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. No product code changes in WS-F.
- Model split: Opus writes the checklist and decides go/no-go input; Haiku runs read-only steps and copies evidence; Sonnet builds `tools/shadow`.

## Read first
`docs/PLAN_P1_RCLONE_LAN_2026-11-25.md` (§2.1 flags, fact sheet, §6 risks, Phase 1b), `docs/DEPLOY_RUNBOOK.md`, `docs/DEPLOY_LOG_2026-10-19.md`, `SESSION_HANDOFF.md` 2026-10-23 block (deploy checklist: slot, lock, service order, DB copy), `docs/INSTALL_FRESH.md` (production target may be a domain controller), `core/lan_sync.py` (`build_lan_rclone_command`, `plan_lan_mirror`, `check_lan_arrival`).

## Packages
| ID | Goal | Output | Starts |
|---|---|---|---|
| F0 | Opus: `docs/SHADOW_TRIAL_2026-11-25.md` = this brief turned into a dated checklist with owner sign-off lines | doc | NOW |
| F1 | `tools/shadow/run_step.py` (new): builds the EXACT product argv via `core.lan_sync.build_lan_rclone_command` from a given config (no hand-written flags), runs it with `run_tool`, samples `rclone.exe` peak working set and CPU every 5 s (psutil), writes `shadow_<step>_<stamp>/` (argv, log, combined, stats, memory.csv, timings.json). Refuses any destination inside the real mirror for a non-dry-run (test). | code + tests (`tools/shadow/tests`) | NOW |
| F2 | **Step A** (after G1, OD3): read-only. As `aambackup` (scheduled task run once, or `runas`-free alternative the owner picks), on the box with a `code_rclone` copy (never `C:\AAMBackup`): (1) `plan` = product dry-run `E:\FY26-27` → real mirror; (2) `rclone check --one-way --size-only` (product argv); (3) source facts: file count, bytes, paths > 260, read-only files (`dir /a:r /s`), case-only duplicates, names with trailing dot/space; (4) read-only files ON THE NAS MIRROR (listing only, P1 plan R11: rclone cannot replace them; feeds OD1); (5) review M4 timings: wall time of `rclone-lan-plan`, `rclone-lan-check` and `rclone-lan-hash` over a typical night's copies (hash run against the real mirror is read-only). | evidence folder + `agent_runs/shadow_A.md` | after G1 |
| F3 | **Step B** (after G5, OD3): copy of the source (or a synthetic tree of the size the owner allows, default 50k files / 5 GB under `C:\AAMShadow\src`) → `<share>\AAM_SHADOW_TEST\FY26-27`: first full sync, no-change rerun, 300 renames, 20 deletes, 20 replaced files (versions folder), read-only files at destination replaced/deleted, long paths > 260, case-only rename, a locked source file, arrival check, versions purge with a 0-day test retention on the test versions folder only; NAS mtime precision probe (write, read back, compare ns). | evidence + `agent_runs/shadow_B.md` | after G5 |
| F4 | **Cable pull** (owner at the NAS): during a step-B full sync, owner unplugs the NAS network for ~2 min, then replugs; then a second pull held > S6 threshold (OD2). Expect: rclone exit 1/5 → PARTIAL/FAILED retryable, no deletions, no `.partial` left after the next run, S6 kills a hung run within threshold + one poll; next run COMPLETE. | evidence + `agent_runs/shadow_cable.md` | after F3 |
| F5 | Go/no-go sheet for the owner (criteria below) | `docs/SHADOW_TRIAL_2026-11-25.md` result section | after F4 |
| F6 | Deploy + rollback (after G6 and OD10) | deploy log `docs/DEPLOY_LOG_<date>.md` | after G6 |

## What to measure (each with a number in the evidence)
Durations of plan, sync, check, hash vs keys `lan.rclone_measure_timeout_seconds`, `lan.subprocess_timeout_seconds`, `lan.verify_timeout_seconds`, `lan.arrival_hash_timeout_seconds` (need >= 3x headroom; and `budget.lan_post_reserve_minutes*60` >= check + hash + `wol.shutdown_verify_timeout_seconds` with margin, P1 plan "Before the box switch"); case-only renames with and without `--fix-case` (review S8b); peak memory of `--check-first` (MB per 100k files, extrapolated to 1M and 2.5M); `--track-renames` cost (extra bytes read and time; rclone hashes on the local/SMB side); read-only file count and what rclone did with each; paths > 260 copied and verified; NAS mtime precision and whether a no-change rerun is truly 0 transfers with `modify_window` 2 s; case-only rename result; `.partial` files after a cable pull; versions folder growth per night (replaced + deleted bytes); `--retries` 3 wall time on a locked-file night.

## Go/no-go (G6), all must hold
1. Step A: planned copies and planned deletes against the robocopy-made mirror are 0, or every difference is listed and explained (no mass re-copy, no deletions of data that exists in the source). Any unexplained planned delete = NO-GO.
2. Step B: no-change rerun 0 transfers / 0 deletes; renames = moves with 0 deletes; deletes and replaces land in the versions run folder with equal MD5; arrival check verified; long paths OK; read-only result accepted by the owner (OD1).
3. Cable pull: no deletion during or after the outage, status never COMPLETE for the interrupted run, S6 behaves per OD2, next run COMPLETE.
4. Timeouts >= 3x measured durations; memory at the extrapolated production size < 50 % of the production server RAM.
5. Harness verdict G5 passed; RC gates G4 passed.

## Deploy runbook (F6, owner go; window: weekday 09:00-17:00 IST, after the 02:01 cloud run is terminal, not Sunday/Monday 08:00)
1. Pre-checks: `aam-backup` concurrency slot `active_slots` = 0; no `C:\BackupAgent\backup.lock`; no python/rclone/robocopy PIDs of ours; note `git -C C:\AAMBackup log -1` (expect `dc50265`).
2. Backups: copy `C:\BackupAgent\manifest.db` → `manifest.db.bak-before-rclone-<stamp>`; `C:\AAMBackup\config.yaml` → `config.yaml.bak-before-rclone-<stamp>`; record sha256 of both.
3. Stop `AamWatchdog`, then `AamBackupAgent` (leave `AamPrefectServer` running unless the runbook needs it).
4. Code: git bundle of `rclone-rc1` piped over ssh; `git fetch <bundle>`; `git checkout --detach rclone-rc1` (or `git reset --keep`); verify HEAD.
5. Config: remove deprecated keys (`lan.mt_threads`, `log_tail_kb`, `dry_run_timeout_seconds`, `use_backup_mode`, `privilege_check_timeout_seconds`, `maintenance.orphan_robocopy_log_max_age_hours`, plus WS-C deprecations); add `lan.versions_*` (folder on the NAS share, outside the FY mirror root); set the timeout keys and reserve minutes from step A (M4); if OD1 chose a one-time `attrib -R /S` of the mirror, do it now with the owner present; validate with `.venv\Scripts\python.exe -c "from models.config import load_config; load_config()"` (with `AAM_CONFIG` set) as `aambackup`.
6. Readiness: `deploy/04_check_readiness.ps1 -ServiceAccount .\aambackup` (from a separate worktree/copy if it would strip a dev venv).
7. Start `AamBackupAgent`, then `AamWatchdog`; check `/health`, deployments listed, schedules unchanged (crons equal to before).
8. First night: watch the 21:01 LAN run: plan counts match step A (≈0 copies, 0 deletes), status `LAN_COMPLETE`, arrival verified, NAS shutdown per policy; 02:01 cloud unchanged. Watch 7 nights (G7).
**Rollback** (any NO-GO after deploy): stop `AamWatchdog`, `AamBackupAgent`; `git checkout --detach dc50265`; restore `config.yaml.bak-before-rclone-<stamp>` (old StrictModel rejects new keys, risk R7); restore `manifest.db` backup only if the new build changed the schema (check `user_version`; runs after deploy are then lost from history, write them down first); start `AamBackupAgent`, `AamWatchdog`; verify `/health`. The NAS mirror written by rclone stays valid for robocopy `/MIR`; the `AAM_versions` folder is outside the mirror and is left for the owner to keep or delete.

## Report back (<= 20 lines)
Per step: date, owner go reference, key numbers, deviations, go/no-go per criterion, evidence paths, anything that needs a product change (to Opus).
