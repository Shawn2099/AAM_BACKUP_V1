# AAM Backup Automation — MVP Hardening Plan

**Date:** 2026-10-14
**Status:** APPROVED by owner 2026-10-14 — all decisions DEC-1…DEC-8 resolved (see §11). Implementation on branch `mvp-hardening`.
**Basis:** full code review of `flow.py`, `core/*`, `models/config.py`, `launch.py`, `serve.py`, `watchdog.py`, `ui.py`, `deploy/*`; forensic analysis of production-demo logs (2026-07-01 → 2026-10-12); committed test suite (24 failed / 1311 passed / 16 skipped); audit lab (233 passed / 3 failed — all 3 intentional reds); 7 confirmation probes (all reproduced).

---

## 0. How to read this document

- §1–§3: the rules every change must obey (owner requirements).
- §4: scope — what is in, what is an accepted risk.
- §5: shared building blocks — written once, reused everywhere.
- §6: human-in-the-loop design (the "pause and ask" requirement).
- §7: work packages (WP0–WP17), in execution order. Each has: problem + evidence, design, config keys, files touched, tests, acceptance criteria.
- §8: complete list of new/changed config keys.
- §9: milestones and definition of done.
- §10: handover checklist (environment, not code).
- §11: decisions the owner must make before/while we build.
- §12: traceability — every review finding → the WP that closes it.

---

## 1. Owner requirements (verbatim intent)

| # | Requirement | How this plan enforces it |
|---|---|---|
| R1 | Core must work every time: cloud backup, LAN backup, WoL + shutdown, FY rollover, reports, mail alerts. | WP3–WP14, on-box acceptance run (WP17). |
| R2 | Everything must be **truthful** — status, metrics, reports, dashboard, alerts. | WP5–WP9; single status vocabulary (§5.3). |
| R3 | **No data loss caused by our program.** | WP3 guards, WP11 rollover safety, WP4 preflight. |
| R4 | If the program thinks an action may cause a wrong result or data loss → **stop and ask a human**. | §6 approval workflow; never auto-proceed. |
| R5 | **All thresholds in config files.** No magic numbers in code. | §8; a lint test fails the build on new hard-coded thresholds in guarded modules. |
| R6 | **Don't reinvent the wheel** — use the tools/libraries we already have; write a function once for repeated work. | §5; dependency rule §2.4. |
| R7 | `/ZB` permission must be checked in preflight. | WP4. |
| R8 | **Full logs** kept (not just tails), cleaned roughly every 3 months. | WP2. |
| R9 | New branch; professional process; reliability ≫ speed/optimisation; no guesses. | §2, WP0, §9. |

## 2. Engineering rules for this branch

1. **Fail closed, then ask.** Any ambiguity about data safety ends in a `*_BLOCKED` status + alert + dashboard approval request — never in a silent pass and never in an automatic retry that could repeat the harm.
2. **Truth over convenience.** A status/metric/label may only claim what was measured. Unknown is recorded as unknown (`NULL` / `UNVERIFIED`), never as zero or "OK".
3. **Test first.** Every defect gets a regression test that is **red on the current code** and green after the fix. The red run is recorded in the PR description.
4. **Dependency rule.** Prefer, in order: (a) the tool's own feature (rclone/robocopy flags, Prefect primitives); (b) Python stdlib; (c) a library already in `uv.lock` (pydantic, tenacity, psutil, cronsim, loguru, pendulum, httpx); (d) a new dependency only with a written justification and owner approval.
5. **One function per concern.** Subprocess execution, status decisions, alert sending, DB opening, retention cleanup, guard evaluation each live in exactly one place (§5).
6. **No behaviour change without a test and a log line.** Every guard decision logs its inputs and result at INFO (pass) or ERROR (block).
7. **Small, reviewable commits.** One WP (or sub-WP) per commit; message references the finding IDs in §12.
8. **Tests never touch production.** Test fixtures refuse to run if the resolved config points at a real runtime dir, bucket, or UNC share (WP16).
9. **Windows Server 2016 is the target.** Every subprocess behaviour (robocopy, sc, shutdown, whoami) is verified on Windows, not mocked only.
10. **Every tunable is config (DEC-2).** Timeouts, retries, intervals, limits, thresholds, tool flags that change behaviour (checkers, transfers, retries-sleep, log level), cron schedules, retention, rate limits, alert cooldowns. Code holds only the *default* (in the pydantic model, with bounds and a description) — never a literal at the call site. A test scans guarded modules for numeric literals used as timeouts/limits and fails on new ones.
11. **Attention emails point to the dashboard (DEC-3).** Any condition that needs a human (BLOCKED run, stale leg, failed audit, failed notification streak, watchdog CRITICAL) sends one email with the reason, the key numbers and the dashboard URL; decisions are only taken on the dashboard.

## 3. Definitions (used throughout)

- **Leg:** `cloud` (rclone → GCS) or `lan` (robocopy → NAS).
- **Run:** one execution of one leg; one `run_history` row, always written (WP5).
- **Guard:** a pre-transfer check that can return `PASS` or `BLOCK(reason, metrics)`.
- **Blocked:** run stopped *before any destructive step* because a guard needs a human decision.
- **Approval:** a single-use, time-limited, metrics-bounded human authorisation to run once despite a guard (§6).
- **Run artefacts:** the full raw logs and outputs of every tool invocation in a run (WP2).

---

## 4. Scope

### 4.1 In scope (MVP)
Cloud backup, LAN backup, Wake-on-LAN, NAS shutdown, FY rollover, weekly/monthly reports, failure alert emails, dashboard, integrity audit (truthful reporting only), watchdog, full log retention, human-approval workflow.

### 4.2 Accepted risks (owner decision 2026-10-14) — documented, not fixed
| Risk | Owner position |
|---|---|
| Ransomware / bad data propagates to LAN mirror in one night (`/MIR`, no versions). | Accepted for MVP. |
| GCS lifecycle keeps current + 1 noncurrent version; soft-delete cleared; SA has `objectAdmin`. | Design choice; current lifecycle stays. |
| Network egress controlled by Sophos (TLS inspection, DNS). | Environment; handled by handover checklist §10 + truthful reporting (WP7, WP12). |
| No alert channel other than email + local dashboard. | Accepted; dashboard must therefore be truthful and show staleness (WP9). |

### 4.3 Out of scope
Performance tuning, cost optimisation, new features beyond the approval workflow, VSS snapshots (noted as next-MVP candidate).

---

## 5. Shared building blocks (written once, reused)

These come first (WP1) because most fixes depend on them. Each is small and has its own unit tests.

### 5.1 `core/tool_runner.py` — one subprocess wrapper
Replaces 10+ hand-written `subprocess.run` blocks (cloud_sync, cloud_verify, cloud_reporter ×3, cloud_preflight, lan_sync, lan_preflight, integrity, shutdown, watchdog `sc`, fy_rollover gcloud).

```
run_tool(name, argv, *, timeout_s, artefacts: RunArtefacts | None,
         log_file_flag: str | None, encoding, env=None) -> ToolResult
ToolResult: exit_code | None, timed_out: bool, spawn_error: str | None,
            log_path: Path | None, stdout_path, stderr_path,
            started_at, ended_at, duration_s
            .tail(n_lines) / .error_lines(pattern) helpers
```
- Output always streams **to files** inside the run-artefact directory (never unbounded in memory — fixes the 11 MB error string, finding T7).
- `timed_out`/`spawn_error` are explicit fields; no `-1` sentinels leak into decisions (fixes bool/sentinel class, finding D8).
- Exit code is validated `type(x) is int` (rejects `bool`).
- Message strings for DB/email are built from `.tail()` with a config-driven cap.

### 5.2 `core/run_artefacts.py` — full log storage + retention (R8)
Layout: `<runtime_dir>/run_logs/<YYYY-MM-DD>/<run_id>/<step>.<ext>` (+ `manifest.json` listing every artefact, tool version, argv, exit, timing).
- robocopy: `/UNILOG:<path>` (UTF-16, so non-ASCII file names are preserved — today `/LOG` is read as UTF-8 and garbles them).
- rclone: `--log-file <path> --use-json-log --log-level INFO` (full per-file log), `--combined <path>` for checks.
- After the run, artefacts > `run_logs.compress_after_kb` are gzip-compressed (stdlib `gzip`/`shutil`).
- **Retention:** one function `purge_older_than(root, days)` reused for run_logs, NSSM-rotated service logs, and old `backup_*.log`. Default `run_logs.retention_days: 90`. Runs daily inside the existing `rollover-check` flow as a separate task (no new deployment).
- **Disk guard:** before a run, free space on the runtime volume is checked against `run_logs.min_free_gb`; below it → WARNING alert + dashboard flag; the backup still runs (log storage is not a data-safety condition) but the run row records `artefacts_degraded=true`.
- Path of the artefact folder is stored in `run_history.artefacts_path` and shown on the dashboard.

### 5.3 `core/statuses.py` — single status vocabulary
One `StrEnum` per leg plus helpers `is_success(status)`, `is_failure(status)`, `needs_human(status)`, `display_label(status)`.
Used by: flow terminal policy, `ManifestDB.last_successful_run` (replaces `GLOB '*_COMPLETE'`), `core/report.py` counters, `ui.py`, `dashboard.js` (served from `/status` as display labels so JS stops re-deriving them — fixes "CLOUD_SUSP" truncation).
New statuses: `CLOUD_BLOCKED`, `LAN_BLOCKED` (needs approval), `CLOUD_SLOT_TIMEOUT`, `LAN_SLOT_TIMEOUT` (could not start), `AUDIT_INCOMPLETE` (audit could not reach a verdict).

### 5.4 `ManifestDB.from_config(cfg)` + connection fixes
Removes the 8 copies of `busy_timeout_ms=…, vacuum_freelist_threshold=…, synchronous=…`.
`PRAGMA busy_timeout` becomes the **first** statement on every connection; the legacy dedup `DELETE` moves into the one-time versioned migration (fixes M-6 / L-3; audit-lab reds `test_w9_*`, `test_w1e_*` must turn green).

### 5.5 `core/notify.py` — one alert path
`notify(event: AlertEvent) -> DeliveryResult` used by every alert site (flow pipelines, audit, rollover, watchdog, reports).
- SMTP send via existing code, retry policy via **tenacity** (replaces the hand-written loop).
- **Delivery result persisted** in a new `notifications` table (event key, subject, status SENT/FAILED/SUPPRESSED, error, timestamp) → dashboard shows failed deliveries (critical because email is the only push channel).
- **De-duplication:** same `event_key` within `alerts.repeat_cooldown_hours` is SUPPRESSED (recorded, not sent); a state change (fail → recovered) is always sent ("RECOVERED" email).
- SMTP auth optional, STARTTLS configurable (`notifications.smtp_security: starttls|ssl|none`).

### 5.6 `core/guards.py` — data-safety guards
`evaluate(guard_name, metrics, limits) -> GuardResult(PASS|BLOCK, reason, metrics)`; each guard is a pure function (easy to property-test). Used by both legs and by rollover. Details in WP3.

### 5.7 Config hygiene (in `models/config.py`)
- Every model: `model_config = ConfigDict(extra="forbid")` → a typo (`shutdown_after_bakup`) fails at load with the exact key name (finding: silently ignored keys, probe P5).
- `CONFIG_PATH` resolved absolutely: env `AAM_CONFIG` → else `<project_dir>/config.yaml` (never CWD-relative).
- Schedule-budget validator (cronsim, already locked) — see WP14.

---

## 6. Human-in-the-loop design (R4)

### 6.1 Principle
When a guard cannot prove an action is safe, the run **stops before any destructive step**, records `*_BLOCKED` with the measured numbers, alerts once, and waits for a human. The program **never** proceeds on timeout, never auto-approves, and a scheduled run never consumes an approval.

### 6.2 Mechanism — recommended: "block + approve-and-rerun"
1. Guard returns BLOCK during preflight (no transfer has started; slot/lock released).
2. Row `CLOUD_BLOCKED`/`LAN_BLOCKED` written with `guard`, `reason`, `metrics` JSON; `notify()` emails all recipients "ACTION REQUIRED — open the dashboard to review" with the reason, the measured numbers and the dashboard link (`dashboard.public_url`). Re-sent per `alerts.repeat_cooldown_hours` while still unresolved.
3. Dashboard shows a red card: e.g. *"LAN run blocked: would delete 4,812 of 5,000 destination files (96.2%; limit 10%). Source file count dropped from 5,000 to 188 since last success."* with buttons **Approve this run once** and **Dismiss**.
4. Approve requires an authenticated session **and** typing a confirmation phrase shown on screen (e.g. `DELETE 4812`). Stored in new table `approvals` (approval_id, leg, guard, approved_metrics, approved_by (session/IP), created_at, expires_at, used_at, used_by_run_id).
5. Dashboard triggers the deployment with parameter `approval_id` (`arun_deployment`, already used by `ui.py`).
6. The rerun re-measures. It proceeds **only if** fresh metrics are within the approved envelope (`approvals.tolerance_percent`, default 0 → must not be worse than what was approved). Otherwise it blocks again with a new request.
7. Approval is single-use and expires after `approvals.expiry_hours` (default 12).
8. While blocked, nightly scheduled runs re-evaluate; if the condition cleared (e.g. drive remounted) they run normally; if not, they block again (alert de-duplicated by §5.5).

**Why not Prefect `pause_flow_run`/`suspend_flow_run` (both exist in 3.7.2)?** `pause` keeps the flow process, the concurrency slot and the watchdog lock held for up to the timeout, and is lost if the agent restarts (frequent on this box: 33 restarts in the logs). `suspend` needs result persistence + infrastructure re-submission that `serve()` does not give us reliably. The approval-rerun uses only primitives we already depend on (`arun_deployment`, ManifestDB) and survives restarts. → **Decision DEC-3** asks the owner to confirm.

### 6.3 Guards that require a human (all thresholds in config, §8)
| Guard | Trigger | Leg |
|---|---|---|
| G1 Source has no files | recursive scan finds 0 regular files (excluding system entries + canary) | both |
| G2 Source shrank | source file count < `(1 - guards.max_source_shrink_percent/100)` × count at last successful run | both |
| G3 Mass deletion | planned deletions > `guards.max_delete_files` OR > `guards.max_delete_percent` of destination | both |
| G4 Destination identity changed | destination string differs from last successful run's destination, or resolves to loopback/this host when `guards.forbid_local_destination` | both |
| G5 Destination canary missing on a populated destination | canary absent but destination has files | lan |
| G6 FY rollover anomaly | computed FY is not exactly the next FY after configured FY, or final backups not COMPLETE | rollover |
| G7 Clock unverified on cloud leg | (policy, see DEC-6) | cloud |

---

## 7. Work packages

Format per WP: **Problem/evidence → Design → Config → Files → Tests → Acceptance.**

### WP0 — Branch and green baseline
- **Problem:** branch `chaos-validation` has 53 uncommitted changes; committed suite is red (24 failures: 3 missing `C:\ChaosTest` tools, 21 stale tests vs drifted signatures); `audit_lab/` not committed.
- **Design:**
  1. Owner decides what to do with uncommitted work (DEC-1).
  2. Create branch `mvp-hardening` from the agreed base.
  3. Fix the 21 stale tests to match current *intended* behaviour (no production code change); mark chaos-tool tests `skipif` tool dir absent with an explicit reason.
  4. Add `run_tests.bat`/`pyproject` markers: `unit`, `lab` (audit_lab), `onbox` (Wave 7). Add `pytest-timeout` to dev group (already used).
  5. Record the baseline: committed suite 0 failed; audit lab = exactly the 3 known intentional reds.
- **Acceptance:** baseline report committed in `docs/audit/BASELINE_mvp-hardening.md`.

### WP1 — Shared building blocks (§5)
- Implement §5.1–§5.7 with unit tests each. Migrate call sites module by module (each migration its own commit with existing tests still green).
- **Acceptance:** `grep subprocess.run` in `core/`, `flow.py`, `watchdog.py` only hits `tool_runner.py`; a test asserts no `extra="allow"` model; config loads reject an unknown key with its name in the message.

### WP2 — Full log retention (R8)
- **Problem:** robocopy log deleted after each run; rclone stderr temp file deleted; only a 100 KB tail survives; rclone check output only in memory; NSSM rotated files never pruned; `log_retention_days` default 30 in `core/logging.py` vs 90 in config model.
- **Design:** §5.2. Also: separate loguru file sink for watchdog (`watchdog_app_*.log`) so NSSM stdout and loguru never write the same file (interleaved lines in production logs). App log retention unified to `maintenance.log_retention_days` (default 90).
- **Config:** `run_logs.dir`, `run_logs.retention_days` (90), `run_logs.compress_after_kb`, `run_logs.min_free_gb`, `maintenance.service_log_retention_days` (90).
- **Tests:** artefacts written for success/failure/timeout/kill; purge keeps day-89, deletes day-91 (fake clock — `audit_lab/labclock.py` exists); UTF-16 robocopy log with non-ASCII names parsed correctly; compression round-trip.
- **Acceptance:** after a real run on the box, `run_logs/<date>/<run_id>/` contains the complete robocopy and rclone logs and a `manifest.json`; dashboard links the path.

### WP3 — Data-loss guards (R3, R4)
- **Problem/evidence:** dirs-only source passes the empty check (probe P6, audit C-1); a wrong/unmounted source or path change mirrors deletions; LAN target silently changed to `\\127.0.0.1\…\CHAOS01` in production logs; no mass-delete limit on either leg.
- **Design:**
  - **G1** `source_has_files(root)` — `os.scandir` recursive walk that stops at the first regular non-system file (fast on healthy drives, exact on empty ones). Replaces `check_source_drive`'s top-level `iterdir`.
  - **G2** source count compared with `extended_metrics.source_file_count` of the last successful run (recorded from now on — first run after deploy has no baseline → PASS with INFO "no baseline").
  - **G3 LAN:** preflight runs `robocopy /L /MIR` **without `/NJS`**; planned deletions = the `*EXTRA File` lines (as built in 2.2: the Files-row `Extras` column also counts `/XF`-excluded destination files such as the mount marker, which `/MIR` keeps — verified 2026-10-14); Files `Total` = source count for G2. Destination total from the existing `walk_lan_destination()`. Evaluate limits.
  - **G3 cloud:** pre-sync `rclone sync --dry-run --use-json-log --log-file …`; count planned deletes from the JSON log (`"Skipped delete as --dry-run is set"`). Then the real sync also passes `--max-delete <approved or configured limit>` as a **hard backstop** (rclone's own feature) so even a race between dry-run and sync cannot exceed the limit.
  - **G4** destination identity: store `destination` in each run row; compare; loopback/own-IP detection via `ipaddress` + `socket.gethostbyname` of the UNC host vs `psutil.net_if_addrs()`.
  - **G5** canary on populated destination: today it raises "create the canary"; becomes BLOCKED with an approval that **creates** the canary (so the human decision is recorded).
  - All guards evaluated before `_backup_slot` transfer starts; BLOCK → `*_BLOCKED` row, alert, release slot.
- **Config:** `guards.max_delete_files`, `guards.max_delete_percent`, `guards.max_source_shrink_percent`, `guards.forbid_local_destination`, `approvals.expiry_hours`, `approvals.tolerance_percent`.
- **Tests:** property tests (Hypothesis, already used in the lab) for each guard boundary; lab end-to-end: unmount/empty/dirs-only/renamed source → BLOCKED, destination untouched (byte-identical before/after); approval → exactly one run proceeds; expired/used approval refused; `--max-delete` backstop proven with a forced race.
- **Acceptance:** no scenario in the lab can reduce destination file count by more than the configured limit without an approval row.

### WP4 — Preflight permission checks (R7)
- **Problem:** `/ZB` exits 16 without `SeBackupPrivilege` (audit C-2; reproduced — this machine's account lacks it). Robocopy `/L` cannot detect destination write denial (21 nights of `[WinError 5] Access is denied` on the canary in production logs). Remote shutdown permission is never checked.
- **Design:**
  - **Privilege:** run `whoami /priv /fo csv` via `tool_runner` (built-in Windows tool, verified output format) and parse with `csv`. A privilege listed (Enabled *or* Disabled) is held; robocopy enables it itself. If `lan.use_backup_mode: true` and `SeBackupPrivilege` absent → preflight FAILS with exact remediation text. If `false` → `/ZB` is not passed. **Note:** in an interactive *non-elevated* admin session UAC hides this privilege; the service context shows the real token — the on-box test must run as the service account (WP17).
  - **Destination write probe:** create, write, read-back, delete `<dest>\.aam_write_probe_<run_id>` (excluded by `/XF` like the canary). Any failure → FAILED with the OS error.
  - **Source read probe:** open + read first bytes of the first file found by G1.
  - **Destination free space:** `shutil.disk_usage` on the UNC path vs bytes-to-copy from the `/L` summary `Bytes` row + `lan.min_free_dest_gb` margin.
  - **Cloud write probe:** `rclone touch` + `rclone deletefile` of `<bucket>/_aam_preflight/probe_<run_id>` (outside the FY prefix, so never part of sync/verify). Decision DEC-5 (it leaves a tiny noncurrent version in a versioned bucket).
  - **Remote shutdown permission:** cannot be tested non-destructively. Verified in on-box acceptance (WP17) and truthfully reported at runtime (WP10).
- **Config:** `lan.use_backup_mode`, `lan.min_free_dest_gb`, `cloud.write_probe_enabled`.
- **Tests:** parser tests on captured `whoami` CSV samples (with/without privilege, localized descriptions — parse by privilege *name* column only); lab: read-only share → FAILED before any copy; full disk simulated → FAILED.
- **Acceptance:** each preflight failure message names the missing right and the fix.

### WP5 — Truthful run status
- **Problem (all confirmed):** verify-phase exception leaves `CLOUD_NO_CHANGES_COMPLETE` (probe P1) — counted as last success; post-phase exception leaves `CLOUD_COMPLETE`/`LAN_COMPLETE` (probe P2, audit M-2); slot-acquire timeout raises outside `try` → **no row at all**; status logic duplicated in two pipelines.
- **Design:** one function `decide_terminal_status(leg, phase, sync_status, exception, evidence)` in `core/statuses.py`; rule: *any* exception after the sync phase ⇒ a failure status that preserves the sync verdict in `extended_metrics.sync_status`. Row is opened (`RUNNING`) **before** the slot is requested and finalised in `finally` — so slot timeouts become `*_SLOT_TIMEOUT`. A stale `RUNNING` row older than the max run budget is marked `*_ABANDONED` at agent start (truthful after crashes).
- **Tests:** table-driven test over every (phase × exception × sync status) combination; P1/P2 probes become permanent regression tests.
- **Acceptance:** no code path can end with a failing Prefect state and a success row, asserted by a lab test that injects an exception at every task boundary.

### WP6 — Truthful metrics
- **Problem:** cloud "579 files, 57 MB copied" every no-change night (shared `mtime` column overwritten by the LAN leg; ISO vs float compare); exit 0 runs reported "0 files"; LAN copied count derived from snapshot diff.
- **Design:** take counts from the **tool's own accounting** instead of re-deriving:
  - rclone: final stats line in the JSON log (`stats.transfers`, `stats.bytes`, `stats.deletes`, `stats.errors`).
  - robocopy: Files/Bytes summary rows (parser already exists; add `Bytes` row).
  - Snapshot/manifest diffs remain only as a cross-check; disagreement → WARNING + `metrics_consistent=false` (never silently "fixed").
  - Split `file_entries` timestamps per leg (`lan_mtime`, `cloud_mtime`, `lan_size`, `cloud_size`) via schema migration v2 (versioned migration framework exists).
- **Tests:** replay of real production log fixtures (Sep 12 exit 9 → 0 copied); migration test from a v1 DB copy.
- **Acceptance:** for every run, `files_copied` equals the tool-reported count.

### WP7 — Truthful verification and audit
- **Problem:** daily `--size-only` check labelled "Verification Passed"/"Cryptographic Checks" (audit H-1); audit records DNS/TLS/unreachable failures as "content mismatch, missing-from-dest=20, elapsed_s=0" (Oct 4, Oct 11, Sep 27 in production logs — **false data-loss signals**); capability label rewritten after logging.
- **Design:**
  - Daily check renamed everywhere to **"Size check"** (dashboard badge, Prefect artifact, report, extended_metrics key `size_check_passed`; old key kept for back-compat reads).
  - Audit classification order: (1) process/transport error — rclone log contains a network/TLS/DNS/listing error **or** the destination listing failed → `AUDIT_INCOMPLETE` with the cause; (2) completed with differences → `VERIFICATION_FAILED`; (3) clean → `VERIFIED`. Network error patterns kept in config-overridable list with tests from real log lines (`no such host`, `dial tcp`, `x509`, `error listing`, `directory not found`, `The network path was not found`, `getaddrinfo`).
  - `record_audit` accepts `AUDIT_INCOMPLETE`; dashboard/report show it as "Audit could not run — <cause>", not as divergence.
  - Capability label decided before logging.
- **Tests:** fixtures from the actual Oct 4/Oct 11 log lines; lab: unreachable dest → `AUDIT_INCOMPLETE`; real corruption → `VERIFICATION_FAILED`.

### WP8 — Alerts and reports
- **Problem:** 56 alert emails failed and nothing recorded/showed it; duplicate alerts (SUSPECT ×2, verify failure ×2, 3 identical in 18 min); report flows end `Completed` when the email fails; a week with no runs is logged as "normal" and nothing is sent; weekly+monthly collide when the 1st is a Monday (audit-lab red); SMTP forces auth + STARTTLS.
- **Design:** all through `notify()` (§5.5). Exactly one alert per failed run, sent from one place (the flow tail), using the terminal status from WP5. Report flows raise if delivery fails (Prefect shows Failed; dashboard shows it). "No runs in N days" ⇒ sends a **STALE** alert instead of skipping. Monthly cron default moved to `30 8 1 * *` (config). Recovery email on first success after a failure streak.
- **Config:** `alerts.repeat_cooldown_hours`, `alerts.body_max_chars`, `alerts.attachment_max_kb`, `notifications.smtp_security`, `notifications.smtp_timeout_seconds`, `notifications.retry_attempts`, `notifications.retry_base_seconds`, `reports.stale_after_hours`.
- **Tests:** one-alert-per-failure matrix; cooldown with fake clock; SMTP fake server (stdlib `smtplib` against `aiosmtpd`? — **no new dep**: use a socket-level fake already in audit lab W20); audit-lab `test_w16_schedule_collisions_across_year` turns green.

### WP9 — Dashboard truthfulness
- **Problem:** dashboard is the only non-email channel, yet: `/health` always returns "healthy"; no staleness indicator (11-day outage invisible); email failures invisible; clock "skip" invisible; FY shown from calendar while backups use config FY.
- **Design:** top banner computed server-side from DB: per leg *last success age* vs `dashboard.stale_after_hours` (default 26), open BLOCKED approvals, last audit status, failed notifications in last 7 days, clock unverified, artefact disk low. `/health` returns 200 only when nothing is red, else 503 with reasons (still unauthenticated, no secrets). FY shown = effective FY used by backups. Status labels come from `core/statuses.display_label`.
- **Tests:** endpoint tests for each banner condition (wave 14/19 harness exists).

### WP10 — Wake-on-LAN and NAS shutdown
- **Problem:** 31 WoL timeouts in production; shutdown result never checked (function returns dict, caller only catches exceptions); `1190 already scheduled` logged as failure; TCP-445 connect treated as "online" even if SMB is not serving (audit L-5).
- **Design:**
  - WoL: unchanged mechanism (magic packet via `wakeonlan`, already a dependency); readiness = TCP 445 **plus** a real SMB operation (`os.stat` of the destination canary) before declaring online. Each attempt logged with timings into the run artefacts.
  - Shutdown: only after `LAN_COMPLETE` (kept). Result inspected: `0` → poll TCP 445 until closed within `wol.shutdown_verify_timeout_seconds`; `1190` → treated as "already scheduled" (INFO); other codes → ERROR + dashboard flag `nas_shutdown=FAILED` (the backup status itself stays truthful and separate). Never shut down while another LAN run/audit holds or is queued for the slot.
  - Integrity audit LAN leg follows the same wake/shutdown helpers (no duplication — today `flow.py` re-implements the conditions).
- **Realistic wake timings (owner issue 2026-10-14):** the post-WoL / preflight SMB probe is hard-coded to a **2.0 s** connect timeout × 4 attempts × 3 s wait (`core/lan_preflight.py:36,66` ≈ 15 s total) and `core/wol.py:19` hard-codes 5 s; `wake_timeout_seconds` is capped at 600 s (`models/config.py:119`). A NAS cold boot (POST + RAID/disk spin-up + OS + SMB service) routinely exceeds these → false "not reachable" failures (31 WoL timeouts in production logs). Fix: one shared readiness function (`wait_for_smb_ready`) used by WoL, LAN preflight and the LAN audit, with **all timings from config** and generous real-world defaults:

  | Key | New default | Bounds | Was |
  |---|---|---|---|
  | `wol.wake_timeout_seconds` | 900 | 60–3600 | 300 (max 600) |
  | `wol.ping_interval_seconds` | 15 | 5–120 | 15 |
  | `wol.stability_wait_seconds` | 90 | 0–900 | 30 |
  | `wol.wake_retry_count` (magic-packet rounds) | 5 | 1–20 | 3 |
  | `wol.wake_retry_interval_seconds` | 10 | 1–120 | 5 |
  | `wol.smb_connect_timeout_seconds` | 15 | 1–120 | 2.0 / 5.0 hard-coded |
  | `lan.preflight_smb_attempts` | 8 | 1–60 | 4 hard-coded |
  | `lan.preflight_smb_wait_seconds` | 15 | 1–300 | 3 hard-coded |

  Readiness = TCP 445 open **and** a real SMB operation (stat of the destination canary) succeeds **and** the stability wait has passed. Every attempt is logged with elapsed time, so a slow NAS can be tuned from the run artefacts. Defaults are starting points: the on-box acceptance run (WP17) measures the real cold-boot time and the config is set to measured time × 2.
- **Config:** `wol.smb_ready_check_canary` (bool), `wol.shutdown_delay_seconds` (today hard-coded 300), `wol.shutdown_verify_timeout_seconds`, plus the wake-timing keys above.
- **Tests:** fake `shutdown.exe` outcomes; on-box test with the real NAS (WP17).

### WP11 — FY rollover
- **Problem:** final backup runs outside the backup slot/lock (concurrent mirrors possible); detection is `!=` so a clock behind rolls **backwards** (probe P4); new FY source folder is empty → every backup fails from April 1; archive transition `600 s` hard limit for a whole FY of objects; boot-time rollover in `launch.py` runs synchronously before the scheduler starts; default `config_path="config.yaml"` CWD-relative.
- **Design:**
  - Rollover runs inside `_backup_slot()` and reuses the **normal pipelines** (`_run_cloud_pipeline` / `_run_lan_pipeline` with an explicit FY override) — so the final backup gets the same guards, verification, run rows and alerts. No separate copy of sync logic.
  - G6: proceed automatically **only** when computed FY == configured FY + 1 and both final backups end `*_COMPLETE`; anything else → `ROLLOVER_BLOCKED` + approval (DEC-4).
  - New source FY folder gets a placeholder `README_FY.txt` (content explains the folder) so G1 passes and the first nights back up truthfully. (Name configurable.)
  - Config update stays atomic (`ruamel` + `os.replace`, already correct); a timestamped backup of the old `config.yaml` is written first.
  - Archive transition: timeout from config; failure = WARNING + dashboard flag; never blocks the rollover; idempotent re-run command documented.
  - Boot-time rollover removed from `launch.py`; the scheduled `rollover-check` deployment is the single entry (plus a manual dashboard trigger).
  - Rollover recorded in `run_history` (mode `rollover`) with old/new FY.
- **Tests:** fake-clock lab (wave 12 exists): Mar 31 23:59 → Apr 1 00:00; clock behind; one leg failing; agent restart mid-rollover (idempotency); concurrent nightly run queued on the slot.

### WP12 — Health checks
- **Problem:** clock-skew check passes when Google is unreachable/TLS-intercepted (24 nights "skipping"); free-space check is on the *source* drive, not the destination.
- **Design:** clock result tri-state `OK | SKEWED | UNVERIFIED`. `SKEWED` → FAILED. `UNVERIFIED` → policy `health.clock_unverified_policy: warn|block` (DEC-6) — default `warn`: run proceeds, run row + dashboard show "clock unverified". Fallback time source: `w32tm /stripchart /computer:<health.ntp_server> /samples:1 /dataonly` (built-in tool) before declaring UNVERIFIED. Destination free space moved to WP4.
- **Config:** `health.clock_unverified_policy`, `health.ntp_server`.

### WP13 — Watchdog
- **Problem:** restart fails with `sc stop` 1051 (dependent agent running) and logs an empty reason (`sc` writes to stdout); agent stuck in `STOP_PENDING` 14+ min with no escalation; thresholds hard-coded; shares a log file with NSSM.
- **Design:** restart sequence: stop `AamBackupAgent` → wait STOPPED (config timeout) → stop `AamPrefectServer` → start server → wait API healthy → start agent. `STOP_PENDING` beyond `watchdog.stop_pending_timeout_seconds` → kill the service PID (`sc queryex` PID + `psutil`) — **only** if no live backup lock, otherwise alert and wait. All constants (interval, thresholds, deferral caps, breaker) moved to `watchdog:` config section. Every watchdog CRITICAL also goes through `notify()` (de-duplicated) and is written to the DB so the dashboard shows it.
- **Tests:** existing W17 harness extended with 1051 path and STOP_PENDING escalation.

### WP14 — Concurrency and time budget
- **Problem:** worst-case cloud run ≈ 6 h × 3 attempts + verify/diff/manifest up to 5 h each; next job waits only `concurrency_wait_seconds` (1 h) then fails unrecorded; audit and rollover share the slot.
- **Design:** config validator computes each job's worst-case duration from its timeouts × attempts and, using **cronsim** (already locked), the minimum gap to the next job that needs the slot; if worst-case > gap + wait → config load fails with the arithmetic in the message. Slot timeout recorded truthfully (WP5).
- **Tests:** validator tests on the default and the production-demo configs.

### WP15 — Database and parser robustness
- `busy_timeout` first; dedup into migration (§5.4) — audit-lab M-6 reds turn green.
- `decide_lan_result` and all exit-code consumers reject `bool` (audit W13 finding).
- Robocopy summary parser: read UTF-16 `/UNILOG`; keep positional parsing; English label dependency documented and asserted by a startup self-test (`robocopy /?` header check) so a localized OS fails loudly instead of misparsing.

### WP16 — Deployment and test isolation
- Services start with `uv run --frozen --no-sync python …` (no network dependency at boot — Sophos).
- `AAM_CONFIG` env set by installer to the absolute config path.
- Installer documents (and `04_check_readiness.ps1` verifies) the service account rights: `SeBackupPrivilege` (if `use_backup_mode`), NAS share + NTFS write, remote shutdown right on the NAS. Granting rights stays a documented manual step (secpol.msc / NAS admin) — the readiness script and the preflight (WP4) *verify* them.
- Test isolation guard in `tests/conftest.py` and `audit_lab/conftest.py`: abort if config resolves to the production runtime dir, a real bucket name list, or a non-loopback UNC; loguru sinks in tests always point at `tmp_path`.
- Remove `MagicMock/` junk dir and stray probe scripts from the repo root (after owner review — DEC-1).

### WP17 — Verification and acceptance
1. Unit + regression tests for every WP (red-first evidence in each PR).
2. Audit lab: full run must be green except intentional reds that the owner accepts in writing (target: zero).
3. **On-box acceptance (Wave 7) on the client's Windows Server 2016, as the service account**, with production config:
   - preflight shows all permissions OK (or fails with the exact missing right);
   - cloud backup end-to-end; LAN backup with real WoL wake + verified shutdown;
   - forced failure → exactly one alert email received + dashboard red;
   - mass-delete simulation on a scratch FY folder → BLOCKED → approve → single run;
   - weekly + monthly report received;
   - FY rollover rehearsal on a scratch config with a fake clock;
   - integrity audit VERIFIED, then network blocked → `AUDIT_INCOMPLETE` (not mismatch);
   - **restore test**: `deploy/09_restore_from_gcs.bat` restores a sample folder; hashes compared;
   - 7 consecutive scheduled nights with zero untruthful rows (reviewed against run artefacts).
4. Sign-off checklist stored in `docs/audit/ACCEPTANCE_<date>.md`.

---

## 8. Config keys (new or moved from code)

| Section.key | Default | Was |
|---|---|---|
| `guards.max_delete_files` | 500 *(DEC-2)* | none |
| `guards.max_delete_percent` | 10 *(DEC-2)* | none |
| `guards.max_source_shrink_percent` | 20 *(DEC-2)* | none |
| `guards.forbid_local_destination` | true | none |
| `approvals.expiry_hours` | 12 | — |
| `approvals.tolerance_percent` | 0 | — |
| `run_logs.dir` | `<runtime_dir>/run_logs` | — |
| `run_logs.retention_days` | 90 | — |
| `run_logs.compress_after_kb` | 1024 | — |
| `run_logs.min_free_gb` | 5 | — |
| `maintenance.log_retention_days` | 90 | 30 in `core/logging.py` |
| `maintenance.service_log_retention_days` | 90 | never pruned |
| `lan.use_backup_mode` | true | `/ZB` hard-coded |
| `lan.min_free_dest_gb` | 5 | none |
| `lan.preflight_smb_attempts` / `lan.preflight_smb_wait_seconds` | 8 / 15 | 4 / 3 hard-coded `lan_preflight.py` |
| `wol.smb_connect_timeout_seconds` | 15 | 2.0 (`lan_preflight.py`) / 5.0 (`wol.py`) hard-coded |
| `wol.wake_timeout_seconds` / `stability_wait_seconds` / `wake_retry_count` / `wake_retry_interval_seconds` | 900 / 90 / 5 / 10 | 300 (cap 600) / 30 / 3 / 5 — see WP10 table |
| `cloud.write_probe_enabled` | true *(DEC-5)* | — |
| `cloud.check_checkers` | 4 | hard-coded |
| `cloud.retries_sleep` | `30s` | hard-coded |
| `cloud.error_tail_lines` | 100 | whole log |
| `audit.checkers` / `audit.retries` | 1 / 3 | hard-coded |
| `audit.detail_max_paths` | 100 | hard-coded |
| `audit.network_error_patterns` | list (WP7) | missing |
| `wol.shutdown_delay_seconds` | 300 | hard-coded |
| `wol.shutdown_verify_timeout_seconds` | 600 | — |
| `wol.smb_ready_check_canary` | true | — |
| `health.clock_unverified_policy` | `warn` *(DEC-6)* | silent pass |
| `health.ntp_server` | `time.windows.com` | — |
| `alerts.repeat_cooldown_hours` | 12 | none |
| `alerts.body_max_chars` / `attachment_max_kb` | 1000 / 2048 | hard-coded / unbounded |
| `notifications.smtp_security` | `starttls` | forced |
| `notifications.smtp_timeout_seconds` / `retry_attempts` / `retry_base_seconds` | 30 / 3 / 10 | hard-coded |
| `reports.stale_after_hours` | 26 | — |
| `dashboard.stale_after_hours` | 26 | — |
| `dashboard.public_url` | `http://<bind_address>:<port>` | — (used in attention emails, DEC-3) |
| `schedule.*` | required in config (DEC-7) | code defaults |
| `dashboard.session_ttl_hours` / rate limits | 24 / 5,10,10 | hard-coded |
| `watchdog.*` (interval, failure_threshold, request_timeout, backup_wait_interval, max_deferrals, max_transfer_deferrals, restart_cooldown, max_starts_per_window, start_window_seconds, stop_pending_timeout_seconds) | current values | hard-coded |
| `schedule.monthly_cron` | `30 8 1 * *` | `0 8 1 * *` |
| `rollover.source_placeholder_name` | `README_FY.txt` | — |

All new sections get pydantic bounds (`ge`/`le`) and descriptions; `config.example.yaml` updated with comments; `extra="forbid"` everywhere.

## 9. Milestones and definition of done

| Milestone | WPs | Exit criteria |
|---|---|---|
| M0 Baseline | WP0 | branch created, committed suite green, lab baseline recorded |
| M1 Foundations | WP1, WP2, WP15, WP16 (config/test isolation part) | shared blocks in use; full logs stored; DB reds green |
| M2 Data safety | WP3, WP4, WP11, §6 approvals | lab proves no unapproved mass deletion; preflight permission checks; rollover safe |
| M3 Truth | WP5, WP6, WP7, WP8, WP9 | no success row on failure; metrics = tool counts; audit classification; one alert per failure; dashboard banner |
| M4 Operations | WP10, WP12, WP13, WP14, rest of WP16 | WoL/shutdown verified; watchdog restart works; budget validator |
| M5 Acceptance | WP17 | on-box acceptance signed; 7 truthful nights |

**Definition of done (per WP):** red-first regression test → fix → full suite + audit lab green → code review (diff read back line by line) → docs/config example updated → commit referencing finding IDs.

---

## 10. Handover checklist (environment — owner/client IT)

1. **Sophos:** exclude `storage.googleapis.com`, `oauth2.googleapis.com`, `www.googleapis.com` from TLS inspection; allow outbound to the SMTP host/port; stable DNS.
2. **Service account — ROOT CAUSE of the NAS "permission issue" (verified read-only on the production-trial box 2026-10-14):** the server is in a **WORKGROUP** (not domain-joined) and all three services run as **LocalSystem**. In a workgroup, LocalSystem reaches network shares without usable credentials, so the NAS denies it — matching the 21 nights of `[WinError 5] Access is denied` on the canary, while manual runs as Administrator worked. Fix: run `AamBackupAgent` (and `AamWatchdog`, which sends alerts) as a **dedicated local account** with the same username/password on the NAS (workgroup pass-through) or with NAS credentials stored for that account (`cmdkey`, run as that account). The WP4 destination write-probe will catch this before any copy. That account needs (one dedicated account, not a personal one): NAS share + NTFS modify on the FY root; `SeBackupPrivilege` (if `use_backup_mode`); "Force shutdown from a remote system" on the NAS + remote shutdown allowed by its firewall.
3. **NAS:** Wake-on-LAN enabled in BIOS/UEFI and NIC power settings, wake from S5 verified; static IP / DHCP reservation; MAC recorded.
4. **Production config:** real NAS path, real bucket, intended schedule; remove `CHAOS01`, demo bucket, test schedules.
5. **Clean test data** from `E:` and the bucket (`E2E_TEST_FY`, `dummy_*`, canary-test folders) — with owner confirmation, never scripted blindly.
6. **Never run the test suites on the client box** (the isolation guard will also refuse).
7. **Restore drill** performed and recorded before go-live.

---

## 11. Owner decisions (resolved 2026-10-14)

| ID | Decision |
|---|---|
| DEC-1 | Commit the existing test/lab/docs work on `chaos-validation` first, then branch `mvp-hardening` from that commit. Raw lab run output (`audit_lab/raw/*.err`, `*.txt`, coverage json), `.coverage`, `qc`, `query` and the one-off root scripts are **not** committed; listed for owner review. |
| DEC-2 | Recommended thresholds (500 files / 10 % deletions, 20 % source shrink) are the **defaults**. **Every parameter must be configurable via config** — this extends rule R5 to all tunables, not only thresholds (§2 rule 10). |
| DEC-3 | Block + approve-and-rerun. **Additionally: every BLOCKED run (and every other issue that needs attention) emails the configured recipients, telling them to open the dashboard** — the email includes the dashboard URL (`dashboard.public_url`, config) and the blocked numbers; the decision itself is only taken on the dashboard. |
| DEC-4 | FY rollover automatic only for the normal +1 FY with both final backups COMPLETE; anything else → approval. |
| DEC-5 | Cloud write probe enabled (configurable). |
| DEC-6 | Clock unverified → warn + dashboard flag (policy configurable). |
| DEC-7 | Production schedule is set via config (no code default assumed); the budget validator (WP14) checks whatever is configured. |
| DEC-8 | Keep `/ZB`; grant `SeBackupPrivilege` to the service account; preflight verifies it. |

<details><summary>Original questions and recommendations</summary>

| ID | Question | Recommendation |
|---|---|---|
| DEC-1 | Uncommitted work on `chaos-validation` (53 files: tests, audit_lab, docs, stray scripts, `MagicMock/`): commit to `chaos-validation` first, or carry into the new branch? | Commit test/lab/docs work on `chaos-validation`; delete `MagicMock/` and one-off scripts after your review; branch `mvp-hardening` from that commit. |
| DEC-2 | Guard thresholds: max deletions (files and %), max source shrink %. | 500 files / 10 % / 20 % to start; tune from 30 nights of artefacts. |
| DEC-3 | Approval mechanism: block + approve-and-rerun (recommended) vs Prefect `pause_flow_run`. | Block + approve-and-rerun (§6.2). |
| DEC-4 | FY rollover: fully automatic when it is the normal +1 FY and both final backups COMPLETE, or always require approval? | Automatic in the normal case, approval for anything else. |
| DEC-5 | Cloud write probe (leaves a tiny noncurrent object version per run under `_aam_preflight/`, deleted by lifecycle after 92 days). | Enable. |
| DEC-6 | When the clock cannot be verified (Sophos/DNS), warn and continue, or block the cloud leg? | Warn + show on dashboard (GCS itself rejects a badly skewed clock, which then fails truthfully). |
| DEC-7 | Production schedule: LAN 01:00 / cloud 18:00 (code defaults) or 21:00 / 22:00 (what ran on the demo box)? Office closing time and Tally/Office files open at sync time matter. | Your call; the budget validator (WP14) will check it. |
| DEC-8 | Robocopy `/ZB`: keep (grant privilege) or drop (`use_backup_mode: false`)? | Keep, grant privilege — fewer "access denied" files; preflight verifies. |

</details>

---

## 12. Traceability — findings → work packages

| Finding (review 2026-10-14) | Evidence | WP |
|---|---|---|
| Dirs-only/empty source wipes destination (C-1) | probe P6, audit lab C-1 | WP3 (G1) |
| No mass-delete limit; target silently changed to loopback | prod logs Sep 9 → Oct 7 | WP3 (G3, G4) |
| `/ZB` without privilege → exit 16 | audit lab C-2, `whoami /priv` on this machine | WP4 |
| Canary access denied on 21 nights | prod logs Jul 10 → Aug 20 | WP4 write probe, §10 |
| Rollover outside slot / backwards / empty new FY / 600 s archive | code `core/fy_rollover.py`, probe P4 | WP11 |
| Success row on failed run (verify / post phase) | probes P1, P2; audit M-2 | WP5 |
| No row on slot timeout | code `flow.py:154` outside try | WP5 |
| Cloud copied metric wrong (579 files nightly) | prod logs Sep 12–25 | WP6 |
| 11 MB error message into DB/email | probe P3 | WP1 (§5.1) |
| Audit reports network failure as content mismatch | prod logs Sep 27, Oct 4, Oct 11 | WP7 |
| Size-only check labelled "Verification Passed" | `ui`/artifact text; audit H-1 | WP7 |
| 56 undelivered alerts, invisible | prod logs | WP8 (§5.5), WP9 |
| Duplicate alerts | code + prod logs Sep 1 | WP8 |
| Report flows "Completed" on email failure; no-runs = "normal" | prod logs Aug 3, Sep 1 | WP8 |
| Weekly/monthly collision | audit-lab red W16 | WP8 |
| `/health` always healthy; outage invisible 11 days | code `ui.py:576`; prod logs Aug 22–Sep 1 | WP9 |
| Shutdown result unchecked; WoL timeouts | code `core/shutdown.py`; prod logs | WP10 |
| Clock check fail-open under Sophos | prod logs (24×) | WP12 |
| Watchdog `sc stop` 1051; STOP_PENDING no escalation | prod watchdog logs | WP13 |
| Timeout budget vs 1 h slot wait | code/config | WP14 |
| ManifestDB busy_timeout order (M-6) | audit-lab reds W9/W1e | WP15 (§5.4) |
| Bool exit code accepted | audit W13, probe P7 | WP15 |
| Typo'd config keys ignored | probe P5 | §5.7 |
| `uv run` may sync at boot; CWD-relative config | install script, `models/config.py:14` | WP16, §5.7 |
| Tests polluting production logs/bucket/source | prod watchdog logs, bucket `E2E_TEST_FY` | WP16 |
| Watchdog + NSSM writing the same file | prod watchdog logs | WP2 |
| Full logs not retained | code `lan_sync.py`, `cloud_sync.py` finally blocks | WP2 |
| Committed suite red (24) | test run 2026-10-14 | WP0 |

---

## 12a. Execution notes (recorded during M1)

- **Tool-runner migration is sequenced by WP**, not done all at once: the two transfer modules (rclone sync, robocopy) moved in M1 1.5 (full logs). The checks (cloud verify/diff/size/manifest, integrity audit) move in WP6/WP7, preflights in WP4, shutdown in WP10, watchdog `sc` in WP13, rollover gcloud in WP11 — each together with the rewrite that WP does anyway, so ~190 test mocks are changed once, not twice.
- **Config keys are added in the same commit as the code that reads them** (no dead config).
- **WP8 must also fix (found in M1 1.6):** `core/report.py` treats `SMTPSenderRefused` / `SMTPRecipientsRefused` as permanent, but smtplib raises them for transient 4xx replies too (e.g. `451 try again later`), so retryable alerts are given up after one try. `core/notify.py` already classifies by reply code (5xx permanent, 4xx retried); WP8 moves every alert/report send onto `core.notify.send_email` (~29 tests coupled to report.py SMTP internals change then, once).
- **Legacy statuses in the production DB:** 59 historical rows are `CLOUD_SKIPPED` / `LAN_SKIPPED` although they were preflight *failures* (pre-v1.1 behaviour). History is never rewritten (audit principle); WP5/WP9 will display legacy `*_SKIPPED` rows that carry an `error_message` as "Failed (legacy record)" and count them as failures in reports.
- **Production-trial facts (read-only, 2026-10-14):** Windows Server 2016 Standard, workgroup, services as LocalSystem, rclone v1.74.3, robocopy 10.0.14393; SSH user holds SeBackup/SeRestore/SeRemoteShutdown; both configured LAN targets (10.10.186.231, 10.10.151.53) do not answer on 445; LAN preflight message still shows the hard-coded "within 2.0s".

## 13. Execution checklist (subtasks)

Cost-control rules for execution: work inline (no parallel agents), run only the affected test files while iterating, run the full suite + audit lab once per milestone (not per commit), read files in targeted ranges.

### M0 — Baseline
- [x] 0.1 `.gitignore`: `audit_lab/raw/*.err`, `audit_lab/raw/*_report.txt`, `.coverage`, `htmlcov/`, `MagicMock/`
- [x] 0.2 Fix 21 stale tests to current intended behaviour (no production change)
- [x] 0.3 `skipif` the 3 chaos-tool tests when `C:\ChaosTest\tools` is absent
- [x] 0.4 Baseline recorded (markers deferred: `tests/` and `audit_lab/` are already separate roots; on-box tests live only in `audit_lab/test_w7_deployed.py`); record baseline in `docs/audit/BASELINE_mvp-hardening.md`

### M1 — Foundations
- [x] 1.1 Config hygiene: `extra="forbid"`, absolute `CONFIG_PATH` (`AAM_CONFIG`), deprecated-key handling. New config keys are added with their consumer (run_logs/alerts/notifications in 1.4-1.6; **WoL timings move to 2.5**)
- [x] 1.2 `core/statuses.py`
- [x] 1.3 `ManifestDB.from_config()` + busy_timeout-first + dedup into migration (WP15)
- [x] 1.4 `core/tool_runner.py` + migrate call sites one module per commit
- [x] 1.5 `core/run_artefacts.py` + retention purge task (WP2)
- [x] 1.6 `core/notify.py` (tenacity, delivery table, cooldown, dashboard link)
- [x] 1.7 Test-isolation guard (WP16)
- [x] 1.8 Bool exit-code rejection (WP15)

### M2 — Data safety
- [x] 2.1 `core/guards.py` G1–G5 + property tests (e41db7f) — pure module, wiring is 2.2
- [x] 2.2 LAN deletion count from `/L` summary; cloud `--dry-run` count + `--max-delete` backstop (7dcea6e LAN, efb2d98 cloud, 219f057 G1 replaces health emptiness). G1–G4 wired; G5 (canary → BLOCKED + approval that recreates it) moves to 2.3 with the approval action. Baselines are per leg **and FY** (rollover starts a fresh baseline, DEC-4). Measurement failure = `*_FAILED`; guard verdict = `*_BLOCKED`.
- [x] 2.3 Approvals (8afa499 request + attention email + approve-once rerun; 0c627c0 dashboard approve/dismiss/run; 975d499 G5 wired, approval recreates the marker). Approving needs `dashboard.auth_enabled: true`; a scheduled run never consumes an approval; the approval is used up before any transfer.
- [x] 2.4 Preflight permission checks (99ad2c1): `whoami /priv` (SeBackupPrivilege, only with `lan.use_backup_mode`), destination write probe, source read probe, NAS free space (`/BYTES`), cloud write probe; failures are `*_FAILED` with the fix. LAN dry-run on `run_tool` + `/UNILOG`, full listing kept and streamed (efc222b).
- [x] 2.5 Shared `wait_for_smb_ready` wired into WoL + LAN preflight with config timings (b59a6d6)
- [x] 2.6 FY rollover (f778164): forward-only (`classify_rollover`; clock behind = refused, not approvable), final backups through the normal pipelines, G6 approval for JUMP / failed final backup, switch inside the slot (folders + `README_FY.txt` placeholder, config backup, atomic update, archive last via run_tool), boot-time rollover removed from launch.py.

### M3 prep already done
- [~] WP7 prep: `core/rclone_errors.py` transport-failure classifier from real prod signatures (f4cc48b); TODO use it in `core/integrity.py` (AUDIT_INCOMPLETE) and preflight error text

### M3 — Truth
- [ ] 3.1 Terminal-status function; RUNNING row before slot; SLOT_TIMEOUT / ABANDONED (WP5)
- [ ] 3.2 Metrics from tool accounting; schema v2 per-leg size/mtime (WP6)
- [ ] 3.3 "Size check" relabel; audit `AUDIT_INCOMPLETE` classification (WP7)
- [ ] 3.4 One alert per failure; report flows fail on send failure; stale alert; monthly cron offset (WP8)
- [ ] 3.5 Dashboard banner, truthful `/health`, status labels from server (WP9)

### M4 — Operations
- [ ] 4.1 Shutdown result verification (WP10)
- [ ] 4.2 Clock tri-state + `w32tm` fallback (WP12)
- [ ] 4.3 Watchdog restart order, STOP_PENDING escalation, config, own log file (WP13)
- [ ] 4.4 Schedule/time-budget validator with cronsim (WP14)
- [ ] 4.5 Installer: `uv run --frozen --no-sync`, `AAM_CONFIG`; readiness script verifies rights (WP16)
- [ ] 4.6 `config.example.yaml` fully documented

### M5 — Acceptance
- [ ] 5.1 Full suite + audit lab green; review of every diff
- [ ] 5.2 On-box Wave 7 checklist (WP17), incl. measured NAS cold-boot time → WoL config
- [ ] 5.3 7 truthful nights; sign-off doc
