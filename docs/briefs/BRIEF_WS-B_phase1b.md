# Brief WS-B: Phase 1b (copy-only on block, approval reminders, `--backup-dir` versions, space, notify fixes)

Paste this whole file to a fresh agent. Master plan: `docs/PLAN_MASTER_PARALLEL_2026-11-25.md` (§3 file tokens, §6 parallel safety).

## Context
Nightly backup automation for an accounting firm on Windows Server 2016: rclone to GCS (cloud leg) and rclone to a NAS over
SMB (LAN leg, branch `rclone-engine`; robocopy removed in P1). Prefect 3 flows in `flow.py`; business logic in `core/`;
config in `models/config.py` (pydantic `StrictModel`, `DEPRECATED_KEYS`). Data guards G1-G5; G3 = mass-delete guard; a G3
block creates an approval the operator grants on the dashboard. Phase 1b is owner-approved (2026-11-25):
- On a **G3-only** block run `rclone copy` (same flags/excludes) instead of `sync`: new and changed files still arrive, no deletions; status stays `*_BLOCKED`, row says "copied N, deletions held". G1/G4/G5 blocks still stop everything. Both legs.
- Never auto-approve. Pending approval reminders follow a ladder: day 1, 2, 4, 7, then weekly. Dashboard shows "N deletions waiting, D days".
- `--backup-dir <share>\AAM_versions\<YYYY-MM-DD_HHmm>_<FY>\` per RUN and per FY pass, same share, outside the mirror root; deleted AND replaced files (also with `copy`). Retention 91 days from the run-folder date, purge `rclone delete --min-age 91d` + `rmdirs`. Keys `lan.versions_enabled`, `lan.versions_folder`, `lan.versions_retention_days` (default 91, bounds 7..3650). Free-space check counts the folder; weekly report shows its size.
- An approval covers only the count it was granted for (`evaluate_approval`); a bigger plan blocks again.
- Notify fixes deferred from P1: **P1** escalation mail suppressed by the 24 h cooldown on early start; **P7** two near-identical PROBLEM mails 31 s apart; **P8/R2** activation-blocked WARNING mailed as "ACTION NEEDED". Proposed semantics in master plan OD8.

## Rules (owner, binding)
- Reliability first, no guesses; fail closed; never auto-proceed; ask the owner when an action could lose data.
- Every tunable is a config key with default + bounds + `core/config_help.py` entry, added in the same commit as the code reading it.
- Reuse building blocks: `core/statuses.py` (only place defining statuses), `core/tool_runner.run_tool` (only subprocess wrapper), `core/run_artefacts.py`, `ManifestDB.from_config`, `core/notify.py` (single alert path). No new dependencies without owner approval.
- Test first by a different agent; tests frozen for the code agent. Do not monkeypatch a Prefect `@task` with a plain function (patch `.fn` or a MagicMock with `.with_options`). Tests never use the real `config.yaml`.
- Never touch the real NAS or the trial box. Never power off this PC. No reboot/shutdown.
- GitNexus `impact` before editing a symbol; `detect_changes --scope all` before committing.
- Commit per chunk citing the ID; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Never bare `git stash`.
- Escalation: Haiku 2 attempts → Sonnet; Sonnet blocked / interface change → Opus. Scope guard: `git diff --stat` within scope; no weakened or removed asserts, no new skips.
- Model split: Opus writes the matrix and reviews; Haiku writes tests and runs loops/docs; Sonnet writes code and debugs.

## Read first
`CLAUDE.md`; `docs/PLAN_P1_RCLONE_LAN_2026-11-25.md` (§2 flow, §2.1 flags, §4 guards, "Phase 1b" section with V1-V12, owner decisions); `docs/TESTMATRIX_P1_REVIEW_2026-11-25.md` (V13 if defined there, and any interface WS-A changed); `docs/ARCHITECTURE.md` §10 gates; `docs/PLAN_ALERT_CONTROLS_2026-10-22.md` (alert classes, follow-up limits); `docs/EMAIL_CONTENT_SPEC.md`; code `core/lan_sync.py`, `core/guards.py`, `core/approvals.py`, `core/notify.py`, `flow.py` (`lan_guards_task`, cloud guard task, `_run_lan_pipeline`, `_run_cloud_pipeline`), `core/permissions.py`, `core/report.py`, `ui.py`.

## Packages (in order; each = one commit of tests, one of code)
| ID | Goal | Files (approx lines) | Rows | Starts |
|---|---|---|---|---|
| B0 | Opus: `docs/TESTMATRIX_P1B_2026-11-25.md` (V1-V13 made concrete: inputs, expected, layer; N1-N6 ladder: day 0 first mail, 1, 2, 4, 7, 14, 21...; no mail between rungs; a granted/closed approval stops reminders; restart-safe because it reads the DB; P1/P7/P8 rows). Define where the ladder runs (proposal: the daily 06:00 `rollover_check_flow` or the LAN pre phase; decide from code) and the dashboard API field names. | doc | - | NOW |
| B1 | Notify fixes P1, P7, P8/R2 | `core/notify.py` (~60), `tests/test_b1_notify_fixes.py` | P1/P7/P8 rows | after B0 + OD8 (may run before G1 if WS-A does not touch notify.py) |
| B2 | Keys `lan.versions_*`, reminder key (e.g. `approvals.reminder_days` = [1,2,4,7], weekly after), validator: versions folder on the same share as the mirror and NOT inside the mirror root (V10) | `models/config.py`, `core/config_help.py`, `config.example.yaml`, `core/config_edit.py` (~80) | V10, K rows | after G1 (config token) |
| B3 | Builder: `build_lan_rclone_command(..., mode="sync"/"copy", backup_dir=...)`; run folder name from run start + FY; same argv invariants (dry-run = sync + `--dry-run`). Measured facts (`TESTMATRIX_P1_REVIEW` X4/X5, V13): with `--backup-dir` a deletion is `"skipped":"move into backup dir"`, a replace is `"skipped":"move"` + `"copy"`; `--max-delete` counts backup-dir deletions, not replace moves; `stats.renames` includes backup moves, so metrics `moves` must come from the plan's `move to` count | `core/lan_sync.py` (~60) | V6, V7, V8 (argv), V13, M rows still green | after B2 |
| B4 | Copy-only: guard result says "G3 only" → pipeline runs copy, status BLOCKED, row/metrics "deletions held N"; cloud same with `rclone copy` | `flow.py`, `core/guards.py` (~120) | V1, V2, V5 | after B3 (flow token after E4) |
| B5 | Reminder ladder + dashboard count/age | `core/approvals.py`, `flow.py`, `ui.py`, `static/js/dashboard.js` (~120) | V3, N1-N6 | after B1, B4 |
| B6 | Versions purge job (pure decision + `run_tool` call), never touches the mirror root | new `core/lan_versions.py`, `flow.py` hook (~100) | V9 | after B3 |
| B7 | Space accounting (needed bytes include versions growth = sizes of the dry-run's `"skipped":"move"` (replaced) and `move into backup dir` entries, V13) and weekly report line | `core/permissions.py`, `core/report.py`, `flow.py` (~60) | V11, V13 | after B6, OD9 |
| B8 | Real-rclone lab: local disk → `\\127.0.0.1\C$` under `AAM_LAB_ROOT`; deletes + replaces land in the run folder with identical paths and MD5; V4 approve after 40 simulated nights (flow level, fake clock) | new `audit_lab/test_w26_versions_lab.py` (do not edit labconf/conftest; ask WS-C) | V12, V4 | after B7 |

## How to run tests (worktree has no .venv)
```
git worktree add C:\Users\malliath\Desktop\aam_ws_b -b ws-b-phase1b rclone-engine    (once)
cd C:\Users\malliath\Desktop\aam_ws_b
$env:PYTHONPATH='.'; $env:PREFECT_HOME="$env:TEMP\aam_ph_wsb"; Remove-Item -Recurse -Force $env:PREFECT_HOME -ErrorAction SilentlyContinue
$py='C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe'
& $py -m pytest tests/test_b1_notify_fixes.py -q -p no:cacheprovider -o addopts="" --basetemp="$env:TEMP\aam_bt_wsb"
& $py -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600 -m "not slow" --basetemp="$env:TEMP\aam_bt_wsb"
& $py -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=1200 -m slow --basetemp="$env:TEMP\aam_bt_wsb_slow"
$env:AAM_LAB_ROOT="$env:TEMP\aam_lab_wsb\lab"; & $py -m pytest audit_lab/test_w26_versions_lab.py audit_lab/test_w25_rclone_lan.py -q -s --timeout=1800 -p no:cacheprovider -o addopts=""
C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\ruff.exe check <changed files>
```
Known environment reds: `test_v113_round3::test_r3_repo_config_timeouts`/`::test_r3_loaded_runtime_timeouts`, `test_review2_driver.py` on a dirty tree.
Before taking a token: `git rebase rclone-engine` after the previous holder merged.

## Definition of done (gate G2)
All V1-V13, N1-N6, P1/P7/P8 rows green; fast loop + slow file green; B8 lab green; ruff clean on changed files; Opus review with mutation checks (copy-vs-sync selection on G3-only vs G1/G4/G5, ladder rung list, `>=` on 91 days, versions-inside-mirror refusal, space sum) each turning a test red; findings in `docs/PROGRESS_WS-B_2026-11-25.md`; new mail keys listed for WS-G (EMAIL_CONTENT_SPEC).

## Report back (<= 20 lines)
Commits per package, rows covered, suite counts, mutation results, new config keys, new mail keys, any interface you needed from WS-A, open owner questions.
