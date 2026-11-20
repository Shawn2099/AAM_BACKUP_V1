# Tasks - FY overlap (follow this list; tick each task in the commit that finishes it)

**Plan:** `docs/PLAN_FY_OVERLAP_2026-11-20.md` (rev 4, FINAL). Section numbers below (§) refer to the plan.
**Status:** FINAL. Owner gave the go 2026-11-20; build in progress.

## Rules for every task

1. **Order:** do the work packages in order (WP0 → WP12); inside a WP, in task order. A task starts only when the
   tasks it depends on are ticked.
2. **Before editing a symbol:** GitNexus `impact` (upstream); report HIGH / CRITICAL / UNKNOWN in the task's commit
   message and confirm UNKNOWN with a text search.
3. **Test first:** write the regression test(s) named in the task, see them fail on the old code, then fix.
4. **Every tunable is a config key** (default + bounds in `models/config.py`, label in `config_help.py`) in the same
   commit as the code that reads it.
5. **Before committing:** the task's tests + the WP's test files green; at the end of each WP the full unit suite
   (`.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600`) green; GitNexus
   `detect-changes --scope all` (not partial / truncated).
6. **Commit per task** (or a tight group of tasks), message cites the task IDs (`OV-1.2`) and plan items (`R-1`),
   ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never commit the owner's unrelated
   uncommitted files (restore / upload work).
7. **Scope:** nothing outside this list. A new finding goes to "Found while building" at the bottom and is raised
   with the owner before it is built.
8. **Session end:** update `SESSION_HANDOFF.md` (top block) with the last ticked task and the next one.

---

## WP0 - Dependency upgrade + httpx2 (plan D-15, D-16)

- [x] **OV-0.1 Baseline.** Full unit suite on the current versions; save the pass/fail list in
  `agent_runs/ov0_baseline_<stamp>.txt`. Done when: the known failures (if any) are written down. Result: 3055 passed, 18 skipped (SMB share tests), 0 failures, 1 warning (Starlette httpx).
- [x] **OV-0.2 Pins.** `pyproject.toml`: raise every minor / patch pin to the latest (prefect 3.9.x, pydantic 2.14.x,
  fastapi, uvicorn, loguru 0.8, pendulum 3.3, humanize, python-multipart, filelock, tenacity; dev: hypothesis, pytest,
  ruff, time-machine); keep wakeonlan 3.3.0, cron-descriptor 1.4.5; add constraints `sqlalchemy<2.1`,
  `websockets<17`; fastapi is 0.150.0 not latest (Prefect 3.9.4 caps `fastapi<0.151`); `uv lock --upgrade` (certifi ≥ 2026.10.19 must be in the lock). Done when: lock resolves, no major
  jumps except those listed, certifi fixed.
- [x] **OV-0.3 Sync + suite.** `uv sync`; full unit suite; fix every failure caused by the upgrade (behaviour, not by
  loosening tests). Done when: suite = baseline or better.
- [x] **OV-0.4 httpx2 for our own calls + TestClient.** Add `httpx2`; `launch.py` and `watchdog.py` health checks
  use it; our tests that patch `httpx.get` / build `httpx.Client` follow; Starlette's TestClient picks up httpx2 (the
  baseline shows its deprecation warning for httpx - it must be gone). `httpx` stays only for Prefect. Test first: the
  existing watchdog / launch health tests point at the httpx2 path. Done when: no project file imports `httpx`
  directly and the Starlette httpx deprecation warning no longer appears in the suite.
- [x] **OV-0.5 Audit lab** with a clean `PREFECT_HOME` (`tools\run_audit_lab.ps1`); fix upgrade-caused failures.
  Done when: green except the known intentional red.
- [x] **OV-0.6 rclone 1.76.** Read the 1.75 / 1.76 changelogs (exit codes, `--use-json-log`, `check`, `--max-delete`);
  replace `deploy\bin\rclone.exe`; rerun the audit-lab cloud waves. Done when: green, changelog notes in the commit.

## WP1 - Database v6 (plan §5.4, D-7)

- [x] **OV-1.1 Schema.** DDL v6: `fy` on `file_entries` (`UNIQUE(fy, relative_path)`), `run_history.fy NOT NULL`,
  `integrity_audits.fy`, `notifications.fy` (nullable), `approvals.fy` required for backup approvals; table
  `overlap_activations`; `SCHEMA_VERSION = 6`; a v5 file is refused with the rename message. Tests:
  `tests/test_ov1_schema_v6.py` (fresh DB has the columns / unique key; v5 file refused; activations never purged).
- [x] **OV-1.2 File catalog scoped.** `upsert_file_entry`, `get_entry`, `prune_stale_synced`, `clear_mode_status`,
  `delete_entries`, `file_count`, `get_cloud_synced_entries`, `get_synced_paths`, `backup_repository.record_sync_results`
  take `fy` (no default). Test: same relative path in two FYs stays independent; pruning / clearing one FY leaves the
  other (R-1 part, G-1).
- [x] **OV-1.3 Run history scoped.** `insert_run` / `start_run_row` / `record_run_history` / `sweep_abandoned_runs`
  write `fy` (sweep reads it from the RUNNING row); readers `last_successful_run`, `last_run`, `previous_run_status`,
  `previous_run_fault_code`, `leg_success_after`, `first_partial_after`, `guard_baseline`, `get_recent_runs`
  (`fy` or explicit `ALL`). Test: one FY's success / failure never answers for the other (R-2, R-6, R-8).
- [x] **OV-1.4 Audits + notifications scoped.** `latest_audit`, `audit_verified_after`, `told_about_leg_since`,
  `sent_in_mode_after`, `last_open_incident`, `record_notification` take / store `fy`. Test: per-FY answers (R-2, R-5).
- [x] **OV-1.5 Callers compile.** Every caller passes the FY of its config (`_effective_fy_prefix(config)`); a missed
  caller fails the suite (no default). Done when: full suite green, behaviour unchanged with one FY.

## WP2 - Active set, activation, sibling check (plan §3, §5.1, §5.2)

- [x] **OV-2.1 Config keys.** `rollover.overlap_enabled` (true), `rollover.early_start_month` (1, bounds 1-3),
  `rollover.activation_alert_after_hours` (24), `rollover.sibling_ignore` ([]); Advanced + risky in
  `config_edit` / `config_help`. Test: defaults, bounds.
- [x] **OV-2.2 `core/fy_overlap.py`.** `run_date` (IST, scheduled time), `next_fy`, `in_window`, `switch_pending`
  (journal `config_switch` step, not follow-ups), `active_folders` (new first, needs an activation row), `folder_view`,
  `folder_for`. Tests: every month; 1 Jan / 31 Mar / 1 Apr; pending 5 days; after switch; overlap off; clock behind;
  JUMP; century wrap; drive-root / nested / UNC; cloud-only placeholder untouched (R-12, R-25).
- [x] **OV-2.3 Activation.** `ensure_overlap_activated(config, run_date)`: direct `check_clock` = OK, parent exists,
  not a link; adopt a canonical folder (no placeholder into a non-empty one) or create without `parents=True` +
  placeholder; durable row; INFO mail `overlap:start:<fy>`; ACTION REQUIRED after `activation_alert_after_hours`.
  Tests: created vs adopted; unproven clock retried + alert after 24 h; parent missing; junction refused (R-15, R-22, R-24).
- [x] **OV-2.4 Missing after activation.** A missing / unreadable early folder fails that FY's legs through the existing
  health check, never recreated. Test (R-15).
- [x] **OV-2.5 NAS folder rule.** `ensure_overlap_nas_folder`: missing → create (no parents) + marker; marker present →
  OK; empty → marker; non-empty without marker → untouched (G5 asks). Rollover `nas_folder` / `source_folder` steps treat
  "exists (+ marker)" as done. Tests (R-21).
- [ ] **OV-2.6 Sibling check** in `rollover_check_flow` replaces ROL-06 (`unprotected_new_fy` removed with its alarm /
  attention item / mail): misnamed FY-looking folder → ACTION REQUIRED; canonical newer FY not active with real files →
  ACTION REQUIRED; past FYs and `sibling_ignore` ignored. Tests: December use, overlap off, `FY 27-28`, `FY2027-28`, past FY.
- [ ] **OV-2.7 Hook-up.** `ensure_overlap_activated` at the start of every backup flow run and of
  `rollover_check_flow`. Test: whichever flow runs first activates; the other is a no-op.

## WP3 - Orchestration (plan §5.3)

- [ ] **OV-3.1 Slot held once per leg.** `_backup_slot` around the whole folder loop; pipelines get `slot_held=True`
  (no nested acquire). Test: no other flow can take the slot between two folders; no deadlock (R-3).
- [ ] **OV-3.2 Folder loop.** `backup(..., fy=None)`: active set once per flow run, new FY first, `fy` must be active
  (else refused), `mode="all"` = cloud loop then LAN loop; one folder's failure never stops the other; the flow still
  raises when any folder failed. Tests (R-16).
- [ ] **OV-3.3 Shared budget + MISSED.** Both folders of a leg share the leg budget; the second folder with no budget
  left is `<LEG>_MISSED` for its FY (fix the `late_s = 0` shortcut). Tests (R-18).
- [ ] **OV-3.4 One NAS power cycle.** First LAN pipeline wakes; all pipelines `allow_shutdown=False`; loop-level
  `finally` makes one shutdown decision with today's rules over all folders. Tests: wake / shutdown counted for
  success, folder 2 MISSED, crash before folder 2, slot timeout (R-4).
- [ ] **OV-3.5 Ids, logs, artifacts.** Run id `<flow run>-<leg>-<fy>`; `run_artefacts` folder
  `<date>_<HHmm>_<leg>_<fy>_<short id>` + parser; Prefect artifact keys per FY. Tests (R-7).
- [ ] **OV-3.6 Early NAS folder in the LAN loop** (after wake, before the first pipeline) via OV-2.5.

## WP4 - Alerts, approvals, closed FY (plan §5.4)

- [ ] **OV-4.1 AlertEvent.fy + keys.** `backup:<fy>:<leg>:<status>`, `blocked:`, `missed:`, `recovered:`, `stale:`,
  `audit:`, abandoned-run key with FY; `[FY27-28]` in `email_compose.compose` and the notify fallback subject;
  `nas_shutdown:` / `watchdog:*` stay global. Tests: subjects, keys (R-2).
- [ ] **OV-4.2 Incidents per FY.** Closure, RECOVERED, cooldown, threading per (mode, fy). Test: FY27-28 success never
  closes / recovers an FY26-27 incident (R-2).
- [ ] **OV-4.3 Approvals per FY.** Supersede per (leg, fy); `evaluate_approval` requires `approval.fy == run fy`;
  the approved re-run passes `fy`; closed-FY approvals unusable. Tests: the R-1 scenario (500 vs 400 deletes) is
  refused (R-1 BLOCKER).
- [ ] **OV-4.4 Closed FY at the switch.** Pending approvals → CLOSED "FY closed"; open incidents closed silently;
  the final-backup G3 → G6 two-approval path works. Tests.

## WP5 - Guards (plan §5.5)

- [ ] **OV-5.1 Small folders.** Keys `guards.small_folder_files` (100), `guards.small_folder_max_deletes` (10); rule
  inside `max_delete_allowance` (G3, rclone backstop agrees) and the G2 check. Tests: 9 files + 1 rename passes;
  99 files + 11 deletes blocks; 100-file boundary; `--max-delete` matches (R-14, A-1).
- [ ] **OV-5.2 First-run deletes.** No baseline for (leg, FY) and planned deletes > 0 → DECISION. Tests: filled
  `gs://bucket/FY27-28` and NAS folder are not trimmed unasked.

## WP6 - Overdue, attention, watchdog (plan §5.6)

- [ ] **OV-6.1 Overdue per (leg, FY)**; early FY's base = `activated_at`. Test: no red / mail on 1 Jan before the
  first run (R-5).
- [ ] **OV-6.2 Attention + watchdog codes** `stale:<fy>:<leg>`, `approval:<fy>:<leg>`, `last_run:<fy>:<leg>`;
  watchdog parser and suppression per FY; closed FYs never appear. Tests (R-5, R-8).

## WP7 - Dashboard and settings (plan §5.7)

- [ ] **OV-7.1 `/status` `folders[]`** (old top-level fields kept). Test: one FY = today's payload + `folders[1]`.
- [ ] **OV-7.2 Cards** in `static/js/dashboard.js`: one per FY, role label, "backed up since".
- [ ] **OV-7.3 Per-FY triggers** `?fy=` on cloud / lan / verify; FY must be active; refused while any backup or audit
  run is active; "running" per (leg, FY) from the RUNNING row. Tests.
- [ ] **OV-7.4 Rollover card** shows the active set; Settings keys from OV-2.1 / OV-5.1; texts that say "NOT backed
  up until the switch" reworded (R-13).

## WP8 - Integrity audit (plan §5.8)

- [ ] **OV-8.1** Loop the active set, new first, one NAS wake / shutdown, shared `audit_max_hours`, `fy` on rows and
  alerts, `_pending_context` per FY. Tests (R-9).

## WP9 - Reports (plan §5.9)

- [ ] **OV-9.1** Sections per FY; scheduled counts from `max(period start, activation)` to `min(period end, switch)`.
  Tests: first January weekly, December monthly sent 1 Jan, March monthly, 30 Mar-5 Apr weekly (R-10).

## WP10 - Tools, readiness, validator (plan §5.10)

- [ ] **OV-10.1** `upload_fy.py` / `restore.py` refuse every active FY. Tests (R-20). (Built on top of the owner's
  restore / upload work - coordinate before touching those files.)
- [x] **OV-10.2** Readiness probes: create + delete a test folder in `parent(source_drive)` and the NAS share root as
  the service account (`deploy/probe_service_account.py`, `04_check_readiness.ps1`). Tests (R-17).
- [ ] **OV-10.3** Validator: NAS FY folder directly at a share root refused while LAN + overlap are on. Test (R-25).

## WP11 - Lab, docs, trial box

- [ ] **OV-11.1** Audit-lab scenario: 15-month fake clock (Dec → Jan activation → Apr switch → next Jan), both legs,
  the audit, real robocopy / rclone. Green.
- [ ] **OV-11.2** `docs/ARCHITECTURE.md` (flow, gates, decision points) and `SESSION_HANDOFF.md` updated.
- [ ] **OV-11.3** Trial box: folder-creation rights test (create / delete a test folder next to `E:\FY26-27` and in
  the NAS share root as the service account).
- [ ] **OV-11.4** Trial-box deploy: no backup running; stop watchdog → agent; rename `manifest.db` + `-wal` / `-shm`;
  deploy (git bundle); start agent → watchdog; `/health`; watch the next LAN and cloud runs.
- [ ] **OV-11.5** Box overlap rehearsal: a rehearsal script under `tools/` (NOT production code, no date switch in
  config) that runs one cloud + one LAN flow on the box with the run date patched to a January date, proving the
  two-card dashboard, the INFO mail and both folders in the cloud and on the NAS; test folders / prefixes removed
  afterwards; the system clock is never changed.

## WP12 - GCS lifecycle (plan D-13)

- [ ] **OV-12.1** Lifecycle rules: never ARCHIVE an active FY prefix (current + early); keep the noncurrent-version
  rules; review with the owner (the owner's staged variant is in the session scratchpad); apply with
  `gcloud storage buckets update --lifecycle-file`; verify with `gcloud storage buckets describe`.

---

## Found while building (raise with the owner before building)

- **WP0 Opus review (2026-11-20), no blocker. Carried forward:**
  1. OV-11.4 (box deploy) must first back up the Prefect server DB (`<PREFECT_HOME>/prefect.db` + -wal/-shm, services stopped):
     Prefect 3.7.2 -> 3.9.4 runs alembic migrations at server start and there is no way back without a copy (plan WP0 says
     "Prefect server DB backed up first"; no script does it).
  2. rclone 1.76.0 is proven in the lab only on `type = local` remotes (`audit_lab/labconf.py`). The real GCS backend and the
     DPAPI key env path (`core/gcs_key.py`) are NOT proven on 1.76: run `04_check_readiness` / `deploy/probe_service_account.py`
     (cloud bucket check) on the box with 1.76.0 at OV-11.3/11.4, and watch the first real cloud run. Also not covered by the
     lab waves: `core/upload.py` / `core/restore.py` use `--exclude` and `--max-duration` (owner's tools).
  3. Transitive majors that came with Prefect 3.9.4 (beyond OV-0.2's commit text): apprise 1->2, cyclopts 4->5, redis 7->8,
     python-slugify 8->9, pywin32 311->312, ruamel-yaml-clib dropped. `prefect server start --host/--port` still works (checked).
  4. httpx2 2.16.2 was published 2026-11-16 (4 days old) and adds httpcore2, truststore, httpx2-jsfetch: owner informed.
  5. Pre-existing: 9 SyntaxWarnings (invalid escapes) in old tests; `test_launch_comprehensive` + `test_p2_sched_pause` fail when
     run as only those two files in that order (same on old code).
- **OV-2.3..2.5 findings (2026-11-20), to decide with the owner:**
  1. A link / junction at an early folder is refused only AT ACTIVATION. After activation `health.source_drive_fault` only checks
     exists()/iterdir(), so a folder later replaced by a junction would not fail that FY's legs (plan 5.1 says it should).
     Needs a small link check in pre-flight for early FYs; not built (it would also change behaviour for the configured FY).
  2. The clock-unproven activation alert reuses fault code AAM-ROL-03 ("switch waits"); parent-missing / link deferrals have no
     fault code. A dedicated AAM-ROL-07 + a row in docs/EMAIL_CONTENT_SPEC.md would be cleaner.
  3. Old fault text AAM-ROL-06 ("Files in the new year are not backed up yet") no longer fits once OV-2.6 removes that alarm.
