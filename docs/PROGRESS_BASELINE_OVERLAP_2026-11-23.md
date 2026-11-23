# Progress and handoff - FY overlap on the baseline (written 2026-11-23)

Read this first when resuming. Binding plan: `docs/PLAN_BASELINE_OVERLAP_2026-11-23.md` (rev 2 FINAL, tasks A1-D3). Project rules: `CLAUDE.md`.

## Setup facts
- Branch `baseline-v1-overlap`, cut from tag `baseline-v1` (9461ac2 = version on the trial box). Worktree: `C:\Users\malliath\Desktop\aam_baseline_overlap`. The main checkout `C:\Users\malliath\Desktop\aam_backup` is on branch `fy-overlap` (older, heavy attempt; reference only, never edited by this work).
- Build and test ONLY on this PC (owner, 2026-11-23). The trial box (`ssh innoviztawinserver2016`) is untouched until the owner says we are confident. Push to git only when the owner asks; no secrets scan needed (owner: all passwords are fake and will be rotated).
- No `.venv` in the worktree. Run tests with `C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe -m pytest <files> -q -p no:cacheprovider -o addopts="" --timeout=600` with cwd = the worktree and `PYTHONPATH=.`. Full suite about 5.5 min.
- Known failures that are NOT ours: `tests/test_v113_round3.py::test_r3_repo_config_timeouts` and `::test_r3_loaded_runtime_timeouts` (need the untracked real `config.yaml`). `tests/test_m5_reports_plain.py::test_fy_switch_sends_an_info_email` failed once on untouched code and passed afterwards (flaky, cause not found).
- GitNexus: the worktree is indexed (`node .gitnexus/run.cjs analyze --index-only`; re-index after big changes; `status` may read "stale" because of a runner mismatch, harmless). Run `impact` before editing and `detect-changes --scope all --repo .` before each commit.
- Model split (owner): Haiku builds, Sonnet fixes after testing, Opus only verifies and decides. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (project CLAUDE.md rule; early commits carry the Haiku line).

## Owner decisions (all recorded)
- Real layout: source `E:\FY26-27`, `E:\FY27-28` (drive root parent); NAS `\\nas\backup\FY26-27`, `\\nas\backup\FY27-28`.
- Activation 31 Dec: create next FY source folder (+ placeholder); NAS folder is made by the first next-FY LAN pass after the NAS is awake. Config switch only on 1 April.
- From 1 Jan both FYs are backed up on schedule: cloud FY26-27 then cloud FY27-28, LAN FY26-27 then LAN FY27-28. Manual: four cards (cloud/LAN x two FYs), verification per FY.
- Overlap ON by default in code and every config (a config without the key counts as on).
- April gate: required = every switched-on leg; a "final copy" = successful run of the OLD FY started on or after 1 April 00:00 IST (cloud COMPLETE/NO_CHANGES_COMPLETE after verify, LAN LAN_COMPLETE). Manual run counts. When the last blocker clears the software rolls over by itself on the next daily check. Forced rollover (typed phrase) stays. Both FYs keep being backed up until then.
- Manual-only legs in scope: `cloud.scheduled` / `lan.scheduled` (default true); unscheduled leg works from the UI, no overdue/watchdog alarms, needs one manual final copy after 1 April; cloud runs normally when LAN is disabled or unscheduled.
- Weekly audit covers both FYs in the existing 8 h budget (no new key). Activation needs a proven clock; waits and mails (AAM-ROL-07) if not.
- Log file names of a next-FY pass carry the FY (`..._lan_FY27-28_robocopy.log`), not only the folder.
- No database schema change (user_version stays 5); the next FY's pass is told apart by an FY suffix on run/audit ids.

## Done (commits on `baseline-v1-overlap`)
| Task | Commit | Notes |
|---|---|---|
| plan | b6a19c8, 70cc0ff | plan rev 1, rev 2 FINAL |
| A1 | 640c453 | keys `rollover.overlap_enabled`, `overlap_activate_on` |
| A2 | 55e9041 | `core/fy_overlap.py` pure helpers, `ensure_nas_folder` |
| A3 | ef88f64 | run-log folder name carries FY |
| A4 | 87d417d | DB `fy=` filters, `final_copy_since`, approvals `keep_fy`, overlap/scheduled records |
| A5 | 72d1013 | alerts per FY (`AlertEvent.fy`), watchdog `stale:leg:FY`, AAM-ROL-07 |
| A6 | 9a83294 | `cloud.scheduled`/`lan.scheduled`, `FINAL_COPY_STATUSES` |
| A3b | 13518e5 | FY in run-log file names |
| B1 | c805230 | activation on 31 Dec in `rollover_check_flow` (3 rollover fixtures set `overlap_enabled` false with comments) |
| B2 | bfd5bb2 | pipelines take one FY pass (`FyPass`, per-pass config copy) |
| B3 | 243602a | `backup()` loops FY passes per leg |
| B2 fix | d72f872 | alert keys keep a literal prefix |

Suite after B3: 3229 passed, 2 failed (known), 19 skipped.

## Review fixes landed (Sonnet): eab0aad F1, a423cfb F2, 27758c3 F3, 15cd758 F5, 86a18fd F6, 2e3c00f F7
Suite: 3239 passed, 2 failed (known), 19 skipped. F4 (NAS shutdown when the last LAN pass is missed/skipped) deliberately NOT implemented: a shutdown from `backup()` would run outside the slot and could power the NAS off under a concurrent manual run; failure direction is safe (NAS stays on until next night). Noted in the plan, section 12 - OWNER DECISION pending whether a slot-guarded shutdown-only step is wanted.

## (historic) What was in progress when this file was first written
Sonnet fixer for the Opus review of B2/B3 (verdict PASS WITH FIXES), commits `fix(overlap): B3 review F1..F7`:
- F1 second pass shares the first pass's budget origin when a run starts late (D-7).
- F2 a database error while reading the overlap record must not stop the backup (run configured FY only, end FAILED with a note).
- F3 race guard (config switched mid-run) must end the flow FAILED, not COMPLETED.
- F4 NAS shutdown gap when the last pass is missed/skipped/fails early (fakes only; if unsafe, record under "Found while building").
- F5 `_MISSED`/`_SLOT_TIMEOUT` rows of a next-FY pass carry `fy`/`overlap` metrics.
- F6 one `fake_run_tool` test proving next-FY command lines (`E:\FY27-28` -> `gs://bucket/FY27-28`, `\\nas\backup\FY27-28`).
- F7 abandoned-run sweep alert (`core/backup_repository.py`) carries the FY.
Check `git log --oneline` for which of these landed.

## B4 done (April 1 final-copy rule)
- Owner decision F4 (2026-11-23): the NAS shutdown gap is ACCEPTED - the NAS stays on for a day; no shutdown step (plan section 12 says so).
- `core/fy_overlap.py`: `overlap_open`, `final_copy_state` (per leg: required, scheduled, done, source check/earlier,
  status, outstanding, hint, warning), `outstanding_legs`, `announce_text`, `end_overlap`.
- `flow.py`: `_run_final_backups(skip_unscheduled=, started=)`; `step_final_backups` uses the per-leg state only while the
  overlap is open (baseline call otherwise); forced legs named in the switch mail and the rollover row; `end_overlap`
  after the switch.
- `core/guards.py` `g6_fy_rollover(overlap_active=, hints=)`: text only, verdicts identical (tested).
- `core/fy_rollover.py` `unprotected_new_fy`: silent while the overlap of the next FY is open (AAM-ROL-06).
- `ui.py` `/api/rollover`: `final_copy` while the overlap is open.
- Tests: `tests/test_fy_overlap_rollover.py` (23 cases).

## Opus review findings kept as facts
- Verified: baseline path identical with overlap off (except F2); no data-loss path found; every `config.paths.*` use in the pipelines reads the per-pass config; next-FY pass writes no `file_entries`; the refusal when the next-FY NAS folder name is not its FY is the last barrier against a mirror into `\\nas\backup` (keep it); an FY27-28 success cannot mark FY26-27 healthy or send its recovery mail.
- NOT safe to deploy past 31 Dec until B8 and B9 are done: `report.py` (~317) and the dashboard night strip (`ui.py` ~600) still mix both FYs' rows.
- Builder deviations accepted: pipeline tests patch tasks; one WoL achieved through the `ensure_server_online` probe (not run on a real NAS); approval pre-check `_approval_of_pass`.

## Remaining tasks (plan section 9)
B4 final-copy state machine (April gate, auto rollover when blocker clears, forced rollover, silence AAM-ROL-06 while overlap active, G6 text keyword) - B5 calendar simulation Dec 30 - Apr 3 incl. Feb 29 2028 - B6 integrity audit per FY - B7 attention/overdue/watchdog - B8 reports - B9 dashboard API + UI (four cards, `?fy=` triggers, `/status` overlap block) - C1 registration of unscheduled legs (probe on a local Prefect server with clean `PREFECT_HOME`) - C2 manual-only behaviour - D1 docs (ARCHITECTURE.md, SESSION_HANDOFF.md) - D2 targeted audit lab test with real robocopy/rclone on a sandbox - D3 trial box (only when the owner says; tell the owner before any restart).
After every Part: full unit suite, then an Opus review of the diff before the next Part.

## Found while building
(append here; also keep the plan's own section up to date)
- Pipeline tests patch tasks instead of `fake_run_tool` (see F6).
- The unprotected-new-FY alarm (AAM-ROL-06) still fires for office files in the next-year folder until B4 silences it while overlap is active.
