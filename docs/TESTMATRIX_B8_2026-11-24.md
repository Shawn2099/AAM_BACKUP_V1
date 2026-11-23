# Test matrix - B8 Reports (written 2026-11-24, before the build)

Reference for the B8 test writer and the B8 code writer (CLAUDE.md "Build process"). Binding plan:
`docs/PLAN_BASELINE_OVERLAP_2026-11-23.md` sections 2.1 (`row_fy`), 2.5 (Reports bullet), 5 (manual-only legs), 9 (B8,
C2), 10. Line numbers are from the worktree as of 2026-11-24 (B7 in progress, uncommitted: `core/fy_overlap.py`
`active_next_fy` exists at `:343`). **B8 starts after B7 is committed** (R12 uses `active_next_fy`).

## DECISIONS (Opus, 2026-11-24, binding for B8)

Owner expectations checked: E1 both FYs are backed up until the April switch, so a report that covers those days
shows both; E2 the report shows the actual truth (a row is reported under the FY it really backed up, never under the
FY the config says today); E3 baseline output byte-identical with the overlap off or no activation record; E4
cloud-only / LAN-only / manual-only legs (`<leg>.enabled`, `<leg>.scheduled`) are honoured; E5 fail closed (unknown is
never reported as fine).

- **D8-1 When the report splits by FY ("FY view").** Only when (a) an activation record `overlap:<F>` (for
  F = next FY of the configured FY, or the configured FY itself) has an interval `[activated_at, ended_at or +inf)` that
  intersects the report period, or (b) a backup row of the period has an FY suffix on its `run_id` (`fy_of_id`). Never
  because of `extended_metrics.fy` / `guards.fy` alone: a baseline rollover week (overlap off, no record) has rows with
  `guards.fy` FY26-27 and FY27-28 and must stay byte-identical (E3, row R04). A view in which, after D8-4, every leg
  has only the configured FY's section is dropped (baseline output, row R26). Without a split the model has exactly
  today's keys and values.
- **D8-2 FY of a row inside an FY view** = `fy_overlap.row_fy(row)` (suffix, `extended_metrics.fy`, `guards.fy`);
  when that is None: `prev_fy(configured)` if `overlap:<configured>.ended_at` exists and the row started before it
  (same truth rule as B7 Q10), else the configured FY. This is the plan's "legacy rows without `fy` fall into the
  configured-FY section", made true across the switch (E2): the April final-backup rows (`<id>-final-cloud`, no suffix)
  and every unsuffixed row of March land in FY26-27 even when the report is built after the switch.
- **D8-3 Scheduled-run window per (leg, FY F)** = `[max(period.start, act(F)), min(period.end, close(F)))`, where
  `act(F)` = `overlap:F.activated_at` (none = -inf) and `close(F)` = `overlap:next_fy(F).ended_at` (none = +inf).
  `scheduled` = `overdue.firings_between(<leg>_cron, tz, a, b)` when `a < b`, else 0. So FY27-28 is expected only from
  its activation (first January week, December monthly: no false "no backup ran") and FY26-27 only until the switch.
  A record whose `activated_at` (or `ended_at`) cannot be parsed -> `scheduled` unknown (key absent) -> an empty
  section reads "no backup ran" (E5, today's `_scheduled_counts` rule `core/report.py:303-304`).
- **D8-4 Which sections exist** (per enabled leg; order per leg by FY ascending, legs in `legs` order): the configured
  FY always; the next FY when its record is open and `activated_at < period.end`; the closed FY when
  `overlap:<configured>.ended_at > period.start`; any other FY that has rows in the period. Overlap switched off
  (`rollover.overlap_enabled: false`) with the record still open: the next FY gets a section only when it has rows in
  the period, `scheduled` = 0 (no false "no backup ran"; the dashboard RED `overlap_off:` of B7 carries the risk) and
  the label says "(not backed up - switched off in Settings)".
- **D8-5 Manual-only legs are done in B8** (the report bullet of C2 moves here; C2 keeps attention, watchdog, card
  line, `_next_text`). `<leg>.scheduled: false` -> `scheduled` = 0 for every FY of that leg, label
  "<leg label> (run by hand)", empty detail "No backup was scheduled in this period - this backup runs only when
  started from the dashboard." A failed manual run still counts as a problem (`not resolved` -> action needed). Read
  from the passed config (`getattr(..., "scheduled", True)`); a period that spans a change of the flag uses today's
  flag (accepted, noted in D1).
- **D8-6 Labels.** Section label `"<LEG_LABEL> - <FY>"` ("Online backup (Google Cloud) - FY27-28"); run table pipeline
  column `"CLOUD FY27-28"`; integrity row `"CLOUD FY27-28 Integrity"`; per-section stats "Files copied (Online backup
  (Google Cloud) - FY27-28)". The headline uses the section label ("action needed: Office NAS backup - FY27-28 - no
  backup ran"). Without a split: today's texts exactly.
- **D8-7 Integrity rows** in an FY view: one per (enabled leg, FY active at build time = configured FY and the open
  next FY). The closed FY gets no integrity row; its section detail ends "FY26-27 was closed on <ended_at IST> (switch
  to FY27-28); it is not backed up any more." The configured FY's audit after a switch = the newer of
  `latest_audit(leg)` (only if that row maps to the configured FY by D8-2) and `latest_audit(leg, fy=configured)` (its
  overlap audits); next FY = `latest_audit(leg, fy=next)`.
- **D8-8 Last good copy and "resolved" per FY** through one helper `fy_overlap.last_good_of_fy` (R2): configured FY =
  newer of `last_successful_run(leg)` (only if it maps to the configured FY) and `last_successful_run(leg,
  fy=configured)`; next FY = `last_successful_run(leg, fy=next)`; closed / other FY = newest success of that FY among
  `get_runs_between(2000-01-01, close or now, mode=leg)` mapped by D8-2. A problem is resolved only by a later success
  of the SAME FY (P3 per FY). Baseline (no records) returns exactly `last_successful_run(leg)`.
- **D8-9 API.** New keyword `config=None` on `build_report_model`, `generate_report_html`, `build_summary_email`,
  `send_summary_report`, `send_weekly_report`, `send_monthly_report`. `config=None` = today's behaviour, byte-identical
  (every existing test and `core/email_samples.py` keep calling without it; samples stay baseline). Callers that pass
  it: `flow._send_summary_report` (`flow.py:2422`), `ui.trigger_weekly_email` (`ui.py:1466`, `:1470`),
  `ui.trigger_monthly_email` (`:1506`, `:1510`), `ui._serve_report` (`:1544`). `schedule=` stays a separate argument.
- **D8-10 CSV** gets a last column "Financial Year" only in an FY view; baseline header unchanged (`tests/test_c3`
  checks it).
- **D8-11 Errors.** `db.overlap_record` raises -> no exception; rows split by suffix only (D8-1 b), next/closed windows
  unknown (`scheduled` absent), and the summary carries one line "The financial-year state could not be read; the
  next financial year may not be backed up. Open the dashboard." with `act` = True (E5, as B7 Q9 RED). A config without
  `paths` / `rollover` / a readable configured FY -> baseline.
- **D8-12 G4 leftover** (record `overlap:<configured>` without `ended_at` after a switch, `PROGRESS` B4 G4): no closed
  window is known; rows go by `row_fy` (nightly rows of the overlap carry `extended_metrics.fy`), fy-less unsuffixed rows
  go to the configured FY, the old FY's window ends at the period end. Accepted (rare, only after a DB error at the
  switch); noted in D1.
- **D8-13 Dashboard night strip** (`ui._nights` `ui.py:595-604`, `get_runs_between(..., mode)` with no FY filter; the
  PROGRESS note "dashboard night strip still mixes both FYs' rows") is **B9, not B8**: it is a card widget, plan
  section 6 lists `_nights` taking `fy`. Same for `/status` `recent_runs` `fy`. No B8 rows for it.
- **D8-14 Alert mails' "Is my data safe?"** (`email_compose._data_safety` `core/email_compose.py:125-139`, also in the
  support bundle `core/support_bundle.py:177`) is in B8 (plan 2.5 Reports bullet): while the overlap is active one line
  per (enabled leg, FY) "Office NAS backup (FY27-28): last good copy ..." / "...: no good copy recorded yet."; outside
  the overlap today's lines, but the configured FY's time comes from `last_good_of_fy` (after a switch never the old
  FY's final copy). `_next_text` "runs only when started from the dashboard" stays in C2.

---

## 1. Current behaviour (found in the code)

| What | Where | Fact |
|---|---|---|
| Runs of the period | `core/report.py:317-318` `build_report_model` | `db.get_runs_between(start, end)` filtered on `mode in ("cloud","lan")`; both FYs' rows of a leg are mixed (suffix ignored). |
| Leg summary | `core/report.py:201-234` `leg_summaries(db, runs, legs)` | one entry per leg: `total` (finished), `ok`, `problems` (fault title, `resolved` = any later OK of the leg in the period **or** `db.last_successful_run(leg)` newer `:219-226`), `last_good` from `last_successful_run(leg)` (`fy=None` = unsuffixed rows). **An FY27-28 failure is "resolved" by an FY26-27 success; FY27-28 OK runs hide FY26-27 failures in the counts.** |
| Scheduled count | `core/report.py:293-304` `_scheduled_counts` | `overdue.firings_between(<leg>_cron, tz, rp.start, rp.end)` (`core/overdue.py:51`); exception -> key removed (unknown = empty is action needed). No activation base, no manual-only leg. |
| Headline | `core/report.py:244-268` `report_headline(db, summaries)` | first leg with `total == 0` and `scheduled > 0` -> "action needed: <label> - no backup ran"; then open problems; then `db.latest_audit(leg)` VERIFICATION_FAILED; then open decisions; "all backups OK". |
| Audit rows | `core/report.py:271-290` `_audit_row(db, leg)` | `latest_audit(leg)` (`fy=None`): the next FY's audit (`-FY27-28` audit ids, B6) is never shown. |
| Per-leg data | `core/report.py:338-340` | `per_leg[leg] = {files, bytes}` summed over both FYs. `tests/test_c3_report_model.py:50-51` asserts this exact dict. |
| HTML | `core/report.py:353-388` `_summary_html`, `:397-488` `render_report_html` | label `LEG_LABEL` (`core/email_compose.py:27-35`), pipeline column `mode.upper()` `:417`, stats per leg `:429-433`, integrity `"<LEG> Integrity"` `:446-450`. |
| CSV | `core/report.py:128-157` `_generate_csv_data(runs)` | 11 columns, no FY. |
| Entry points | `generate_report_html` `:491`, `build_summary_email` `:535`, `send_summary_report` `:512`, `send_weekly_report` `:555`, `send_monthly_report` `:570` | none receives the config (only `legs`, `schedule`, `alerts_off`). |
| Callers | `flow.py:2412-2446` `_send_summary_report` (previous week/month of the scheduled time, `legs=_enabled_legs`, `schedule=config.schedule`); `ui.py:1462-1472`, `:1502-1512` (email buttons, `last_days`), `ui.py:1533-1555` `_serve_report` (download); `core/email_samples.py:69` (preview DB) | |
| Row FY helpers | `core/fy_overlap.py:90` `fy_of_id`, `:162` `row_fy`, `:51` `next_fy`, `:57` `prev_fy`, `:343` `active_next_fy` (B7) | `row_fy`: suffix, `extended_metrics.fy`, `guards.fy`, else None. |
| What rows carry | `flow.py:457-467` `_pass_metrics` | overlap active: configured pass `{"fy": FY26-27}`, next pass `{"fy": FY27-28, "overlap": true}` + suffixed `run_id`; no overlap (`fy_pass=None`): nothing (only `guards.fy` when the guards ran). April final rows `<id>-final-<leg>` (`flow.py:3009`) have no suffix and no `fy`. |
| DB | `core/manifest.py:789` `get_runs_between`, `:814` `last_successful_run(mode, *, fy)`, `:932` `latest_audit(mode, scope, *, fy)`, `:1323` `overlap_record(fy)` | `fy=None` = unsuffixed rows only. |
| Alert "data safe" | `core/email_compose.py:125-139` `_data_safety(config, db, now)` | one line per enabled leg from `last_successful_run(leg)`; next FY never named. |
| Night strip | `ui.py:595-604` `_nights` | mixes both FYs -> **B9** (D8-13). |

## 2. B8 behaviour to build

**FY view** (D8-1..D8-4, D8-11, D8-12) = `report_fy_view(config, db, rp)`, a dict or None:
`{"configured": "FY26-27", "next": "FY27-28" | None, "next_off": bool, "closed": "FY26-27" | None,
"closed_at": iso | None, "windows": {fy: (start, end) | None}, "error": str | None}`. None = baseline (no config, no
configured FY, or nothing to split by D8-1 after the rows are known; `build_report_model` decides with the rows).

| # | Model / output | Baseline (no FY view) | FY view |
|---|---|---|---|
| S1 | `summaries` | today | one per (leg, FY) (D8-4 order), keys as today + `"fy"`; `label` per D8-5/D8-6; `total/ok/problems` from that FY's rows only; `resolved` and `last_good` per FY (D8-8); `scheduled` per window (D8-3, D8-5); closed FY detail note (D8-7); `audit_status` for the headline (D8-7) |
| S2 | `headline`, `act` | today | today's order over the sections; audit check reads `s["audit_status"]` when present; D8-11 error -> act True |
| S3 | `per_leg` | today (`{leg: {files, bytes}}`) | keys `"<leg>:<fy>"`, values `{files, bytes}`; never summed across FYs |
| S4 | `audits` | today | one per (leg, active FY), each with `"fy"` |
| S5 | new keys | none | `"fy_view"` (the dict), `"run_fy"` `{run_id: fy}`, `"labels"` `{"<leg>:<fy>": label}` |
| S6 | HTML | byte-identical | labels with FY, pipeline column `"CLOUD FY27-28"`, integrity `"CLOUD FY27-28 Integrity"`, closed note, D8-11 line |
| S7 | CSV | identical | + last column "Financial Year" |
| S8 | subject | identical | headline with the section label |
| S9 | `_data_safety` | today's lines (time via `last_good_of_fy`, equal to today when there is no record) | while the overlap is active: one line per (leg, FY) with "(FYxx-yy)"; record read error -> today's lines + "The next financial year's backup state could not be read - it may not be backed up." |

Manual-only (D8-5) applies with and without an FY view whenever `config` is passed.

## 3. Test matrix (all in `tests/test_fy_overlap_report.py` unless stated)

Fixtures: real `ManifestDB(tmp_path / "m.db")`; rows via `db.insert_run({...})` (pattern `tests/test_m5_reports_plain.py:29-33`):
configured pass `run_id "<n>-cloud"`, `extended_metrics {"fy": "FY26-27"}`; next pass `"<n>-cloud-FY27-28"`,
`{"fy": "FY27-28", "overlap": true}`; legacy rows with `extended_metrics` `"{}"` / NULL. Record via
`db.set_overlap_record("FY27-28", {"activated_at": "2026-12-31T06:00:00+05:30", "from_fy": "FY26-27"})`; switch =
`ended_at "2027-04-01T06:10:00+05:30"`. Config = `SimpleNamespace(paths=SimpleNamespace(source_drive=r"E:\FY26-27",
lan_destination=r"\\nas\backup\FY26-27"), rollover=SimpleNamespace(overlap_enabled=True),
cloud=SimpleNamespace(enabled=True, scheduled=True), lan=SimpleNamespace(enabled=True, scheduled=True),
schedule=SimpleNamespace(cloud_cron="0 18 * * *", lan_cron="0 1 * * *", timezone="Asia/Kolkata"))` (after the switch
the paths say FY27-28); one row (R44) with a real `AppConfig` from the sandbox. Periods are built with
`ReportPeriod(start, end, label, kind)` or the `previous_week` / `previous_month` / `last_days` constructors; HTML
comparisons under `pendulum.travel_to(..., freeze=True)` (the "generated" stamp). Calendar facts: 28 Dec 2026 and
29 Mar 2027 are Mondays; default crons cloud 18:00 daily, LAN 01:00 daily. Layer: M = `build_report_model`,
H = HTML / CSV / subject, E = `email_compose._data_safety`, P = plumbing, U = helper unit.

| ID | Scenario | Inputs | Expected | Layer / test name | Task |
|---|---|---|---|---|---|
| R01 | Baseline guard: config given, no record, no suffix | week 12-19 Oct 2026, nightly rows both legs, one LAN failure | model with `config=cfg` == model with `config=None` (same keys and values); HTML byte-identical (frozen time); CSV bytes identical | M+H `test_no_record_is_byte_identical_to_baseline` | B8 |
| R02 | Overlap off, no record | as R01, `overlap_enabled False` | identical to R01 | M+H `test_overlap_off_without_record_is_baseline` | B8 |
| R03 | Record exists but period before activation | record 31 Dec 06:00, period 21-28 Dec | identical to baseline; no `fy_view` key | M `test_period_before_activation_is_baseline` | B8 |
| R04 | Baseline April rollover, overlap off, no record (D8-1) | 29 Mar - 5 Apr 2027, rows with `guards.fy` FY26-27 before 1 Apr 06:10 and FY27-28 after, config FY27-28 | identical to `config=None`; no FY text | M+H `test_baseline_rollover_week_does_not_split` | B8 |
| R05 | Config without `paths` / `rollover`; MagicMock db | `SimpleNamespace(cloud=..., lan=...)` | no exception; baseline | M `test_partial_config_is_baseline` | B8 |
| R06 | First January week, both FYs OK | `previous_week(4 Jan 2027 08:00)` = 28 Dec - 4 Jan; configured rows nightly 28 Dec-3 Jan (7+7); FY27-28 cloud 31 Dec,1,2,3 Jan 18:00 (4), LAN 1,2,3 Jan 01:00 (3) | 4 summaries in order cloud FY26-27, cloud FY27-28, lan FY26-27, lan FY27-28; `ok/total` 7/7, 4/4, 7/7, 3/3; `scheduled` 7, 4, 7, 3; headline "all backups OK", `act` False | M `test_first_january_week_two_sections_per_leg` | B8 |
| R07 | December monthly, sent 1 Jan 08:30 (core B8 case) | `previous_month(1 Jan 2027 08:30)`; configured nightly; FY27-28 cloud 31 Dec 18:00 OK; no FY27-28 LAN row | LAN FY27-28 `scheduled` 0, detail "No backup was scheduled in this period."; headline not "no backup ran", `act` False; cloud FY27-28 1/1 `scheduled` 1 | M+H `test_december_monthly_no_false_no_backup_ran` | B8 |
| R08 | Server off on 31 Dec | record `activated_at` 1 Jan 2027 06:05; week 28 Dec - 4 Jan | FY27-28 `scheduled` cloud 3 (1,2,3 Jan), LAN 2 (2,3 Jan) | M `test_late_activation_moves_the_window` | B8 |
| R09 | Next FY truly missing (truth kept) | R06 but no FY27-28 LAN rows at all | headline "action needed: Office NAS backup - FY27-28 - no backup ran"; `act` True | M `test_next_fy_missing_runs_are_reported` | B8 |
| R10 | Failure not resolved by the other FY | FY27-28 LAN failed 3 Jan 01:00 (`AAM-NAS-01`), FY26-27 LAN OK 3 Jan 01:00 and after; and the mirror case (FY26-27 fails, FY27-28 OK) | the failing FY's problem `resolved` False, the other section "No problems."; `act` True | M `test_problem_resolved_only_by_same_fy` | B8 |
| R11 | P3 per FY after the period | FY27-28 LAN failed 3 Jan; FY27-28 LAN OK 4 Jan 01:00 (after the period); FY26-27 has no later success | FY27-28 problem resolved; FY26-27 untouched | M `test_resolved_after_period_per_fy` | B8 |
| R12 | Last good copy per FY | FY26-27 LAN last OK 3 Jan 01:00, FY27-28 LAN last OK 2 Jan 01:00 | sections show "3 Jan 2027, 1:00 AM" / "2 Jan 2027, 1:00 AM"; FY27-28 never "none yet" and never the FY26-27 time | M `test_last_good_copy_per_fy` | B8 |
| R13 | Legacy rows in an overlap period | R06 plus configured rows with `extended_metrics` NULL and `"{}"` | they are counted in the FY26-27 sections; `run_fy` gives FY26-27 | M `test_legacy_rows_fall_into_configured_fy` | B8 |
| R14 | `row_fy` sources | unsuffixed rows with only `guards.fy` FY26-27, only `extended_metrics.fy`, a suffixed row with `extended_metrics.fy` absent | placed by suffix > `fy` > `guards.fy` | U `test_row_fy_or_default_order` | B8 |
| R15 | Data per (leg, FY) | FY26-27 cloud 100 files / 1000 B, FY27-28 cloud 40 / 400 | `per_leg["cloud:FY26-27"] == {"files": 100, "bytes": 1000}`, `["cloud:FY27-28"] == {"files": 40, "bytes": 400}`; HTML "Files copied (Online backup (Google Cloud) - FY27-28)" | M+H `test_data_per_leg_and_fy_never_summed` | B8 |
| R16 | Run table labels | R06 | every row's pipeline cell is "CLOUD FY26-27" / "CLOUD FY27-28" / "LAN FY..."; baseline (R01) cells stay "CLOUD" / "LAN" | H `test_run_table_names_the_fy` | B8 |
| R17 | CSV FY column | R06 via `build_summary_email(..., config=cfg)` | header ends `"Financial Year"`, each line's last cell = its FY; R01 header unchanged (starts `Started At`, 11 columns) | H `test_csv_has_fy_column_only_when_split` | B8 |
| R18 | Integrity per FY | R06 + `latest_audit(cloud, fy=FY27-28)` VERIFICATION_FAILED (audit id `u-cloud-full-FY27-28`), configured audit VERIFIED | rows "CLOUD FY26-27 Integrity" (VERIFIED) and "CLOUD FY27-28 Integrity" (VERIFICATION_FAILED); headline "action needed: Online backup (Google Cloud) - FY27-28 - safety check found differences"; FY27-28 LAN with no audit -> "NOT VERIFIED" | M+H `test_integrity_rows_per_fy` | B8 |
| R19 | Switch week, plan range 28 Mar - 3 Apr, built after the switch | period 28 Mar 00:00 - 4 Apr 00:00 2027; config FY27-28; record `ended_at` 1 Apr 06:10; rows: 28-31 Mar cloud 18:00 both passes, LAN 01:00 28 Mar - 1 Apr both passes; finals `x-final-cloud` / `x-final-lan` 1 Apr 06:00 (no fy); after the switch unsuffixed fy-less rows cloud 1,2,3 Apr 18:00, LAN 2,3 Apr 01:00 | per leg sections FY26-27 then FY27-28; cloud FY26-27 5 runs (4 nightly + final), `scheduled` 4; LAN FY26-27 6 runs, `scheduled` 5; cloud FY27-28 7 runs, `scheduled` 7; LAN FY27-28 7, `scheduled` 7; finals in FY26-27 (D8-2); FY26-27 detail ends "FY26-27 was closed on 1 Apr 2027, 06:10 (switch to FY27-28); it is not backed up any more."; integrity rows only FY27-28 | M+H `test_switch_week_labels_both_fys` | B8 |
| R20 | Same rows, built before the switch | period 28 Mar - 1 Apr 00:00, config FY26-27, record open | the same rows land in the same FY sections as in R19 | M `test_split_is_the_same_before_and_after_switch` | B8 |
| R21 | Switch blocked 1-2 Apr, switched 3 Apr 06:10 | `previous_week(5 Apr 2027 08:00)` = 29 Mar - 5 Apr; FY26-27 nightly both legs until 2 Apr, finals failed 1 and 2 Apr, manual LAN `LAN_COMPLETE` 2 Apr 14:00, finals OK 3 Apr 06:00; `ended_at` 3 Apr 06:10 | FY26-27 `scheduled` cloud 5 (29 Mar - 2 Apr), LAN 6 (29 Mar - 3 Apr 01:00); failed finals resolved by the 3 Apr final; manual 2 Apr row counted in FY26-27; no "no backup ran" for FY26-27 after 3 Apr | M `test_blocked_switch_window_ends_at_the_switch` | B8 |
| R22 | March monthly sent 1 Apr 08:30 after a 06:10 switch | `previous_month(1 Apr 2027 08:30)`; config FY27-28; March rows both passes daily | cloud FY26-27 31/31 from the unsuffixed March rows (never under FY27-28), cloud FY27-28 31/31 from the suffixed rows; repeat with the switch blocked (config FY26-27, record open): identical sections, no closed note | M `test_march_monthly_after_switch_keeps_true_fy` | B8 |
| R23 | Configured last good after the switch | config FY27-28, `ended_at` 1 Apr 06:10, last unsuffixed success = final 1 Apr 06:00 (FY26-27), last `-FY27-28` success 1 Apr 01:00, no post-switch success | FY27-28 section last good "1 Apr 2027, 1:00 AM"; FY26-27 section "1 Apr 2027, 6:..." | M `test_configured_last_good_after_switch` (+ U `test_last_good_of_fy_*`) | B8 |
| R24 | Configured audit after the switch | newest unsuffixed audit = FY26-27's 28 Mar VERIFICATION_FAILED; `latest_audit(cloud, fy=FY27-28)` VERIFIED 28 Mar | "CLOUD FY27-28 Integrity" VERIFIED; no FY26-27 integrity row; headline not "safety check found differences" | M `test_configured_audit_after_switch_is_its_own` | B8 |
| R25 | Overlap switched off, record open, FY27-28 rows exist | week 28 Dec - 4 Jan; FY27-28 rows 31 Dec - 1 Jan, then `overlap_enabled False` (no FY27-28 rows after) | FY27-28 sections from those rows, `scheduled` 0, label "Online backup (Google Cloud) - FY27-28 (not backed up - switched off in Settings)"; FY27-28 rows never in FY26-27 | M `test_overlap_off_keeps_rows_apart_no_false_alarm` | B8 |
| R26 | Overlap off, record open, no FY27-28 rows in the period | week 11-18 Jan, configured rows only | no FY27-28 section; the view is dropped (D8-1 last sentence): model and HTML identical to `config=None` | M+H `test_overlap_off_no_rows_is_baseline` | B8 |
| R27 | Bad `activated_at` | record `{"activated_at": "garbage"}`, no FY27-28 rows | FY27-28 section present with `scheduled` absent -> "action needed: ... FY27-28 - no backup ran" (fail loud) | M `test_unreadable_activation_fails_loud` | B8 |
| R28 | DB error reading the record | `db.overlap_record` patched to raise; R06 rows | no exception; sections split by suffix; FY27-28 `scheduled` absent; summary has "The financial-year state could not be read; the next financial year may not be backed up. Open the dashboard."; `act` True | M+H `test_record_read_error_is_reported` | B8 |
| R29 | Cloud-only | `lan.enabled False`, `legs=("cloud",)`, R06 rows (cloud only) | sections cloud FY26-27, cloud FY27-28 only; no "NAS" / "LAN" in summary, stats or integrity | M+H `test_cloud_only_sections` | B8 |
| R30 | LAN-only | `cloud.enabled False`, `legs=("lan",)` | LAN sections only | M `test_lan_only_sections` | B8 |
| R31 | Manual-only LAN, no overlap | `lan.scheduled False`, week 12-19 Oct, no LAN rows | LAN `scheduled` 0, label "Office NAS backup (run by hand)", detail "No backup was scheduled in this period - this backup runs only when started from the dashboard."; headline "all backups OK" (cloud OK) | M+H `test_manual_only_leg_is_not_missing` | B8 (from C2) |
| R32 | Manual-only LAN failed manual run | as R31 + one `LAN_FAILED` 15 Oct 14:00 | problem "not resolved", `act` True | M `test_manual_only_failure_still_needs_action` | B8 (from C2) |
| R33 | Manual-only LAN during the overlap | R06 rows minus the LAN FY27-28 rows, `lan.scheduled False` | both LAN sections "(run by hand)", `scheduled` 0, no "no backup ran"; cloud FY27-28 `scheduled` 4 | M `test_manual_only_leg_both_fys` | B8 (from C2) |
| R34 | Disabled leg | `legs=("cloud",)` with LAN FY27-28 rows in the DB | no LAN section for any FY; LAN rows not in `runs` | M `test_disabled_leg_no_section_any_fy` | B8 |
| R35 | Window boundaries | `activated_at` == period.end / == period.start / 1 s before period.end | no FY27-28 section / full window / section with `scheduled` 0 | M `test_activation_on_period_boundaries` | B8 |
| R36 | Dashboard rolling report | `last_days(7, 2 Jan 2027 10:00)` | FY27-28 `scheduled` cloud 2 (31 Dec, 1 Jan 18:00), LAN 2 (1, 2 Jan 01:00) | M `test_dashboard_rolling_window_counts_from_activation` | B8 |
| R37 | Subject | R09 via `build_summary_email(..., config=cfg)` | subject ends "- action needed: Office NAS backup - FY27-28 - no backup ran" | H `test_subject_names_the_fy` | B8 |
| R38 | Next-FY MISSED / RUNNING rows | FY27-28 cloud `CLOUD_MISSED` 2 Jan; FY27-28 `CLOUD_RUNNING` 3 Jan | MISSED is a problem of FY27-28; RUNNING not in `total` (as today) | M `test_next_fy_missed_and_running` | B8 |
| R39 | Feb 29 2028 | config FY27-28, record `overlap:FY28-29` activated 31 Dec 2027, `previous_month(1 Mar 2028 08:30)`, daily rows both FYs | cloud FY27-28 and FY28-29 `scheduled` 29 each, 29/29 | M `test_february_2028_has_29_scheduled` | B8 |
| R40 | Data safety during the overlap | record open, LAN FY26-27 OK 3 Jan, FY27-28 none; cloud both OK | lines in order: server line, "Office NAS backup (FY26-27): last good copy ...", "Office NAS backup (FY27-28): no good copy recorded yet.", cloud FY26-27, cloud FY27-28 | E `test_data_safety_one_line_per_fy` (in `tests/test_fy_overlap_report.py`) | B8 |
| R41 | Data safety outside the overlap | no record | lines identical to today's (string compare against a baseline call with the old function body captured in the test as literal expectations) | E `test_data_safety_baseline_unchanged` | B8 |
| R42 | Data safety after the switch | config FY27-28, `ended_at` 1 Apr 06:10, LAN last unsuffixed success = final 1 Apr 06:00, last `-FY27-28` LAN success 1 Apr 01:00 | LAN line (no FY in text) shows 1 Apr 2027, 1:00 AM | E `test_data_safety_after_switch_uses_true_fy` | B8 |
| R43 | Data safety record error | `overlap_record` raises | today's lines + "The next financial year's backup state could not be read - it may not be backed up."; no exception | E `test_data_safety_record_error_line` | B8 |
| R44 | Plumbing | monkeypatch `report.build_summary_email` / `generate_report_html` / `send_*_report` to capture kwargs; run `flow._send_summary_report(cfg, 7, "Weekly")`, POST `/trigger/report/weekly/email`, `/trigger/report/monthly/email`, GET the weekly download | each call got `config=` the loaded config (real `AppConfig` from the sandbox) | P `test_report_callers_pass_the_config` | B8 |
| R45 | Regression | `test_c1_report_period.py`, `test_c3_report_model.py`, `test_c6_report_consistency.py`, `test_m3_reports.py`, `test_m5_reports_plain.py`, `test_report_comprehensive.py`, `test_report_schedule.py`, `test_b_attachment_ladder.py`, `test_recipients_callers.py`, `test_alert_switches.py`, `test_characterization_core.py`, `test_integrity_audit.py`, `test_chaos_harness_batch2.py`, `test_fy_overlap_*.py` | green, unchanged | existing files | B8 |

Not in B8: `ui._nights` / `recent_runs` / cards (B9, D8-13); `_next_text` "runs only when started from the dashboard",
attention / watchdog for manual-only legs (C2); C2's report bullet is covered by R31-R33 (tick it with a pointer).

## 4. Chunks (one function per chunk; tests by one Haiku, code by another)

Signatures are fixed (Opus-defined; nobody below Opus changes them). Every overlap read uses `getattr(..., None)`
(test configs are `SimpleNamespace`, dbs may be `MagicMock`).

| Chunk | Function / change | Signature | Rows | Scope |
|---|---|---|---|---|
| R1 | row FY with default | `core/fy_overlap.py`: `row_fy_or_default(row: dict, configured: str, closed_at: str \| None) -> str` - `row_fy(row)`, else `prev_fy(configured)` when `closed_at` parses and `row["started_at"]` is before it (instants), else `configured`; an unparseable `started_at` -> `configured` | R13, R14, R19 | ~12 lines |
| R2 | last good per FY | `core/fy_overlap.py`: `last_good_of_fy(db, leg: str, fy: str, configured: str, closed_at: str \| None) -> dict \| None` (D8-8; the history scan for a closed / other FY uses `db.get_runs_between(pendulum.datetime(2000, 1, 1, tz=IST), <closed_at or now>, mode=leg)` and `statuses.SUCCESS_STATUSES`) | R12, R23, R42 | ~25 lines |
| R3 | FY view | `core/report.py`: `report_fy_view(config, db, rp: ReportPeriod) -> dict \| None` (shape in section 2; reads `overlap_record(next)` and `overlap_record(configured)`; DB error -> dict with `error` set and windows None; never raises) | R01-R05, R08, R25-R28, R35 | ~40 lines |
| R4 | sections of a leg | `core/report.py`: `_section_fys(view: dict, leg_runs: list[dict]) -> list[str]` (D8-4, FY ascending) | R06, R19, R25, R26, R34 | ~15 lines |
| R5 | summaries per FY | `core/report.py`: `leg_summaries(db, runs, legs, view: dict \| None = None, config=None) -> list[dict]` - `view=None` byte-identical to today; with a view one entry per (leg, FY) with `fy`, `label`, `audit_status`, closed note (D8-6, D8-7, D8-8); `config` only for D8-5 labels | R06, R09-R13, R19-R24, R31-R33, R38 | ~40 lines |
| R6 | scheduled per window | `core/report.py`: `_scheduled_counts(schedule, summaries, rp, view: dict \| None = None, config=None) -> None` - D8-3 windows, D8-4 overlap-off 0, D8-5 manual-only 0; unparseable / unknown window -> key removed | R06-R08, R19, R21, R25, R27, R31, R33, R35, R36, R39 | ~20 lines |
| R7 | audit per FY | `core/report.py`: `_audit_row(db, leg, fy: str \| None = None, view: dict \| None = None) -> dict` (D8-7; adds `"fy"` only when given) and `report_headline(db, summaries)` reads `s["audit_status"]` when present (signature unchanged) | R18, R24 | ~20 lines |
| R8 | model wiring | `build_report_model(db, report_period, legs=None, schedule=None, alerts_off=None, config=None) -> dict` - view from R3, kept only when it splits (D8-1 with the rows); new keys S5 only with a view; `per_leg` S3; D8-11 error line + `act` True | R01-R39 | ~30 lines |
| R9 | HTML | `_summary_html(model)` and `render_report_html(model, firm_name, is_email=False, name="")` (signatures unchanged): labels from `model.get("labels")`, pipeline cell `"<LEG> <fy>"` from `model.get("run_fy")`, integrity title with `a.get("fy")`, closed note, error line; without a view byte-identical | R01, R07, R15-R19, R28, R29, R31 | ~20 lines |
| R10 | CSV | `_generate_csv_data(runs, run_fy: dict \| None = None) -> bytes` - extra column only when `run_fy` is given | R17 | ~8 lines |
| R11 | entry points + callers | keyword `config=None` on `generate_report_html`, `build_summary_email`, `send_summary_report`, `send_weekly_report`, `send_monthly_report` (pass-through; `build_summary_email` passes `model.get("run_fy")` to R10); `flow._send_summary_report` `flow.py:2422` and `ui.py:1466`, `:1470`, `:1506`, `:1510`, `:1544` pass `config=` | R37, R44 | ~15 lines |
| R12 | data safety | `core/email_compose.py`: `_data_safety(config, db, now) -> list[str]` (signature unchanged; D8-14; uses `fy_overlap.active_next_fy` and R2; any error reading the record -> the extra line) | R40-R43 | ~20 lines |

Order: R1 -> R2 -> R3 -> R4 -> R5 -> R6 -> R7 -> R8 -> R9 -> R10 -> R11 -> R12. Before each: GitNexus `impact`
(`build_report_model` / `render_report_html` feed every report mail and the dashboard download; `_data_safety` feeds
every alert mail and the support bundle - expect MEDIUM/HIGH; R01, R02, R04, R41 are the guards that baseline output
did not move). After R8 and after R12 run all R45 files. Opus edits nothing in the existing report tests; if one of
them goes red, the code is wrong (baseline must be byte-identical), not the test.
