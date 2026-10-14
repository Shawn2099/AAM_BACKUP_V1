# SESSION HANDOFF — read this first in every new session

**Last updated:** 2026-10-15 (session 2: 2.2 and 2.3 done) · **Branch:** `mvp-hardening` · **Owner decisions:** all made (plan §11)

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
| M2 Data safety | **in progress** — 2.1, 2.2, 2.3, 2.5 done; 2.4, 2.6 TODO |
| M3 Truth | not started (WP7 classifier module already done) |
| M4 Operations | not started |
| M5 Acceptance (on-box) | not started |

**Test status at last check (2026-10-15):** `tests/` **1538 passed / 0 failed / 19 skipped**.
Audit lab after 2.3 (~20 min): 234 passed / 3 failed → the two canary tests were moved to the
G5 contract (W5c is now the end-to-end approval-cycle proof with real robocopy; targeted re-run
green); the only remaining red is intentional (`test_w16_schedule_collisions_across_year` → WP8 / 3.4).

### Built but NOT wired yet (M2/M3 must call these)
| Module | What | Wire in |
|---|---|---|
| `core/guards.py` | G1–G5 **wired** (2.2, 2.3) | — |
| `core/approvals.py` | **wired** (2.3): `approvals` table, attention email, `approval_id` flow param, dashboard approve/dismiss/run | — |
| `core/smb_ready.py` | NAS readiness | **wired** in 2.5 (b59a6d6) |
| `core/rclone_errors.py` | transport-failure classifier | WP7 (`core/integrity.py` → `AUDIT_INCOMPLETE`) |
| `core/notify.py` | single alert path, delivery recorded, cooldown, dashboard link | WP8 (replace `report.send_failure_alert` call sites; also fixes 4xx-retry bug in report.py) |

## 4. Next steps (in order)

1. **2.3 approvals — DONE** (8afa499, 0c627c0, 975d499). How it works: guard BLOCK → `*_BLOCKED`
   row → one approval request (all blocked guards, phrase) + ONE `ACTION_REQUIRED` email
   (`blocked:<leg>`) → dashboard card → typed phrase → `POST /approvals/{id}/approve` (needs
   `dashboard.auth_enabled: true`) → re-runs `backup-<leg>` with `approval_id` → re-measure;
   proceeds only if every blocked guard is covered and not worse; approval used up before any
   transfer. G5: missing marker → BLOCKED; approval creates the marker. **Production check before
   go-live:** the trial box config must have `dashboard.auth_enabled: true`, otherwise nobody can
   approve (by design).
2. **NEXT: 2.4** preflight permission checks (`whoami /priv`, write probe, free space, cloud write probe); LAN dry-run moves to `run_tool` + `/UNILOG` here.
3. **2.6** rollover inside slot, forward-only, through the normal pipelines (today the rollover final backup bypasses the guards; only the builder G1 backstop covers it).
4. Then M3 (plan §13).

### Verified tool facts (real binaries, 2026-10-14/15)
- **robocopy `/L` writes nothing**: a missing destination is not created (exit 1); an existing one
  keeps names, sizes and mtimes (verified 2026-10-15). That is why the listing still runs when the
  mount marker is missing.
- **robocopy 10.0 `/L /MIR` (no `/NJS`)**: Files **Total** = source file count (G2). **Planned deletions = number of `*EXTRA File` lines** (one per file `/MIR` deletes, nested files in extra folders included). The Files **Extras column is NOT the delete count**: it also counts `/XF`-excluded destination files (the mount marker, any depth), which `/MIR` keeps; files under `/XD` folders are counted in neither.
- **rclone v1.74.3 `sync --dry-run --use-json-log`**: one `"skipped":"delete"` line per planned file deletion (nested included; folder removals are separate `"remove directory"` lines). Dry-run exit 0 = copies planned, 9 = none (`--error-on-no-transfer`), deletes do not change it.
- **rclone `--max-delete N`**: deletes up to N, then fatal error, **exit 7** (→ CLOUD_FAILED), log "failed to delete K files".
- **Missing source**: robocopy `/MIR` exit 16 ("ERROR 2 Accessing Source Directory"), rclone sync exit 3 ("not deleting files as there were IO errors"); destination untouched in both.
- `rclone size <local dir> --json` → `{"count":N,...}` = what `rclone sync` sees.

## 5. Gotchas learned (save time)

- **Audit lab now takes ~30 min**; its LAN rigs mirror to `\\127.0.0.1\<drive>$` so `labconf` sets `guards.forbid_local_destination: false`; mirror-mechanics rigs (tiny random trees) use `labconf.MIRROR_MECHANICS_GUARDS`. Prefect temp-server `409 Conflict` failures are flakes - re-run the single test.
- New pipeline tasks must be stubbed in existing pipeline tests: decorator stacks use `@patch("flow.<task>", new=MagicMock())` (no extra arg).
- Mount-marker name is duplicated in 5 modules (`wol.CANARY_NAME`, `integrity`, `lan_preflight`, `lan_sync`, `fy_rollover`); new code uses `guards.MOUNT_MARKER` - consolidate when those modules are touched.

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
