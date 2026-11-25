# Test Matrix: Phase 1b (WS-B — Copy-Only, Versions `--backup-dir`, Reminder Ladder, Notify Fixes)

Date: 2026-11-25. Author: Opus / Master Plan WS-B B0. Branch: `rclone-engine`.

## 1. Decisions (B0)
- **Where the reminder ladder runs**: Checked on each daily `06:00` `rollover_check_flow` AND when a G3-only block recurs in `_run_lan_pipeline` / `_run_cloud_pipeline`. Reading `approvals` + `notifications` from `ManifestDB` makes it restart-safe and avoids duplicate sends on the same rung.
- **Dashboard API field names** (`ui.py` `_approval_view` / `/status` card):
  - `deletions_waiting: int` (measured `planned_deletes` from the pending G3 approval)
  - `waiting_days: int` (whole days since `created_at`)
  - `waiting_label: str` (`f"{deletions_waiting} deletions waiting, {waiting_days} days"`)

## 2. Notify Fixes (B1 — OD8)

| ID | Scenario | Inputs | Expected | Layer |
|---|---|---|---|---|
| P1 | Escalation bypasses cooldown | `WARNING` SENT 1 h ago on key `k`; now `FAILURE` or `ACTION_REQUIRED` on `k` (within 24 h cooldown) | `SENT` (not `SUPPRESSED`); threaded via `In-Reply-To` | U (`test_b1_notify_fixes.py`) |
| P7 | Duplicate window | `FAILURE` SENT 31 s ago on key `k`; second `FAILURE` on `k` within `alerts.duplicate_window_seconds=300` (even with different `fault_code`) | `SUPPRESSED`; after > 300 s, a new incident sends | U (`test_b1_notify_fixes.py`) |
| P8 | WARNING never ACTION NEEDED | `AlertEvent(kind="WARNING", fault_code="AAM-AUD-01", ...)` (catalog severity `ACTION NEEDED`) | Subject contains `WARNING:` and never `ACTION NEEDED` | U (`test_b1_notify_fixes.py`) |

## 3. Config & Validation (B2)

| ID | Scenario | Inputs | Expected | Layer |
|---|---|---|---|---|
| K1b | New Phase 1b keys | `LanConfig()`, `ApprovalsConfig()`, `AlertsConfig()` | `lan.versions_enabled=True`, `lan.versions_folder="AAM_versions"`, `lan.versions_retention_days=91` (7..3650); `approvals.reminder_days=[1,2,4,7]`; `alerts.duplicate_window_seconds=300` (0..86400); all have `config_help` + `config.example.yaml` | U |
| V10 | Versions folder inside mirror or wrong share | `versions_folder` inside `lan_destination` or UNC root mismatch | Rejected by config validation / preflight (`ValueError` / `SafetyRefusal`) | U |

## 4. Versions `--backup-dir` & Copy-Only (B3, B4, B6, B7, B8)

| ID | Scenario | Inputs | Expected | Layer |
|---|---|---|---|---|
| V1 | G3-only block | G3 blocks, G1/G4/G5 pass | `rclone copy` runs; new + changed files arrive; 0 deletions; status `*_BLOCKED`; row/metrics `"copied N, deletions held M"` | F |
| V2 | G1/G4/G5 block | G1, G4, or G5 blocks | Nothing runs (no copy-only) | F |
| V3 | 30-night open block | Block open 30 nights | Every night copies new files, 0 deletes, never auto-approves, reminders only on rungs, dashboard count/age exact | F |
| V4 | Approve after 40 nights | Operator approves at night 41 | Deletions run once; deleted files in `AAM_versions\<run>_<FY>\<rel>`; mirror matches source | L (`test_w26`) |
| V5 | Approved N, plan N+1 | Approved for `planned_deletes=N`, tonight's plan `N+1` | Blocks again (copy-only, no deletions) | F |
| V6 | Recreated + re-deleted file | Deleted night 1, recreated + deleted night 2 | Both versions exist in distinct run folders | U/L |
| V7 | Two runs same day | Retry / second FY pass / manual run | Distinct `<YYYY-MM-DD_HHmm>_<FY>` folders, no overwrite | U |
| V8 | Replaced file | Source file modified | Old version in `AAM_versions\<run>_<FY>\<rel>`; mirror has new version | U/L |
| V9 | 91-day retention purge | Run folder > 91 d vs <= 91 d | Old purged (`rclone delete --min-age 91d` + `rmdirs`); young kept; mirror root untouched | U |
| V11 | Space accounting + weekly report | Dry-run with replaced + deleted sizes | `bytes_needed` includes versions growth; low space -> `FAIL` (`AAM-NAS-05`); weekly report shows versions size | U/F |
| V12 | Real rclone local → UNC | `audit_lab/test_w26_versions_lab.py` | Deletes + replaces land in run folder with identical relative paths and MD5 | L |
| V13 | Counters & backstop with `--backup-dir` | `lan_backupdir_dryrun.jsonl` | `planned_deletes` = `delete` + `move into backup dir`; `moves` = `move to ` only; `--max-delete` ignores replace moves | U/L |

## 5. Reminder Ladder (B5)

| ID | Scenario | Inputs | Expected | Layer |
|---|---|---|---|---|
| N1 | Day 0 first block | Pending approval created today | First alert sent (`rung=0`), not repeated same day | U |
| N2 | Ladder rungs 1, 2, 4, 7 | Pending approval aged 1, 2, 4, 7 days | Exactly one reminder sent on each rung day | U |
| N3 | Quiet days between rungs | Days 3, 5, 6, 8..13 | No reminder mail sent | U |
| N4 | Weekly after day 7 | Days 14, 21, 28 | One reminder sent every 7 days (`day >= 14 and day % 7 == 0`) | U |
| N5 | Approval granted/dismissed/used | Status != `PENDING` | Reminders stop immediately | U |
| N6 | Restart safety | New DB connection / process | Reads `approvals` + `notifications`; never double-sends a rung | U |
