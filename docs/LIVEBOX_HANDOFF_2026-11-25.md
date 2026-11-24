# HANDOFF (end of 2026-11-25 session) - START HERE IN A NEW SESSION

Read after this: the addendum `LIVEBOX_TEST_PROGRESS_2026-11-25_ADDENDUM.md` (box facts, smoke results, findings P1-P8), the STATE block of `LIVEBOX_TEST_PROGRESS_2026-11-24.md`, and `LIVEBOX_TEST_PLAN_2026-11-24.md`.

## Where things are
- These docs and the harness both live on branch `baseline-v1-overlap` (worktree `C:\Users\malliath\Desktop\aam_baseline_overlap`, `docs/`). They are NOT the fy-overlap build (`C:\Users\malliath\Desktop\aam_backup`); never mix the two branches.
- Harness code: worktree `C:\Users\malliath\Desktop\aam_baseline_overlap`, branch `baseline-v1-overlap`, pushed, last commit 3d4127b (722 dev tests pass). Run tests from that worktree:
  `C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe -m pytest tools/livebox/tests tests/test_review2_driver.py tests/test_review2_rig.py -q -p no:cacheprovider -o addopts="" --deselect tools/livebox/tests/test_wire_injections.py`
- Product under test on the box: `C:\AAMBackup` @ dc50265 (baseline-v1-overlap), runtime `C:\BackupAgent`. Never change product code during testing. Do not disturb the live schedule (LAN 21:01, cloud 02:01, rollover 06:00 IST). No reboot/shutdown; never power off the NAS. Do not commit product fixes without the owner's go.
- Box harness copy is OLD (pre-fixes): `C:\AAMLivebox\harness\tools\livebox` (only rig.py has the loguru fix). The new harness (commits a00001c, 68d067b, 3d4127b) is NOT yet on the box.
- Lane A (stamp a728, start 2090-12-01, 728 days, OLD code) was running, ETA ~01:00 box time 2026-11-25. Evidence `C:\AAMLivebox\a728\A\evidence`; stdout/err `C:\AAMLivebox\smoke_a728_out.txt` / `_err.txt`. A Haiku agent was to check results, copy evidence to `C:\AAMLivebox\evidence_keep_a728`, verify production unchanged, then run cleanup (`--cleanup A --stamp a728 --root C:\AAMLivebox`). If the session ended first, do those by hand (read-only first).

## Built and ready for the NEXT run (local + pushed, not yet on the box)
- `box_run.ps1` detached runner (start/status), `progress.json` heartbeat, `flow_state`/`exc_type` per event, FY derived from `--start` (2026 start works), `env_versions.json` in evidence, SPY_AUDIT expectation follows `lan.shutdown_after_backup` (sandbox has it false), loguru 0.7/0.8 patch.
- Scenarios in `tools/livebox/scenarios/variants.py` (`--scenario tools.livebox.scenarios.variants:<ID>`): X01b (3 nights no internet), X01c (DNS down), X04c (`--cloud-only`), X04l (`--lan-only`), X06w (outage + 3 watchdog ticks per midnight), X08b (one wake-fail night; first live run may show a RESOLVED-mail mismatch the calendar does not model). X05 already works with `--cloud-only`/`--lan-only` (`tools.livebox.scenarios.w1:X05`).
- NOT wirable: X17, X39 (flows run inside the driver process; need a real-process lane). X42 gated. X37-X38, X45-X48 NOT_RUN. A real `lan_shutdown_task` variant is feasible (~half a day, weakens S9): deferred.

## Steps for the next run (in order)
1. Confirm lane A finished (events.jsonl ends, exit file, no `run_lane` python process). Do NOT copy files to the box while a lane runs.
2. Copy `tools/livebox` (excluding `overlay/`) from the worktree to `C:\AAMLivebox\harness\tools\livebox` on the box (pipe python writer scripts / .ps1 files over ssh; Git Bash mangles backslashes; no scp). Verify sha256 of rig.py, run_lane.py, box_run.ps1 match. Copy `box_run.ps1` also to `C:\AAMLivebox\`. Keep `overlay\time_machine_pkg` on the box as is.
3. Test the launcher: `box_run.ps1 -Action start -Name t1 -RunArgs "--help"`, then `-Action status`; then start a short lane and CLOSE the ssh session to prove it survives.
4. Lane command template: copy `C:\AAMLivebox\run_smoke4.ps1` (s14e, worked) for `--root`, `--code-root`, `--prod-api` (must end in `/api`), `--expected-commit dc50265`, `--overlay`. Fresh lower-case `--stamp` each run. Run with `C:\AAMBackup\.venv\Scripts\python.exe -m tools.livebox.run_lane` from `C:\AAMLivebox\harness`.
5. Lane A2 on the 2026 clock: `--lane A --days 728 --start 2026-10-01` (31 Dec 2026 activation, 1 Apr 2027 switch). Then X01, X02, X04, X05, X34, X35, X09, X44, X24, X40, the new variants, and the A730 retention lane. Model split: Haiku runs/moves, Sonnet codes/fixes, Opus only reviews.
6. Then `python -m tools.livebox.make_report --evidence <lane evidence dirs> --out REPORT.md`; clean up every lane (`--cleanup <lane> --stamp <stamp> --root C:\AAMLivebox`); production-unchanged check (services, schedules, config hash, db).

## Open classification items
- SPY_AUDIT fail x2: CLASSIFIED as a harness expectation bug, fixed in 68d067b. Open owner question: should an audit shut the NAS down when `lan.shutdown_after_backup` is off? (docstring flow.py:3052 says it shuts down after the last pass; code gates on the flag.) Assume no.
- Lane A watchdog "Stale backup" ERROR lines: 1746 (cloud 874, LAN 872; first 2091-04-04, last 2092-05-08; FY91-92 520, FY92-93 145, untagged 1081; "482 h ago" at 2091-04-23 LAN and 2092-04-21 LAN FY92-93). Watchdog FY selection is correct (`fy_overlap.active_fys`). Need lane A run_history/db_dump for 2092-04-02 and April 2091 to say whether runs were really missed (finding) or a harness artefact. It also mails (watchdog.py:419), delivery unconfirmed.
- P1-P8, F-E1, F-OWN-1/2/3/5: owner decides fixes after REPORT.md.

## Realism gaps to state in REPORT.md (Sonnet review 2026-11-25)
Flows are called directly by the driver. NOT exercised: serve.py / `prefect serve` cron scheduling, deployment params, missed-run catch-up, launch.py boot, the watchdog service loop, real WoL/smb_ready, real GCS, real network-outage classification (P2), reboot/restart recovery. Real: Prefect server, robocopy/rclone, SQLite, flows, SMTP on loopback; faked clock. Recommended later: one small real-process lane on the box (real serve.py/launch.py/watchdog, short crons, taskkill restart); it would also enable X17/X39.

## OWNER INSTRUCTION (2026-11-25, after lane A): next run defaults
- Next run (A2 and all later lanes): start from 2026 and use 2-year (730 day) retention everywhere.
  Pass `--start 2026-10-01 --retention-days 730` explicitly (code defaults still 2090-12-01 / box config 90; changing code defaults breaks 7 pinned tests, so do it via args or update those tests first).
- Lane A finding: lane config db_retention_days=90 purged early run_history rows (product default is 730); use 730.
- Add a harness scenario/step that plays the owner and approves AAM-DEL-01 blocks, else one rename blocks a whole year (lane A: Client_000/Invoices rename 2091-04-04 blocked every run after).

## STATE 2026-11-25 ~04:00 IST: lane A2 running (check in a later session)
- Lane A (a728) finished; evidence kept at C:\AAMLivebox\evidence_keep_a728 (box). Findings: docs/LIVEBOX_FINDINGS_AND_FIXES_2026-11-25.md. Opus audit: docs/OPUS_AUDIT_PREFLIGHT_FLOW_2026-11-25.md.
- Product fix committed 0ec61f2 (shared rclone --track-renames mass-delete measurement, LAN robocopy cross-check, owner approver). Harness fixes 4b12c73 (log retention cap 365) and 92a3057 (rig writes start-derived FY). Box: code at C:\AAMLivebox\code_fix (0ec61f2, cloned from GitHub), harness at C:\AAMLivebox\harness (old copy in tools\livebox_old_a728). C:\AAMBackup stays dc50265 (live product untouched).
- Launcher proven: lt3 (5 days) passed and survived ssh close.
- RUNNING: lane A2, stamp a2y26, 728 days from 2026-10-01, --retention-days 730, --approve-del-after-days 3, code-root code_fix. It pauses itself around production runs (02:01 cloud, 06:00 rollover, 21:01 LAN), so it takes most of a day. Do NOT start another lane or stop it.
- Check later (one cheap Haiku agent): on the box `powershell -File C:\AAMLivebox\box_run.ps1 -Action status -Name a2y26 -Lane A` (running/finished/DIED, progress.json). When finished: read exit code + `lane A: completed...` line in C:\AAMLivebox\a2y26_out.txt, copy evidence to C:\AAMLivebox\evidence_keep_a2y26, production-unchanged check, `python -m tools.livebox.run_lane --cleanup A --stamp a2y26 --root C:\AAMLivebox` (from C:\AAMLivebox\harness, box venv). Then the next lanes (X01,X02,X04,X05,X34,X35,X09,X44,X24,X40, variants, A730) and make_report -> REPORT.md.
- Correction: on this branch product db_retention_days default is 90 (730 is the fy-overlap default); lane A's early run_history purge (R4) was just the 90-day config, not a bug. log_retention_days max is 365.
- Opus design review (rclone-only for LAN+cloud, remove Prefect): docs/OPUS_DESIGN_RCLONE_ONLY_NO_PREFECT_2026-11-25.md (if present; otherwise it was still being written).
- Owner rules: Haiku runs/moves/checks, Sonnet codes, Opus reviews only; never mix fy-overlap and baseline-v1-overlap; no product commit without the owner's go (0ec61f2 was authorised for the test run).
