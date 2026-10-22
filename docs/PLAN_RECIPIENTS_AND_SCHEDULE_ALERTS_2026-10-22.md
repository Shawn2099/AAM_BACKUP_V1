# Plan: two recipient lists + alerts that follow the schedule (2026-10-22, rev 2)

Status: rev 3 = rev 1 + two Opus reviews (6 blockers, ~20 should-fix, all taken in below). Coding starts after this rev.
Owner decisions (2026-10-22): two separate lists; the same address may be in both lists but not twice in
one; the test email goes to both; an empty list uses the other one (symmetric); alerts and reports must
follow the chosen backup frequency with no false alerts; "overdue" mails must not spam.
Rules: CLAUDE.md (every tunable a config key, test first, reuse locked libs - `cronsim` is already used by
`core/budget.py` and `core/email_compose.py`, fail closed but never silent, one source of truth per concern).

## Facts (verified in code)

- ONE list `notifications.recipients` (models/config.py:462), read in exactly three places: `notify._send_once`
  envelope (notify.py:153), `notify.send_email` To header (:266), `notify.email_config_problem` (:171).
- `send_email` callers: notify.py:458 (fallback alert) and :546 (every alert), flow.py:2254 (scheduled
  report, row written directly at :2257 - NO AlertEvent has kind REPORT), core/report.py:38 (+ legacy
  `send_failure_alert` :42, unused in production), tools/preview_emails.py:51, UI buttons ui.py:1247/1407/1444.
- The stale check: `core/attention.compute_attention` (attention.py:44-62) with ONE fixed
  `dashboard.stale_after_hours` (26), measured from the last success's `ended_at`; shared by /health
  (ui.py:607/623), the banner and the watchdog mail `stale:<leg>` / AAM-SYS-03 (watchdog.py:355-385).
- `dashboard.js:208-231` ("Late" after 1 day, "Overdue" after 2), report.py:244 ("action needed: no backup
  ran"), night_strip ("No backup"), attention.py:66 comment and the texts at models/config.py:899 and
  config.example.yaml:570 also assume daily backups.
- Schedule keys are NEXT_RUN/AGENT_RESTART (take effect on "Apply changes"), notifications.* keys are NEXT_RUN
  and risky (typed phrase) - not "hot".
- Existing DB opens run the schema script with `INSERT OR IGNORE` on every open (manifest.py:190).
- `sendmail()` returns a dict of refused addresses that notify.py:153 ignores. `notification_failures_since`
  (manifest.py:1169) is cleared by ANY later SENT row.

## Part A - two recipient lists

A1 Config. `notifications.recipients` stays = ALERTS list. New `notifications.report_recipients` (default
  `[]`). One shared validator: format, no duplicate WITHIN a list, CASE-INSENSITIVE (also in settings.js:411);
  the same address in both lists is allowed. Registered in config_edit (the 3 lists, widget email_list, same
  risk/effect class as `recipients`), config_help, config.example.yaml.
A2 Routing - one function, explicit everywhere.
  - `Audience = "alerts" | "reports" | "both"`; `AlertEvent.audience` (new field, default "alerts"). NO
    routing by kind (no producer uses REPORT). Test email "both"; password-changed "both" (security notice,
    owner can narrow it); FY-switch INFO "reports" (informational; owner can change); everything else alerts.
  - `resolve_recipients(notifications, audience)` -> addresses + `note`: alerts -> recipients else
    report_recipients; reports -> report_recipients else recipients; both -> case-insensitive de-duplicated
    union (one mail per address); all empty -> `email_config_problem` "no recipients" = recorded FAILED
    delivery, never silent.
  - `send_email(..., recipients: list[str])` REQUIRED (the resolved list); envelope and To header use the same
    list. Callers: notify (event.audience), fallback alert (event.audience), flow report (reports), core/report.py
    report sender (reports) and `send_failure_alert` (alerts, or deleted if unused), UI send buttons (reports),
    test email (both), watchdog (through notify). Preview tool passes `recipients=[--to]` ONLY (never the real
    lists).
  - Partly refused recipients: `sendmail` returned refusals => the delivery is FAILED and names the refused
    addresses (a typo in one list must not be dropped silently); permanent/transient classification unchanged.
  - Schema v5 (one-time in-place upgrade following the existing v3/v4 pattern, manifest.py:343-349:
    `ADD COLUMN ... TEXT` under BEGIN IMMEDIATE, gated by `user_version < SCHEMA_VERSION`, re-checked inside the
    lock; also add the columns to the `CREATE TABLE notifications` DDL; NO index on them in DDL - DDL runs before
    the migration; back up the box DB before deploying; old code on a v5 DB still works):
    `notifications.audience` and `notifications.recipients` (JSON list actually used). `notification_failures_since` clears a failure only
    by a later SENT row of the SAME audience. Old rows: audience NULL = treated as alerts.
  - Dashboard attention: RED item when all lists are empty ("no one will receive any email"); AMBER item ONLY
    when the ALERTS list is empty ("alerts go to the reports list" - the case that hides a mistake). An empty
    REPORTS list is a normal single-list setup: a hint under the field in Settings, never a banner item
    (otherwise every config shows it; tests expecting an empty attention list stay valid).
  - UI success/failure texts show the addresses actually used; "Fix the mail server" is not shown for a
    "no recipients" failure.
A3 Settings: second email group "Who gets reports", first renamed "Who gets problem alerts", fallback
  sentence under each, validation messages name the list.
A4 Tests (red first): validator (both lists, case-insensitive, format); `resolve_recipients` full table
  (audience x empty/non-empty, dedupe); real SMTP server tests of envelope AND To header for alert, RECOVERED,
  weekly, monthly, FY INFO, password INFO, test email (union once), fallback both ways, both empty, refused
  recipient, attachment-rung resend keeps the list; preview tool never reaches the real lists; attention
  items; failure clearing per audience; schema v5 upgrade from a v4 DB; settings round trip; config.example.
  Existing tests that pass `recipients`-less or SimpleNamespace configs are updated in the SAME commit
  (audit_lab/test_w20_report.py, test_characterization_core.py, test_b_attachment_ladder.py,
  test_m3_reports.py, test_m5_email_headers.py, test_s2_smtp_secret.py, test_report_comprehensive.py).

## Part B - "overdue" that follows the schedule (exact, not a fixed window)

B1 Rule. A leg is overdue when `now > next scheduled firing after the last success's STARTED time
  + that leg's run budget (budget.cloud_max_hours / lan_max_hours) + dashboard.stale_grace_hours`.
  One `CronSim(...).next()` per poll (no 366-day scan). It reacts at the right moment for any frequency:
  daily, weekdays-only (Friday success -> due Monday), weekly, monthly. All time arithmetic on
  `.timestamp()` values (correct across DST).
B2 New key `dashboard.stale_grace_hours` (default 2, 0..168). `dashboard.stale_after_hours` is DEPRECATED
  (DEPRECATED_KEYS; dropped with a warning on YAML load; removed from settings/help/example; the box config
  line is removed at deploy to stop the warning on every load). Tests that build `AppConfig(dashboard=
  {"stale_after_hours":...})` are updated (extra="forbid" does not apply DEPRECATED_KEYS).
B3 Crons "in effect": `serve._deployments()` records the schedule from the SAME config object it registers
  (cloud_cron, lan_cron, timezone) in `db_meta`, just before `serve()` (serve.py loads its own config; launch
  starts the dashboard before `serve()`, so db_meta can be empty at first). The getter validates types
  (a MagicMock db or garbage value falls back to the config) and attention uses db_meta first, config second.
  A schedule saved but not yet applied therefore changes neither the banner nor the watchdog; after Apply it
  does. The dashboard thread and the watchdog both read it through ManifestDB.
  `started_at` (IST ISO string) is converted to an aware datetime in `schedule.timezone` before CronSim.
B4 Never-succeeded leg: amber "waiting for its first scheduled run" until the first scheduled firing after
  max(DB first created, last `<leg>.enabled` config change) + budget + grace; then red. The "DB first created"
  marker is written ONLY when the DB file did not exist before opening (not by the every-open INSERT OR IGNORE);
  for existing DBs use the oldest `run_history` row. The marker is decided by `os.path.exists(db_path)` BEFORE
  connect (legacy and fresh DBs both have user_version 0) and written with INSERT OR IGNORE (several processes).
  PARTIAL rule (also for a leg that HAS succeeded before): when overdue but the latest run is PARTIAL with
  `bytes_copied > 0` and `extended_metrics.budget.stopped` set (a big upload spread over nights, faults.py:189),
  the item is AMBER and sends no watchdog mail; any other overdue state is RED.
B5 Texts: attention message, models/config.py:899, config.example.yaml:570, attention.py:66 comment and the
  docs describe the new rule ("next scheduled run + run budget + 2 h").
B6 Other daily assumptions: dashboard.js "Late/Overdue" and the night strip derive from the same schedule
  (expected-run days), report.py:244 `report_headline` says "no backup was scheduled in this period" when none
  was due (and "action needed" only when a scheduled run is missing). `report_headline(db, summaries)` has no
  config today: `build_report_model`, `generate_report_html`, ui.py:1395/1432, test_c3_report_model,
  test_c6_report_consistency and audit_lab/test_w20_report change in ONE commit.
B7 Tests (also: a manual run at 00:55 with a 01:00 firing; a success that starts exactly on a firing minute;
  PARTIAL-progressing after a success): daily, twice daily, weekdays-only (Fri -> Mon quiet, Tue 08:00 red), weekly, every 2 days
  (`*/2`), monthly, day-of-month + weekday OR rule, `MON-FRI`; at/just before/just after the due moment;
  a long-running healthy run (ends after the next firing) is NOT overdue; DST timezone; saved-not-applied
  schedule; fresh DB, existing DB, leg enabled later; PARTIAL upload stays amber; disabled leg; cronsim vs
  Prefect `CronSchedule` agreement on the test crons. SimpleNamespace configs in tests (test_audit_pending_
  verdict, test_a1_audit_verdict_authoritative, test_m4_watchdog:334, test_m4_shutdown:34, test_m4_clock:222,
  test_m3_dashboard_truth, test_calendar_simulation:284-289, test_d2_attention_latest_run) gain `schedule` and
  `budget`; test_m5_stale_alert.py:40/96 and audit_lab/test_w19_ui.py:631 are rewritten for the new rule.

## Part C - fewer repeats (watchdog stale mail)

C1 The watchdog's `stale:<leg>` mail is skipped only while a failure/action mail for that leg exists, created
  AFTER the latest scheduled firing that should have produced a success (the owner has already been told about
  THIS missed run). Counted rows: status SENT or SUPPRESSED (SUPPRESSED is only ever written when an earlier
  SENT mail of the same unchanged incident exists, notify.py:522-537, so it means "already told"; without it a
  schedule more frequent than `repeat_cooldown_hours` would re-send the stale mail), `event_key NOT LIKE
  'stale:%'`, mode = leg, audience alerts. DISABLED / FAILED rows never count (a silenced or undeliverable mail
  must not silence the stale mail). The `created_at` values are PARSED to aware datetimes before comparing. A scheduler that stopped (no new rows) is NOT covered by an old "could not start" mail:
  after the next missed firing the stale mail goes out.
C2 New key `alerts.stale_repeat_hours` (default 24, 1..720) used for `stale:*` only; `notify` gets a per-event
  cooldown override (used in `_notify` notify.py:533 AND in `_fallback_alert` :449); other alerts keep
  `alerts.repeat_cooldown_hours`.
C3 Tests: incident open -> no stale mail; incident mail DISABLED/FAILED -> stale mail sent; stale mail itself
  never counts; dead scheduler after one "could not start" -> stale mail on the next missed firing; 24 h
  repeat; RESOLVED/new success closes.

## Order of work (each step: test first, full unit suite, GitNexus impact + detect_changes, one commit)
1 A1 config+registry  2 A2 routing/send/refusals/schema v5/attention items  3 A3 settings UI  4 B1-B3
5 B4-B6  6 C1-C2  7 docs (EMAIL_CONTENT_SPEC, ARCHITECTURE email + C6, DEPLOY_RUNBOOK, config.example)
8 full unit suite; audit lab w8/w14/w16/w19/w20; 9 deploy (git bundle), set the two lists in Settings, remove
`stale_after_hours` from the box config, Apply, test email, check banner with the real schedule.

## Open items for the owner (defaults chosen, change on request)
- Password-changed notice -> both lists; FY-switch notice -> reports list only.
