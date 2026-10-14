# SESSION HANDOFF — read this first in every new session

**Last updated:** 2026-10-14 (end of session 1) · **Branch:** `mvp-hardening` · **Owner decisions:** all made (plan §11)

> New session: read this file, then `MVP_HARDENING_PLAN.md` §13 (the task checklist) and
> `docs/ARCHITECTURE.md` (how the program works). Do not re-plan; continue at "Next steps".

---

## 1. What we are doing

Hardening the AAM Backup Automation MVP (rclone → GCS, robocopy → NAS, WoL/shutdown,
FY rollover, reports, alerts; Prefect 3 on Windows Server 2016) for client handover.
Acceptance bar (owner): **core works every time, everything is truthful, no data loss
caused by our program, a human is asked whenever an action could be wrong.**

| Document | Purpose |
|---|---|
| `MVP_HARDENING_PLAN.md` | the approved plan: rules (§2), WPs (§7), config keys (§8), decisions (§11), **checklist (§13)** |
| `docs/ARCHITECTURE.md` | block diagrams, decision points, file map, **§10 gates (every check per leg)** |
| `docs/audit/BASELINE_mvp-hardening.md` | M0 test baseline |
| `SESSION_HANDOFF.md` | this file — current position |
| `HANDOVER.md` | OLD (audit-lab era, 2026-10-10) — background only |

## 2. Owner rules (non-negotiable)

1. Reliability ≫ speed. No shortcuts, no guesses — ask the owner when unsure.
2. **Every tunable lives in config** (pydantic model holds the default + bounds); no literals at call sites.
3. Reuse tools/libraries (rclone/robocopy flags, Prefect, stdlib, locked deps: tenacity, psutil, cronsim, pendulum, pydantic). New dependency only with owner approval. Shared functions for repeated logic.
4. If an action could cause a wrong result/data loss → stop (`*_BLOCKED`), email "open the dashboard", decide on the dashboard (approval). Never auto-proceed.
5. Full tool logs kept (`run_logs/`, 90-day retention, configurable).
6. Test first: every fix has a regression test that is red on the old code.
7. Commit per sub-step with finding IDs; end commit messages with
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
8. **Production-trial box is READ-ONLY** for us (see §6). Never change it without asking.
9. Accepted risks (do not work on): ransomware vs LAN mirror; GCS lifecycle keeps ~2 versions.

## 3. Status

| Milestone | State |
|---|---|
| M0 Baseline | **done** (a8f7f7f) — suite 0 failed |
| M1 Foundations | **done** (1.1–1.8, see plan §13) |
| M2 Data safety | **in progress** — 2.1 & 2.5 done; 2.3 logic done (wiring TODO); 2.2, 2.4, 2.6 TODO |
| M3 Truth | not started (WP7 classifier module already done) |
| M4 Operations | not started |
| M5 Acceptance (on-box) | not started |

**Test status at last check:** `tests/` 1455 passed / 0 failed / 19 skipped (environment skips).
Audit lab (`audit_lab/`, ~20 min) final M1 run 2026-10-14: **235 passed / 1 failed / 3 skipped** — the one red is intentional (`test_w16_schedule_collisions_across_year` → fixed in WP8 / 3.4). Both M-6 DB reds are now green.

### Built but NOT wired yet (M2/M3 must call these)
| Module | What | Wire in |
|---|---|---|
| `core/guards.py` | G1–G5 data-safety guards | 2.2 (preflight of both pipelines) |
| `core/approvals.py` | approval decision logic | 2.3 (DB table, dashboard, flow param) |
| `core/smb_ready.py` | NAS readiness | **wired** in 2.5 (b59a6d6) |
| `core/rclone_errors.py` | transport-failure classifier | WP7 (`core/integrity.py` → `AUDIT_INCOMPLETE`) |
| `core/notify.py` | single alert path, delivery recorded, cooldown, dashboard link | WP8 (replace `report.send_failure_alert` call sites; also fixes 4xx-retry bug in report.py) |

## 4. Next steps (in order)

1. **2.2**: guards config section (`guards.max_delete_files=500`, `max_delete_percent=10`, `max_source_shrink_percent=20`, `forbid_local_destination=true`); LAN planned deletions = `Extras` column of `robocopy /L` summary (drop `/NJS` in dry-run); cloud = `rclone sync --dry-run` delete count + `--max-delete` backstop; G1 replaces `check_source_drive` emptiness; record `source_file_count` + `destination` in extended_metrics for G2/G4 baselines.
2. **2.3**: `approvals` table + `*_BLOCKED` run rows + dashboard approve/dismiss (typed phrase) + `approval_id` parameter on `backup()`; attention email via `core/notify`.
3. **2.4** preflight permission checks (`whoami /priv`, write probe, free space, cloud write probe).
4. **2.6** rollover inside slot, forward-only.
5. Then M3 (plan §13).

### Verified tool facts for 2.2 (probed 2026-10-14, real binaries)
- `robocopy /L /MIR /BYTES` (no `/NJS`): Files-row **Extras column counts every file that would be deleted, including files inside extra folders** (test: 1 extra file + extra dir with 2 files -> Files Extras = 3; Dirs Extras = 2). Files **Total** = source file count (for G2).
- `rclone sync --dry-run --use-json-log --log-level INFO`: one NOTICE line per planned deletion: `"msg":"Skipped delete as --dry-run is set ..."`, `"skipped":"delete"` (count these for G3; files inside deleted folders included).
- Destination file count for G3 %: LAN = `lan_snapshot_before` walk (if it fails -> unknown -> G3 blocks); cloud = `rclone size` of the destination.

## 5. Gotchas learned (save time)

- **Git Bash heredocs mangle backslashes** (`\n`, `\t`, `\\`) even with `<<'EOF'` in this environment. For any code/text containing backslashes use the Write/Edit tools, or a Python script file written with Write. Always re-check diffs for stray `\n`.
- Remote PowerShell over SSH: no `&&` (use `;`), set `[Console]::OutputEncoding=[Text.Encoding]::UTF8`, no scp/sftp — pipe Python via stdin: `cat script.py | ssh innoviztawinserver2016 'C:\AAMBackup\.venv\Scripts\python.exe -'`.
- Run tests with `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600`.
- Audit lab: `-m pytest audit_lab --ignore=audit_lab/test_w7_deployed.py -q -s --timeout=1800 -p no:cacheprovider -o addopts=""` (~20 min; run in background; do NOT edit modules it imports while it runs — its child processes import fresh code).
- Tests are sandboxed: `test_support/sandbox_config.py` sets `AAM_CONFIG` to a temp config; never point tests at the real `config.yaml`.
- `test_support/fake_tools.py` = fake `run_tool` (writes real-format logs, UTF-16 for robocopy); `test_support/smtp_server.py` = real SMTP test server.
- `RunArtefacts.open(dir)` is used inside Prefect tasks (pass the folder path string, not the object).
- `smtplib` raises `SMTPSenderRefused` for 4xx too — classify SMTP errors by reply code.

## 6. Production-trial box (read-only facts, 2026-10-14)

- SSH alias `innoviztawinserver2016` (user `administrator`), code `C:\AAMBackup` (branch `production`, v1.1.3 `6550aad`), runtime `C:\BackupAgent`.
- Windows Server 2016 Standard, **WORKGROUP**; all 3 services run as **LocalSystem** → root cause of NAS "Access is denied" (plan §10 item 2): needs a dedicated service account.
- rclone v1.74.3, robocopy 10.0.14393. Config: LAN 21:00, cloud 22:00, LAN target `\\10.10.151.53\aam_test\CHAOS01\FY26-27` (test leftover), demo bucket. Both NAS IPs do not answer on 445.
- Production `config.yaml` passes the new strict validation (only deprecated `notifications.send_on_success`, dropped with a warning).
- DB: 59 legacy `*_SKIPPED` rows are really preflight failures (display as failures, never rewrite history).

## 7. Housekeeping

- Not committed on purpose: `audit_lab/raw/*.err`, coverage files, `qc`, `query`, one-off root scripts (`analyze_logs*.py`, `coverage_func_scan.py`, `scan_stale_imports.py`, `search_lan.py`, `test_cloud_verify_timeout.py`) — owner to decide on deletion.
- Update this file at the end of every working session (status table, next steps, gotchas).
