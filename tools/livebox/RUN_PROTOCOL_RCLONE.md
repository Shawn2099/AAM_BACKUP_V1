# Livebox Run Protocol for `rclone-rc1` (D8)

**Do not execute any box action without explicit owner go (OD7).**

## 1. Preconditions
1. Tag `rclone-rc1` pushed (gate G4 complete).
2. No lane running on the box (`box_run.ps1 -Action status`).
3. Production at `dc50265`, all three NSSM services (`AamPrefectServer`, `AamBackupAgent`, `AamWatchdog`) `Running`.
4. Previous `a2y26` evidence preserved in `C:\AAMLivebox\evidence_keep_a2y26` and its lane folder cleaned up.

## 2. Box Setup (Staging Only, Never Touch `C:\AAMBackup`)
1. Clone `rclone-rc1` to `C:\AAMLivebox\code_rclone`.
2. Copy `tools/livebox` to `C:\AAMLivebox\harness_rclone\tools\livebox` (leave `C:\AAMLivebox\harness` untouched for baseline evidence).
3. Verify SHA-256 of `rig.py`, `run_lane.py`, `box_run.ps1`, `churn.py` against `rclone-rc1`.
4. Overlay `time_machine_pkg` as on baseline; record `code_rclone\deploy\bin\rclone.exe` version in `env_versions.json`.
5. Confirm loopback admin share (`\\127.0.0.1\C$\AAMLivebox\...`) is writable by the lane runner account using a 1-day smoke check before starting long lanes.

## 3. Lane Layout
Common flags:
`--stamp <lower-case> --expected-commit <rclone-rc1 sha> --code-root C:\AAMLivebox\code_rclone --prod-api http://127.0.0.1:4200/api --start 2026-10-01 --retention-days 730 --approve-del-after-days 3`
Launched via `box_run.ps1 -Action start` from `C:\AAMLivebox\harness_rclone` using `C:\AAMBackup\.venv\Scripts\python.exe -m tools.livebox.run_lane`.

1. **Gate 1 — `S14` smoke**: 14 days from `2026-12-25` (activation, Sunday audit, 1 Jan monthly). Gate to continue: `FAIL == 0`.
2. **Gate 2 — `A2R` (Lane A, 728 days)**: Run alone (pauses around `02:01`, `06:00`, `21:01` production jobs).
3. **Gate 3 — Scenario pack (max 3 lanes concurrently, S13)**:
   - `XR1`–`XR4` (Phase 1b: locked source file 5 nights, rename burst 300 files, mass delete > G3 copy-only 30 nights + approval at 31, versions retention crossing 91 days)
   - `X01` (no internet), `X04l` (LAN only), `X05`, `X08b` (wake fail), `X34`, `X35`.
   - **Out of scope for `rclone-rc1`**: `X17`, `X37`–`X39`, `X45`–`X48` (unwired red-first injection scenarios covered by `test_wire_injections.py`, whose wiring APIs `clock_skew`, `race_next_flow`, `trigger_during_next_flow`, and `_install_step_hook` are not implemented on `baseline-v1-overlap` or `rclone-engine`; they remain `injection_wired=False` / `NOT_RUN`, and `test_wire_injections.py` is kept unskipped).

## 4. Monitoring & Stop Conditions
- Poll read-only: `box_run.ps1 -Action status -Name <stamp> -Lane <lane>`.
- Stop all lanes immediately and notify the owner if:
  - any safety refusal (`S1`–`S15`) fires mid-run,
  - production lock is seen,
  - `C:` free space drops below `50 GB`,
  - any production file is touched.

## 5. Evidence & Cleanup
1. Copy `<stamp>\<lane>\evidence` to `C:\AAMLivebox\evidence_keep_<stamp>` and pull to `agent_runs/livebox_<stamp>/`.
2. Generate report: `python -m tools.livebox.make_report --evidence <dirs> --out REPORT.md`.
3. Clean up lane: `--cleanup <lane> --stamp <stamp> --root C:\AAMLivebox`.
4. Verify production unchanged: `C:\AAMBackup` `HEAD` and `git status`, services `Running`, schedule crons, `config.yaml` SHA-256 before == after, production `manifest.db` modified only by scheduled production runs.

## 6. Pass Criteria (G5)
- `FAIL == 0` on every lane.
- Every `FINDING` classified by Opus with evidence (`expected` / `harness` / `product`).
- `SPY_WAKE_PER_PASS`: `PASS` (1 wake per LAN flow; `2` wakes = `FAIL` regression, E4 merged).
- `SPY_ROLLOVER_NAS`: `PASS` per OD6 (wake allowed at 1 Apr switch).
- `2027-01-03` Sunday audit: `PASS` (with D0 churn mtime fix).
- `0` watchdog `"Stale backup"` lines on `A2R`.
- No-change LAN nights: `LAN_COMPLETE` with `transfers == 0` (never `LAN_NO_CHANGES_COMPLETE`).
- `orphan_rclone == []`, `S1`–`S15` never refused mid-run, mail rules `M1`–`M5` green, `AAM_versions` never inside a mirror root, production unchanged.
