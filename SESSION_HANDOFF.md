# SESSION HANDOFF — read this first in every new session

**Last updated:** 2026-10-15 (session 2: M2 and M3 done; next M4)

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
| M2 Data safety | **done** (2.1–2.6) |
| M3 Truth | **done** (3.1–3.5) — see plan §13 incl. the deferred tool_runner note |
| M4 Operations | in progress — 4.1, 4.2, 4.3 done |
| M5 Acceptance (on-box) | not started |

**Test status at last check (2026-10-15, after M4 4.3):** `tests/` **1741 passed / 0 failed / 19 skipped**
(full audit lab not re-run since M3; every changed lab test - W16 shutdown/watchdog, W12.5, W11 clock, W17/W18/W1e - passes on its own).
Audit lab: see the M3 milestone line in §4 (only expected red: W16, which needs the owner's
config.yaml `monthly_cron` changed - DEC-7).

### Built but NOT wired yet (M2/M3 must call these)
| Module | What | Wire in |
|---|---|---|
| `core/guards.py` | G1–G5 **wired** (2.2, 2.3) | — |
| `core/approvals.py` | **wired** (2.3): `approvals` table, attention email, `approval_id` flow param, dashboard approve/dismiss/run | — |
| `core/smb_ready.py` | NAS readiness | **wired** in 2.5 (b59a6d6) |
| `core/rclone_errors.py` | **wired** (3.3): integrity audit → `AUDIT_INCOMPLETE`; cloud write probe cause | — |
| `core/notify.py` | **wired** (3.4): every alert + report email; `report.send_failure_alert` remains only as a legacy helper (unused by flow/watchdog) | — |
| `core/attention.py` | **new** (3.5): dashboard banner + `/health` | — |

## 4. Next steps (in order)

1. **2.3 approvals — DONE** (8afa499, 0c627c0, 975d499). How it works: guard BLOCK → `*_BLOCKED`
   row → one approval request (all blocked guards, phrase) + ONE `ACTION_REQUIRED` email
   (`blocked:<leg>`) → dashboard card → typed phrase → `POST /approvals/{id}/approve` (needs
   `dashboard.auth_enabled: true`) → re-runs `backup-<leg>` with `approval_id` → re-measure;
   proceeds only if every blocked guard is covered and not worse; approval used up before any
   transfer. G5: missing marker → BLOCKED; approval creates the marker. **Production check before
   go-live:** the trial box config must have `dashboard.auth_enabled: true`, otherwise nobody can
   approve (by design).
2. **2.4 preflight permissions — DONE** (99ad2c1, efc222b): `core/permissions.py`; SeBackupPrivilege
   in the LAN health step (only with `lan.use_backup_mode`); NAS write probe, source read probe, NAS
   free space (`/BYTES`) in `lan-access`; cloud `touch`+`deletefile` probe in `cloud-access`; LAN
   dry-run on `run_tool` + `/UNILOG` (full listing kept, streamed). Lab: `use_backup_mode: false`
   (lab account has no SeBackupPrivilege); production-command experiments use
   `labconf.PRODUCTION_LAN`. **Go-live:** service account needs SeBackupPrivilege + NAS modify
   rights + bucket objectAdmin - the preflight now names each missing one.
3. **2.6 rollover — DONE** (f778164): see plan §13. Unit suite 1580 passed / 0 failed; lab W6 proves
   the real rollover end to end; W12/W13 green. The full-lab M2 milestone run had not finished when
   M2 full lab: 234 passed / 6 failed -> the 5 W18 launch tests patched the removed
   `core.fy_rollover.rollover`; fixed (W18 29/29 green). Remaining red: W16 schedule collision
   (intentional, WP8 / 3.4). **M2 milestone: green except the intentional red.**
4. **M3 DONE** — 3.1 22706fc, 3.2 fd1abce, 3.3 992cd8d, 3.4 c2ed9f2 + 9665489, 3.5 f8783dd.
   **M3 milestone lab: 236 passed / 3 failed** -> W8 report-flow test moved to the new truthful
   contract (fixed, re-run green). The other two (W16 schedule collision, W8 deployment
   schedule == config) both read the owner's local config.yaml and turn green with:
   Owner action: change `schedule.monthly_cron` to `"30 8 1 * *"` in the local and the
   production config.yaml (code default + example already changed) - lab W16 then turns green.
5. **M4 4.1 + 4.2 + 4.3 DONE** (shutdown verified; clock tri-state; watchdog - see plan §13).
   **NEXT: 4.4 schedule/time-budget validator** (count the LAN shutdown verify window and the
   watchdog never restarting under a lock). 4.3 was not run against the real services: on-box
   check in WP17 (restart order + STOP_PENDING kill on the trial box needs owner OK).
   4.2 note: `block` policy = cloud FAILED (not an approval); owner may want it as G7 BLOCKED + approval
   later - ask if it matters. The audit's LAN shutdown has
   no run row: a non-VERIFIED audit shutdown is the WARNING email only (the banner reads the last
   LAN *run* row).
6. **M4 (plan §13)** - 4.1 shutdown result verification (WP10), 4.2 clock tri-state +
   w32tm fallback (WP12), 4.3 watchdog restart order / STOP_PENDING / config / own log (WP13),
   4.4 schedule/time-budget validator with cronsim (WP14), 4.5 installer `uv run --frozen
   --no-sync` + `AAM_CONFIG` + readiness checks (WP16), 4.6 config.example.yaml fully documented.
   Deferred from M3 (plan §13 note): tool_runner migration of cloud_verify / cloud_reporter /
   integrity `subprocess.run` calls.

### Verified tool facts (real binaries, 2026-10-14/15)
- **rclone final stats**: the LAST JSON log line with a "stats" key holds the totals
  (`transfers` = files uploaded, `bytes`, `deletes`, `errors`, `checks`); no change -> all 0.
- **whoami**: Git Bash's `whoami` shadows Windows' - call `%SystemRoot%\System32\whoami.exe`.
  `/priv /fo csv /nh` = one CSV row per privilege, column 1 = name (locale independent).
- **robocopy `/BYTES`**: exact Bytes row (without it: rounded `3.33 m`); Files row and
  `*EXTRA File` lines unchanged. robocopy reads paths beyond MAX_PATH (no `/ZB` needed).
- `shutil.disk_usage` works on UNC paths; `rclone touch` / `deletefile` exit 0 (deletefile of a
  missing object: exit 4).
- **robocopy `/L` writes nothing**: a missing destination is not created (exit 1); an existing one
  keeps names, sizes and mtimes (verified 2026-10-15). That is why the listing still runs when the
  mount marker is missing.
- **robocopy 10.0 `/L /MIR` (no `/NJS`)**: Files **Total** = source file count (G2). **Planned deletions = number of `*EXTRA File` lines** (one per file `/MIR` deletes, nested files in extra folders included). The Files **Extras column is NOT the delete count**: it also counts `/XF`-excluded destination files (the mount marker, any depth), which `/MIR` keeps; files under `/XD` folders are counted in neither.
- **rclone v1.74.3 `sync --dry-run --use-json-log`**: one `"skipped":"delete"` line per planned file deletion (nested included; folder removals are separate `"remove directory"` lines). Dry-run exit 0 = copies planned, 9 = none (`--error-on-no-transfer`), deletes do not change it.
- **rclone `--max-delete N`**: deletes up to N, then fatal error, **exit 7** (→ CLOUD_FAILED), log "failed to delete K files".
- **Missing source**: robocopy `/MIR` exit 16 ("ERROR 2 Accessing Source Directory"), rclone sync exit 3 ("not deleting files as there were IO errors"); destination untouched in both.
- `rclone size <local dir> --json` → `{"count":N,...}` = what `rclone sync` sees.
- **shutdown.exe** (Win 11 + trial box WS2016, owner-authorised 2026-10-15): exit = Win32 error, text on
  **stderr** as `<host>: <text>(<code>)`, stdout empty. `/s /m` 0 = accepted, **1190** = already
  scheduled, **5** = access denied (this PC → trial box, workgroup), **1707** = invalid address;
  `/a` 0 = aborted, **1116** = nothing to abort. Real `/t 60` on the trial box: port 445 closed at
  **t+107 s** (~47 s after the delay) and stayed closed (no flapping).
- **The committed 4.1 code run for real against the trial box from this PC (2026-10-15, owner-authorised):**
  FAILED (no rights: exit 5 + "Access is denied.(5)", no polling); with an Administrator IPC$ session
  (`net use \\<ip>\IPC$ /user:Administrator *`): NOT_VERIFIED (accepted, cancelled with `shutdown /a`
  after 10 s, still reachable after 90 s) and VERIFIED (`/t 30`: port 445 closed after 56 s).
  The IPC$ session also shows the fix for production: credentials the NAS accepts.
- **w32tm /stripchart /computer:X /samples:1 /dataonly** (Win 11 **and trial box WS2016**, identical, 2026-10-15): ok = rc 0, last line
  `12:10:52, +00.9227616s`; NTP server not answering = **rc 0** with `12:10:52, error: 0x800705B4`;
  unknown host = rc 2147953401, `The following error occurred: No such host is known. (0x80072AF9)`.
- **sc.exe** (trial box WS2016, read-only `queryex`/`qc`, 2026-10-15): all text incl. errors on
  **stdout**, exit = Win32 error (1060 missing service). `queryex`: `STATE : 4  RUNNING`,
  `PID : 1100`. AamBackupAgent `DEPENDENCIES : AamPrefectServer`; all 3 run as LocalSystem.
- Sending `\\host` through `ssh ... 'shutdown ...'` mangles the backslashes (got 1707); pipe a Python
  script written with the Write tool instead.

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
- 2026-10-15 ~11:56 and ~12:08 IST: the box was **shut down twice by our 4.1 tests** with the owner's
  permission (owner restarts it by hand; after the first restart all 3 services came back Running).
  No backup was running either time.

## 7. Housekeeping

- Not committed on purpose: `audit_lab/raw/*.err`, coverage files, `qc`, `query`, one-off root scripts (`analyze_logs*.py`, `coverage_func_scan.py`, `scan_stale_imports.py`, `search_lan.py`, `test_cloud_verify_timeout.py`) — owner to decide on deletion.
- Update this file at the end of every working session (status table, next steps, gotchas).
