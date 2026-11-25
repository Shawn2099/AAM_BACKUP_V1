# Brief WS-E: the 2027-01-03 audit failure (lane a2y26) and the P6 wake-per-pass defect

Paste this whole file to a fresh agent. Master plan: `docs/PLAN_MASTER_PARALLEL_2026-11-25.md`. **Never touch the box in this workstream.**

## Context
Product: nightly backup automation (rclone → GCS, LAN mirror → NAS, weekly integrity audit Sunday 08:00, FY overlap from
31 Dec). The live-box harness lane A2 (`a2y26`, 728 simulated days on branch `baseline-v1-overlap`) had 5 FAILs, all on
Sunday 2027-01-03: integrity audit `ExceptionGroup` (2 sub-exceptions), LAN audit `VERIFICATION_FAILED` x2, one
"ACTION NEEDED ... (AAM-AUD-01)" mail. An analysis exists (untracked) at
`C:\Users\malliath\Desktop\aam_baseline_overlap\docs\REPORT_LIVEBOX_A2Y26_2026-11-25.md`. Its verdict: **harness artefact**.
`tools/livebox/churn.py` `sim_timestamp(day)` stamps every churn file at NOON of the simulated day, but `apply_day` writes
the files at the start of the day (before 02:01). The dated event `shared_add` on (1, 3) runs even on an idle Sunday. At the
08:00 audit the file has a "future" mtime (12:00 > 08:00 + 2 s), so `core/audit_pending.py` `_new_file_is_pending`
(lines ~101-111) correctly refuses "pending backup" and fails closed. Other Sundays pass because churn is idle. The same
churn code is on `rclone-engine` (diff empty). The report also confirms **P6**: `flow.py` `_run_lan_pipeline` calls
`wol_check_task(config)` on every FY pass (rclone-engine `flow.py` ~2050), so on overlap nights the NAS is woken twice
(plan D-8 says once).

## Rules (owner, binding)
- Never touch the trial box or the real NAS (no ssh at all here). Never power off this PC. No reboot/shutdown.
- No product fix committed on `baseline-v1-overlap` without the owner's go (OD4). E4 product fix on `rclone-engine` needs OD5.
- Test first by a different agent (show red on old code); the code agent never edits those tests.
- Reliability first: the audit's fail-closed rule for future mtimes is CORRECT; do not loosen `core/audit_pending.py`.
- Commit per package with its ID; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Never bare `git stash`. GitNexus `impact` before editing a symbol.
- Escalation: Haiku 2 attempts → Sonnet; Sonnet blocked → Opus. Scope guard on `git diff --stat`.
- Model split: Opus verifies the analysis and reviews; Haiku writes tests; Sonnet writes the fix.

## Read first
The report above; `aam_baseline_overlap/docs/LIVEBOX_HANDOFF_2026-11-25.md`; `aam_baseline_overlap/docs/LIVEBOX_TEST_PLAN_2026-11-24.md` §3 (churn model) and §2 (calendar); code `tools/livebox/churn.py` (`sim_timestamp`, `apply_day`, dated events), `tools/livebox/run_lane.py` (when churn is applied in the day loop), `core/audit_pending.py`, `core/integrity.py` (~366), `flow.py` `_run_lan_pipeline`, `backup()` FY-pass loop, `integrity_audit_flow` `_wake_nas_once()` (the pattern to copy).

## Packages
| ID | Goal | Where | Files | Rows |
|---|---|---|---|---|
| E1 | Opus verifies the report against code (not the box): churn apply time vs `sim_timestamp`; `audit_pending` rule; that every churn op (not only `shared_add`) is stamped in the future relative to the 02:01 cloud run; whether other oracles relied on noon. Writes rows E-R1..E-R4 into `docs/PROGRESS_WS-E_2026-11-25.md`. | read only | doc | - |
| E2 | Harness fix on baseline: churn files get an mtime equal to the simulated instant they are written (e.g. 00:30 of the day, before the first job), never later than the next job that reads them; tests: E-R1 every churn op's mtime <= the sim time of the first scheduled job that day; E-R2 a Sunday dated event (2027-01-03) followed by the 08:00 audit before the 21:01 LAN classifies the new file `PENDING_BACKUP` (uses real `audit_pending.classify`). | `baseline-v1-overlap` (`..\aam_baseline_overlap`), owner go OD4 before commit | `tools/livebox/churn.py`, `tools/livebox/tests/test_churn_mtime.py` | E-R1, E-R2 |
| E3 | Product regression test (no product change): E-R3 `audit_pending` keeps failing closed for a file whose mtime is later than audit time + modify window (already fail-closed; pin it if not pinned in `tests/test_audit_pending*.py`). Port E2 to `rclone-engine` through WS-D D0 (cherry-pick), not directly. | `rclone-engine` (new test file only) | `tests/test_e3_audit_future_mtime.py` | E-R3 |
| E4 | P6 fix: WoL/wake once per LAN flow (first pass only; later passes reuse the result; a failed wake on pass 1 still fails every pass as today). Test E-R4: two-FY overlap `backup(mode="lan")` calls `wol_check_task` exactly once; single-FY unchanged; shutdown still once after the last pass. | `ws-e-p6` off `rclone-engine` after G1 (flow token, OD5) | `flow.py` (~20), `tests/test_e4_wake_once.py` | E-R4 |

A local short-lane rerun is optional: only if `tools.livebox.run_lane` can run on the dev PC without the box (check `--help`; safety S1 expects `C:\AAMBackup`); otherwise the window 2026-12-27..2027-01-10 is re-run on the box later by WS-D (owner go).

## How to run tests
```
cd C:\Users\malliath\Desktop\aam_baseline_overlap        (E2)   |   cd C:\Users\malliath\Desktop\aam_ws_e   (E3/E4)
$env:PYTHONPATH='.'; $env:PREFECT_HOME="$env:TEMP\aam_ph_wse"; Remove-Item -Recurse -Force $env:PREFECT_HOME -ErrorAction SilentlyContinue
$py='C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe'
& $py -m pytest tools/livebox/tests tests/test_review2_driver.py tests/test_review2_rig.py -q -p no:cacheprovider -o addopts="" --deselect tools/livebox/tests/test_wire_injections.py --basetemp="$env:TEMP\aam_bt_wse"
& $py -m pytest tests/test_e3_audit_future_mtime.py tests/test_e4_wake_once.py tests/test_audit_pending.py -q -p no:cacheprovider -o addopts="" --basetemp="$env:TEMP\aam_bt_wse"
& $py -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600 -m "not slow" --basetemp="$env:TEMP\aam_bt_wse"   (after E4)
```

## Definition of done
E1 verdict written (confirm or reject the report, with code lines); E2 committed on baseline after OD4 (or left as a patch file in `agent_runs/` if the go is not given); E3 green; E4 green + fast loop green, Opus mutation check (wake gate `index == 0` → always) turns E-R4 red.

## Report back (<= 15 lines)
Verdict (harness / product), commits (branch + sha), rows, whether other churn ops were affected, whether the baseline needs a box rerun, P6 fix status.
