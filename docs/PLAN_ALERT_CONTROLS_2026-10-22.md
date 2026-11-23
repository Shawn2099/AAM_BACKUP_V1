# Plan: alert controls - group switches, reminder limits, delayed failure mails (2026-10-22, rev 2)

Status: rev 2 = rev 1 + Opus review (4 blockers, 9 should-fix; all taken in below). No code yet.
Owner request (2026-10-22): the "always sent" mails must be switchable; a setting for how often an alert repeats
instead of one fixed rule; "decision needed" editable with a recommended default; optionally "email a failure only
after N hours without a good backup". Rules: CLAUDE.md (every tunable a config key with bounds, test first, reuse,
fail closed but never silent, one source of truth per concern). Builds on docs/PLAN_RECIPIENTS_AND_SCHEDULE_ALERTS_2026-10-22.md.

## Facts (verified in code, line numbers as of 4d32a24)

Decision point: `core/notify._notify` (:593) for every alert; `_incident` (:573); the short DB-less notice
`_fallback_alert` (:514). Order today: `_incident` -> render -> `send_on_failure` check -> cooldown -> send -> record.
- Incident = same `event.key`, unchanged `fault_code`, and no SENT RECOVERED in the same `mode` after the key's last
  SENT row. Inside it, a repeat is SENT as a reminder after `alerts.repeat_cooldown_hours` (12 h) since the last SENT
  row, else recorded SUPPRESSED. No upper limit on reminders. Every SENT row stores the incident's first Message-ID
  in `thread_id` (notify.py record).
- INCIDENTS OFTEN NEVER CLOSE TODAY: modes `audit`, `rollover`, `dashboard` have no RECOVERED producer;
  `recovered:<leg>` is sent only when the previous RUN failed (flow.py:354-374), so after a dead scheduler (no rows)
  `stale:<leg>` stays open; `watchdog:recovered` depends on in-memory `_attention_raised` (watchdog.py:310/345).
  Today this only means "reminders continue"; with reminder limits it would silence a NEW problem with the same
  key (blocker 1 of the review) - so closure must be fixed first.
- `ALWAYS_SENT_KINDS = {ACTION_REQUIRED, RECOVERED, INFO}` bypass `notifications.send_on_failure`; only FAILURE and
  WARNING obey it (notify.py:47/520/604). `send_on_failure` is a risky key (typed phrase); attention.py shows an amber
  `alerts_off` item whose text says decisions/overdue/differences "are still emailed" (attention.py:322-325).
- `alerts` is NOT in `config_edit._RISKY_SECTIONS` (config_edit.py:47): new switch keys must be added to `_RISKY_KEYS`.
- Producers (kind, key): FAILURE `backup:<leg>:<status>` (flow.py:338), abandoned (backup_repository.py:142/150);
  WARNING `missed:<leg>` (flow.py:467), `nas_shutdown:<ip>` (flow.py:1332, fires after a SUCCESSFUL LAN run),
  audit AUD-02 (flow.py:2882), rollover archive `rollover:<fy>:archive` (flow.py:2689-2691, WARNING until
  `archive_alert_after_days`); ACTION_REQUIRED `blocked:<leg>` (flow.py:1117), `stale:<leg>` (watchdog.py:380),
  rollover problems (flow.py:2482/2496/2523/2643), audit AUD-01 (flow.py:2882), `watchdog:<key>` (watchdog.py:337);
  RECOVERED `recovered:<leg>` (flow.py:370), `watchdog:recovered` (watchdog.py:347); INFO test and password-changed
  (ui.py), financial year started (flow.py:2606). The scheduled report (flow.py:2260) is not an AlertEvent: outside
  the classes, unaffected.
- `flow._send_recovery_if_needed` does not check that a failure mail was ever SENT (RESOLVED goes out even with
  `send_on_failure=false`).
- email_compose.py:227 prints "first reported on ..." only (no reminder numbering).

## Design

### Step A - one classifier, `core/alert_class.py`
`classify(event) -> str`, pure, decided by KIND first, then key prefix (never message text):
`failure` (FAILURE, WARNING except below) | `decision` (ACTION_REQUIRED key `blocked:`) | `overdue` (key `stale:`) |
`action` (other ACTION_REQUIRED: rollover, audit differences, watchdog service problems) | `resolved` (RECOVERED) |
`info` (INFO: financial year started) | `security` (INFO `password:`; always sent, NOT switchable) | `test` (INFO `test:`;
always sent). `rollover:<fy>:archive` WARNING is class `failure`. A unit test fails when a producer key in the code
base is not covered (scan of `AlertEvent(` call sites), so a new alert cannot dodge the rules.

### Step B - incident closure independent of delivery (blocker 1 + 2)
`ManifestDB.incident_closed(mode, key, since_iso)` is true when, after the incident's last row:
- leg modes (`cloud`/`lan`): a successful run (`run_history`, exact `SUCCESS_STATUSES`) started after it;
- `audit`: a VERIFIED `integrity_audits` row after it;
- `watchdog`: a "healthy" mark stored in `db_meta` (written by the watchdog when its check passes; replaces the
  in-memory flag);
- `rollover` / `dashboard`: a quiet gap longer than 2 x the class repeat window (an incident with no new row for that
  long is over).
`_incident` ends an incident on a SENT RECOVERED **or** `incident_closed`; closure is recorded even when the RESOLVED
mail is switched off or suppressed. Reminder COUNT = SENT, non-RECOVERED rows with `thread_id = root` (the incident's
first Message-ID) - never "same key after last RECOVERED"; the limit is never applied when `same is None` (first mail
of an incident is always sent). Tests: A -> B -> A history restarts the count; a dead scheduler followed by a success
closes `stale:`; audit difference a month later is a new incident; RESOLVED switched off still closes.

### Step C - switches per class (Settings > Advanced, typed phrase)
Keys under `alerts` (default true = today): `send_overdue`, `send_action_needed`, `send_decision_needed`,
`send_resolved`, `send_info`; `notifications.send_on_failure` stays the `failure` switch. `security` and `test` have no
switch. All added to `config_edit._RISKY_KEYS`. A switched-off class is recorded DISABLED; attention shows an amber
`alerts_off:<class>` item per class that is off (stronger text for `decision`, `action`, `overdue`, `resolved`: "a
stopped / overdue backup will not be mailed - check the dashboard"); the `alerts_off` text and its tests are rewritten
(it currently says decisions and overdue are "still emailed"). The weekly/monthly report gets a line "These alert classes
are switched off: ..." when any is off; `/health` is unaffected (503 for outside monitoring). The short fallback
notice (no DB) applies the SWITCHES but ignores limits and delays (it cannot evaluate them) and always sends.

### Step D - reminder limits per class
Count/limit applies only to a reminder (`same` exists). Keys (bounds aligned: hours 1..720; `repeat_cooldown_hours`
keeps 0..720 where 0 = never suppress as today):
- general (`failure`, `action`): `alerts.repeat_cooldown_hours` (12), `alerts.max_reminders` (0..365, default 100 =
  no practical limit, today's behaviour; 0 = first mail only);
- overdue: `alerts.stale_repeat_hours` (24), `alerts.stale_max_reminders` (0..365, default 100);
- decision needed: `alerts.decision_repeat_hours` (12, 1..720), `alerts.decision_max_reminders` (**1..365**, default 6;
  never 0);
- limit reached -> recorded with the NEW status `LIMITED` (not SUPPRESSED), reason "reminder limit reached (n of m)".
  `told_about_leg_since` (watchdog dedupe) keeps counting only SENT and SUPPRESSED, so a LIMITED row never silences the
  overdue mail. Settings unit "reminders" (`_UNITS`, test_m4_settings_admin); no "-1" value anywhere.
- the reminder text says "reminder n of m"; recommended values for the trial box (set in Settings): failure/action
  12 h + 2, overdue 24 h + 1, decision 12 h + 6. After the decision limit the daily overdue mail continues (open
  question 2).

### Step E - RESOLVED only after a sent incident
`recovered:<leg>` and `watchdog:recovered` are sent only if the mode has an open incident with a SENT problem mail
(`last_open_incident`); otherwise recorded SUPPRESSED ("nothing was reported"). Closure (Step B) is recorded either way.

### Step F - failure delay
`alerts.failure_alert_after_hours` (default 0 = immediate, 0..720). Applies ONLY to class `failure` events whose key
starts `backup:<leg>:` or `missed:<leg>:` on legs `cloud`/`lan` (so `nas_shutdown:<ip>`, rollover and audit warnings
are never delayed). If the leg's last success started less than N hours ago the mail is recorded `DEFERRED` (new status;
not a delivery failure) and the failure stays on the dashboard; the first failure after the threshold sends the normal
mail. Tests ensure `last_notification(key, SENT)`, `last_open_incident`, `told_about_leg_since`,
`notification_failures_since`, the support bundle and Settings history ignore or render DEFERRED/LIMITED correctly.

### Step G - docs/settings
Plain names/help, `config.example.yaml` (same commits: `test_m4_config_example` checks every key), EMAIL_CONTENT_SPEC
section 6, runbook.

## Order of work (each: test first, full unit suite, GitNexus impact + detect_changes, one commit)
1 classifier + coverage test  2 incident closure (B)  3 switches + attention items + report line (C)
4 RESOLVED rule (E)  5 reminder counting/limits + LIMITED (D)  6 failure delay + DEFERRED (F)  7 docs
8 full unit suite, audit lab (w8, w14, w16, w19, w20), Opus code review, deploy, set recommended values.

## Open questions for the owner
1. Count semantics: "max reminders after the first mail" - confirm.
2. After the decision-needed reminder limit: rely on the daily overdue mail (proposed) or one slow reminder per day
   with no limit?
3. Failure delay: infrastructure faults (NAS unreachable, cannot start) - proposed: also delayed like other
   `backup:<leg>:*` failures when the setting is on, or exempt? (decision/overdue/service mails never delayed.)
4. Password-changed notice: proposed NOT switchable (security). Financial-year-started is switchable (`send_info`).

## Owner answers (2026-10-22 evening) - rev 3, binding

1. The current behaviour stays the default (follow-up reminders ON). The new thing the owner wants: **follow-ups
   switchable** so a client inbox is not spammed. Three follow-up switches (bool, default true), each also keeping its
   interval and maximum: `alerts.follow_ups_general` (failure + action classes), `alerts.follow_ups_overdue`,
   `alerts.follow_ups_decision`. Off = only the first mail of an incident; the banner and the approval card stay.
   (Typed phrase; the decision switch shows the strongest amber note.) The minimum of 1 for the decision maximum applies
   only while its switch is on.
2. After the reminder limit nothing more is sent (no daily fallback mail, no unlimited slow reminder). The overdue mail
   keeps its own rule (once per `stale_repeat_hours` and only if no failure mail already told the owner).
3. With `failure_alert_after_hours` on, infrastructure faults stay IMMEDIATE (NAS unreachable `AAM-NAS-*`, cannot start /
   interrupted / stuck slot `AAM-SYS-01/02/08`): a new `delay_exempt` flag on those entries of `core/faults.CATALOG`
   (data, not a literal at the call site). Decision, overdue and service problems are never delayed.
4. The password-changed notice is not switchable (class `security`).

## Progress (update when a step is committed)
- [x] A classifier + scan test - 11107ae
- [x] B incident closure (success / VERIFIED audit / healthy mark / quiet days) - ccd71b5
- [x] C switches per class + amber items + report line - a46976f
- [ ] D follow-up switches + reminder limits (+ LIMITED status, "reminder n of m")
- [ ] E RESOLVED only after a sent incident
- [ ] F failure delay + DEFERRED + `delay_exempt` faults
- [ ] G docs/spec/runbook
- [ ] full unit suite, audit lab, Opus code review, deploy, set recommended values (see SESSION_HANDOFF 2026-10-22)
