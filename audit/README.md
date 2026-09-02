# AAM Backup Automation - Code Audit Index
Branch: development (Merged with origin/main - commit e4f7348)
Updated: 2026-09-02

---

## Severity Legend
- CRITICAL - Data loss, crash, security hole
- HIGH     - Wrong logic, silent failure, production blocker
- MEDIUM   - Reliability risk, race condition, edge case
- LOW      - Code quality, minor smell

---

## Audit Progress

### Root Level Files
| File                   | Status  | CRIT | HIGH | MED | LOW | Report                              |
|------------------------|---------|------|------|-----|-----|-------------------------------------|
| flow.py                | Done    |  0   |  0   |  4  |  3  | audit_flow_py.md                    |
| launch.py              | Done    |  0   |  1   |  2  |  2  | audit_launch_py.md                  |
| watchdog.py            | Done    |  1   |  1   |  3  |  2  | audit_watchdog_py.md                |
| serve.py               | Done    |  0   |  0   |  2  |  2  | audit_serve_py.md                   |
| ui.py                  | Done    |  0   |  2   |  3  |  3  | audit_ui_py.md                      |
| collect_config_data.py | Done    |  0   |  0   |  2  |  3  | audit_collect_config_data_py.md     |

### core/ Files
| File                    | Status  | CRIT | HIGH | MED | LOW | Report |
|-------------------------|---------|------|------|-----|-----|--------|
| core/__init__.py        | Pending |      |      |     |     |        |
| core/health.py          | Pending |      |      |     |     |        |
| core/manifest.py        | Pending |      |      |     |     |        |
| core/lan_sync.py        | Pending |      |      |     |     |        |
| core/cloud_sync.py      | Pending |      |      |     |     |        |
| core/lan_preflight.py   | Pending |      |      |     |     |        |
| core/cloud_preflight.py | Pending |      |      |     |     |        |
| core/cloud_reporter.py  | Pending |      |      |     |     |        |
| core/cloud_verify.py    | Pending |      |      |     |     |        |
| core/fy_rollover.py     | Pending |      |      |     |     |        |
| core/report.py          | Pending |      |      |     |     |        |
| core/process.py         | Pending |      |      |     |     |        |
| core/time_utils.py      | Pending |      |      |     |     |        |
| core/backup_repository.py | Pending |    |      |     |     |        |
| core/lan_manifest.py    | Pending |      |      |     |     |        |
| core/logging.py         | Pending |      |      |     |     |        |
| core/hashing.py         | Pending |      |      |     |     |        |
| core/rclone_config.py   | Pending |      |      |     |     |        |
| core/shutdown.py        | Pending |      |      |     |     |        |
| core/wol.py             | Pending |      |      |     |     |        |

---

## Running Totals (6 / 26 files audited)
| Severity | Count |
|----------|-------|
| CRITICAL |   1   |
| HIGH     |   4   |
| MEDIUM   |  16   |
| LOW      |  15   |
| Total    |  36   |
