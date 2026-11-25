# Test Matrix: Phase 2 (WS-C — Cloud Trims, One Retry Level, S6 Stall Watchdog, F1–F3)

Date: 2026-11-25. Branch: `rclone-engine`.

## 1. Retry Policy & Evidence Gate Equivalence (C0)
- **Single retry level**:
  - Preflight & verify tasks: Prefect `retries=1` (or `preflight_retries`); no nested retry loops.
  - Sync step: rclone internal `--retries` handles transient per-file errors; flow-level `_sync_with_retries` (`max_attempts`) handles exit 5 / S6 `stalled=True` only.
- **Cloud verify equivalence**:
  - `rclone check --one-way --size-only --combined` + one `rclone size` produces the same `matching`, `missing`, `differ`, `errors` evidence-gate inputs without redundant `lsjson -R` + second `check`.

## 2. Rows

| ID | Chunk | Scenario | Expected | Layer |
|---|---|---|---|---|
| P2-C1 | C1 | Cloud dry-run uses `--combined` (`combined_counts`, `planned_copy_bytes`) | Replaces `rclone size` walks; G2 dropped on cloud | U |
| P2-C2 | C2 | Cloud preflight probe A removed | `core/cloud_preflight.py` skips redundant source probe (covered by `health.py`) | U |
| P2-C3 | C3 | Cloud write probe removed | Key moved to `DEPRECATED_KEYS`; no probe write on cloud preflight | U |
| P2-C4 | C4 | Cloud verify merged | One `check --combined --size-only` + one `size`; evidence gate rows unchanged | U |
| P2-C5 | C5 | One retry level | `test_task_retry_config` green; no double-retry stacking | U |
| P2-C6 | C6 | S6 stall watchdog (`tools.stall_timeout_seconds`, default 1800, 300..14400, 0=off) | Real hanging child process (sleeps without log/output progress) killed -> `stalled=True`; `run_lan_mirror` / `run_cloud_sync` return `FAILED`, `retryable=True` (OD2) | U |
| P2-C8 | C8 | Review F2 (`cloud_sync_task`) | Budget-limited `timed_out=True` with `max_duration` set -> `CLOUD_PARTIAL`, `killed_after_cutoff=True`, `budget_stopped=False`, `AAM-LAN/CLD-04` | F |
| P2-C9a | C9 | Review F3 (`decide_lan_arrival`) | `line[2:].rstrip("\r\n")` preserves leading/trailing spaces in filenames | U |
| P2-C9b | C9 | S8c explicit `--delete-after` | `build_lan_rclone_command(..., mode="sync")` includes `--delete-after`; never-list holds | U |
| P2-C10 | C10 | Review F1 + S8f | `counted_deletes` counts real `Deleted`/`Moved into backup dir` entries (not failed delete attempts); killed runs mark `stats_at_least=True` | U |
