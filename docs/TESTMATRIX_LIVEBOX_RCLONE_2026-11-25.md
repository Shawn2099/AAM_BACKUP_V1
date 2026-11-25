# Test Matrix: `tools/livebox` rclone LAN Adaptation (WS-D D3–D6, XR1–XR4)

Date: 2026-11-25. Branch: `rclone-engine`.

## 1. D3 — rclone LAN Spies, Evidence, Config, Oracles

| ID | Scenario | Inputs / Setup | Expected | Layer |
|---|---|---|---|---|
| D3-1 | `derive_config` strips deprecated keys | `PROD_RAW` with `mt_threads`, `log_tail_kb`, `dry_run_timeout_seconds`, `use_backup_mode`, `privilege_check_timeout_seconds`, `orphan_robocopy_log_max_age_hours` | None of the deprecated keys present in derived config | U (`test_derive_config.py`) |
| D3-2 | `derive_config` emits new keys | Minimal `PROD_RAW` | `lan.transfers`, `checkers`, `modify_window_seconds`, `verify_timeout_seconds`, `error_tail_lines`, `guards.min_delete_floor_files` present | U (`test_derive_config.py`) |
| D3-3 | Step-keyed LAN rclone spy | `run_tool` called with `rclone-lan-plan`, `rclone-lan-sync`, `rclone-lan-check` | Appends `{"step", "start", "end", "argv", "exit_code"}` to `lan_rclone_runs.jsonl`; cloud `rclone` stays in `rclone_runs.jsonl` | U (`test_driver_evidence.py`) |
| D3-4 | Orphan rclone check | `LaneHooks._orphan_rclone()` + `_write_summary()` | Scans `rclone*` processes with lane root in cmdline; writes `orphan_rclone` in `summary.json` | U (`test_driver_evidence.py`) |
| D3-5 | W3 overlap & orphan checks | `Results.lan_rclone_runs` overlapping; `Results.orphan_rclone` non-empty | `X37`/`X38` flag overlap; `X39` flags orphan rclone (fallback to `robocopy_runs` / `orphan_robocopy`) | U (`test_scenarios_w3.py`) |
| D3-6 | `make_report` backward compat | `summary.json` with `orphan_rclone` or legacy `orphan_robocopy` | Report renders `orphan_rclone: N` without error | U (`test_make_report.py`) |
| D3-7 | `check_tree` empty directories | Source/dest with missing or extra empty folder | `check_tree` returns `TREE` violation naming `empty_dirs` | U (`test_rclone_compat.py`) |
| D3-8 | rclone LAN status/metrics oracle | `run_history` rows: `LAN_COMPLETE`, `LAN_PARTIAL` (`deletes_held`), `LAN_SUSPECT`, `LAN_BLOCKED` (`AAM-DEL-03`) | `counts_source == "rclone"`, `files_deleted == 0` when `deletes_held`, `arrival.verified` false on `LAN_SUSPECT` | U (`test_rclone_compat.py`) |
| D3-9 | `killed_after_cutoff` & `failed_paths` | `LAN_PARTIAL` with `killed_after_cutoff=True` or `.<8hex>.partial` in `failed_paths` | Requires `fault_code == "AAM-LAN-04"`, `budget_stopped` falsy, and stripped `failed_paths` | U (`test_rclone_compat.py`) |

## 2. D5 — Nightly Hash Check (`rclone-lan-hash`)

| ID | Scenario | Inputs / Setup | Expected | Layer |
|---|---|---|---|---|
| D5-1 | `derive_config` hash keys | `derive_config(PROD_RAW, ...)` | Emits `lan.arrival_hash_transferred`, `lan.arrival_hash_max_gb`, `lan.arrival_hash_timeout_seconds` | U (`test_derive_config.py`) |
| D5-2 | `rclone-lan-hash` spy & S15 | `run_tool("rclone-lan-hash", [..., "--files-from-raw", path])` | Logged to `lan_rclone_runs.jsonl`; S15 refuses `--files-from-raw` path outside lane root | U (`test_driver_evidence.py`, `test_rclone_compat.py`) |
| D5-3 | `metrics.arrival.hash` complete | `LAN_COMPLETE` with `files_copied > 0` | If `hash.ran` is `True`: `verified is True`, `files > 0`, `missing == 0`, `differ == 0`; else non-empty `reason` | U (`test_rclone_compat.py`) |
| D5-4 | Hash mismatch / unverified | `LAN_SUSPECT` with `hash.ran=True, hash.verified=False` | Requires `fault_code == "AAM-LAN-04"` | U (`test_rclone_compat.py`) |

## 3. D6 — Spy Expectations

| ID | Scenario | Inputs / Setup | Expected | Layer |
|---|---|---|---|---|
| D6-1 | `SPY_ROLLOVER_NAS` (OD6) | `ROLLOVER_CHECK` on 1 Apr (`is_switch_day=True`, `wake=2, shut=0`) vs non-switch day (`wake=1`) | 1 Apr switch day -> `PASS`; non-switch day -> `FINDING` | U (`test_rclone_compat.py`) |
| D6-2 | `SPY_WAKE_PER_PASS` (P6) | 2-pass overlap LAN flow with `wake=2` | Stays `FINDING`/`KNOWN` until WS-E E4 merges; becomes `FAIL` after E4 | U/F (`run_lane.py`) |

## 4. D4 — Phase 1b Oracles (Gated on Phase 1b / G2)

| ID | Scenario | Expected | Layer |
|---|---|---|---|
| D4-1 | Copy-only G3 block | `LAN_BLOCKED`, destination has source + held deletions (never fewer files) | Oracle |
| D4-2 | Reminder ladder & dashboard | Mail counts match M1–M5 on rungs 1, 2, 4, 7, weekly; dashboard shows `"N deletions waiting"` | Oracle |
| D4-3 | Versions folder & S2/S3/S15 | `--backup-dir` under lane NAS share, outside mirror root; S2/S3/S15 validate paths | Safety/Oracle |
| D4-4 | 91-day purge & approver | Run folders > 91 d purged; `--approve-del-after-days` triggers single deletion pass | Oracle |

## 5. D7 — New Scenarios `XR1`–`XR4` (Skeletons Ready; Full Wiring Gated on Phase 1b)

| ID | Scenario | Setup / Trigger | Expected |
|---|---|---|---|
| XR1 | Source file held open (share mode 0) for 5 nights | Lock one source file nights 1–5, release night 6 | `LAN_PARTIAL` each night, 0 deletions, single incident alert, then `LAN_COMPLETE` |
| XR2 | Rename burst of 300 files | Rename client folder(s) totaling 300 files | `moves == 300`, `deletes == 0`, no G3 block |
| XR3 | Mass delete > G3 (30 nights) | Delete > G3 threshold; approve at night 31 | `LAN_BLOCKED` copy-only for 30 nights, ladder mails, deletions run once at night 31, versions saved |
| XR4 | Versions retention crossing 91 days | Delete/replace files, advance simulated clock past 91 days | Run folders > 91 d purged; younger folders kept; mirror root untouched |
