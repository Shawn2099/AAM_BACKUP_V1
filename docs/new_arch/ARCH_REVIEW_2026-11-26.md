# Architecture and reliability review - 2026-11-26

Branch `rclone-engine`. Read-only review, no product code changed. Method: one Haiku inventory agent and two Opus review agents (FY rollover/activation; core + Prefect). File:line references come from the reviewers and were not re-verified by the main session. The working tree had uncommitted AF-1..3 edits from another session; they were left alone.

## 0. Owner decisions (2026-11-26, after the review)

- **Closed FY needs no further sync.** Finding 2 in section 4 (old FY unprotected after Apr 1) is **withdrawn as a bug**; current behaviour is the intended one. Section 7 (date-derived "old year until" model) is no longer motivated by it; the remaining case for simplifying the rollover is size/complexity only.
- **Dead-man's switch: later, not now.** Finding 1 and action 1 in section 8 are **deferred** (future work).

## 1. Verdict

- **Reliability:** conditionally reliable. The data path is solid. The risk is in the machinery around it.
- **Strong:** one decision point for terminal status (a failed backup cannot show as success), exclusive `backup.lock` with PID liveness and stale takeover, rclone hang kill (timeout + stall watchdog), `--backup-dir` versions, overdue check by the watchdog, fail-closed rollover.
- **Over-built** for one box and one firm.
- **Two HIGH gaps** to close before trusting it for a year (section 4).

## 2. Inventory (Haiku)

| Item | Value |
|---|---|
| Production code | ~23.4k lines (core/ 14.9k in 56 modules; flow.py 3,672; ui.py 1,936; models/config.py 1,437; manifest.py 1,567) |
| Tests | 61.9k lines (252 test files + 44 audit_lab files); ratio 2.65x production |
| Config | 235 fields, 19 models, 18 sections |
| Statuses / faults | 32 status strings, 72 distinct AAM-* fault codes, 8 alert classes |
| Prefect | 5 flows, 16 tasks, up to 6 deployments |
| Long functions in flow.py | `_run_lan_pipeline` 391, `_run_cloud_pipeline` 364, `_rollover_resume` 350, `integrity_audit_flow` 207, `backup` 186 |
| Dead modules | none (every core module has a non-test importer) |
| Doc drift | `docs/ARCHITECTURE.md:239` still says `core/notify.notify` is "built, not wired yet"; SESSION_HANDOFF says wired |

Full per-file table (scratchpad, may be cleared): `C:\Users\malliath\AppData\Local\Temp\claude\b4bcf21203d17516\fcce5805-6b0c-4d02-bde6-64f382311ab1\scratchpad\inventory_full.txt`

## 3. Is it over-complicated? Yes

- Test volume does not equal real coverage: the G3 copy-only feature never fired at HEAD (`is_g3_only_block` read keys `enforce()` never writes) while ~3,764 tests were green, because tests used hand-built dicts.
- Safety machinery can itself cost backups:
  - G2 shrink, G4, G5 blocks repeat nightly until a human approves (no copy-only). For G2 a legitimate cleanup of old client folders means no new data is protected until approval. Copying never deletes, so copy-only is safe for every block except G4/G5 (wrong destination).
  - "Could not measure" in G2/G3 becomes BLOCKED + approval request instead of FAILED (guards.py:93-96, 126-130). Approving it is approving blind.
  - Daily cloud size check flags files changing during the run (AF-12); biggest source of false-alarm fatigue.
  - `rclone_version_policy=block` combined with the PATH fallback in `resolve_binary` (process.py:246).
- FY rollover + overlap + journal + G6 is ~1,500 lines (fy_rollover ~506, fy_overlap ~627, `_rollover_resume` ~350, plus UI/attention/reports) for a once-a-year event. Most of it exists because the design rewrites `config.yaml`, a one-way step, which forces the clock proof, the 7-step journal, hand-switch/re-entered/crash detection, compare-and-set, final-copy rules, overlap records, `ended_at`, and year-guessing heuristics (`row_fy_or_default`).

### Keep / merge / delete (core reviewer)

| Action | What |
|---|---|
| Keep | `tool_runner`, `statuses`, `run_artefacts`, `manifest` (trim), `notify` (simplify), `guards` G1/G3/G4/G5, `approvals`, `smb_ready`, `wol`, `shutdown`, `health`, `cloud_sync`, `lan_sync`, `lan_versions`, `integrity`, `backup_repository`, `process` |
| Merge | `attention` + `overdue` + `alert_class` + `faults` + `messages` -> one alerts/status module. `report` + `email_compose` + `email_samples` + `support_bundle` -> one email module (move `email_samples` to `tools/`). `cloud_preflight` / `lan_preflight` / `permissions` / `rclone_errors` -> into the leg modules. `secrets` / `secret_setup` / `password_setup` / `admin_auth` / `gcs_key` -> one module. `config_edit` + `config_help` + `source_picker`: keep, but cut the 235 config keys to the few an operator should touch. |
| Delete | `report.send_failure_alert` (report.py:44, no production caller). Tag concurrency limit `aam-backup` (launch.py:98-111, no flow/task uses the tag). Cloud write probe (design doc already says so). `create_new_fy_folders` (fy_rollover.py:305, legacy, no callers). `detect_rollover` (only logs). Watchdog Prefect babysitting, once Prefect goes. |

## 4. Findings

### HIGH

1. **Invalid `config.yaml` or unreadable DB means no alert at all.** Flow raises in `load_config` before any row/alert (flow.py:3516); agent exits (launch.py:348); the watchdog loads config fresh for every mail (watchdog.py:319) and the overdue check (397) and on failure only logs a warning (430-432). No dead-man's switch. *Fix:* last-good-config cache for alert settings, plus an external heartbeat or a daily "all OK" mail.
2. **Old FY gets no protection after the Apr 1 switch.** FY26-27 is never synced again and is moved to ARCHIVE; no code reads the old folder after the switch. Accounting firms usually keep editing the closed year until return/audit deadlines (about Jul-Oct). The existing overlap runs before Apr 1 (adds the next year early); an overlap that keeps syncing the old year after Apr 1 does not exist. **OWNER DECISION NEEDED.**
3. **(AF-1, fixed only in the uncommitted tree)** At HEAD, `is_g3_only_block` reads keys `enforce()` never writes, so copy-only never fired. Lesson: build test records with `enforce()`.

### MEDIUM

4. **Prefect DB grows forever.** Prefect 3.7.2 vacuums only `events`; flow-run vacuum is off. Deploy scripts do not set `PREFECT_SERVER_SERVICES_DB_VACUUM_ENABLED` (06_install_services.ps1:311-317); the loguru->Prefect bridge pushes every log line in. If the API is down at schedule time nothing starts; only the overdue mail catches it about a day later.
5. **Stuck Prefect slot (AAM-SYS-08)** needs a human: each night waits 3600 s and ends `SLOT_TIMEOUT` (flow.py:227, 604-621). The slot is redundant: `backup.lock` already serializes, and the concurrency call uses `strict=False`.
6. **Wedged agent never self-heals.** The watchdog only checks service state; the overdue check emails but never restarts the agent (watchdog.py:382-432).
7. **Archive step can retry every morning forever.** If `gcs_key_path` is the DPAPI-protected key or gcloud is missing, `run_archive_transition` always returns False (fy_rollover.py:464-477); the journal never completes, resumes daily, writes a `ROLLOVER_COMPLETE` row each day (flow.py:3156), escalates AAM-ROL-05 after 3 days. The archive rewrite adds one object version per file; with the lifecycle rule (deploy/gcs_lifecycle.json:20, delete a version once 2 newer exist) each file loses its oldest saved version. GCS behaviour not tested live.
8. **Switch does not check the new year's backup (P4).** G6 only looks at the old year (guards.py:230, fy_overlap.py:477). The new year becomes the only one backed up even if its cloud pass was blocked.
9. **Blocked rollover is noisy (P5).** The morning's final LAN run uses `allow_shutdown=False` (flow.py:3201); after a block the NAS stays on about 15 h until the 21:01 nightly run; wake, block and mail repeat daily.
10. **AF-12:** cloud verify race on files that change during the run (flow.py:1872-1880).

### LOW

11. Antivirus holding `backup.lock` reads as a live lock (process.py:117-120) -> RuntimeError -> night lost as `SLOT_TIMEOUT` (flow.py:245-254).
12. Copy-only branch: if the access probe raises, it replaces the `GuardBlocked`; status FAILED, no approval request (flow.py:1783, 2188).
13. launch.py:170-171 deletes a "stale" lock directly, skipping the double-read rule.
14. `update_config_yaml` builds the new path from the parent folder loaded at flow start (flow.py:2962, 3030); compare-and-set only checks the year (fy_rollover.py:416). A parent-folder change saved in Settings between the final backups and the switch is silently undone (the lock is free in those gaps; config_edit.py:13).
15. Two rollover checks can run at once (ui.py:1562-1568 race window); a failed second run could back up FY27-28 under an FY26-27 label and send a misleading G6 block.
16. If `end_overlap` fails and the journal finishes in the same run, it is never retried (flow.py:3146); old-year rows are attributed to the new year in reports.
17. First pass of a backup flow started before the switch is not covered by the race guard (flow.py:3581); harmless re-sync of FY26-27 after the archive.
18. `overlap_activate_on` accepts any date except 04-01 (config.py:916); `04-02` turns on a nearly year-long double backup.
19. Dead code: `report.send_failure_alert`, `aam-backup` tag limit.
20. Doc drift at `docs/ARCHITECTURE.md:239`.

### Checked and dropped
`--backup-dir` moves do not inflate `stats.transfers` (measured, rclone 1.74.3: transfers=2 matched two "Copied (" lines, renames=2), so `_hash_plan` does not falsely flag runs as suspect.

### Untested
Prefect catching up a missed 06:00 run, a reboot mid-journal, concurrent rollover checks. The calendar test fakes pipelines, clock and archive.

## 5. FY rollover trace (summary)

- **Activation (Dec 31 or later):** 06:00 `rollover_check_flow` (flow.py:2712) calls `activate_if_due` (fy_overlap.py:253-311): needs a proven clock, creates `E:\FY27-28` plus placeholder, writes `db_meta['overlap:FY27-28']` last, so a re-run is harmless. A server off on Dec 31 activates at the next check.
- **Nightly:** `active_passes` (fy_overlap.py:127) adds a second pass per leg; cloud prefix = folder name (flow.py:104); run ids end `-FY27-28`; the next year's NAS folder is made after WoL (flow.py:2174).
- **Apr 1:** 7-step DB journal (rollover_journal.py): final backups, G6, source folder, NAS folder, config switch (compare-and-set rewrite of config.yaml, fy_rollover.py:397), archive, NAS shutdown, announce. A verified copy started at or after Apr 1 00:00 IST counts as the final copy (fy_overlap.py:467). `end_overlap` writes `ended_at` (flow.py:3145).
- **State lives in three places:** config.yaml, journal tables, `db_meta` overlap records. Disagreements are caught: config written but journal never saw it (flow.py:2824), hand switch (2839), old config restored ("reentered", 2869), lost DB during overlap (activation re-runs).
- **Fail-closed:** clock behind/unproven, jump of more than one FY, missing/renamed folder, partial copy (never counts), missed Apr 1 (daily check retries; both years stay backed up meanwhile). Leap years and DST are a non-issue (IST fixed offset, `02-29` refused, config.py:913).
- 93 targeted unit tests (activation, journal flow, hand-switch, re-entered, gaps) passed.

## 6. Prefect question

Prefect gives cron, one slot, run IDs and a UI; our own `run_history`, `backup.lock` and dashboard already duplicate all of these. Cost: a second service with its own DB, an API on the path of every task-state change, a lease service that can get stuck, DB growth, ~1,200 lines in launch.py + watchdog.py.

**Recommendation (core reviewer):** remove Prefect, but only after the rclone LAN switch is proven (do not stack both changes). Prefer one Windows Task Scheduler entry every 5 min running `aam tick`: read the schedule from config.yaml (cronsim), check `run_history` for due/missed jobs, run under `backup.lock`. Windows supplies firing, reboot catch-up and no wedged scheduler. Remaining: 1 service (dashboard) + a tiny watchdog (overdue check + heartbeat). Cost: a no-op `@task` shim so the ~1,000 Prefect-mocking tests still work, plus a few days of work. See also `docs/OPUS_DESIGN_RCLONE_ONLY_NO_PREFECT_2026-11-25.md`.

## 7. Simpler FY model (rollover reviewer)

Config holds the parent folder, not the year. Years to back up are derived from the date each run: current FY, previous FY until a configured "old year until" date (e.g. 10-31), next FY from the activation day if its folder exists. Each year is an independent, idempotent pass to its own prefix and NAS folder. No switch, no journal, no config rewrite; the "final copy" question disappears because the old year keeps syncing; a wrong clock only adds or drops a year's pass. Guard: never drop a year until its last pass after the cut-off succeeded (one DB lookup). Alternative: back up the parent folder (all FY folders), since closed years are static. Delete the archive step and its gcloud dependency (the 365-day lifecycle rule already moves objects to ARCHIVE).

## 8. Recommended actions (in order; nothing done yet)

1. Dead-man's switch: external heartbeat or daily "all OK" mail, plus last-good-config alerting for the config/DB-broken path.
2. Enable Prefect flow-run vacuum now; make the watchdog restart a wedged agent when a leg is overdue and no lock is held; auto-reset a timed-out slot wait when the lock is dead.
3. Answer the old-year question (section 4, item 2). If yes: date-derived "old year until" pass, drop the immediate archive.
4. Kill false failures: ship AF-12; give copy-only to G2 and to unmeasured G3; keep only G4/G5 as hard human gates.
5. Freeze features. Rewrite guard/approval tests against real `enforce()` and real rclone output. Run the shadow trial before anything new.
6. Rollover hardening if the current design stays: require a successful new-year backup before the switch (P4); make the archive step optional, capped, and skipped for a DPAPI key; build the new path inside the config lock; add a flow-level lock for the rollover check; shut the NAS down in a `finally` when blocked; validate `overlap_activate_on`.
7. After the shadow trial: decide on dropping Prefect and simplifying the rollover (sections 6-7); merge/delete modules per the table in section 3.
