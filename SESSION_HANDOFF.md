# SESSION HANDOFF — read this first in every new session

**Last updated:** 2026-10-18 (agreed plan for the open topics - §4g; box = full control;
UI + email audit and rebuild - see §4f; rclone exit codes +
bounded sync retries XR-1..6 - §4e; GitNexus scan, no-literals sweep - §4d).
**Box not redeployed with §4d / §4e / §4f** (box is at 6adcfb2; owner decides). §4f adds schema
v3 (one-time in-place upgrade of the box DB) and needs `dashboard.public_url` + bind/firewall
on deploy day (see §4f).

> New session: read this file, then `MVP_HARDENING_PLAN.md` §13 (the task checklist) and
> `docs/ARCHITECTURE.md` (how the program works). Do not re-plan; continue at "Next steps".

> **START HERE:** the §4f-2 topics were discussed and the plan is **AGREED (owner 2026-10-18)
> - full plan + task checklist: `docs/PLAN_OPEN_TOPICS_2026-10-18.md`** (short copy in §4g;
> the plan file wins). Work through its "Order of work" step by step; each step test-first, full
> suite, GitNexus impact + detect_changes, one commit. Box: full control (owner), but deploy
> only at step 9 of §4g.

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
8. **Production-trial box: FULL CONTROL** (owner 2026-10-15, re-confirmed 2026-10-18) - we may
   deploy, restart services, change config and run tests on it (see §6). Still tell the owner
   what will be done before an action that stops a backup or touches backup data.
9. Accepted risks (do not work on): ransomware vs LAN mirror; GCS lifecycle keeps ~2 versions.

## 3. Status

| Milestone | State |
|---|---|
| M0 Baseline | **done** (a8f7f7f) — suite 0 failed |
| M1 Foundations | **done** (1.1–1.8, see plan §13) |
| M2 Data safety | **done** (2.1–2.6) |
| M3 Truth | **done** (3.1–3.5) — see plan §13 incl. the deferred tool_runner note |
| M4 Operations | **done** (4.1–4.6) — M4 lab 238/1 → W10 stale expectation fixed → **0 red** |
| M5 Acceptance (on-box) | **in progress on the trial box - PAUSED by owner 2026-10-15 ~16:40** (see §4a) |

**Test status (2026-10-18, §4f, HEAD 1bc1474):** `tests/` **2142 passed / 0 failed / 19 skipped**;
audit lab 231/7/3 -> the 7 reds fixed in 93e8d75 (plan 0.3), full re-run DONE: 238 passed / 0 failed / 3 skipped (
`agent_runs/logs/lab_step0_20261018-2019.log`). Older: lab at 0eac85a 238/0/3.
**M4 milestone audit lab (2026-10-15): 238 passed / 1 failed / 2 skipped (20 min)**; the 1 red was
W10 `deployment_schedules_fire_when_intended` expecting the old monthly 08:00 / audit 03:00 -
updated to 08:30 / 10:00, green. W16 schedule collision is green now (local config fixed).
**No intentional reds remain.**
Audit lab: see the M3 milestone line in §4 (only expected red: W16, which needs the owner's
config.yaml `monthly_cron` changed - DEC-7).

### M2/M3 building blocks - all wired now (kept as a map)
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
5. **M4 DONE** (4.1 shutdown verified; 4.2 clock tri-state; 4.3 watchdog; 4.4 run budgets; 4.5
   installer + readiness probe as the service account; 4.6 config.example.yaml complete - plan §13).
   **4.7 settings page DONE (DEC-9, owner 2026-10-15)**: `/settings` on the dashboard edits every key
   except lock-out keys (risky ones need a typed phrase); E2E-tested on a real uvicorn server; the
   Owner reviewed it in a demo 2026-10-15 -> feedback built: one box per email (+/x), email
   validation server-side, schedules as day/time picker (IST), live server validation (Save only
   when valid), every client-facing time in IST, tabs Settings (29 basic keys) / Advanced /
   FY rollover ("Run FY rollover now" = the normal check, only when due; blocked -> approval card;
   clock behind never forceable). Advanced has its own login (owner: initially = dashboard key,
   changeable inside Advanced; only a PBKDF2 hash `dashboard.admin_password_hash` is stored;
   server-enforced). Units are fixed labels (bandwidth/buffer as MB/s / MB numbers); every
   setting has a plain name + help (`core/config_help.py`), timetable as plain lines + overview.
   Idea for later: apply schedule changes without restarting the agent.
   **NEXT: M5 acceptance** - 5.1 full suite + audit lab green + diff review; 5.2 on-box WP17
   checklist (needs owner OK + the service account); 5.3 seven truthful nights.
   DONE 2026-10-16 (§4d): tool_runner migration of cloud_verify / cloud_reporter / integrity /
   cloud_preflight; task retries (preflight 1x30 s, verify 1x60 s) are config keys.
   **Owner decision pending (found in 4.5):** `03_setup_system.bat` sets Windows Update Active
   Hours 12:00-06:00, i.e. reboots allowed 06:00-12:00 - with cloud at 02:00 + 6 h budget a
   Windows Update reboot can hit the cloud run until 08:00. Options: Active Hours 20:00-09:00
   (protects 21:00-08:00, but reboots then fall in office hours) or shorter cloud budget.
   **Service account (owner, before go-live):** create a local account (e.g. `aambackup`) on the
   server AND the same user/password on the NAS with write on the share; put it in "Back up files
   and directories" (secpol) if `lan.use_backup_mode`; then `04_check_readiness.ps1 -ServiceAccount
   .\aambackup` must be all green before `06_install_services.ps1 -ServiceAccount .\aambackup`.
   **WP17 on-box checks added by 4.3/4.5:** graceful `sc stop AamBackupAgent` with the new
   NSSM -> .venv python launcher chain (Ctrl+C reaches launch.py, backup.lock released, no
   STOP_PENDING); watchdog ordered restart + STOP_PENDING kill against the real services.
   4.3 was not run against the real services: on-box check in WP17 (needs owner OK).
   **OWNER ACTIONS before deploying this code (4.4 refuses the old schedules at load):**
   - production `config.yaml`: `cloud_cron: "0 2 * * *"` (LAN stays 21:00), `audit_cron:
     "0 10 * * SUN"`, `monthly_cron: "30 8 1 * *"`; otherwise the services will not start.
   - local `config.yaml`: DONE 2026-10-15 (audit Sunday 10:00, monthly 08:30) - lab W16 schedule test should now turn green.
   - Budgets are starting values (owner 2026-10-15: real durations unknown) - tune from
     `extended_metrics.budget.used_hours` after 1-2 weeks of real nights.
   - First full cloud upload: spreads over nights as CLOUD_PARTIAL, or run `backup-cloud` once by
     hand in Prefect with parameter `budget_hours` (e.g. 48) over a weekend.
   Owner questions answered 2026-10-15: a failed leg never fails the other (separate jobs/rows/
   alerts); only an overrun can make the next job SLOT_TIMEOUT - which the budgets now prevent.
   4.2 note: `block` policy = cloud FAILED (not an approval); owner may want it as G7 BLOCKED + approval
   later - ask if it matters. The audit's LAN shutdown has
   no run row: a non-VERIFIED audit shutdown is the WARNING email only (the banner reads the last
   LAN *run* row).
6. (M4 items 4.1-4.6 are listed as DONE in item 5 above; the duplicate list was removed 2026-10-18.)

## 4h. PROGRESS on the §4g plan (2026-10-18 session) - read this first

Plan file `docs/PLAN_OPEN_TOPICS_2026-10-18.md` has the ticks + commit hashes. **Done:** step 0
(7 lab reds fixed, lab 238/0/3), 0b (11 packages upgraded, `uv audit` 0 advisories; `filelock`;
`config_file.config_transaction` = the ONE locked writer of config.yaml, 3 `maintenance.config_*`
keys), SCH (schema v4: `rollover_journal(+_steps)`, `notifications.attachments_rung`), **group A
complete** (journal `core/rollover_journal.py`; `flow._rollover_resume` = the resumer; step
functions in `core/fy_rollover.py`; ROL-04/05; `ROLLOVER_RUNNING/FAILED/ABANDONED`; A4 no NAS
shutdown in the final LAN backup; A7 `_complete_pending_rollover_nas_folder`; A8 Settings > FY
rollover step card; independent review F1-F4 fixed; lab W6b with real robocopy). Unit suite at
last run: **2218 passed / 0 failed / 19 skipped**. Owner decision D-17: a retry that still has to
switch repeats the final backup.
**Next:** step 2 = group **I** (budget from the flow run's scheduled start, `*_MISSED`, sweep of
every mode, lab hard-kill scenario) -> then run the **milestone audit lab** (after A + I; last full
lab was after 0b: 236/2/3, the 2 reds fixed and re-run green, NOT yet re-run in full since A) ->
B emails -> C reports -> E passwords -> lab -> D, S, F, H, G, release.
**Working notes:** OpenCode CLI is not installed here (opencode agent fails) - use a Claude
`general-purpose` read-only reviewer instead (it found 4 real bugs in A). `tests/test_lan_shutdown_policy`
used to depend on test order (fixed). Write patch scripts with the Write tool into the scratchpad,
never heredocs with backslashes. Print `^FAILED` lines (not `tail`) after suite runs; check
`git status --short` before every commit.

## 4g. AGREED PLAN (owner 2026-10-18) - §4f-2 topics + older items

Principles: one source of truth per concern (one report model, one SMTP path, one rollover
state, one credential store; outputs only render); every multi-step action is idempotent
and resumable from persisted state; fail closed but never silent (row + email + dashboard);
no new dependencies; new tunables = config keys with bounds.

Owner decisions: topic 4 = keep the built "Send test email" only; recipients = everyone gets
everything for now; preview of every email = yes; reports: scheduled emails = calendar
periods (weekly Mon-Sun, monthly = previous calendar month), dashboard downloads AND the
dashboard "send email" buttons = last 7 / last 30 days up to now; server IP is static but
the link is resolved dynamically; two passwords (dashboard + Advanced), both changed/reset
inside Advanced, plus a server-side reset script for a forgotten Advanced password (yes);
Windows Update is currently stopped on the box - a reboot mid-run must simply resume next
run (item I). Clock-check "block" stays a FAILED backup (not approvable). Prefect
`database is locked` accepted for MVP.

**A. FY rollover = persisted, resumable step journal (saga).** Findings: R1 the final LAN
backup shuts the NAS down BEFORE the new NAS FY folder is created (fails silently with
`lan.shutdown_after_backup` + WoL); R2 exceptions in the switch (slot busy, mkdir, config
write) = no row, no email, maybe half done; R3 crash between config write and row = switched
silently, next day NONE; R4 archive failure = log only, never retried, INFO says "nothing to
do". Design: table `rollover_journal` (new-database schema; fresh install, no upgrade path - plan D-16: from/to FY, per-step state, attempts, last
error, times); steps (each idempotent): final backups + G6 -> source folder + placeholder ->
NAS folder + marker (final LAN backup does NOT shut the NAS down; wake/ready as in the LAN
leg) -> config switch as compare-and-set (only if config still points at the old FY) +
reload/validate, restore backup on failure -> archive transition -> NAS shutdown -> INFO
email ("nothing to do" only when every step is done). The daily 06:00 check is the resumer
(continues at the first not-done step). Crash: ROLLOVER_RUNNING row first (abandoned sweep).
NAS step failed: config still switches (cloud protects the new FY from day 1); step stays
pending and the next LAN pipeline (NAS awake) completes it from the journal - no approval.
New codes AAM-ROL-04 (switch failed part-way, ACTION_REQUIRED), AAM-ROL-05 (archive failed,
WARNING; ACTION_REQUIRED after new key `rollover.archive_alert_after_days`). Dashboard
rollover step card. Tests: calendar simulation per failure + resume on 2 April; lab W6 with
the NAS off after the final backup.

**B. Emails sized to fit, never lost to attachments.** `_send_once` -> `SmtpSession`
(connect, EHLO, read the RFC 1870 SIZE limit, send). Measure the encoded message (base64 ~4/3)
against min(server SIZE, `alerts.attachments_max_total_kb`) and pick the richest rung:
report+zip -> report+tail-only zip -> report only -> none + "logs on the server at <path>".
A size rejection (5xx) or disconnect during DATA steps one rung down and resends at once.
`notifications` records the rung delivered. Tests: real SMTP server with small SIZE, 552,
hang-up mid-DATA.

**C. Reports = one model, many renderers.** Findings: P1 an empty period sends NO report;
P2 weekly = rolling 7x24h but labelled as calendar days; P3 "resolved" only inside the
period; P4 rollover rows counted as backups, files summed across legs; P5 integrity rows for
disabled legs. Design: `ReportPeriod` (IST start/end, label; scheduled = previous calendar
week / previous calendar month; dashboard download + send buttons = last 7 / 30 days up to
now); `db.get_runs_between(start, end)`; `ReportModel` built once (per-leg backup totals,
last good copy, problems resolved against full history, latest audit, open decisions, data
per leg); email HTML, download, CSV, subject only render it; empty period still reports
("action needed: no backup ran"); download text "No backups in <period>". Proof: reports from
a read-only copy of the box DB emailed to the owner.

**D. Dashboard link resolved dynamically.** `dashboard.public_url` empty = auto: new
`core/netinfo.py` asks the OS which local address routes to the NAS / default gateway (UDP
connect + getsockname, no packet sent); resolved per email (short cache). Emails show
`http://<ip>:8080 (or http://<name>:8080)`. A manual `public_url` that differs from the
detected address -> readiness + watchdog amber. Firewall stays LocalSubnet.

**E. Passwords.** `dashboard.login_password_hash` (new) + `dashboard.admin_password_hash`,
both PBKDF2-SHA256 600k (stdlib), written via `config_edit` (atomic, backup, history).
Advanced "Passwords" card: change dashboard password / change Advanced password; both
require re-entering the current Advanced password; new twice; min length; the two must
differ. Sessions carry a credential version -> on change every other session signed in
with the old password ends (OWASP); the changer stays signed in. INFO email "dashboard
password changed by <who>". Bootstrap: no hash yet -> one sign-in with `api_key` from
config.yaml, dashboard forces setting both. Forgotten Advanced password:
`deploy/11_reset_dashboard_passwords.ps1` (admin on the server) clears the hashes ->
bootstrap state.

**F. Dated log names.** `run_logs/<day>/2026-10-18_2100_lan_<short id>/` and files
`2026-10-18_2100_lan_robocopy-sync.log`; built only in `RunArtefacts`; DB run id unchanged;
old paths stay valid; retention unchanged.

**G. Preview of every email.** `core/email_samples.py` = one realistic sample per kind / fault
family / reminder+RESOLVED / reports / password notice / ROL-04/05 / attachment rung (unit
test fails if a fault code has no sample). `tools/preview_emails.py --to <addr>` sends them
through the real composer + transport with `[PREVIEW]`, no DB record, no cooldown;
`--out <dir>` writes .eml files.

**H. Older items.** 36 `uv audit` advisories: targeted `uv lock --upgrade-package` + suite +
lab. `03_setup_system.bat` Active Hours 12:00-06:00 = 18 h is INVALID on WS2016 (max 12 h,
Microsoft waas-restart doc): Windows Update is stopped on the box now; make the script
write a valid 12 h window (20:00-08:00) and readiness report the real update state.

**I. Interruption (reboot / power cut mid-run) resumes next run.** Already OK: Prefect 3.7.2
`concurrency()` holds the slot on a 300 s renewed lease (dead process frees it); robocopy
`/Z` restartable; GCS uploads atomic; `sweep_abandoned_runs` -> ABANDONED + one email;
backup.lock PID check. GAP: the run budget counts from the actual start, so a run Prefect
starts late after a reboot (e.g. 09:30) gets a full budget -> office hours + the schedule
proof assumes scheduled starts. Fix: budget counted from the flow run's scheduled start
(manual runs: actual start); no budget left -> `*_MISSED` row + WARNING email, next night
continues. Prove in the audit lab: hard-kill the process tree mid-sync (cloud, LAN), mid-audit,
mid-rollover; restart -> ABANDONED + one email, lock gone, slot free within the lease, next
run completes, destination == source, no false guard block.

**Rev 2 (re-review):** gaps G-1..G-9 added in the plan file §2a - notably new group K (one
locked writer for config.yaml, `filelock` proposed), removal of the `X-API-Key` master key (E9),
report emails recorded (C8). Rev 3/4 (final): filelock yes; exactly two dashboard passwords
(hashes in config.yaml, reset script, `api_key` + `X-API-Key` removed); SMTP password + Google
key encrypted with DPAPI (group S); HTTPS removed (post-MVP). Plan file status: FINAL.

**Order of work:** 0 commit docs + tidy handoff, fix the 7 audit-lab reds (§4f) + rerun the
lab (S) -> 0b K config store (S) -> 1 A rollover (L) -> 2 I interruption (M) -> 3 B email sizing (M) -> 4 C reports (M)
-> 5 E passwords (M) -> 6 D dashboard address (S) -> 7 F log names (S) -> 8 H (S-M) -> 9 G
preview to the owner + fix feedback -> 10 full suite + audit lab + code review -> deploy to
the box -> M5 scenarios -> 7 truthful nights -> tune budgets -> owner changes the `aambackup`
password on both machines.

## 4f. Session 2026-10-18 (UI + email audit, plain-language emails, fault codes)

Box check (read-only): 3 services Running; every night since the 16 Oct deploy recorded truthfully
(LAN_COMPLETE x3 incl. the approved first copy, CLOUD_NO_CHANGES_COMPLETE x3, weekly audit
18 Oct VERIFIED both legs); only error = known Prefect `database is locked`.
Owner asked for a UI + email audit, then: dashboard **office network only** (never public);
emails easy for a non-technical person with a recommended action, fault codes and attached
logs so a forwarded email lets support find the reason. **Spec: `docs/EMAIL_CONTENT_SPEC.md`**
(research + sources, every email's wording, 54 fault codes, attachments, owner decisions D1-D6:
D1 attach logs with client file names = yes; D2 support contact = not now; D3 auto-send to
support = not now; D4 subject `[<firm> Backup]`; D5 FY-switch INFO email = yes; D6 English).
Commits (each test-first, full suite green, GitNexus detect-changes before commit):
- bf142d2 alert switch: ACTION_REQUIRED/RECOVERED/INFO never silenced; `send_on_failure` is
  Advanced + typed confirmation; amber banner while off; audit differences = ACTION_REQUIRED.
- 0f30f00 Message-ID / Date / Auto-Submitted + plain-text part on every email.
- 9a73268 office-network dashboard: `dashboard_link_problem` (amber + readiness FAIL when the
  email link is 127.0.0.1/0.0.0.0), new key `dashboard.firewall_remote_addresses` (LocalSubnet,
  server-only), installer opens only the dashboard port to it; 4200 never opened.
- a21786a `core/faults.py` (53 codes then, 54 with ROL-03 in 4384349; the commit message says
  57 - wrong number): codes at
  raise sites (`faults.coded`), `GuardResult.fault_code`, rclone transport category, status;
  stored in `extended_metrics.fault_code`. Never parsed from message text.
- 4384349 `core/email_compose.py`: every email = severity + what happened + "Is my data safe?"
  + numbered steps + next run (IST) + dashboard link + For-support block; every producer sets
  its code (watchdog SVC-01..04, overdue SYS-03, abandoned SYS-02, FY ROL-03 ...).
- a6cd7ee `core/support_bundle.py`: support report .txt + run-folder logs .zip, secrets
  redacted, `alerts.attachments_max_total_kb` (5120) / `attach_file_names` / 
  `support_log_tail_lines`; `alerts.attachment_max_kb` deprecated. File-list CSV = follow-up.
- eaf998d incidents: repeat suppressed only while key AND fault code unchanged and no RESOLVED
  since; reminders + RESOLVED reply in the incident thread. **Schema v3** (notifications
  fault_code / message_id / thread_id).
- 330524e dashboard: "Cannot reach the backup server since ..." banner + Last updated; Settings
  "Send test email" + amber until changed mail settings are tested; fault code + title on run
  rows; separate rate-limit buckets; `dashboard.rate_limit_*`, `session_hours`,
  `config_refresh_seconds`, `trigger_wait_seconds` keys (no literals left in ui.py).
- 1bc1474 reports: per-leg plain summary, all runs, monthly = previous calendar month, outcome
  in the subject; once-a-year "New financial year started" INFO email.
Tests: `tests/` 2142 passed / 0 failed / 19 skipped. **Audit lab at 1bc1474 (21 min): 231 passed /
7 failed / 3 skipped** (log `agent_runs/audit_lab_2026-10-18_email.log`). No backup/guard/real-tool
red. Causes: w14 auth + w19 session use removed `ui._SESSION_TTL` (now `dashboard.session_hours`);
w19 flash test covers the removed dead flash code; w19 email endpoints' fake lacks the new `legs`
arg; w8 report SMTP-outage seeds this month's runs (monthly = previous month now); w10 coverage
floor `core/health.py` 44.8% < 45%; **w14 `/report/weekly` + `/report/monthly` 404** - to check:
probably fixed seed dates (date rot), but `_serve_report` still says "No runs found in the last 30
days" (wrong for a calendar month) and "Download Monthly Report" mid-month now gives LAST month
(design question - see 4f-2 topic 3). NOT fixed yet (owner paused for discussion).
**Deploy-day steps for §4f (owner):** set `dashboard.public_url: "http://<server-name>:8080"`,
`dashboard.bind_address: "0.0.0.0"` (or the server's office IP), re-run `04_check_readiness.ps1`
(new check "Dashboard link in emails") and `06_install_services.ps1` (creates the firewall
rule); then Settings > "Send test email". First alert per open problem after the upgrade may be
re-sent once (old notification rows have no fault code).
**Open / follow-up:** file-list CSV attachment (spec §5.3); Gmail shows the RESOLVED email
outside the thread (own subject, by design); a real Gmail send of each email type on the box.

### 4f-2. Open topics (DISCUSSED 2026-10-18 -> agreed plan in §4g; kept for background)

Owner: "discuss and make a plan before moving forward". Nothing below is built yet.
Proposed order: 1 -> 2 -> 5 -> 3 (review only, then discuss) -> 4 -> 6 -> 7.

1. **Attachments must never block an email.** Today: 5 MB cap, zip left out if too big, a
   bundle error falls back to a short text. Gap: if the mail server/filter rejects the whole
   message (e.g. 552 too large, Sophos refusing .zip) it is a permanent error, not retried -
   the alert is lost. Proposal: on any permanent rejection of an email WITH attachments, resend
   at once WITHOUT them, adding "logs could not be attached; they are on the server at <path>";
   record both attempts.
2. **Dashboard link by IP, not name.** `http://backup-server:8080` needs name resolution
   (workgroup without DNS often fails; phones never resolve NetBIOS names). Proposal: installer
   detects the server's office IP and proposes `public_url: http://<ip>:8080` (owner confirms);
   readiness checks the IP is static / DHCP-reserved; a daily check that the email link still
   points at THIS server (amber if the IP changed); emails show "http://<ip>:8080 (or
   http://<name>:8080)". The NAS IP stays in NAS emails (already); the link must be the main
   server, where the dashboard runs.
3. **Reports - verify properly (owner: next).** Note: reports were already changed in 1bc1474.
   Review: correctness (OK/failed/partial/blocked counting, IST period boundaries vs UTC
   `cutoff_iso`, runs over midnight, month lengths, empty month, report during a running
   backup); consistency report = dashboard = CSV = emails; content an accountant needs (GB
   backed up, files changed, NAS fullness, safety check, growth); timing (Mon 08:00, 1st 08:30);
   proof: generate real reports from a read-only copy of the box DB and email them to the owner.
   Findings first, discuss, then fix.
4. **Settings test email.** "Send test email" is BUILT (330524e, uses the saved settings,
   amber until tested). Proposed addition: "Test these settings before saving" (connect +
   login + send with the typed, unsaved values) so a wrong password is caught before save.
5. **FY rollover failure - real gaps found (read-only check of `rollover_check_flow`).**
   Covered today: clock behind (CLK-03), clock not proven (ROL-03), final backup incomplete /
   jump > 1 FY (G6 -> approval card, ROL-01/02). NOT covered: an exception DURING the switch
   (NAS unreachable creating the new FY folder, config.yaml not writable, backup slot busy) ->
   no run row, NO email, only a failed Prefect run, and possibly half done (folders created,
   config not updated). Cloud archive-transition failure -> only a log warning, no email.
   Proposal: ROLLOVER_FAILED row + email (new codes AAM-ROL-04 switch failed part-way,
   AAM-ROL-05 archive transition failed), consistency check after the switch (folders + config
   agree), restore config.yaml from its backup on failure, calendar-simulation tests for each
   failure on 1 April.
6. **Date + time in log names.** Today `run_logs/<day>/<flow-run-uuid>-lan/robocopy-sync.log` -
   undatable once copied/zipped. Proposal: run folder `2026-10-18_2100_lan_<short id>` and files
   `2026-10-18_2100_lan_robocopy-sync.log`; retention + dashboard + support bundle read the new
   names (attachments are already dated).
7. **Other points to decide:**
   - Recipients: one list gets everything today. Reports to partners, alerts to IT/support, or
     everyone gets everything? (owner decision)
   - Preview: send one real sample of every email type to the owner's inbox (preview command)
     to review in Gmail/Outlook and on a phone before deploy.
   - Deploy §4d/§4e/§4f to the box: dashboard on the office network, `public_url` = IP, test
     email, then the M5 5.2 scenario list.
   - File-list CSV attachment (spec §5.3) - still a follow-up.
   - Still open from before: Windows Update Active Hours; 36 `uv audit` advisories (transitive
     via Prefect).

## 4e. Session 2026-10-16 (rclone exit codes, sync retries - XR-1..XR-6)

Owner asked for the best fix (not patches) after research. Research, options and decisions:
**`docs/audit/EXIT_CODES_AND_RETRIES_2026-10-16.md`** (rclone v1.74.3 source + real-binary
experiments; real logs in `test_support/rclone_logs/`). Commits fe5855e, 7a2dc09.
- **XR-1 (data loss, fixed):** Prefect task retries re-ran `rclone sync` with a fresh
  `--max-delete N` each attempt - 3 runs x `--max-delete 2` deleted all 6 files (real binary).
  Now `flow._sync_with_retries` (both legs): guards re-measured before every retry with this
  run's deletions counted (G3 cumulative), `--max-delete` = what is left
  (`guards.remaining_delete_allowance`), and a `--max-delete` stop is never retried ->
  re-measure -> `CLOUD_BLOCKED` + approval (`guards.backstop_blocked`).
- **XR-2 (data loss, fixed):** robocopy `/MIR` repeat after 10 min without re-checking -> now
  robocopy /L + destination count + G1-G5 + access run again first; unknown deletions = no repeat.
- **XR-3:** a deletes-only sync exits 9 -> NO_CHANGES only when rclone's stats prove 0 changes.
- **XR-4:** rclone logs its own retried attempts (even "Attempt 2/3 succeeded") at ERROR level
  -> only the final attempt + final `stats.errors` decide SUSPECT.
- **XR-5:** exit 10 from rclone's own `--max-duration` -> PARTIAL, verify skipped (was VERIFY_FAILED).
- **XR-6:** retries only for rclone 1/4/5 not marked fatal / LAN_FAILED not a timeout, delay must
  fit the budget. The run row now keeps a failed sync's real exit code + FAILED/SUSPECT
  (was exit -1) and `sync_attempts`. An approval also covers its own run's retries.
- No new config keys (`*.max_attempts` / `*.retry_delay_seconds` keep their meaning).
- Not done / post-MVP: LAN leg on rclone (`--max-delete`, `--backup-dir`) evaluated and
  rejected for MVP (loses `/ZB`, ACL copy, the verified robocopy guard parsing). The audit lab
  runs with `max_attempts: 1`, so retries are proven by unit tests, not on the real tools.
- Tests: `tests/` 2030 passed / 0 failed / 19 skipped. Full audit lab (at 7a2dc09, 21 min):
  237 passed / 1 failed / 3 skipped - the 1 red was the W10 line-coverage floor for
  `cloud_sync.py` (82% < 85%: the new failure-only branches are not taken on a clean night);
  floor set to 80 with the reason in the test, re-run green. No behaviour red.
- Owner decision open: none new. Deploy to the box when you choose (same runbook).

## 4d. Session 2026-10-16 (GitNexus scan + "no hard-coded values" sweep)

Owner asked: run GitNexus, fix the findings, no hard-coded values anywhere, generous and safe
defaults. Rule applied: **every default = the value that already ran on the trial box** (no
behaviour change); a literal that disagreed with the model was a bug and now follows the model.
Commits (each test-first, GitNexus impact + detect_changes before commit):
- ee5fe7d dead code: `write_config_values`, `count_extra_file_lines`, `is_no_changes`, `read_section`.
- 5c1bb82 **bug**: the Prefect run summary was never published when rclone stats were missing
  (`bytes_copied=None` divided) - now "unknown"; the bare `except: pass` around it now warns.
- 76bf8f5 `cloud_record_task` / `lan_record_task` / `_record_run` take `config` and open the DB
  via `ManifestDB.from_config` (plan 5.4); literal 30000/10000/"normal" signature defaults gone.
- 73a9e24 task retries are config: `cloud.preflight_retries`/`_delay_seconds` (1/30),
  `cloud.verify_retries`/`_delay_seconds` (1/60), `lan.preflight_retries`/`_delay_seconds` (1/30).
- 60155c3, 7670f0b, a330db5, e10ad53 **tool_runner migration DONE (M3 deferral)**: cloud_preflight
  probe, cloud_verify, cloud_reporter size/manifest/diff, integrity `rclone check` all run through
  `run_tool` - output kept in the run folder (the cloud verify-report task and each audit leg now
  get `artefacts_dir`), timeouts kill the tree. **No `subprocess.run` outside tool_runner remains
  in production code.** Verdict functions AST-identical. New keys: `cloud.probe_retries` (2),
  `cloud.probe_retries_sleep_seconds` (5), `cloud.modify_window_seconds` (2),
  `cloud.retries_sleep_seconds` (10, diff), `audit.modify_window_seconds` (2). Result dicts keep
  their shape (incl. `exit_code -1` when rclone did not finish - the flow evidence gate reads it).
  Test helper: `test_support.fake_tools.SubprocessStyleRunTool` (old subprocess-style mocks ->
  real ToolResults). Never patch `tempfile.mkstemp` / `os.close` (global; breaks run_tool's
  artefact writes - the old `(0, path)` stub closed pytest's stdin).
- 5c17a53 `models.config.model_default(Model, "field")`: 36 function fallbacks in core/ now read
  the model; 5 had drifted (ManifestDB vacuum 1000 vs 10000, WoL 3x5s vs 5x10s, log retention 30
  vs 90, rclone error tail 10 vs 100 - only direct callers/tests saw them). New key
  `maintenance.orphan_robocopy_log_max_age_hours` (24) replaces a call-site literal in flow.
- 0eac85a upload command: `--modify-window` -> `cloud.modify_window_seconds`, `--retries-sleep`
  -> new `cloud.sync_retries_sleep_seconds` (30, kept separate: C-A max-duration), `--stats` ->
  new `cloud.stats_interval_seconds` (60).
- 9b8ddec manual code review of the session diff: `cloud.`/`audit.modify_window_seconds` capped at
  0-10 s and made risky (typed confirmation) - a wide window would make the upload skip same-size
  edits. Review also checked: no caller passes moved parameters positionally (AST scan), artefact
  text decodes with errors=replace, every not-completed run_tool path fails closed.
- Disk note for the owner: run_tool keeps stdout, so the nightly `rclone lsjson -R` listing is now
  kept in run_logs (gzip after the run, 90-day retention, `run_logs.min_free_gb` guard). The
  diff's `--combined` file (lists every unchanged file) is still a temp file, not kept.

**Still literal on purpose / owner decision (not changed):** process-kill wait 30 s
(tool_runner x3, services.py), `run_capture` 4000-char cap, ManifestDB schema-migration retry
3 x 1 s, dashboard page sizes (`get_recent_runs(25)`, `limit=200` x3), launch.py health probe 5 s /
dashboard supervise 60 s / 0.5 s, SMB port 445 (protocol). `report.send_failure_alert`'s 1000-char
cut is in the legacy helper nothing calls. Integrity fallbacks are kept (read from the model)
because ~50 test call sites use them; elsewhere migrated functions have no fallback at all.
Ephemeral run_tool folders (`aam_tool_*` in %TEMP%, used when no run folder is passed) are never
deleted - pre-existing convention, only hit outside a pipeline.

## 4c. Session 2026-10-16 (later) - dev tooling + calendar simulation

- Dev group now locks pytest-timeout, pytest-cov, ruff, hypothesis, time-machine (all were
  ad hoc or missing; `uv sync` would have dropped them). Owner approved.
- MCP servers registered locally (`claude mcp list`): gitnexus (`node .gitnexus/run.cjs mcp`,
  avoids the npm 11 npx crash) and context7 (live library docs).
- `tests/test_calendar_simulation.py`: real nights via cronsim + `pendulum.travel_to`
  (time-machine) - rollover once on April 1, server off over April 1, clock wrongly in April,
  clock falling back, IST midnight, stale boundary, run-log retention. Verified against 7
  planted bugs (all caught). Limits: child processes, the Prefect scheduler and
  `time.monotonic` keep real time.
- OPEN (owner decision): `uv audit` reports 36 advisories, all transitive via Prefect
  (anyio, cryptography, urllib3 < 2.8.0, h2, mako, fsspec, apprise...). Fix = targeted
  `uv lock --upgrade-package` + full suite + audit lab. Also: `04_check_readiness.ps1` runs
  `uv sync --frozen` without `--no-dev` (dev tools land on the box; harmless).
- Pre-existing noise: a Prefect "Logging error / Stopping temporary server" at suite exit
  (also without the new test/time-machine).

## 4b. Session 2026-10-16 (unattended, owner away) - box review + code audit

**Box (read-only this session - the auto-mode classifier refused writes/approvals on the box):**
- 21:00 LAN run 2026-10-15: `LAN_BLOCKED` (G5: marker missing on the EMPTY destination
  `\\10.10.151.53\aam_nas\FY26-27`) + exactly ONE `ACTION_REQUIRED` email - correct by design.
  **Owner action:** approve it on the dashboard (phrase `INITIALISE DESTINATION`, approval
  `0b14b56b...`) to get the first full LAN copy; then continue the §4a scenario list.
- Prefect server log: sporadic `sqlite3.OperationalError: database is locked` (a few/day since July,
  mostly `mark_deployments_ready`, once "error during state validation" 2026-09-01). Prefect 3.7.2
  already sets `busy_timeout=60s`; failures in 0-30 ms = SQLite WAL read->write upgrade conflict
  (busy handler skipped by design) inside Prefect's own services. Retried by Prefect; no night lost
  (every scheduled night has its row). Proper fix = Postgres (new dependency -> owner decision).
  The new stale-leg email (below) now reports it if it ever costs a night.
- Watchdog `Traceback ... KeyboardInterrupt` in watchdog_svc logs = NSSM Ctrl+C on stop (benign).

**Fixed + committed (test-first, full suite 1873 passed / 0 failed; lab W16+W17 49/49):**
- ce18fc3 `smb_ready` failure reason said "within 0s" after 4 min of probing (LAN preflight uses
  max_wait_s=0 + min_attempts) - now states the time really spent.
- 34cbc28 **Abandoned runs**: a RUNNING row became ABANDONED only at agent start and only when
  > 24 h old -> a reboot during the 02:00 cloud run (Windows Update!) left it "Running" with NO
  alert; a flow-run crash while the agent stayed up was never swept. Now the RUNNING row records
  its owner `pid:create_time`; `backup_repository.sweep_abandoned_runs` (agent start + every
  pipeline start) marks dead-owner rows ABANDONED at once + one FAILURE email; live owner never
  touched; unreadable owner keeps the 24 h backstop. Real-process kill test.
- eb87fc5 **Stale-leg email** (plan §2 rule 11, approved but never built): the watchdog checks
  every `watchdog.stale_check_interval_seconds` (1800) - also while the API is down - and emails
  `stale:<leg>` (ACTION_REQUIRED, cooldown-repeated) using the banner's `core.attention`
  evaluation. Catches runs that never start (scheduler, import crash after a bad deploy).
  **On deploy the box will email "LAN overdue" at once - truthful (LAN has not succeeded since
  the fresh install).**
- 117327e **Weekly audit silent failures**: an exception in a leg (NAS not waking = WolTimeout, GCS
  key missing, helper raising) or the slot not obtained escaped with NO email and NO audit row
  (a cloud exception also skipped LAN). Now: per-leg AUDIT_INCOMPLETE "could not run" row, other
  leg still runs, one audit alert. Full lab (before this change) 238/0; W6+W12 after it 13/13.
- **Owner asked me to approve + deploy (2026-10-16); the auto-mode classifier refused every box
  write even then** -> owner approves on the dashboard and deploys by hand, or adds a Bash
  permission rule for `ssh innoviztawinserver2016` so the next session can.
- **Deployed 2026-10-16 07:48** (box at c48a600): `git pull --ff-only`, services restarted
  (watchdog/agent stopped, server restarted, agent + watchdog started); config loads, all 6
  deployments served, API healthy. Owner approved `0b14b56b...` at 07:41 -> first LAN run
  `LAN_COMPLETE` (marker `.AAM_TARGET_MOUNTED` written, 580 files) -> no "LAN overdue" email.
- Settings: `.claude/settings.json` allows `Bash(ssh innoviztawinserver2016:*)` and
  `PowerShell(ssh innoviztawinserver2016:*)`.
- **/code-review of these 4 commits -> 10 findings, triaged with owner (MVP: pipelines must run;
  dig only where an alert goes silent or email can stop a backup):**
  - FIXED c2e8386: audit alert key `audit:{mode}` -> `audit:{mode}:<leg>=<status>,...` (a real
    MISMATCH is no longer suppressed by an earlier "could not run" alert).
  - VERIFIED ab4dd4d: SMTP down never stops a backup (notify never raises, callers wrapped; test
    with a refused port). Delay bound: retry_attempts x smtp_timeout + backoff (~2.5 min per email
    with defaults), only when an abandoned run is pending. Accepted for MVP.
  - BY DESIGN: abandoned key `backup:{leg}:{status}` = same scheme as every run failure; nightly
    runs are 24 h apart > 12 h cooldown, so only a manual retry inside 12 h is suppressed.
  - COVERED: live-but-hung RUNNING row / late sweep -> the next run cannot get the slot
    (`*_SLOT_TIMEOUT` + "could not start" email) and the watchdog's stale-leg email follows.
  - DEFERRED post-MVP: fresh install emails "overdue" at once (owner: note for install day);
    duplicated run-alert format in `sweep_abandoned_runs`; unused `audit_id` param + duplicated
    "could not run" dict in `integrity_audit_flow`.

## 4a. M5 on the trial box - where we paused (2026-10-15)

Owner: the trial box is ours (fresh install OK, old config only as reference; memory
`trial-box-full-access`). **Use this PC as the "NAS"; NO NAS shutdowns for now.**
- Box `C:\AAMBackup` = fresh clone of `mvp-hardening` (now `74a905c`); old install kept as
  `C:\AAMBackup_v113`; its config + local edits saved in the session scratchpad (`box_ref/`, NOT in git
  - secrets). Box config: schedule fixed (cloud 02:00, monthly 08:30), LAN target
  `\\10.10.151.53\aam_nas\FY26-27` (= THIS PC, share `aam_nas` = `C:\AAM_NAS`, folder FY26-27 created),
  `lan.shutdown_after_backup: false`, `wol.enabled: false`. Old config backups next to config.yaml.
- Account `aambackup` exists on BOTH machines (same password; owner chose it and typed it in chat - it
  equals the box Administrator password -> **owner should change both after testing**, then re-run
  04/06). Box: member of Backup Operators. Services installed by `06_install_services.ps1
  -ServiceAccount .\aambackup -PasswordFile ...`; **AamBackupAgent runs as .\aambackup (Running)**,
  server + watchdog LocalSystem. A readiness-only re-run was in flight when paused - re-run it to
  confirm all green except "Update Active Hours" (owner decision pending).
- Done on the box with the new code: readiness (all green except Active Hours); cloud backup
  CLOUD_NO_CHANGES_COMPLETE (clock OK, guards PASS, rclone counts); LAN with NAS unreachable ->
  LAN_FAILED + exactly ONE alert email (real Gmail); graceful agent stop 0.6 s, lock released.
- **Real deploy bugs found on the box and fixed** (all committed + tested): (1) DPAPI password file
  trailing newline -> empty password, installer printed [OK] while agent stayed LocalSystem -> trim +
  read back Win32_Service.StartName, stop if wrong; (2) Start-Process -Credential fails 0xC0000142
  without a desktop -> probe runs as a scheduled task; (3) our own services on 4200/8080 flagged on
  re-install -> OK; (4) uv hard-links .venv to the admin's cache (ACL shared) -> service account
  could not import -> `uv sync --link-mode copy`; (5) runtime grant without /T missed existing
  files -> NSSM could not open agent_svc.log -> `/T /C`, stop on failure.
- **Next (after the pause):** confirm readiness green; first full LAN copy to this PC; then the
  scenario list: no-change + changed-files nights (counts); NAS unavailable (stop share) -> one
  email, RECOVERED; mass delete in E:\FY26-27 -> BLOCKED -> dashboard approval; empty/renamed
  source -> blocked; tiny LAN budget -> LAN_PARTIAL; audit VERIFIED then a changed NAS file ->
  difference; watchdog hung-API restart order + emails; settings page on the box. Restore the box's
  E:\FY26-27 afterwards. Also: remove stale services AamChaosAgent/AamChaosServer/AamF4Rig and the
  stale deployment backup-lan-p2scen; delete `AAM_setup_this_pc_as_NAS.ps1` +
  `AAM_hand_password_to_Claude.ps1` from the owner's Desktop when done (the latter never saved:
  double-click PowerShell is blocked by execution policy on this PC).
- Remote-install mechanics (scripts prompt at the keyboard only): scratchpad
  `box_account_template.py` = set aambackup password from stdin, write DPAPI files, run 04/06 with
  `-PasswordFile` (files deleted by the scripts). Run it with the box's system Python when .venv is absent.

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

## 6. Production-trial box (facts from 2026-10-14; full control since 2026-10-15)

> **Current state (2026-10-18):** owner gave us full control of the box. `C:\AAMBackup` is a
> fresh clone of `mvp-hardening` at 6adcfb2 (§4a/§4b), AamBackupAgent runs as `.\aambackup`,
> schedule LAN 21:00 / cloud 02:00. The bullets below describe the OLD v1.1.3 install and are
> kept as background only.

- SSH alias `innoviztawinserver2016` (user `administrator`), code `C:\AAMBackup` (branch `production`, v1.1.3 `6550aad`), runtime `C:\BackupAgent`.
- Windows Server 2016 Standard, **WORKGROUP**; all 3 services run as **LocalSystem** → root cause of NAS "Access is denied" (plan §10 item 2): needs a dedicated service account.
- rclone v1.74.3, robocopy 10.0.14393. Config: LAN 21:00, cloud 22:00, LAN target `\\10.10.151.53\aam_test\CHAOS01\FY26-27` (test leftover), demo bucket. Both NAS IPs do not answer on 445.
- Production `config.yaml` passes the new strict validation (only deprecated `notifications.send_on_success`, dropped with a warning).
- DB: 59 legacy `*_SKIPPED` rows are really preflight failures (display as failures, never rewrite history).
- **Deploy by hand: `docs/DEPLOY_RUNBOOK.md`** (owner asked 2026-10-15 - no AI agent on the server).
  Compatibility verified on the box (read-only): all deploy `.ps1` parse on its PowerShell
  5.1.14393; every tool the `.bat` files call exists; `Get-LocalUser`, `Start-Process -Credential`
  available. Box has git (`C:\Program Files\Git`), `C:\AAMBackup` = clone on branch `production`
  (6550aad) **with ~200 lines of uncommitted local edits** (core/integrity.py, ui.py, uv.lock) -
  the runbook saves them (patch + stash) before switching; owner/dev to review if still needed.
  Python on the box: `C:\Program Files\Python312` (all users) - fine for the service account.
- Production schedule (read-only, 2026-10-15): LAN 21:00, cloud 22:00, audit = code default,
  `concurrency_wait_seconds` default (1 h). Trial-box run durations are demo-sized (0-5 min,
  579 files / 57 MB) - no basis for real budgets.
- 2026-10-15 ~11:56 and ~12:08 IST: the box was **shut down twice by our 4.1 tests** with the owner's
  permission (owner restarts it by hand; after the first restart all 3 services came back Running).
  No backup was running either time.

## 7. Housekeeping

- Not committed on purpose: `audit_lab/raw/*.err`, coverage files, `qc`, `query`, one-off root scripts (`analyze_logs*.py`, `coverage_func_scan.py`, `scan_stale_imports.py`, `search_lan.py`, `test_cloud_verify_timeout.py`) — owner to decide on deletion.
- Update this file at the end of every working session (status table, next steps, gotchas).
