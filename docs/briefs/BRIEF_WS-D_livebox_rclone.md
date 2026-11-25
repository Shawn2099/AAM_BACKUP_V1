# Brief WS-D: adapt `tools/livebox` to the rclone LAN engine, and the box run protocol for the new code

Paste this whole file to a fresh agent. Master plan: `docs/PLAN_MASTER_PARALLEL_2026-11-25.md`.

## Context
Product: nightly backup automation (rclone → GCS, LAN mirror → NAS, FY rollover, weekly integrity audit, mails, dashboard),
Prefect 3. The live-box harness `tools/livebox` (built on `baseline-v1-overlap`, now also on `rclone-engine`) runs the REAL
flows on the trial box (`ssh innoviztawinserver2016`) inside sandbox lanes under `C:\AAMLivebox\<stamp>\<lane>\`, with a fake
clock, a lane-private Prefect, loopback SMTP, a local rclone remote as "bucket", the NAS as `\\127.0.0.1\C$\AAMLivebox\...`,
NAS wake/shutdown as spies, and oracles writing `evidence\checks.jsonl` (PASS / KNOWN / FINDING / FAIL). Lane A2 (`a2y26`, 728
days from 2026-10-01) on the baseline ended 16,525 PASS, 182 KNOWN, 3 FINDING, 5 FAIL; analysis in
`C:\Users\malliath\Desktop\aam_baseline_overlap\docs\REPORT_LIVEBOX_A2Y26_2026-11-25.md` (5 FAILs = harness churn mtime bug, see WS-E).
On `rclone-engine` only compatibility edits were made (C10c: `rig.py` `_strip_zb`, lane keys). The harness still thinks
LAN = robocopy: `rig.KILLABLE_IMAGES`, `run_lane` argv spy writes `robocopy_runs.jsonl`, `_orphan_robocopy`, `scenarios/w3.py`
overlap checks, `scenarios/results.py`, `evidence.py` (UTF-16 log copy), `make_report.py` (`orphan_robocopy`), `oracles.check_tree`
(robocopy did not create empty folders the same way; rclone does, `--create-empty-src-dirs`).

## Rules (owner, binding)
- Never change product code in this workstream (only `tools/livebox/**` and docs). Product bugs found → `docs/PROGRESS_WS-D_2026-11-25.md` + escalate to Opus.
- Box: never change `C:\AAMBackup` or `C:\BackupAgent`, the live schedule or services; no reboot/shutdown; never power off the NAS; no box action at all before the owner's go (OD7). Do not copy files to the box while a lane runs. Pipe files over ssh (no scp); remote PowerShell uses `;` and `[Console]::OutputEncoding=[Text.Encoding]::UTF8`. Git Bash mangles backslashes: write scripts with the Write tool.
- Safety refusals S1-S15 stay as strict or stricter; never weaken one to make a lane start.
- Test first (tools/livebox/tests) by a different agent than the code; tests frozen for the code agent.
- Commit per package with its ID; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Never bare `git stash`.
- Escalation: Haiku 2 attempts → Sonnet; Sonnet blocked / interface change → Opus. Scope guard on `git diff --stat`.
- Model split: Opus matrix + review + verdicts; Haiku tests, box monitoring, evidence copy, docs; Sonnet harness code + triage.

## Read first
`C:\Users\malliath\Desktop\aam_baseline_overlap\docs\LIVEBOX_HANDOFF_2026-11-25.md`, `LIVEBOX_TEST_PROGRESS_2026-11-25_ADDENDUM.md`,
the STATE block of `LIVEBOX_TEST_PROGRESS_2026-11-24.md`, `LIVEBOX_TEST_PLAN_2026-11-24.md` (§1.3 real/faked, §1.8 safety, §2 calendar, §4 scenarios), `REPORT_LIVEBOX_A2Y26_2026-11-25.md`;
in `rclone-engine`: `docs/PLAN_P1_RCLONE_LAN_2026-11-25.md` (§2 flow, §2.2 statuses, step 8 metrics, Phase 1b), `docs/TESTMATRIX_P1_REVIEW_2026-11-25.md` (S3 nightly hash check), `docs/PLAN_MASTER_PARALLEL_2026-11-25.md`.

## Packages
| ID | Goal | Files | Starts |
|---|---|---|---|
| D0 | Port the WS-E churn fix (cherry-pick the E2 commit from `baseline-v1-overlap`, adapt) | `tools/livebox/churn.py`, its test | when E2 is committed |
| D1 | Compat audit: run the harness dev tests on `rclone-engine`; list every red with cause (robocopy symbol, removed key, status/metric shape) in `agent_runs/d1_livebox_compat.md` | report only | NOW |
| D2 | Opus: `docs/TESTMATRIX_LIVEBOX_RCLONE_2026-11-25.md` (rows for D3-D6 + new scenarios XR1-XR4 below) | doc | NOW |
| D3 | rclone LAN spies + evidence: argv spy keyed on step name (`rclone-lan-plan/-sync/-check`) → `lan_rclone_runs.jsonl`; orphan check = lane `rclone.exe` processes whose cmdline holds the lane root; w3 overlap checks on LAN rclone spans; `evidence.py` copies UTF-8 JSON logs (keep the UTF-16 reader for old evidence); `make_report` keys backward compatible; `derive_config` emits new keys (`lan.transfers`, `lan.checkers`, `lan.modify_window_seconds`, `lan.verify_timeout_seconds`, `lan.error_tail_lines`, `guards.min_delete_floor_files`) and none of the deprecated ones; oracles accept `LAN_SUSPECT`, `LAN_PARTIAL` with `deletes_held`, BLOCKED after exit 7 backstop, `verify` phase, metrics `counts_source: rclone`, `arrival`; `check_tree` treats empty folders as rclone does | `tools/livebox/**` | after D1, D2 |
| D4 | Phase 1b oracles: copy-only nights (destination = source plus held deletions, never fewer files), reminder-ladder mail counts in M1-M5, dashboard "N deletions waiting", versions folder under the lane NAS share (S2/S3/S15 include it; `--backup-dir` path inside the lane), 91-day purge seen on the calendar, approver step (`--approve-del-after-days`) interacting with copy-only | `tools/livebox/**` | after G2 |
| D5 | Nightly hash check (review S3, chunks R8a-R8d): step `rclone-lan-hash` in the argv spy (uses `--files-from-raw`, no excludes: S15 must still check its paths), `metrics.arrival.hash` in evidence, keys `lan.arrival_hash_*` in `derive_config`, oracle: COMPLETE nights with copies show `hash.ran True` (or a recorded skip reason) | `tools/livebox/**` | after G1 |
| D6 | Spy expectations: `SPY_WAKE_PER_PASS` = 1 wake per LAN flow becomes PASS-required once E4 is merged (KNOWN → FAIL if 2); `SPY_ROLLOVER_NAS` per OD6 | `run_lane.py`, `oracles.py` | after E4, OD6 |
| D7 | New scenarios: XR1 a source file held open (share mode 0) for 5 nights → PARTIAL each night, no deletions, then COMPLETE; XR2 rename burst of 300 files → moves, 0 deletes, no G3; XR3 mass delete > G3 → BLOCKED copy-only for 30 nights, ladder mails, approval at night 31, deletions once, versions present; XR4 versions retention crossing 91 days | `tools/livebox/scenarios/rclone.py` | after D4 |
| D8 | Box run per protocol below | box (owner go) | after G4 |

## How to run tests (worktree has no .venv)
```
git worktree add C:\Users\malliath\Desktop\aam_ws_d -b ws-d-livebox rclone-engine    (once)
cd C:\Users\malliath\Desktop\aam_ws_d
$env:PYTHONPATH='.'; $env:PREFECT_HOME="$env:TEMP\aam_ph_wsd"; Remove-Item -Recurse -Force $env:PREFECT_HOME -ErrorAction SilentlyContinue
$py='C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe'
& $py -m pytest tools/livebox/tests tests/test_review2_driver.py tests/test_review2_rig.py -q -p no:cacheprovider -o addopts="" --deselect tools/livebox/tests/test_wire_injections.py --basetemp="$env:TEMP\aam_bt_wsd"
```
`tests/test_review2_driver.py` refuses a dirty tree: commit first, then run.

## Box run protocol for the NEW code (D8; owner go OD7 required)
1. Preconditions: tag `rclone-rc1` pushed (G4); no lane running (`box_run.ps1 -Action status`); production at `dc50265`, services Running; evidence of `a2y26` kept in `C:\AAMLivebox\evidence_keep_a2y26` and its cleanup done (pending steps from the a2y26 report §6).
2. Code under test: clone the tag to `C:\AAMLivebox\code_rclone` (never into `C:\AAMBackup`); harness copy to `C:\AAMLivebox\harness_rclone\tools\livebox` (do not overwrite `C:\AAMLivebox\harness`, which matches the baseline evidence); verify sha256 of `rig.py`, `run_lane.py`, `box_run.ps1`, `churn.py` against the tag. Overlay `time_machine_pkg` as is. `rclone.exe` comes from `code_rclone\deploy\bin` (tracked in git); record its version in `env_versions.json`.
3. Verify the LAN target path works for the account that runs the lane (`\\127.0.0.1\C$\AAMLivebox\...`; an earlier note says the ssh user could not use the admin share; lane A ran, so confirm with a 1-day lane before the long one).
4. Lanes (fresh lower-case `--stamp` each, `--expected-commit <tag sha>`, `--code-root C:\AAMLivebox\code_rclone`, `--prod-api` ending in `/api`, `--start 2026-10-01 --retention-days 730 --approve-del-after-days 3`, launched with `box_run.ps1 -Action start`, run from `harness_rclone` with `C:\AAMBackup\.venv\Scripts\python.exe -m tools.livebox.run_lane`):
   a. `S14` smoke: 14 days from 2026-12-25 (activation, Sunday audit, 1 Jan monthly). Gate to continue: FAIL 0.
   b. `A2R`: lane A, 728 days. One lane alone (most of a day; it pauses around 02:01, 06:00, 21:01 production jobs).
   c. Then at most 3 at once (S13): XR1-XR4, X01 (no internet), X04l (LAN only), X05, X08b (wake fail), X34/X35.
5. Monitoring (Haiku, cheap, read-only): `box_run.ps1 -Action status -Name <stamp> -Lane <lane>` every few hours; stop conditions = LIVEBOX_TEST_PLAN §7.1 (any safety refusal mid-run, production lock seen, C: < 50 GB, production files touched) → stop all lanes, tell the owner.
6. Evidence: copy `<stamp>\<lane>\evidence` to `C:\AAMLivebox\evidence_keep_<stamp>` and pull to `agent_runs/livebox_<stamp>/`; `python -m tools.livebox.make_report --evidence <dirs> --out REPORT.md`; cleanup `--cleanup <lane> --stamp <stamp> --root C:\AAMLivebox`; production-unchanged check (HEAD, `git status`, services, schedule crons, config.yaml sha256 before = after, production `manifest.db` only changed by production runs).
7. Known vs finding (pre-registered; anything else is a FINDING until Opus classifies it):
   | Check | Expected on rclone-rc1 |
   |---|---|
   | `SPY_WAKE_PER_PASS` | PASS (1 wake per LAN flow) once E4 is in; 2 wakes = FAIL (regression) |
   | `SPY_ROLLOVER_NAS` | per OD6 (default: wake allowed at 1 Apr, shutdown follows `lan.shutdown_after_backup`) |
   | 2027-01-03 Sunday audit | PASS after the churn fix (D0); a FAIL there = real regression |
   | Watchdog "Stale backup" lines | 0 on lane A2R |
   | LAN no-change nights | `LAN_COMPLETE`, transfers 0 (legacy `LAN_NO_CHANGES_COMPLETE` never written) |
   | XR1 locked file | `LAN_PARTIAL`, one alert per incident (not per night beyond the ladder/cooldown), no deletions while locked |
   | XR3 | `LAN_BLOCKED` copy-only every night, reminders on rungs 1, 2, 4, 7, weekly, never auto-approved |
8. Pass criteria (G5): FAIL 0 on every lane; every FINDING classified by Opus with evidence (expected / harness / product); safety S1-S15 never refused mid-run; production unchanged; no orphan rclone; mail rules M1-M5 green; versions folder never inside a mirror root.

## Definition of done
D0-D7 committed with tests green; D8 REPORT.md and Opus verdict in `docs/PROGRESS_WS-D_2026-11-25.md`.

## Report back (<= 20 lines)
Commits, harness test counts, compat reds fixed, new scenarios, box run totals per lane (PASS/KNOWN/FINDING/FAIL), FINDING classifications, production-unchanged result, open owner questions.
