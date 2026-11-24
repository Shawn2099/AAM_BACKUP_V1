# Test matrix - B9 Dashboard API + UI (written 2026-11-24, before the build)

Reference for the B9 test writer and the B9 code writer (CLAUDE.md "Build process"). Binding plan:
`docs/PLAN_BASELINE_OVERLAP_2026-11-23.md` sections 2.2, 2.5, 4, 5, 6, 9 (B9), 10. Line numbers are from worktree HEAD
`505afce` plus the uncommitted B7 work in `core/attention.py` / `core/fy_overlap.py` (ui.py, templates and JS are
untouched by B7). **B9 starts only after B7 is committed** (it uses B7 K1 `fy_overlap.active_next_fy`, K9
`leg_timing(..., fy=)`, and the Q10 helper `attention._before_switch`).

Owner expectations for B9: **E-B9-1** the dashboard shows which legs are in use and how (cloud only, NAS only, both;
on a schedule or by hand only); **E-B9-2** it shows the actual truth (a row's real FY, a pass that has not started
is not "running", a manual-only leg is not "late"); **E-B9-3** with the overlap off (or before 31 Dec) everything
the baseline shows and does stays the same. Plus E3 / E4 of B7 (config decides, fail closed).

## DECISIONS (Opus, 2026-11-24, binding for B9)

- **Q1 Per-card running state.** Overlap off: `running` / `run_state` exactly as today (`_run_state_fields(await
  _is_running(leg))`, `ui.py:319`, `:548-549`). Overlap active: one Prefect query per leg returns the active runs
  with their `fy` parameter and start time (K6). Per card: Prefect unreachable -> `unknown`; no active run -> `idle`;
  an active run with `fy` = this card's FY -> `running`. A run without `fy` (every active FY, configured first):
  *next-FY card* -> `running` if that FY's last row is `*_RUNNING`, else `queued` with `run_note` "Waits for the
  FY26-27 backup to finish"; *configured card* -> `running` if its last row is `*_RUNNING` or that row started before
  the active run started (its pass has not begun yet), else `idle` (its pass of this run is done). `running` stays a
  strict bool (`run_state == "running"`); new field `leg_busy` = the leg has any active run (the card's buttons are
  disabled, triggers still answer 400 `already_running` for the whole leg as today).
- **Q2 Trigger `?fy=`.** `/trigger/cloud`, `/trigger/lan`, `/trigger/verify/{mode}`. Without `fy`: exactly today's
  call (`arun_deployment(name=..., timeout=...)`, no `parameters` for backups; `{"mode": mode}` for verify) and no new
  DB read. With `fy`: order = auth, rate limit, disabled leg (409 `disabled`, unchanged text), `fy` check, running
  check (503 / 400 as today), start. `fy` check: upper-cased; not `^FY\d{2}-\d{2}$` -> 400 `bad_fy`; the active set
  (K1) cannot be read -> 503 `fy_unknown` ("the financial years backed up at the moment could not be read"; fail
  closed); not in the active set -> 409 `fy_not_active` "<FY> is not backed up at the moment (backed up: FY26-27)".
  Valid -> `parameters={"fy": fy}` (verify: `{"mode": mode, "fy": fy}`). The configured FY is valid with the overlap
  off too (the flow accepts it, `flow.py:3356-3361`). The scheduled switch is not checked: an unscheduled leg is
  accepted like a scheduled one (plan section 6).
- **Q3 Approved re-run.** `_start_approved_run` (`ui.py:777`) passes `{"approval_id"}` exactly as today while the
  overlap is not active (`tests/test_m2_approval_dashboard.py:79,144,168` assert that dict). While it is active and
  the request has an `fy` -> `{"approval_id", "fy": req["fy"]}`; a request without `fy` -> `{"approval_id"}` (every
  active FY; the flow's `_approval_of_pass` applies it to its own FY). A request whose `fy` is the configured FY's
  next FY but that FY is not active (overlap switched off or ended) -> 409 "This request is for FY27-28, which is not
  backed up at the moment - dismiss it". The check (K18) runs **before** `approve_request`, so a refused request is not
  marked approved. Active-set read error -> 503. Other FYs (e.g. an old-FY request after a baseline switch) keep
  today's behaviour.
- **Q4 Night strip.** `_nights(db, mode, cfg)` (`ui.py:595`) gets `card_fy`. `card_fy=None` = today (all rows of the
  leg). The card builder passes `card_fy` only when FY labels apply (Q5 rule: overlap active, or an
  `overlap:<configured>` record exists = the first year after an overlap switch); then a row counts for the card when
  `attention.row_fy_label(db, cfg, row) == card_fy` (K4, the same rule as the labels, so a legacy row from before the
  switch counts for the old FY). So with the overlap never used the strip is byte-identical; during the
  overlap each card shows only its FY's nights; after the switch the FY27-28 card shows its Jan-Mar `-FY27-28` nights
  and not FY26-27's.
- **Q5 FY of a shown row (truth after the switch, B7 Q10).** New `attention.row_fy_label(db, config, row)` (K4):
  None while no overlap ever happened for the configured FY (no active overlap and no `overlap:<configured>` record:
  baseline, no labels); else id suffix -> it; else `extended_metrics.fy` / `guards.fy` (`fy_overlap.row_fy`); else
  an unsuffixed row started before `overlap:<configured>.ended_at` -> `prev_fy(configured)`; else the configured FY.
  Used for `last_run.fy`, `last_success_fy`, `integrity.fy` and `recent_runs[].fy`. JS shows "(FY26-27)" next to a
  time only when that FY differs from the card's FY (history: from `data.fy_prefix`), so baseline screens never change.
- **Q6 Scheduled / manual (E-B9-1, E-B9-2).** Card field `scheduled` = what the scheduler really runs:
  `schedule_in_effect.<leg>_scheduled` (`core/manifest.py:1292-1293`, absent flag = true), config value when no record;
  `scheduled_setting` = `getattr(cfg.<leg>, "scheduled", True)`. They differ only between saving Settings and the
  restart -> JS adds "(changes after the restart)". Until C1 writes the flags, `serve.py:35-55` still registers the
  cron, so `scheduled: true` is the truth then. `schedule_text`: scheduled -> "Next scheduled backup: <cron_to_human>"
  (identical to the template line `templates/dashboard.html:57,76`), else "Runs only when you press Run backup".
  `timing` for an unscheduled leg = `{"state": "manual", "due_at": null, "overdue_at": null}` (never "late" /
  "overdue"; JS treats it like `on_time`). B9 therefore delivers C2's "card schedule line" item; C2 keeps the
  attention / watchdog / report part.
- **Q7 Legs summary (E-B9-1).** New top-level `/status.legs = {"cloud": "scheduled"|"manual"|"off", "lan": ...,
  "summary": <text>}` from the in-effect values, shown in a new line `#legs-summary` under "Where your files go"
  (always, also with the overlap off - additive text, owner expectation). Texts (NAS = "office NAS copy", cloud =
  "online copy"): both scheduled "Both copies run every night on their schedule."; one off: "Only the online copy is
  in use (the office NAS copy is switched off)." / "Only the office NAS copy is in use (the online copy is switched
  off)."; with a manual one: "The online copy runs on its schedule; the office NAS copy runs only when you press Run
  backup." (and the mirror sentence); one off + the other manual cannot pass the config validator (A6), but the text
  is still defined: "Only the online copy is in use, and it runs only when you press Run backup." While the overlap
  is active the summary adds " Until the April switch both FY26-27 and FY27-28 are backed up."
- **Q8 `/status` shape.** Existing keys and values unchanged when the overlap is off (snapshot test U01). Additive:
  top-level `overlap` (K13, always present), `legs` (Q7); per leg `fy`, `scheduled`, `scheduled_setting`,
  `schedule_text`, `last_success_fy`, `leg_busy`, `run_note`; `last_run.fy`, `last_run.total_files` (from
  `extended_metrics.total_files`, None if absent); `integrity.fy`; `recent_runs[].fy`. `cloud_next` / `lan_next`
  only while the overlap is active **and** the leg is enabled (a disabled leg keeps its one "Disabled" card, plan
  section 6 table), same shape as `cloud` / `lan`, all lookups with `fy=<next>`. The configured cards keep `fy=None`
  lookups. Record unreadable -> `overlap = {"enabled": .., "active": null, "error": "<type>"}`, no `*_next` keys
  (the banner carries B7's RED `overlap:unknown`).
- **Q9 `/api/rollover`.** Adds `overlap` = `fy_overlap.overlap_state(...)` (K3). `final_copy` already exists (B4,
  `ui.py:1248-1255`) and stays. "Run FY rollover now" is also allowed when the activation is due but not done
  (`overlap.activation_pending`): `can_run: true`, `message` = the overlap note; `run_rollover_now` (`ui.py:1281`)
  accepts the same case (it starts the normal check, which activates first - plan section 3). The predicate lives
  once in `fy_overlap.activation_pending` (K2); `attention._activation_item` calls it (no behaviour change, B7
  T30-T36 stay green). `manual_run_state` is not changed (its callers and tests stay).
- **Q10 Check now.** Per-card "Check now" sends the card's `fy` while the overlap is active; "Check both copies now"
  sends none (the flow audits every active FY, next first). `_backup_first_question(cfg, mode, fy=None)` asks about
  that FY's last success (`last_successful_run(leg, fy=<next>)` for the next FY, `fy=None` for the configured one).
  The `?anyway=1` retry keeps the `fy`.
- **Q11 Timing errors.** `ui._leg_timing` keeps today's fallback (`on_time`) for read errors; the banner holds the
  truth (B7 Q9 RED `overlap:unknown`, RED `stale:` on a bad record). Not changed in B9.
- **Q12 Names on screen.** Overlap active: card titles "Online copy (cloud) - FY26-27" / "- FY27-28", "Office copy
  (NAS) - ..."; hero leg names "The cloud backup of FY27-28"; approval card title "LAN backup (FY27-28) blocked" when
  `a.fy` is set; the rail stat labels "Files on the office NAS (FY26-27)" / "Files in the cloud (FY26-27)" (the
  counters hold the configured FY only, plan risk 11). Overlap off: every text as today.
- **Q13 Next-FY card texts.** No row yet: scheduled -> badge "First backup pending", desc "FY27-28 is backed up from
  the next scheduled backup (with FY26-27)."; unscheduled -> "Not backed up yet - press Run backup". Last success
  missing -> "! No successful backup of FY27-28 yet". Details show "Files in this copy (last run): N" from
  `last_run.total_files`.
- **Q14 After the switch.** Two cards (configured FY27-28), no `*_next` (no `overlap:FY28-29` record). The overlap
  block says "FY28-29 will be added on 31 Dec 2027". Rows of FY26-27 in history carry `fy: "FY26-27"` and show the
  label (Q5).

---

## 1. Current behaviour (found in the code)

| What | Where | Fact |
|---|---|---|
| `/status` | `ui.py:514-592` | `cloud` / `lan` built inline: `_run_state_fields(await _is_running(leg))`, `enabled`, `last_run` (`_last_run_summary` `:1611`, `db.last_run(mode)`), `last_success` (`_get_last_success` `:1561`), `timing` (`_leg_timing` `:607`), `last_run_formatted`, `integrity` (`_integrity_summary` `:1569`), `nights` (`_nights` `:595`). All `fy=None`. No `fy`, `scheduled`, `overlap`, `*_next`. `recent_runs` (`:525-540`) has no `fy`. `schedule` = the two crons as text. |
| Night strip | `ui.py:595-604` | `db.get_runs_between(start, end, mode)` = every row of the leg, so during the overlap **both FYs' rows are mixed** into one strip (a failed FY27-28 night turns the FY26-27 strip red). `core/night_strip.build` takes the worst run per IST day. |
| Card timing | `ui.py:607-613` | `leg_timing(db, cfg, leg)`; B7 added `fy=` (`core/attention.py:186`). An unscheduled leg is timed against its cron (shows "late"/"overdue"). |
| Running | `ui.py:202-270` | `_prefect_has_active_run(pipeline)`: one `read_flow_runs` (RUNNING/PENDING/SCHEDULED, limit 200), match by tag or `parameters.mode`; returns tri-state bool, no parameters. A next-FY pass queued behind the configured pass is indistinguishable. Also used by `audit_lab/test_w14_ui.py:156-248`. |
| Triggers | `ui.py:648-683`, `:686-714` | disabled 409, unreachable 503, running 400, `arun_deployment(name=..., timeout=...)` with **no parameters**; no `fy`. Tests fake `arun_deployment(name, timeout=None)` (`tests/test_f2_trigger_remediation.py:282,302`) - a new kwarg on the no-`fy` path would break them. |
| Verify trigger | `ui.py:1369-1420` | `parameters={"mode": mode}`; `_backup_first_question(cfg, mode)` `:1340` uses `last_successful_run(leg)` (configured FY only). Flow accepts `fy` (`flow.py:3041`, refuses inactive `:3081-3083`). Tests use a `SimpleNamespace` cfg with only `cloud`/`lan` (`tests/test_verify_trigger.py:34-38`). |
| Approved re-run | `ui.py:777-793` | `parameters={"approval_id": ...}`; `_approval_view` `:744` already returns `fy`. Flow `backup(fy=)` refuses an inactive FY (`flow.py:3356-3361`). |
| Rollover API | `ui.py:1234-1269` | already returns `final_copy` (B4) while `overlap_open`; no overlap / activation block; `can_run` only from `manual_run_state` (`core/fy_rollover.py:147`) or an open journal; `run_rollover_now` `:1272-1295` same rule. Rendered by `static/js/settings.js:564-582` (shows neither `final_copy` nor any overlap text). |
| Dashboard page | `ui.py:487-511`, `templates/dashboard.html:54-91` | two hand-written cards (`card-lan`, `card-cloud`), schedule line from Jinja (`lan_schedule` / `cloud_schedule`), disabled leg: note instead of the form. |
| Dashboard JS | `static/js/dashboard.js` | two copy-pasted card blocks `:246-307`; `triggerBackup(event, mode)` `:11` posts `/trigger/<mode>`; `triggerVerify(mode)` `:362`; `legClass` `:320`, `renderLegVisuals` `:344`, `updateVerifyUi` `:385`, `setServerUnreachable` `:72` / `setSessionExpired` `:86` loop `['cloud','lan']`; `renderHistory` `:415` (no FY); `approvalCard` `:469` (no FY); `renderHero` `:156`. |
| Overlap helpers | `core/fy_overlap.py` | `active_passes` `:127` (needs a real config: calls `config_for_fy`), `overlap_open` `:333`, `active_next_fy` `:343` (getattr-safe, DB errors propagate), `final_copy_state` `:388`, `row_fy` `:162`. **No `overlap_state`** (plan 2.1 names it) and no `activation_pending`; the activation predicate is inline in `attention._activation_item` `:363-394`. |
| Schedule in effect | `core/manifest.py:1277-1321` | record carries `cloud_scheduled` / `lan_scheduled` (absent = true); `serve.py:107-123` does not write them yet (C1). |
| Test configs | `tests/test_m3_dashboard_truth.py:25-35`, `tests/test_verify_trigger.py:34`, `tests/test_f2_trigger_remediation.py` | `SimpleNamespace` configs without `rollover`, `schedule` flags or `scheduled`; new reads must be `getattr(..., default)`, and the no-`fy` trigger paths must not read `paths` / the DB. |

## 2. B9 behaviour to build

Card keys: `cloud`, `lan` (configured FY, `fy=None` DB lookups) and, while active, `cloud_next`, `lan_next` (DB
lookups `fy=<next>`). Card FY: `card_fy` = configured FY (`fy_rollover.effective_fy_prefix`) or the next FY.

| # | Item | Where | Rule |
|---|---|---|---|
| I1 | card shape | `/status.<key>` | baseline keys + `fy`, `scheduled`, `scheduled_setting`, `schedule_text`, `last_success_fy`, `leg_busy`, `run_note`; `last_run.{fy,total_files}`; `integrity.fy` (Q8) |
| I2 | next cards | `/status.cloud_next`, `lan_next` | only overlap active + leg enabled (Q8) |
| I3 | overlap block | `/status.overlap`, `/api/rollover.overlap` | `overlap_state` (K3): `enabled, active, configured_fy, next_fy, activates_on, activated_at, ended_at, activation_pending, note` |
| I4 | legs plan | `/status.legs` | Q7 |
| I5 | running per card | card `running/run_state/leg_busy/run_note` | Q1 |
| I6 | strip per FY | card `nights` | Q4 |
| I7 | timing per FY / manual | card `timing` | `leg_timing(..., fy=<next>)` for next cards; `manual` for an unscheduled leg (Q6) |
| I8 | FY labels | `last_run.fy`, `last_success_fy`, `integrity.fy`, `recent_runs[].fy` | Q5 |
| I9 | triggers `?fy=` | `/trigger/cloud`, `/trigger/lan`, `/trigger/verify/{mode}` | Q2, Q10 |
| I10 | approved re-run | `/approvals/{id}/approve`, `/run` | Q3 |
| I11 | rollover | `/api/rollover`, `/api/rollover/run` | Q9 |
| I12 | page | template + `dashboard.js` | one `renderLegCard(key, d)`; 4 cards, the next row hidden unless `data.overlap.active`; per-card Run backup / Check now carry `data-fy`; texts Q12, Q13 |
| I13 | settings rollover card | `settings.js` `loadRollover` | shows `overlap.note` and, when `final_copy` is set, one line per required leg: "Cloud: done (1 Apr 2027, 14:00, <this morning's check / an earlier run>)" or "NAS: outstanding - <hint>" |

## 3. Test matrix (all in `tests/test_fy_overlap_ui.py` unless stated)

Fixtures: real `AppConfig` from `test_support.sandbox_config._config_dict(tmp_path)` (source `tmp/source/FY26-27`,
NAS `\\127.0.0.1\...\FY26-27`) written to a temp `config.yaml` and loaded with `load_config`; real `ManifestDB` on
`tmp_path`; `monkeypatch` `ui._cfg`, `ui.get_db`, `ui._require_auth`, `ui._check_rate_limit`; `ui._is_running` /
`ui._is_verify_running` `AsyncMock`; `ui._active_leg_runs` `AsyncMock` returning `[{"fy": ..., "started_at": ...}]`;
fake `arun_deployment(name, parameters=None, timeout=None)` recording calls; `pendulum.travel_to(2027-01-10 12:00
IST, freeze=True)` unless stated; overlap on = `db.set_overlap_record("FY27-28", {"activated_at":
"2026-12-31T06:00:00+05:30", "from_fy": "FY26-27"})`; next-FY rows with run_id `x-cloud-FY27-28` and `"fy":
"FY27-28"` in metrics; configured rows `x-cloud`. Layer: A = HTTP via `TestClient(ui.app)` or `asyncio.run(ui.<endpoint>(_fake_request(q)))`, P = pure helper, J = JS / template source check, E = real uvicorn (`tests/test_e8_dashboard_e2e.py`).

| ID | Scenario | Inputs | Expected | Layer / test name |
|---|---|---|---|---|
| U01 | Baseline snapshot, overlap never used | DB with 3 nights of both legs (one PARTIAL, one FAILED), an audit, no record; frozen time | `/status` minus the additive keys (Q8 list) equals `tests/data/status_baseline_b9.json`, captured by the test writer from the code **before** the B9 code commit (same fixture, same frozen time); no `cloud_next` / `lan_next`; `overlap.active` false; every new label field None | A `test_status_without_overlap_matches_baseline` |
| U02 | Overlap switched off, record open | U01 + record, `overlap_enabled: false` | same as U01 for cards (no `*_next`); `overlap.active` false, `overlap.enabled` false | A `test_status_overlap_off_has_two_cards` |
| U03 | Before activation | 30 Dec 2026 12:00, overlap on, no record | two cards; `overlap.note` "FY27-28 will be added to the backups on 31 Dec 2026"; `activation_pending` false | A `test_status_before_activation` |
| U04 | Activation pending | 1 Jan 2027 08:00, no record | `overlap.activation_pending` true, note says not added yet and "Run FY rollover now"; two cards | A `test_status_activation_pending` |
| U05 | Overlap active, four cards | record + rows of both FYs | `cloud_next.fy == "FY27-28"`, `cloud.fy == "FY26-27"`; each card's `last_run`, `last_success`, `integrity` come from its own FY's rows (seed different statuses per FY and assert each) | A `test_status_four_cards_each_own_rows` |
| U06 | Next FY not run yet | record, no `-FY27-28` rows | `cloud_next.last_run` None, `last_success` None, `timing.state == "waiting"`, `nights` all `none` | A `test_next_card_first_backup_pending` |
| U07 | Next FY overdue timing | record, now 3 Jan 2027 12:00, FY27-28 never succeeded | `cloud_next.timing.state == "overdue"`, `cloud.timing` from the configured rows | A `test_next_card_timing_from_activation` |
| U08 | Strip separation | 9 Jan: configured cloud OK, FY27-28 cloud FAILED | `cloud.nights[-2].state == "ok"`, `cloud_next.nights[-2].state == "bad"` | A `test_strip_per_fy` (Q4) |
| U09 | Strip unchanged without overlap | U01 DB | `cloud.nights` equals `night_strip.build(all rows)` | P `test_nights_without_card_fy_is_baseline` |
| U10 | Strip after the switch | config FY27-28, `overlap:FY27-28.ended_at` = 2 Apr 06:10, rows: `-FY27-28` 28-31 Mar OK, unsuffixed FY26-27 rows 28 Mar-1 Apr FAILED, unsuffixed FY27-28 row 2 Apr 18:00 OK; now 3 Apr | `cloud.nights` 28-31 Mar `ok` (its own `-FY27-28` rows), 1 Apr `none`, 2 Apr `ok`; no `cloud_next` | A `test_strip_after_switch_shows_own_year` (Q4, Q14) |
| U11 | Row label after the switch | U10 + newest unsuffixed row 2 Apr 06:00 CLOUD_FAILED of FY26-27 (`fy` FY26-27 in metrics) newest | `cloud.last_run.fy == "FY26-27"`; `recent_runs` FY26-27 rows `fy == "FY26-27"` | A `test_last_run_names_its_real_fy_after_switch` (Q5) |
| U12 | Label for a legacy row | row without metrics started before `ended_at` | label `prev_fy(configured)`; same row with the overlap never used -> None | P `test_row_fy_label_rules` (K4: suffix, metrics fy, guards fy, before switch, configured, never-overlap None) |
| U13 | Scheduled in effect | schedule record `lan_scheduled: false`, config `lan.scheduled: false` | `lan.scheduled` false, `schedule_text == "Runs only when you press Run backup"`, `timing.state == "manual"`, `legs.lan == "manual"` | A `test_manual_only_leg_card` (Q6) |
| U14 | Setting not applied yet | record without flags, config `lan.scheduled: false` | `lan.scheduled` true, `scheduled_setting` false, `timing` normal | A `test_scheduled_follows_the_running_scheduler` (Q6) |
| U15 | Scheduled text baseline | both scheduled | `cloud.schedule_text == "Next scheduled backup: " + cron_to_human(cloud_cron, tz)` | A `test_schedule_text_matches_template` |
| U16 | Legs summary | variants: both scheduled; `lan.enabled false`; `cloud.enabled false`; LAN manual; cloud manual; overlap active | `legs` values and the exact Q7 texts (overlap sentence appended only when active) | P `test_legs_plan_texts` (K12) |
| U17 | Disabled leg during overlap | `lan.enabled: false`, record | no `lan_next`; `lan.enabled` false; `cloud_next` present | A `test_disabled_leg_has_one_card` |
| U18 | Record unreadable | `db.overlap_record` raises | `/status` 200; `overlap.active` None with `error`; no `*_next`; attention has `overlap:unknown` | A `test_status_record_error_fails_visible` |
| U19 | SimpleNamespace config | `tests/test_m3_dashboard_truth.py` `_cfg()` | `/status` 200, no exception; `overlap.active` false; `scheduled` true | A `test_status_with_minimal_config` |
| R01 | Running: overlap off | `_is_running` True / False / None | `running` / `run_state` exactly as today; `_active_leg_runs` not called | A `test_running_without_overlap_is_baseline` |
| R02 | Run of all FYs, configured pass going | active `[{"fy": None, "started_at": T}]`, configured last row RUNNING | `cloud.run_state "running"`, `cloud_next.run_state "queued"`, `run_note` "Waits for the FY26-27 backup to finish", both `leg_busy` | P+A `test_queued_next_fy_card` (K7) |
| R03 | Configured pass done, next going | same run, configured last row CLOUD_COMPLETE started after T, next last row RUNNING | `cloud.run_state "idle"`, `cloud_next.run_state "running"`, both `leg_busy` true | P `test_configured_done_next_running` |
| R04 | Run just started, no rows yet | run started T, configured last row from yesterday | `cloud` running, `cloud_next` queued | P `test_starting_run_counts_for_configured` |
| R05 | One-FY run | active `[{"fy": "FY27-28"}]` | `cloud_next` running, `cloud` idle with `leg_busy` true | P `test_fy_run_only_marks_its_card` |
| R06 | Prefect unreachable | `_active_leg_runs` None | every card `run_state "unknown"`, `running` false | P `test_unreachable_is_unknown_on_all_cards` |
| R07 | Matcher unchanged | the `_prefect_has_active_run` cases of `tests/test_f2_trigger_remediation.py:201-265` and `audit_lab/test_w14_ui.py::test_w14_prefect_has_active_run_filter_and_tristate` | green unchanged after K5 | existing files |
| R08 | Active runs list | fake client: a RUNNING cloud run `parameters {"mode":"cloud","fy":"FY27-28"}`, an integrity run, a far-future SCHEDULED run, a lan run | `_active_leg_runs("cloud") == [{"fy": "FY27-28", "started_at": ...}]`; client error -> None | P `test_active_leg_runs_filter` (K6) |
| T01 | Trigger without fy | overlap on or off | `arun_deployment` called as `(name="aam-backup/backup-cloud", timeout=...)` with no `parameters` kwarg; `get_db` not called | A `test_trigger_without_fy_is_the_baseline_call` |
| T02 | Trigger active next FY | record, `POST /trigger/lan?fy=FY27-28&wait=false` | 202; parameters `{"fy": "FY27-28"}` | A `test_trigger_next_fy` |
| T03 | Trigger configured FY | `?fy=fy26-27` (lower case), overlap on and off | parameters `{"fy": "FY26-27"}` | A `test_trigger_configured_fy_upper_cased` |
| T04 | Inactive FY | `?fy=FY27-28` without a record; `?fy=FY28-29` with a record | 409 `fy_not_active`, text names the active FYs; no run | A `test_trigger_inactive_fy_is_409` |
| T05 | Bad FY | `?fy=2027`, `?fy=FY27-2` | 400 `bad_fy`; no run | A `test_trigger_bad_fy_is_400` |
| T06 | Disabled leg with fy | `lan.enabled false`, `?fy=FY27-28` | 409 `disabled` (today's text), no DB read for fy | A `test_trigger_disabled_wins` |
| T07 | Running leg with fy | `_is_running` True / None | 400 `already_running` / 503; no run | A `test_trigger_fy_running_and_unreachable` |
| T08 | Record unreadable with fy | `overlap_record` raises | 503 `fy_unknown`; no run | A `test_trigger_fy_record_error_fails_closed` |
| T09 | Unscheduled leg | `lan.scheduled false` (+ record) | `/trigger/lan` and `?fy=FY27-28` both 202 | A `test_trigger_manual_only_leg_accepted` |
| T10 | Verify with fy | `/trigger/verify/lan?fy=FY27-28&anyway=1` | parameters `{"mode": "lan", "fy": "FY27-28"}`; label names the FY | A `test_verify_next_fy` |
| T11 | Verify without fy | as `tests/test_verify_trigger.py:76-93` | parameters `{"mode": mode}` only | existing file green |
| T12 | Verify inactive / bad fy | as T04 / T05 | 409 / 400 | A `test_verify_fy_checks` |
| T13 | Backup-first per FY | configured success 1 h ago, FY27-28 never succeeded, `/trigger/verify/cloud?fy=FY27-28` | 409 `backup_first` "there is no successful cloud backup yet"; same without fy -> starts | A `test_backup_first_uses_the_cards_fy` (Q10) |
| P01 | Approved re-run, overlap off | request `fy` FY26-27, no record | parameters `{"approval_id": aid}` exactly | A `test_approved_rerun_baseline_parameters` (+ `tests/test_m2_approval_dashboard.py` green) |
| P02 | Approved re-run, overlap on | requests `fy` FY27-28 and FY26-27, record | parameters `{"approval_id": aid, "fy": "FY27-28"}` / `... "FY26-27"` | A `test_approved_rerun_passes_fy` |
| P03 | Request without fy, overlap on | `fy` None | `{"approval_id": aid}` | A `test_approved_rerun_without_fy` |
| P04 | Next-FY request after overlap off | request `fy` FY27-28, `overlap_enabled: false` | `/approve` 409, request still PENDING (not approved), no run; `/run` on an APPROVED one 409 | A `test_approval_for_inactive_next_fy_refused_before_approving` (Q3) |
| P05 | Record unreadable on approve | `overlap_record` raises, request FY27-28 | 503, request still PENDING | A `test_approval_record_error_fails_closed` |
| P06 | Rollover approval | `leg rollover` request | unchanged (`{"approval_id"}`, rollover deployment) | A `test_rollover_approval_unchanged` |
| O01 | Rollover API overlap block | U03 / U04 / U05 / U10 states | `overlap` fields and notes per Q9/Q14 (before: date; pending: not added; active: both years; after switch: next date) | A `test_api_rollover_overlap_block` |
| O02 | Final copy during switch pending | 1 Apr 2027 10:00, record open, cloud `CLOUD_COMPLETE` 1 Apr 06:00 `guards.fy` FY26-27, no LAN copy | `final_copy.cloud.done` true, `final_copy.lan.outstanding` true with hint; `overlap.active` true | A `test_api_rollover_final_copy_state` |
| O03 | Final copy absent | no record | `final_copy` None (as today) | A `test_api_rollover_no_final_copy_without_overlap` |
| O04 | Run now when activation pending | U04 state (kind NONE) | `/api/rollover` `can_run` true, message = note; `/api/rollover/run` starts the deployment; U03 state -> 409 as today | A `test_rollover_run_allowed_when_activation_pending` |
| O05 | `activation_pending` = B7 rule | the T30-T36 dates of `TESTMATRIX_B7` | `fy_overlap.activation_pending` returns (nxt, date) exactly on the days `_activation_item` is not None (or the day itself) and `tests/test_fy_overlap_attention.py` T30-T36 green | P `test_activation_pending_matches_attention` |
| J01 | One card renderer | `static/js/dashboard.js` | contains `function renderLegCard(key, d)`; the old per-leg blocks (`badgeCloud`, `badgeLan`) are gone; `['cloud', 'lan', 'cloud-next', 'lan-next']` (or the `_CARD_KEYS` list) used by `setServerUnreachable`, `setSessionExpired`, `renderLegVisuals` | J `test_js_one_card_renderer` |
| J02 | fy on buttons | JS | `triggerBackup` builds `'/trigger/' + mode + (fy ? '?fy=' + encodeURIComponent(fy) ...)`; `triggerVerify` keeps `fy` on the `anyway=1` retry; `data-fy` read from the button | J `test_js_buttons_send_fy` |
| J03 | Next row hidden | template | `card-cloud-next`, `card-lan-next` inside an element with `hidden`; JS shows it only when `data.overlap && data.overlap.active` | J `test_template_next_cards_hidden_by_default` |
| J04 | Labels only on difference | JS | FY label shown when `fy && fy !== cardFy` (history: `!== data.fy_prefix`); `run_state === 'queued'` handled; `timing.state === 'manual'` not prefixed | J `test_js_labels_and_states` |
| J05 | Rollover card | `settings.js` | `loadRollover` renders `d.overlap.note` and `d.final_copy` per leg (`outstanding`, `hint`, `done_at`) | J `test_settings_js_final_copy` |
| J06 | Baseline page text | `GET /` without overlap | still contains "Online copy (cloud)", "Office copy (NAS)", "Next scheduled backup: ", the disabled-leg note when a leg is off | A `test_dashboard_page_baseline_text` |
| E01 | E2E four cards | `tests/test_e8_dashboard_e2e.py` server fixture + `db.set_overlap_record("FY27-28", ...)`, `monkeypatch` `ui._is_running` / `ui._active_leg_runs` / `ui.arun_deployment` (recording), signed in | `GET /` has the four card ids; `/status` has `cloud_next` / `lan_next` with `fy` FY27-28 and `overlap.active` true | E `test_four_cards_with_overlap` |
| E02 | E2E per-card buttons | same | `POST /trigger/cloud?fy=FY27-28&wait=false` -> 202, recorded parameters `{"fy": "FY27-28"}`; `POST /trigger/lan?fy=FY26-27&wait=false` -> `{"fy": "FY26-27"}`; `POST /trigger/verify/lan?fy=FY27-28&anyway=1&wait=false` -> `{"mode": "lan", "fy": "FY27-28"}`; `?fy=FY29-30` -> 409; signed out -> 401 | E `test_per_card_buttons_send_their_fy` |
| E03 | E2E without overlap | same server, no record | `/status` has no `cloud_next`; `POST /trigger/cloud?wait=false` -> recorded call without `parameters` | E `test_two_cards_without_overlap` |
| G01 | Regression | `test_m3_dashboard_truth.py`, `test_f2_trigger_remediation.py`, `test_verify_trigger.py`, `test_m2_approval_dashboard.py`, `test_manual_verify_button.py`, `test_m5_dashboard_audit.py`, `test_m1_statuses.py`, `test_overdue_schedule.py`, `test_ui_auth.py`, `test_d2_dashboard_link.py`, `test_fix_26_1_1_mode_all_status.py`, `test_fy_overlap_attention.py`, `test_fy_overlap_rollover.py`, `test_e8_dashboard_e2e.py` (old test), `audit_lab/test_w14_ui.py` (targeted) | green, unchanged | existing files |

## 4. Chunks (one function per chunk; tests by one Haiku, code by another)

Signatures are fixed (Opus-defined; nobody below Opus changes them). Every new read of the config uses
`getattr(..., default)`; no-`fy` paths add no DB read.

| Chunk | Function / change | Signature | Rows | Scope |
|---|---|---|---|---|
| K1 | active FYs | `core/fy_overlap.py`: `active_fys(config, db) -> list[str]` - `[]` without an FY folder; `[configured]`; `+ [nxt]` when `active_next_fy` gives one. DB errors propagate. | T02-T08 | ~10 lines |
| K2 | activation predicate | `core/fy_overlap.py`: `activation_pending(config, db, today: dt.date) -> tuple[str, dt.date] \| None` - (next FY, activation day) when overlap on, configured known, no record, `today >= activation_date`, `classify_rollover` in (NONE, FORWARD); getattr-safe. `attention._activation_item` (`:368-382`) calls it, behaviour unchanged. | O04, O05 | ~15 lines + ~10 changed |
| K3 | overlap state | `core/fy_overlap.py`: `overlap_state(config, db, today: dt.date) -> dict` - keys `enabled, active, configured_fy, next_fy, activates_on (ISO date \| None), activated_at, ended_at, activation_pending (bool), note` (Q9/Q14 texts; `_when` for times). Never reads files or the network. | U03, U04, O01 | ~35 lines |
| K4 | row FY label | `core/attention.py`: `row_fy_label(db, config, row: dict \| None) -> str \| None` (Q5; reuses `_before_switch`, `fy_overlap.row_fy`) | U11, U12 | ~15 lines |
| K5 | matcher extracted | `ui.py`: `_run_counts_as_active(run, pipeline: str, now: datetime) -> bool` = today's loop body `:238-269`; `_prefect_has_active_run` becomes the query + `any(...)`, identical results | R07 | move ~30 lines |
| K6 | active runs | `ui.py`: `async def _active_leg_runs(pipeline: str) -> list[dict] \| None` - same query as `_prefect_has_active_run`, returns `[{"fy": params.get("fy"), "started_at": iso of run.start_time or run.expected_start_time or None}]` for runs where K5 is true; None on a client error | R08 | ~20 lines |
| K7 | card run state | `ui.py`: `_card_run_state(card_fy: str, extra: bool, active_runs: list[dict] \| None, last_row: dict \| None) -> dict` -> `{"running", "run_state", "leg_busy", "run_note"}` (Q1, pure) | R02-R06 | ~30 lines |
| K8 | strip per FY | `ui.py`: `_nights(db, mode: str, cfg, card_fy: str \| None = None) -> list[dict]` (Q4; `card_fy=None` = today's code path) | U08-U10 | ~8 lines |
| K9 | timing per FY | `ui.py`: `_leg_timing(db, cfg, leg: str, fy: str \| None = None, scheduled: bool = True) -> dict` (manual state; else `leg_timing(db, cfg, leg, fy=fy)`) | U06, U07, U13 | ~6 lines |
| K10 | lookups per FY | `ui.py`: `_get_last_success(db, mode, fy: str \| None = None)`, `_integrity_summary(db, mode, fy: str \| None = None)`, `_last_run_summary(db, mode, fy: str \| None = None)` (adds `total_files`, `run_id`; `fy=None` = today's query); new `_last_success_row(db, mode, fy=None) -> dict \| None` | U05, U06 | ~15 lines |
| K11 | card builder | `ui.py`: `_leg_card(db, cfg, leg: str, *, card_fy: str, db_fy: str \| None, run_fields: dict, scheduled: bool, scheduled_setting: bool, labels: bool) -> dict` - builds one card (I1); labels via K4 only when `labels` | U01, U05, U06, U11, U13-U15 | ~35 lines |
| K12 | legs plan | `ui.py`: `_legs_plan(cfg, sched: dict \| None, overlap_active: bool, fys: tuple[str, str] \| None) -> dict` (Q7 texts) | U16 | ~25 lines |
| K13 | overlap view | `ui.py`: `_overlap_view(cfg, db) -> dict` - `overlap_state(cfg, db, today IST)`; error -> `{"enabled": .., "active": None, "error": type name}`; minimal config -> `{"enabled": False, "active": False, ...}` | U02, U18, U19 | ~12 lines |
| K14 | `/status` wiring | `status(request)` (signature unchanged): baseline cards through K11 (`db_fy=None`); overlap active -> K6 per enabled leg + K7 per card, `*_next` cards; `overlap`, `legs`, `recent_runs[].fy` | U01-U19, R01-R06 | ~40 lines changed |
| K15 | fy parameter | `ui.py`: `_requested_fy(request: Request, cfg) -> tuple[str \| None, JSONResponse \| None]` (Q2: None/None without `fy`; 400 / 409 / 503 responses) | T02-T08, T12 | ~20 lines |
| K16 | backup triggers | `trigger_cloud`, `trigger_lan` (signatures unchanged): K15 after the disabled check; `parameters={"fy": fy}` only when given | T01-T09 | ~10 lines |
| K17 | verify trigger | `trigger_verify` + `_backup_first_question(cfg, mode: str, fy: str \| None = None)` (Q10) | T10-T13 | ~15 lines |
| K18 | approval parameters | `ui.py`: `_approval_params(req: dict) -> dict` (Q3; raises `HTTPException` 409 / 503); `approve_blocked_run` calls it before `approve_request`, `run_approved_backup` before start; `_start_approved_run(req, request)` uses it | P01-P06 | ~20 lines |
| K19 | rollover API | `rollover_status`, `run_rollover_now`: `overlap` block; activation pending allows the run (Q9) | O01-O04 | ~12 lines |
| K20 | template | `templates/dashboard.html`: Jinja macro `leg_card(key, leg, title, help, enabled, schedule)` for the 4 cards (ids `card-<key>`, `badge-<key>`, `desc-<key>`, `last-<key>`, `sched-<key>`, `safety-<key>`, `strip-<key>`, `<key>-last-success`, `integrity-<key>`, `btn-<key>`, `btn-verify-<key>`, `title-fy-<key>`); next row `<div class="dests dests-next" id="dests-next" hidden>`; `<p id="legs-summary">`; baseline ids and texts kept | J03, J06 | ~60 lines |
| K21 | dashboard JS | `static/js/dashboard.js`: `renderLegCard(key, d)` replaces `:246-307`; `_CARD_KEYS`; `triggerBackup(event, key)`, `triggerVerify(key)` with `data-fy`; Q12/Q13 texts; history label; approval FY; hero names; `updateVerifyUi` uses `leg_busy` | J01, J02, J04 | ~120 lines |
| K22 | settings JS | `static/js/settings.js` `loadRollover`: overlap note + `renderFinalCopy(fc)` (+ `templates/settings.html` `<ul id="ro-final-copy">`, `<p id="ro-overlap">`) | J05 | ~30 lines |
| K23 | E2E | `tests/test_e8_dashboard_e2e.py`: E01-E03 (test agent, reuses the `server` fixture; patches added through `monkeypatch` before the requests) | E01-E03 | ~70 lines |

Order: K1 -> K2 -> K3 -> K4 (core, each with its P rows) -> K5 -> K6 -> K7 -> K8 -> K9 -> K10 -> K11 -> K12 -> K13 ->
K14 -> K15 -> K16 -> K17 -> K18 -> K19 -> K20 -> K21 -> K22 -> K23. **U01's snapshot file is captured before K5**
(test writer, from the code as it is after B7). Before each: GitNexus `impact`; expect HIGH on `status`,
`_prefect_has_active_run` (all triggers, `_ensure_leg_idle`), `_start_approved_run`, `_activation_item`
(`compute_attention` -> `/health`, watchdog). After K14 and after K21 run all G01 files; after K23 the full unit suite.

## 5. Questions

None for the owner. Q1-Q14 above were open in the plan text (section 6 names the running/queued rule, `?fy=` and the
four cards but not the error codes, the strip, labels after the switch, the manual timing state or the legs summary)
and are decided by Opus. One consequence to report: Q6 moves C2's "card schedule line" into B9 (C2's task text in the
plan keeps it ticked by B9).
