# Test matrix - B7 Attention / overdue / watchdog (written 2026-11-24, before the build)

Reference for the B7 test writer and the B7 code writer (CLAUDE.md "Build process"). Binding plan:
`docs/PLAN_BASELINE_OVERLAP_2026-11-23.md` sections 2.5, 3 (last paragraph), 5, 6 (state table), 9 (B7, C2), 10.
Line numbers are from worktree HEAD `505afce` (not the plan's base commit). The open questions of section 5 are
decided below (the owner delegated them); the rows marked `[Qn]` follow those decisions.

## DECISIONS (Opus, 2026-11-24, binding for B7)

Checked against the code (`core/fy_overlap.py:237-324`, `flow.py:2513-2552`, `core/attention.py`, `watchdog.py`) and
the owner's expectations E1 (31 Dec: the new FY folder is made with NO blocker, whatever the backups do), E2 (both
FYs until the switch; switch only after the old FY's final verified copy), E3 (config says which legs are scheduled /
manual / off, behaviour follows it), E4 (fail closed, never guess).

E1 check of the code: `activate_if_due` runs first in `rollover_check_flow`, outside the backup slot, before the
rollover alarm and journal; it reads no run row, needs no leg, no NAS, no WoL, and never raises. Backups (pending,
running, failed, missed, NAS off, a leg disabled or manual) can not delay it. The only wait is an unproven server
clock (owner answer A5, kept: a wrong clock must never create a wrong year folder - E4). B7 must keep it that way:
no attention item may present the activation as blocked by, or waiting for, a backup or an approval.

- **Q1** ACCEPT. K11 narrows the B5 test to "no `stale:` code with FY27-28 before the deadline" and adds "`first_run:cloud:FY27-28` present"; a `last_run:cloud:FY27-28` RED on that cloud-failing site is correct and allowed.
- **Q2** CHANGED (E1). Code `rollover:activation:<nxt>` (= the mail key). No item before the daily check has tried: on the activation day the item is AMBER only once a `rollover:activation:<nxt>` notification row exists dated on/after the activation day 00:00 IST (the check ran and waits); from the day after the activation date (IST) it is RED with or without a mail (server off / check never ran = fact, fail closed). The text is a fact, never a blocker: "FY27-28 is not added to the backups yet (<mail reason>, or 'the daily check has not run since <date>'). Backups of FY26-27 go on as usual; nothing waits for a backup or an approval. The check retries every morning; 'Run FY rollover now' tries at once." Not shown when `rollover:unprotected:<nxt>` is already in the list (same fact, see Q7). Rows I10, K6, K8, T30-T33, T36, T51.
- **Q3** ACCEPT both: FY phrase "cloud backup of FY27-28"; `overlap_off:` action "Switch 'back up the next financial year' on again in Settings", fact "Files saved in FY27-28 are no longer backed up"; the watchdog does not mail `overlap_off:` (plan 2.5: only `stale:` is mailed; switching it off is a typed risky change).
- **Q4** ACCEPT. While the overlap is active the configured-FY items name their FY in the text ("cloud backup of FY26-27"); codes unchanged; outside the overlap text unchanged. Row T53.
- **Q5** ACCEPT. Next-FY base with no success = `_later(activated_at, last_config_change_at("<leg>.enabled"), sched["<leg>_since"])`.
- **Q6** ACCEPT. `clock` / `nas_shutdown` read from the latest row of BOTH passes of the leg, emitted at most once each; the text comes from the newer row that shows the problem.
- **Q7** ACCEPT. When `rollover:unprotected:<nxt>` is present, neither `overlap_off:<nxt>` nor `rollover:activation:<nxt>` is added.
- **Q8** ACCEPT. While the overlap is active both stale titles name their FY ("LAN backup (FY26-27) overdue", "LAN backup (FY27-28) overdue"); outside it "LAN backup overdue". Keys, groups and cooldowns unchanged (text only).
- **Q9** CHANGED to RED for the second part (E4). A record without a readable `activated_at` -> RED `stale:<leg>:<fy>` "could not be placed on the schedule" (as R). A DB error reading the record -> configured items as usual + **RED** `overlap:unknown` "the next financial year's backup state could not be read - it may not be backed up" (unknown protection is never amber).
- **Q10** ACCEPT with one addition (owner rule "DB and reports show the actual truth"): after the switch an item built from an unsuffixed row that started before `overlap:<configured>.ended_at` names that row's real FY (`prev_fy(configured)`, e.g. "last integrity check (of FY26-27) failed"), never the new configured FY. The one-day run window and the old audit until the next weekly audit are accepted; noted in ARCHITECTURE at D1. Row T50.
- **Q11** ACCEPT. `leg_timing(..., fy=)` built in B7 (K9); `ui._leg_timing` wired in B9.
- **Q12** ACCEPT. An open approval whose `fy` is the active next FY -> `approval:<leg>:<nxt>`, text names the FY; configured-FY or FY-less requests keep `approval:<leg>`. Row T52.

**E3 (config shows scheduled / manual / off legs):** behaviour is covered - A6 (keys `cloud.scheduled` / `lan.scheduled`,
validator "at least one enabled leg scheduled", help labels, risky keys), C1 (registration without a cron), C2 (no
overdue / first-run / watchdog mail for a manual leg, reports "run by hand", card schedule line), B9 (`/status`
`enabled` + `scheduled` per card, "Runs only when you press Run backup", "Disabled" card), B4/B6 (April gate and weekly
audit follow it); B7 only keeps the disabled-leg rule (T22) and leaves manual legs to C2 (C01-C04). One gap, **added
task C3 (small, docs + one test)**: `config.example.yaml` has no comment on `cloud.enabled` / `lan.enabled` (only
`deploy/config.install.yaml` explains them), so the example does not say the three choices in one place. C3: in
`config.example.yaml` above each leg write "enabled: false = this backup is off (never runs); enabled: true +
scheduled: true = runs on its schedule; enabled: true + scheduled: false = manual only (Run backup on the
dashboard)", and a test that both shipped configs (`config.example.yaml`, `deploy/config.install.yaml`) spell out
`enabled` and `scheduled` for both legs explicitly (no reliance on defaults).

---

## 1. Current behaviour (found in the code)

| What | Where | Fact |
|---|---|---|
| Leg clock | `core/attention.py:93` `_leg_clock(db, config, leg, sched)` | `db.last_successful_run(leg)` (so `fy=None` = unsuffixed rows only, A4 `_fy_clause` `core/manifest.py:197`). No success -> base = `_later(db.first_known_at(), db.last_config_change_at(f"{leg}.enabled"))`; then `_later(base, sched[f"{leg}_since"])`. Base None -> `(last, None, None)`. No `fy` parameter. |
| Card timing | `core/attention.py:115` `leg_timing(db, config, leg, now=None)` | states `on_time / late / overdue / waiting / catching_up`; no `fy`. Called by `ui._leg_timing` `ui.py:607` (B9 adds `fy` there). |
| Catching up | `core/attention.py:65` `_progressing_partial(db, config, leg, last, now)` | `db.last_run(leg)`, `db.first_partial_after(leg, since)` - both `fy=None`. |
| Overdue item | `core/attention.py:142` `_overdue_item(db, config, leg, sched, now)` | codes `stale:<leg>` (RED, has `due_at` = `overdue.latest_owed`), `first_run:<leg>` (AMBER, no success, not late), `catching_up:<leg>` (AMBER). Exception -> RED `stale:<leg>` "could not be placed on the schedule". |
| Banner | `core/attention.py:189` `compute_attention(db, config, now=None)` | per enabled leg (`cloud`, `lan`; disabled leg skipped at `:196`): overdue item (`:199-207`, RED `stale:<leg>` when no schedule), `last_run:<leg>` (`:209-218`, RED, AMBER for MISSED), `run_logs:<leg>` (`:225`), `clock` (`:229`, code without leg), `nas_shutdown` (`:235`, code without leg), `audit:<leg>` (`:242-259`, `db.latest_audit(leg)` = `fy=None`). Then `approval:<leg>` per open approval (`:261-265`, no FY in the code), watchdog, mail items, `rollover:unprotected:<to_fy>` (`:321-331`), alert switches, undelivered mail. Sorted RED first, stable (`:356`). **No item for any next FY; no overlap_off item; no activation-waiting item.** |
| Unprotected next FY | `core/fy_rollover.py:262-292` `unprotected_new_fy` | already silent while `overlap_enabled` and `overlap:<to_fy>` has no `ended_at` (B4, `:281-284`). Only for `classify_rollover == FORWARD` (after 1 April) or a pending journal; Jan-Mar it is None anyway. |
| Overlap record | `core/manifest.py:1323` `overlap_record(fy)` | dict `{activated_at, from_fy, source_folder, clock, [ended_at], [final_copy_state]}` or None. Helper `core/fy_overlap.py:333` `overlap_open(config, db, to_fy)`. |
| Activation waiting mail | `core/fy_overlap.py:314` `_not_added` | key `rollover:activation:<nxt>`, WARNING on the activation day, ACTION_REQUIRED after, AAM-ROL-07, cooldown `alerts.stale_repeat_hours`. Nothing is stored except the notification row. |
| Watchdog | `watchdog.py:382-421` `_check_stale_legs(cfg=None)` | filters `compute_attention` codes `stale:`; `rest = code.split(":",1)[1]`, `leg, _, fy = rest.partition(":")`; skip when `item["due_at"]` and `db.told_about_leg_since(rest, due_at)`; `AlertEvent(key=f"stale:{rest}", mode=leg, fy=fy or None, title=f"{leg.upper()} backup overdue", cooldown_hours=alerts.stale_repeat_hours)`. Parsing done in A5 (`tests/test_fy_overlap_alerts.py:133`), but **the title has no FY** (`watchdog.py:410`). |
| Notify grouping | `core/notify.py:590` `incident_group` | group `lan:FY27-28` for `fy`; cooldown per (group, key) in `_incident` `:630-672`; `told_about_leg_since(mode, since)` `core/manifest.py:1413` queries `notifications.mode` = that group. |
| D-8 shutdown | `flow.py:3425-3429` | during the overlap only the LAST LAN pass (the next FY) may shut the NAS down, so `nas_shutdown` metrics sit on the `-FY27-28` row; the configured row (`fy=None`, the one `compute_attention` reads) has none -> **today the `nas_shutdown` AMBER item is lost during the overlap** (finding F-B7-1). |
| Test configs | `test_support/attention_cfg.py`, many tests | `SimpleNamespace` configs without `rollover` / `paths`, and `MagicMock` dbs. New code must read with `getattr(..., None)` and `isinstance(record, dict)`, else ~20 existing test files break. |
| B5 xfails | `tests/test_fy_overlap_calendar.py:372` | `test_next_fy_overdue_is_counted_from_activation` (strict xfail, **flips at B7**: remove the marker in the B7 commit). `:339` `test_unscheduled_lan_leg_is_never_overdue_in_the_whole_window` stays xfail (C2). **Conflict:** `test_next_fy_gets_no_overdue_item_before_its_deadline` `:363` asserts no code containing `FY27-28` before the deadline; B7 adds `first_run:cloud:FY27-28` (AMBER) and, on that cloud-failing site, `last_run:cloud:FY27-28` (RED) -> it goes red at B7. See Q1. |

## 2. B7 behaviour to build

Inputs of `compute_attention(db, config, now)`: config (`rollover.overlap_enabled`, `rollover.overlap_activate_on`,
`rollover.source_placeholder_name`, `paths.source_drive`, `paths.lan_destination`, `<leg>.enabled`), db
(`overlap_record`, the `fy=` lookups of A4, `last_notification`), `now` (IST). Every overlap read is guarded:
missing `rollover` / `paths` attribute, `configured_fy` None, or a record that is not a dict -> baseline output.

**Active next FY** `nxt` = `next_fy(configured)` when `overlap_enabled` and `overlap_record(nxt)` is a dict without
`ended_at` (same rule as `active_passes` / `overlap_open`).

| # | Item | Code | Level | When | Text source |
|---|---|---|---|---|---|
| I1 | configured FY items | unchanged (`stale:cloud`, `first_run:lan`, `last_run:cloud`, `audit:lan`, ...) | unchanged | as today, `fy=None` queries | unchanged text outside the overlap; while active the text names the configured FY (Q4); a row older than the switch names its own FY (Q10) |
| I2 | next FY overdue | `stale:<leg>:<nxt>` | RED | per enabled leg while active; `_leg_clock(..., fy=nxt)`: last success = `last_successful_run(leg, fy=nxt)`; none -> base = `activated_at` (then `_later` with `<leg>_since` [Q5]) ; `due_at` = `latest_owed` as today | `act(...)` as `_overdue_item`, phrase "cloud backup of FY27-28" |
| I3 | next FY waiting | `first_run:<leg>:<nxt>` | AMBER | active, no success of `nxt`, now <= deadline | as `first_run:` with FY phrase |
| I4 | next FY catching up | `catching_up:<leg>:<nxt>` | AMBER | `_progressing_partial(..., fy=nxt)` | as today with FY phrase |
| I5 | next FY last run failed | `last_run:<leg>:<nxt>` | RED (AMBER for MISSED) | `last_run(leg, fy=nxt)` in CATEGORY_FAILED | as today with FY phrase |
| I6 | next FY run logs | `run_logs:<leg>:<nxt>` | AMBER | `artefacts_degraded` on that row | as today |
| I7 | next FY audit | `audit:<leg>:<nxt>` | RED / AMBER as today | `latest_audit(leg, fy=nxt)` | as today with FY phrase |
| I8 | clock / nas_shutdown | `clock`, `nas_shutdown` (no FY) | AMBER | read from the latest row of BOTH passes of the leg (configured and next); emitted at most once each (F-B7-1); text from the newer row showing the problem [Q6] | as today |
| I9 | overlap switched off | `overlap_off:<nxt>` | RED | `overlap_enabled` false AND `overlap_record(nxt)` dict without `ended_at` AND `fy_rollover.new_fy_has_files(source, nxt, placeholder)` (placeholder alone = no item); not emitted when `rollover:unprotected:<nxt>` is already in the list [Q7] | plan section 3: "files in FY27-28 are no longer backed up" [Q3] |
| I10 | activation waiting | `rollover:activation:<nxt>` [Q2] | AMBER on the activation day only when a `rollover:activation:<nxt>` notification row dated >= activation day 00:00 IST exists (no item before the check tried); RED from the day after the activation date, with or without a mail | `overlap_enabled`, `configured` known, no record for `nxt`, `today(IST) >= activation_date(configured, overlap_activate_on)`, `classify_rollover(configured, get_fy_prefix(today))` in (NONE, FORWARD); not emitted when `rollover:unprotected:<nxt>` is in the list. No clock check here (dashboard must not touch the network). Gone once the record exists. Never a blocker (E1): no approval, no backup gate. | fact: "FY27-28 is not added to the backups yet"; reason = latest `rollover:activation:<nxt>` notification message, else "the daily check has not run since <date>"; always "Backups of FY26-27 go on as usual; nothing waits for a backup or an approval. The check retries every morning; 'Run FY rollover now' tries at once." [Q2] |
| I11 | watchdog | mail key `stale:<leg>:<nxt>` | ACTION_REQUIRED | existing loop; title `"<LEG> backup (<FY>) overdue"` for a code with FY; while the overlap is active the configured code's title names the configured FY too; outside the overlap today's title [Q8] | `watchdog.py:408-415` |

Order: per leg the configured FY's items then the next FY's (cloud, cloud:FY27-28, lan, lan:FY27-28), then the
global items; the final RED-first sort is unchanged. Disabled leg (`enabled: false`): no item of any FY.
Unscheduled leg (C2, not B7): B7 changes nothing for it (it still gets `first_run:` / `stale:` for both FYs until C2).

## 3. Test matrix (all in `tests/test_fy_overlap_attention.py` unless stated)

Fixtures: real `ManifestDB` on `tmp_path` (`ManifestDB.from_config` or the A4 db fixture), `AppConfig` with the owner
layout `E:\FY26-27` / `\\nas\backup\FY26-27` for pure attention rows (no files touched), a temp sibling layout
(`tmp/src/FY26-27`, `tmp/src/FY27-28`) for I9 rows that look at files; default schedule (cloud 18:00 + 6 h, LAN
01:00 + 4 h, grace 2 h); `pendulum.travel_to(..., freeze=True)` or `now=`; overlap record written with
`db.set_overlap_record("FY27-28", {"activated_at": "2026-12-31T06:00:00+05:30", "from_fy": "FY26-27", ...})`;
next-FY rows inserted with run_id `x-cloud-FY27-28`. Notify faked (records events); no real `check_clock`.
Layer: U = `compute_attention` / `_leg_clock` unit, W = `watchdog._check_stale_legs`, T = `leg_timing`.

| ID | Scenario | Inputs | Expected | Layer / test name | Task |
|---|---|---|---|---|---|
| T01 | Overlap on, no record (before 31 Dec) | 30 Dec 12:00, configured rows only | output identical to a baseline call (same list) ; no code contains `FY` | U `test_no_record_gives_baseline_items` | B7 |
| T02 | Overlap off, no record | `overlap_enabled: false` | identical to T01; no `overlap_off:` | U `test_overlap_off_without_record_is_baseline` | B7 |
| T03 | Config without `rollover` / `paths` | `SimpleNamespace` config from `test_support/attention_cfg.py`, `MagicMock` db | no exception, baseline items | U `test_config_without_overlap_keys_is_baseline` | B7 |
| T04 | config.yaml without `overlap_enabled` | loaded YAML lacking the key, record present | treated as enabled: next-FY items present | U `test_missing_key_counts_as_enabled` | B7 |
| T05 | Next FY waiting right after activation | record 31 Dec 06:00, no FY27-28 rows, now 31 Dec 12:00 | `first_run:cloud:FY27-28` and `first_run:lan:FY27-28` AMBER; no `stale:*:FY27-28` | U `test_next_fy_waiting_from_activation` | B7 |
| T06 | Next FY deadline from activation (cloud) | as T05; deadline = `overdue.deadline(cloud_cron, tz, activated_at, 6, 2)` | at deadline - 60 s: no `stale:cloud:FY27-28`; at deadline + 60 s: RED `stale:cloud:FY27-28` with `due_at` | U `test_next_fy_overdue_counted_from_activation` (and B5 xfail flips) | B7 |
| T07 | Base is activation, not first_known_at | `first_known_at` = a year ago, record 31 Dec 06:00, now 31 Dec 12:00 | no `stale:cloud:FY27-28` (would be RED if the base were first_known_at) | U `test_next_fy_base_is_not_system_birth` | B7 |
| T08 | Schedule changed after activation | `schedule_in_effect.cloud_since` = 15 Jan, no FY27-28 success | deadline counted from 15 Jan (`_later`) | U `test_schedule_since_after_activation_wins` [Q5] | B7 |
| T09 | Leg enabled after activation | `last_config_change_at("lan.enabled")` = 10 Jan | `[Q5]` R: counted from 10 Jan | U `test_leg_enabled_after_activation` [Q5] | B7 |
| T10 | Record without / with bad `activated_at` | record `{}` or `activated_at: "garbage"` | fail closed: RED `stale:<leg>:FY27-28` "could not be placed" (never `first_run:` forever) | U `test_record_without_activation_time_fails_closed` [Q9] | B7 |
| T11 | Next FY has a success | `x-cloud-FY27-28` CLOUD_COMPLETE 1 Jan 18:00, now 2 Jan 12:00 | no `first_run:`/`stale:` for cloud FY27-28 | U `test_next_fy_success_clears_waiting` | B7 |
| T12 | Configured success never counts for next FY | only unsuffixed CLOUD_COMPLETE rows (daily), FY27-28 never ran, now past its deadline | `stale:cloud:FY27-28` RED; `stale:cloud` absent | U `test_configured_success_does_not_cover_next_fy` | B7 |
| T13 | Next FY success never counts for configured FY | only `-FY27-28` successes, configured last success 3 days old | `stale:cloud` RED; no `stale:cloud:FY27-28` | U `test_next_fy_success_does_not_cover_configured` | B7 |
| T14 | Legacy rows (no suffix, no `extended_metrics`, or `fy` absent) | baseline-format rows only + record | they are the configured FY's history; FY27-28 is `first_run:` | U `test_legacy_rows_belong_to_configured_fy` | B7 |
| T15 | Both FYs failing | both passes CLOUD_FAILED each night, past both deadlines | `stale:cloud`, `stale:cloud:FY27-28`, `last_run:cloud`, `last_run:cloud:FY27-28` all RED, order configured first | U `test_both_fys_failing_give_two_sets` | B7 |
| T16 | One FY ok, one stale | configured OK nightly, LAN FY27-28 never succeeded past deadline | only `stale:lan:FY27-28` (+ its `last_run:` if failed) | U `test_one_fy_ok_other_stale` | B7 |
| T17 | MISSED next-FY pass | last FY27-28 row `CLOUD_MISSED` | `last_run:cloud:FY27-28` AMBER | U `test_next_fy_missed_is_amber` | B7 |
| T18 | Catching up per FY | FY27-28 last row PARTIAL, budget stopped, bytes > 0, within `catching_up_max_days` | `catching_up:cloud:FY27-28` AMBER, not `stale:`; configured FY unaffected | U `test_next_fy_catching_up` | B7 |
| T19 | Audit per FY | `latest_audit(cloud, fy="FY27-28")` VERIFICATION_FAILED, configured audit OK | `audit:cloud:FY27-28` RED only | U `test_next_fy_audit_item` | B7 |
| T20 | nas_shutdown on the next-FY row | configured LAN row no `nas_shutdown`, FY27-28 row `nas_shutdown.outcome = UNVERIFIED` | one AMBER `nas_shutdown` item | U `test_nas_shutdown_read_from_last_pass` [Q6] | B7 |
| T21 | clock not duplicated | both rows `clock.state = UNVERIFIED` | exactly one `clock` item | U `test_clock_item_once` [Q6] | B7 |
| T22 | Disabled leg | `lan.enabled: false`, record active, FY27-28 LAN never ran | no code starting with `stale:lan`, `first_run:lan`, `last_run:lan` for either FY | U `test_disabled_leg_has_no_items_for_any_fy` | B7 |
| T23 | Recovery | T16 state, then FY27-28 `LAN_COMPLETE` inserted | `stale:lan:FY27-28` gone; watchdog returns `[]` for it and sends nothing more | U+W `test_next_fy_recovery_clears_stale` | B7 |
| T24 | After the April switch | config now `FY27-28`, `overlap:FY27-28.ended_at` set, no `overlap:FY28-29` | no code ends in `:FY27-28` or `:FY28-29`; no `overlap_off:`; no activation item (next activation 31 Dec 2027) | U `test_items_disappear_after_switch` [Q10] | B7 |
| T25 | Switch pending (1-3 Apr, blocked) | config FY26-27, record open, now 2 Apr | next-FY items still evaluated; `rollover:unprotected:` absent (B4) | U `test_switch_pending_keeps_next_fy_items` | B7 |
| T26 | overlap_off: RED item | record open, `overlap_enabled: false`, file `src/FY27-28/a.xlsx` | RED `overlap_off:FY27-28`; no next-FY leg items | U `test_overlap_off_red_item` | B7 |
| T27 | overlap_off: only placeholder / empty | as T26, folder holds only the placeholder | no `overlap_off:` | U `test_overlap_off_placeholder_only_no_item` | B7 |
| T28 | overlap_off: record ended | `ended_at` set, overlap off, files present | no `overlap_off:` | U `test_overlap_off_after_end_no_item` | B7 |
| T29 | overlap_off and unprotected together | 2 Apr, overlap off, record open, files present (unprotected fires, FORWARD) | exactly one RED item about FY27-28 | U `test_overlap_off_not_doubled_with_unprotected` [Q7] | B7 |
| T30 | Activation waiting, day boundaries | no record, overlap on, `12-31`: now 30 Dec 23:59 IST / 31 Dec 00:00 IST (no mail) / 31 Dec 06:05 with a `rollover:activation:FY27-28` WARNING row of 31 Dec 06:00 / 31 Dec 23:59 no mail / 1 Jan 00:00 no mail | none / none / AMBER / none / RED `rollover:activation:FY27-28` | U `test_activation_item_day_boundaries` [Q2] | B7 |
| T31 | Activation waiting, UTC spelling | `now` = `2026-12-31T18:29:00Z` / `2026-12-31T18:30:00Z` (= 31 Dec 23:59 / 1 Jan 00:00 IST), no mail | none / RED (IST day decides) | U `test_activation_item_uses_ist_day` | B7 |
| T32 | Server off 31 Dec | no record, no mail, now 1 Jan 05:00 (before the catch-up check) | RED item, text "the daily check has not run since ..."; after the record is written (activation 1 Jan 06:00) gone and `first_run:*:FY27-28` from 1 Jan 06:00 | U `test_server_off_on_activation_day` [Q2] | B7 |
| T33 | Clock unproven (check mailed `rollover:activation:FY27-28` WARNING) | no record, notification row present | item present, its text carries the mail's reason AND "Backups of FY26-27 go on as usual; nothing waits for a backup or an approval"; no `approval:` item and no `blocked:` code is created by it | U `test_activation_item_names_the_reason_and_is_no_blocker` [Q2] | B7 |
| T34 | Clock behind / JUMP | system date says FY25-26 (behind) or FY28-29 (jump) vs configured FY26-27 | no activation item (rollover check alerts) | U `test_no_activation_item_on_clock_behind_or_jump` | B7 |
| T35 | Activation disabled | overlap off, no record, 1 Jan | no activation item | U `test_no_activation_item_when_off` | B7 |
| T36 | Non-default date | `overlap_activate_on: 01-15`, now 14 Jan / 15 Jan 07:00 with a mail row of 15 Jan 06:00 / 15 Jan 07:00 without mail / 16 Jan 00:00 | none / AMBER / none / RED | U `test_activation_item_custom_date` | B7 |
| T37 | Feb 29 | configured FY27-28, record `overlap:FY28-29` activated 31 Dec 2027, LAN cron, FY28-29 last success 29 Feb 2028 01:00 (LAN 01:00 + 4 h + 2 h -> deadline 1 Mar 07:00; assert against `overdue.deadline`), now 29 Feb 2028 08:00 and 1 Mar 2028 07:01 | 29 Feb 08:00: no `stale:lan:FY28-29`; 1 Mar 07:01: RED | U `test_overdue_across_feb_29` | B7 |
| T38 | DB error reading the record | `overlap_record` raises | no exception from `compute_attention`; configured items present; one RED item `overlap:unknown` ("could not be read - it may not be backed up") [Q9] | U `test_record_read_error_is_reported` [Q9] | B7 |
| T39 | Watchdog mails next FY once | T16 state, `_check_stale_legs(cfg)` twice within `stale_repeat_hours` | one SENT `stale:lan:FY27-28` (mode `lan:FY27-28`), second SUPPRESSED; returns `["lan:FY27-28"]` both times | W `test_watchdog_mails_next_fy_stale_once` | B7 |
| T40 | Watchdog repeats after cooldown | T39 then travel `stale_repeat_hours` + 1 min | second SENT | W `test_watchdog_repeats_after_cooldown` | B7 |
| T41 | Watchdog dedup per FY | a FAILURE mail of group `lan` (configured) after `due_at` | `stale:lan:FY27-28` still mailed; a FAILURE of group `lan:FY27-28` after `due_at` -> skipped | W `test_watchdog_told_about_is_per_fy` | B7 |
| T42 | Watchdog both FYs | T15 | two mails, keys `stale:cloud` and `stale:cloud:FY27-28`, separate cooldowns | W `test_watchdog_both_fys_two_mails` | B7 |
| T43 | Watchdog title | T15 state for LAN (both FYs stale), overlap active; then the same configured-FY stale state with no record | titles `LAN backup (FY26-27) overdue` and `LAN backup (FY27-28) overdue`, keys `stale:lan` / `stale:lan:FY27-28` unchanged; without the overlap `LAN backup overdue` | W `test_watchdog_title_names_fy` [Q8] | B7 |
| T44 | Watchdog ignores non-stale | `overlap_off:`, activation, `first_run:*:FY27-28` present | no mail for them | W `test_watchdog_mails_only_stale` | B7 |
| T45 | Card timing per FY | `leg_timing(db, cfg, "cloud", now, fy="FY27-28")` at T05 / T06+ / T11 | `waiting` / `overdue` / `on_time` | T `test_leg_timing_per_fy` [Q11] | B7 |
| T46 | `leg_timing` default | `fy` omitted | same as today (unsuffixed rows) | T `test_leg_timing_default_is_configured_fy` | B7 |
| T47 | B5 calendar | `tests/test_fy_overlap_calendar.py::test_next_fy_overdue_is_counted_from_activation` | passes; strict xfail marker removed in the B7 commit | existing file | B7 |
| T48 | B5 calendar conflict | `test_next_fy_gets_no_overdue_item_before_its_deadline` | see Q1 (R: assert no `stale:` code with FY27-28, and assert `first_run:cloud:FY27-28` present) | existing file [Q1] | B7 |
| T50 | Truthful FY after the switch | config FY27-28, `overlap:FY27-28.ended_at` = 2 Apr 06:10, newest unsuffixed audit row (FY26-27, 28 Mar) VERIFICATION_FAILED, newest unsuffixed cloud row 2 Apr 06:00 CLOUD_FAILED | `audit:cloud` / `last_run:cloud` texts name FY26-27 (never FY27-28); codes unchanged | U `test_rows_before_switch_name_their_own_fy` [Q10] | B7 |
| T51 | Activation item not doubled | 2 Apr (FORWARD), no `overlap:FY27-28` record, overlap on, files in `src/FY27-28` (`rollover:unprotected:FY27-28` present) | exactly one RED item about FY27-28 (`rollover:unprotected:`); no `rollover:activation:FY27-28` | U `test_activation_item_not_doubled_with_unprotected` [Q2, Q7] | B7 |
| T52 | Next-FY approval code | record active, one PENDING approval `lan` fy FY26-27 and one `lan` fy FY27-28 | two items `approval:lan` and `approval:lan:FY27-28`, the second's text names FY27-28; an FY-less request keeps `approval:lan` | U `test_next_fy_approval_has_own_code` [Q12] | B7 |
| T53 | Configured-FY text during the overlap | T15 state; then the same rows with no record | with the record: `stale:cloud` text names FY26-27, codes unchanged; without: text identical to a baseline call | U `test_configured_items_name_fy_only_during_overlap` [Q4] | B7 |
| T54 | Activation is never gated by backups (E1) | 31 Dec 06:00, clock OK (fake), every leg's last row FAILED / RUNNING / BLOCKED, LAN NAS unreachable, `lan.enabled: false` variant, `cloud.scheduled: false` variant | `activate_if_due` returns ACTIVATED in every variant, folder + placeholder + record made | `tests/test_fy_overlap_activation.py` `test_activation_ignores_backup_state` (guard test for the existing B1 code; must pass on first run) | B7 |
| T49 | Regression | `test_d2_attention_latest_run.py`, `test_m5_stale_alert.py`, `test_stale_mail_dedup.py`, `test_overdue_schedule.py`, `test_m4_watchdog.py`, `test_fy6_unprotected_new_fy.py`, `test_calendar_simulation.py`, `test_fy_overlap_alerts.py` | green, unchanged | existing files | B7 |
| C01 | Unscheduled LAN, configured FY | `lan.scheduled: false`, no LAN success | no `first_run:lan` / `stale:lan` / `catching_up:lan` | `tests/test_manual_only_leg.py` (part 3) | **C2** |
| C02 | Unscheduled LAN, next FY | as C01 + record | no `first_run:lan:FY27-28` / `stale:lan:FY27-28`; no watchdog mail | `tests/test_manual_only_leg.py` | **C2** |
| C03 | Unscheduled leg failed manual run | LAN_FAILED manual row (either FY) | `last_run:lan[:FY]` RED still shown | `tests/test_manual_only_leg.py` | **C2** |
| C04 | B5 xfail `test_unscheduled_lan_leg_is_never_overdue_in_the_whole_window` | | stays strict xfail after B7 (must still fail); flips at C2 | existing file | **C2** |

## 4. Chunks (one function per chunk; tests by one Haiku, code by another)

All in `core/attention.py` unless stated. Signatures are fixed (Opus-defined; nobody below Opus changes them).

| Chunk | Function / change | Signature | Rows | Scope |
|---|---|---|---|---|
| K1 | active next FY | `core/fy_overlap.py`: `active_next_fy(config, db) -> tuple[str, dict] \| None` - (`nxt`, record) when `getattr(config, "rollover", None)` has `overlap_enabled` true, `configured_fy(...)` known, record dict without `ended_at`; never raises on missing attributes; DB errors propagate (caller decides) | T01-T04, T24, T38 | ~20 lines |
| K2 | leg clock per FY | `_leg_clock(db, config, leg: str, sched: dict, fy: str \| None = None, activated_at: str \| None = None)` - `fy=None` byte-identical to today; with `fy`: `last_successful_run(leg, fy=fy)`, no success -> base = `activated_at` (`_later` with `last_config_change_at(f"{leg}.enabled")` per Q5) then `_later(base, sched since)`; `fy` given and no usable `activated_at` -> raise `ValueError` (fail closed, T10) | T05-T10, T12-T14, T37 | ~15 lines |
| K3 | catching up per FY | `_progressing_partial(db, config, leg, last, now, fy: str \| None = None)` - passes `fy=` to `last_run` and `first_partial_after` | T18 | ~5 lines |
| K4 | overdue item per FY | `_overdue_item(db, config, leg, sched, now, fy: str \| None = None, activated_at: str \| None = None)` - codes `<kind>:<leg>` or `<kind>:<leg>:<fy>`; phrase "<phrase> of <fy>" only when `fy` | T05, T06, T10, T11, T15-T18 | ~15 lines |
| K5 | per-leg block extracted | `_leg_items(db, config, leg: str, sched: dict \| None, now, fy: str \| None = None, activated_at: str \| None = None) -> list[dict]` - today's loop body `:199-259` moved unchanged, all lookups with `fy=`, codes suffixed when `fy` (`clock` / `nas_shutdown` never suffixed) | T15-T22 | move ~65 lines, ~10 changed |
| K6 | activation item | `_activation_item(db, config, now) -> dict \| None` (I10; AMBER needs the mail row, RED from the next IST day, fixed "no blocker" text) | T30-T36 | ~30 lines |
| K7 | overlap-off item | `_overlap_off_item(db, config) -> dict \| None` (I9; uses `fy_rollover.new_fy_has_files`) | T26-T28 | ~20 lines |
| K8 | wiring | `compute_attention(db, config, now=None)` (signature unchanged): per enabled leg `_leg_items(..., None)` then, when K1 gives a next FY, `_leg_items(..., nxt, record["activated_at"])`; de-duplicate `clock` / `nas_shutdown` by code; append K6, K7 (both skipped when `rollover:unprotected:<nxt>` present); K1 error -> RED `overlap:unknown` per Q9; `approval:<leg>:<nxt>` per Q12; FY in configured texts while active (Q4) and row-FY text after the switch (Q10); then today's global items | T01-T04, T15, T20-T25, T29, T38, T50-T53 | ~25 lines |
| K9 | card timing | `leg_timing(db, config, leg: str, now=None, *, fy: str \| None = None) -> dict` - reads `activated_at` via K1 when `fy` | T45, T46 | ~10 lines |
| K10 | watchdog title | `watchdog.py:408-410`: title `f"{leg.upper()} backup ({fy}) overdue"` when `fy`; configured code names the configured FY while the overlap is active (Q8) | T39-T44 | ~5 lines |
| K11 | B5 file | remove the strict xfail marker of `test_next_fy_overdue_is_counted_from_activation`; adjust `test_next_fy_gets_no_overdue_item_before_its_deadline` per Q1 (Opus edits this, not the code agent) | T47, T48 | ~5 lines |

Order: K1 -> K2 -> K3 -> K4 -> K5 -> K8 (with K6, K7 stubs returning None) -> K6 -> K7 -> K9 -> K10 -> K11. Before each:
GitNexus `impact` (`compute_attention` is read by `ui._attention`, `/health`, `watchdog._check_stale_legs`; expect HIGH -
any new RED item also turns `/health` 503). After K8 run all T49 files.

## 5. Questions (decided - see DECISIONS at the top; the R texts below are the original recommendations)

- **Q1** B5 test `test_next_fy_gets_no_overdue_item_before_its_deadline` asserts no FY27-28 code at all before the
  deadline, but B7 (plan section 6 "first backup pending") adds AMBER `first_run:cloud:FY27-28`, and on that test's
  cloud-failing site also RED `last_run:cloud:FY27-28`. [R: Opus narrows it to "no `stale:` code with FY27-28" and adds
  "`first_run:cloud:FY27-28` is present" in the B7 commit.]
- **Q2** Activation-waiting item: plan section 6 gives "amber on the day, red after" but no code and no source. Date
  based (shows from 31 Dec 00:00 IST, before the 06:00 check ran, and RED on 1 Jan 00:00-06:00 when the server was off)
  or only after the check mailed `rollover:activation:<fy>`? [R: date based, code `rollover:activation:FY27-28` (same
  as the mail key), text names the mail's reason when one exists.]
- **Q3** Wording of the new items (`overlap_off:`, activation, next-FY phrases). [R: next-FY items say "cloud backup of
  FY27-28"; overlap_off: action "Switch 'back up the next financial year' on again in Settings", fact "Files saved in
  FY27-28 are no longer backed up".] Should the watchdog also mail `overlap_off:` (data unprotected, today only RED on
  the dashboard and `/health`)? [R: no, plan says only `stale:` is mailed; the settings change itself is a typed
  risky change.]
- **Q4** While the overlap is active, do configured-FY items name their FY ("cloud backup of FY26-27")? Plan 2.3 does
  this for alert titles, 2.5 says nothing for the banner. [R: yes, text only, codes unchanged; outside the overlap
  text unchanged.]
- **Q5** Next-FY base when no success: plan says `activated_at`. Also take the later of the leg's `enabled` change and
  the schedule `since` (as the configured FY does)? [R: yes, `_later` of all three.]
- **Q6** F-B7-1: during the overlap the NAS shutdown result is on the next-FY row, so the `nas_shutdown` AMBER item is
  lost today. Read both rows and show `clock` / `nas_shutdown` once each? [R: yes.]
- **Q7** With the overlap switched off after 1 April (switch pending), `rollover:unprotected:FY27-28` and
  `overlap_off:FY27-28` say the same thing. [R: show only `rollover:unprotected:`.]
- **Q8** Watchdog title for a next-FY stale mail: "LAN backup (FY27-28) overdue"; configured stays "LAN backup
  overdue" (plan 2.3 puts the FY in BOTH titles during the overlap - apply to the configured one too?). [R: FY in both
  while the overlap is active.]
- **Q9** Fail-closed choices: a record without a readable `activated_at` -> RED `stale:<leg>:<fy>` (R); a DB error
  reading the record -> configured items + AMBER `overlap:unknown` "the next financial year's state could not be
  read" (R) - or RED?
- **Q10** After the switch, configured FY27-28's `fy=None` lookups return the old FY26-27 rows (unsuffixed) and its
  real Jan-Mar history is in `-FY27-28` rows: a failed FY27-28 run on 31 Mar is no longer shown after the switch until
  the next run, and the last audit shown is FY26-27's. [R: accept (baseline has the same one-day window; the failure
  mail was sent), note in ARCHITECTURE at D1.]
- **Q11** `leg_timing(..., fy=)` is needed by B9's cards; build it in B7 (core) and wire `ui._leg_timing` in B9? [R:
  yes.]
- **Q12** Open approvals of the next FY give a second `approval:<leg>` item with the same code (plan 2.5 lists
  stale/first_run/last_run/audit only). Suffix it `approval:<leg>:<fy>` when the request's `fy` is the next FY? [R:
  yes, text names the FY.]
