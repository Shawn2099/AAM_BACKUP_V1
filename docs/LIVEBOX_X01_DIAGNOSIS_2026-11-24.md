# X01 diagnosis (livebox dev43, lane w1_x01) - 2026-11-24

Evidence: `C:\Users\malliath\Desktop\aam_baseline_overlap\agent_runs\livebox_dev43\w1_x01`. Harness: `tools/livebox` in `C:\Users\malliath\Desktop\aam_baseline_overlap`. Read-only diagnosis; nothing was changed.

## Verdict in one paragraph

The product did what the plan says for X01. With the cloud blackholed and the clock proof set to "nointernet" from 31 Dec, the 02:01 cloud run failed (AAM-CLD-10, the pre-check), the 06:00 rollover check did NOT activate FY91-92 and sent AAM-ROL-07 (key `rollover:activation:FY91-92`), nothing was created, no overlap record existed, LAN ran only FY90-91. On 2 Jan the internet came back, the clock proof agreed, the 06:00 check activated FY91-92 (db_meta `overlap:FY91-92` activated_at 2091-01-02T06:00:00), the INFO mail `rollover:activated:FY91-92` went out once, and from then on cloud and LAN run both FYs. The 22 failures are almost all one root cause: the harness calendar (`expected.jsonl`) does not know about the scenario's blackout/unproven clock and expects normal activation on 31 Dec. Two real product observations remain (P1, P2) plus two small notes.

## Why expected.jsonl is wrong (root cause of the H items)

- `expected.jsonl` for 2090-12-31 says `activation_result: ACTIVATED`, `outage: false`, `skipped: []`, expected mail `rollover:activated:FY91-92` at 06:00, expected `cloud FY90-91` status in (CLOUD_COMPLETE, CLOUD_NO_CHANGES_COMPLETE), `audit_*` FY91-92 and `lan FY91-92` rows, cards `cloud, cloud_next, lan, lan_next`, `overlap_active_end: true` from 31 Dec on.
- The calendar has two mechanisms for abnormal days: `Outage` (whole server off) and `DayOverride` (skip events, force statuses, extra mails) in `tools/livebox/calendar.py:150-215`. It has NO notion of "cloud unreachable" or "clock unproven blocks activation": `activation_result = ACT_ACTIVATED` is decided purely by date (`calendar.py:356-359`).
- X01 injects its blackout only through rig knobs in `on_day` (`scenarios/w1.py:420-423`: `set_cloud("blackhole")`, `set_clock_proof("nointernet")` on 31 Dec; `set_cloud("local")`, `set_clock_proof("agree")` on 2 Jan). `to_driver_scenario` (`scenarios/results.py:301-316`) copies only `outages`, `lateness`, `injections`, so even if a scenario had day overrides they would be dropped. `build_days` (`run_lane.py:422-433`) calls `cal.build_calendar(...)` with `outages` only and never `overrides`. The `Scenario` dataclass (`scenarios/base.py:26-41`) has no `overrides` field at all.
- Result: the oracle expects activation at 06:00 on 31 Dec; the product (correctly) waited until 2 Jan.

Suggested fix (one change fixes about 19 of the 22): add a `day_overrides: Mapping[date, DayOverride]` field to `scenarios/base.py:26` Scenario and `run_lane.Scenario` (`run_lane.py:785`), copy it in `results.to_driver_scenario` (`results.py:301`), pass it as `overrides=` in `run_lane.build_days` (`run_lane.py:432`), and extend `calendar.build_calendar` (`calendar.py:249-363`) with a per-day `clock_unproven` (activation not done: `activation_result` = new `ACT_WAITING_CLOCK`, expected mail `activation_waiting` with WARNING on the activation day and ACTION_REQUIRED on later days) and `cloud_down` (cloud row status in CLOUD_FAILED, no `rollover:activated` mail, no next-FY rows, expected problem mail). X01 then sets overrides for 2090-12-31 and 2091-01-01 (cloud_down + clock_unproven) and activation moves to 2091-01-02 06:00 (so on 2 Jan the 02:01 cloud run has no FY91-92 row and the 21:01 LAN run does).

## Check table

Class: H = harness bug, P = product finding, U = unclear. "Fix" lists the file:line to change for H items.

| # | Day | Check id | Class | Evidence | Fix location |
|---|---|---|---|---|---|
| 1 | 12-31 | WATCHDOG (status_2330) "stale ['cloud']" | H | Cloud failed at 02:01 on 31 Dec (injected). By 23:30 the last good cloud copy is the 30 Dec 02:01 run, about 45 h old, so `watchdog._check_stale_legs` correctly reports cloud overdue (ROL-07 mail text itself says "28 hours old" at 06:00). The check is hard-coded "no overdue leg on a healthy lane". | `run_lane.py:1711`: take the expected-stale legs from the day override (cloud_down) and only FAIL on unexpected legs |
| 2 | 12-31 | ROWS cloud FY90-91 CLOUD_FAILED not in allowed | H | Injected blackhole. DB row is CLOUD_FAILED (AAM-CLD-10) as it should be; `expected.jsonl` row allows only COMPLETE / NO_CHANGES. | `calendar.py` override `statuses` (DayOverride.statuses exists, `calendar.py:203`) but not reachable: `run_lane.py:432` and `results.py:301` |
| 3 | 12-31 | ROWS:AUDIT "no integrity_audits row for cloud FY91-92" (FINDING) | H | Follows from no activation. Audit ran FY90-91 only. FINDING comes from the E1 downgrade (`run_lane.py:1773-1780`). | same calendar fix |
| 4 | 12-31 | ROWS:AUDIT "no integrity_audits row for lan FY91-92" (FINDING) | H | Same as 3. | same |
| 5 | 12-31 | ROWS "no run_history row for lan FY91-92" | H | Overlap not active on 31 Dec, so no next-FY LAN pass; correct product behaviour. | same |
| 6 | 12-31 | MAIL "key rollover:activated:FY91-92 sent 0, expected 1" (FINDING) | H | Correct: the INFO "added" mail is only sent on 2 Jan (mail_index: 2091-01-02 06:00, key `rollover:activated:FY91-92`, once). | same |
| 7 | 12-31 | MAIL "2 problem mails, expected 0" (FINDING) | H | The 2 problem mails are the injected failure (CLD-10 at 02:01) and ROL-07 at 06:00; both expected by the X01 plan row. | same |
| 8 | 12-31 | M2 FAILURE mail on a healthy day (CLD-10) | H | `check_mail_rules` treats every problem mail as unexpected unless the day is an outage/injection day; this lane's injection is a knob, not in `injection_dates`. | `run_lane.py:1814-1825`: add days of cloud_down/clock_unproven overrides to `injection_dates` |
| 9 | 12-31 | M2 WARNING mail on a healthy day (ROL-07) | H | Same as 8. | same |
| 10 | 12-31 | META "overlap:FY91-92 missing" | H | Correct: the record is written only on activation (db_meta `overlap:FY91-92` activated_at 2091-01-02T06:00). The oracle assumes it exists from 31 Dec. | `run_lane.py:1849-1854` (driven by `day.next_fy_end`, fixed by the calendar change) |
| 11 | 12-31 | STATUS_0900 cards [cloud, lan] expected 4 | H | The overlap was not active, so the dashboard rightly shows two cards. | `run_lane.py:1875-1889` (follows `day.overlap_active_at_0900`, fixed by the calendar change) |
| 12 | 12-31 | STATUS_0900 overlap.active False expected True | H | Same as 11. | same |
| 13 | 12-31 | STATUS_2330 cards [cloud, lan] expected 4 | H | Same as 11. | same |
| 14 | 12-31 | STATUS_2330 overlap.active False expected True | H | Same as 11. | same |
| 15 | 01-01 | WATCHDOG status_0900 stale cloud | H | Two failed cloud runs in a row, last good 30 Dec 02:01. Expected during the blackout. | `run_lane.py:1711` |
| 16 | 01-01 | WATCHDOG status_2330 stale cloud | H | Same as 15. | same |
| 17 | 01-01 | ROWS cloud FY90-91 CLOUD_FAILED | H | Blackhole still on until 2 Jan (`inputs.jsonl` seq 24: `set_cloud local` only on 2 Jan). | calendar fix |
| 18 | 01-01 | ROWS no run_history cloud FY91-92 | H | Not active yet. | calendar fix |
| 19 | 01-01 | ROWS no run_history lan FY91-92 | H | Not active yet. | calendar fix |
| 20 | 01-01 | MAIL 1 problem mail, expected 0 | H | The second CLD-10 failure mail (sent, because `alerts.repeat_cooldown_hours` is 12 and the notification gap is about 24 h); the plan allows at most 2 over 36 h. See P2 for the missing ROL-07 escalation. | `oracles.py` check_expected_mails via day override |
| 21 | 01-01 | M2 FAILURE mail on a healthy day | H | Same as 8. | `run_lane.py:1814-1825` |
| 22 | 01-01 | META overlap:FY91-92 missing | H | Same as 10. | same |
| 23 | 01-01 | STATUS_0900 cards / overlap.active (2 checks) | H | Same as 11. | same |
| 24 | 01-01 | STATUS_2330 cards / overlap.active (2 checks) | H | Same as 11. | same |
| 25 | 01-02 | SPY_WAKE_PER_PASS FINDING "wake 2x for 2 passes" | U (known) | On 2 Jan 21:01 two LAN passes (FY90-91, FY91-92) each call `wol_check_task` (`flow.py:1121`, called per pass), the 2nd finds the NAS up. Plan 1.6 wanted one wake per LAN flow. Same text is tagged KNOWN on 3 Jan. Not related to the outage; harmless, a spec/oracle versus implementation mismatch. | decide: accept in oracle (`run_lane.py` SPY_WAKE_PER_PASS) or product change |
| 26 | 01-02 | ROWS no run_history cloud FY91-92 | H | Activation happens at 06:00; the 02:01 cloud run on 2 Jan is before it, so no next-FY pass. The calendar assumes the overlap is active for the whole day on the activation day, which is only true for the baseline schedule where 06:00 precedes nothing of that day's cloud. First cloud FY91-92 row is 3 Jan 02:01 (no failure on 3 Jan). | `calendar.py:356-359` plus `build_calendar` row generation for cloud on `activated_today` when activation is deferred |
| 27 | 01-02 | M2 RECOVERED mail on a healthy day | H | `RESOLVED ... (was AAM-CLD-10)` is the expected recovery after a failure; the day is the recovery day of the injection. | `run_lane.py:1814-1825` |
| 28 | 01-03 | SCENARIO:X01 "expected exactly 1 WARNING for rollover:activation:FY91-92, observed 0" | H (with a P note) | `_x01_expect` (`scenarios/w1.py:144-165`) counts WARNING by `severity`, and `results._mails` derives `severity` from the SUBJECT word (`results.py:114-118`, `results.py:~205`). The 31 Dec mail is stored as kind WARNING in `notifications` (id 2, key `rollover:activation:FY91-92`, kind WARNING) but its subject says "ACTION NEEDED", because the fault catalog entry AAM-ROL-07 has class ACTION (`core/faults.py:23`, `:267`) and the subject comes from the class, not from the event kind. So the harness sees 0 WARNING and (by accident) 1 ACTION_REQUIRED, so the "1 ACTION" check passes for the wrong mail. | `scenarios/w1.py:148-151`: judge by `kind` from `notifications` (`key`+`kind`), not by subject word; `results.py:204` should prefer `m["kind"]` once joined |

(Counts: the 22 verdict FAIL rows are items 1, 2, 5, 10-16, 17-19, 20-24, 26, 27 and 28; the FINDING rows 3, 4, 6-9 (E1 downgrade) are the same cause; item 25 is a FINDING on its own.)

## Product observations (P)

### P1 - the escalation mail (WARNING on the day, ACTION NEEDED from the next day) never reaches the office when the daily check runs a few milliseconds early

Evidence from `manifest.db` `notifications`:

- id 2, `rollover:activation:FY91-92`, kind WARNING, SENT at 2090-12-31T06:00:01.200980+05:30.
- id 4, same key, kind ACTION_REQUIRED, SUPPRESSED at 2091-01-01T06:00:01.195007+05:30, reason "same alert already sent at 2090-12-31T06:00:01.200980+05:30 (cooldown 24h)".

The second check started 6 ms less than 24 h after the first (`core/notify.py:668-672`, `_within_hours`), so the cooldown (`alerts.stale_repeat_hours`, default 24, `models/config.py:583`) suppressed the escalation. Plan row X01 and `docs/ARCHITECTURE.md` R9 / `docs/EMAIL_CONTENT_SPEC.md:185` promise "ACTION NEEDED from the next day". In plain words: the office gets one mail on the first day and, on the second morning, the stronger reminder is dropped because the daily job started a hair earlier than 24 hours after the first mail. With a daily cron this will happen on roughly half of the days. The same happens to any repeat per 24 h for this key. Suggested direction (not applied): do not let the cooldown suppress a kind escalation (WARNING to ACTION_REQUIRED), and/or compare with a small tolerance. The X01 test did not catch it because the harness counted the first mail as "the ACTION mail" (see item 28), so X01 must be re-judged by `kind` and then it will expose this as a real red.

### P2 - first mail on activation day carries subject "ACTION NEEDED" although the event is a WARNING

Evidence: `mail_index.csv` row 2 subject `[LIVEBOX Backup] ACTION NEEDED: Financial-year switch - ... (AAM-ROL-07)`; `notifications` kind WARNING. The plan and spec say "WARNING on the activation day". The subject label comes from the fault-catalog class (`core/faults.py:23`, ROL-07 is ACTION). This is probably the design (the catalog label wins), but then the "WARNING first, ACTION after" distinction is invisible to the reader and the spec wording is misleading. Needs an owner decision, not a bug fix by itself.

### Minor notes

- The plan table (`docs/LIVEBOX_TEST_PLAN_2026-11-24.md:330`) expects the failure code AAM-CLD-01; the product mailed AAM-CLD-10 "Could not check the online backup before starting" (the pre-check failing). The scenario does not assert the code, so it did not fail; check whether the plan row or the product mapping is right (U).
- The mail text of the 31 Dec ROL-07 includes "(UNVERIFIED: Google not reachable: OSError: LIVEBOX: no internet (simulated); w32tm ...)", so the reason is shown to the office; fine.

## What passed (confirms product behaviour)

Activation exactly on 2091-01-02 (`activated_on == [2091-01-02]`, db_meta), exactly one INFO `rollover:activated:FY91-92`, LAN green on all six days (rows from 29 Dec to 3 Jan), cloud FAILURE mails 2 over 36 h (limit 2), no argv violations, all safety rules ok, FY91-92 cloud and LAN passes green on 3 Jan, trees equal.

## Recommended next step for the harness owner

1. Add per-day overrides (`cloud_down`, `clock_unproven`) to `Scenario` and the calendar and wire them through `results.to_driver_scenario` and `run_lane.build_days` (fixes items 1-24, 26, 27).
2. Make `_x01_expect` and `results._mails` use `notifications.kind` instead of the subject word (item 28), then re-run X01. Expect one honest red from P1 (escalation suppressed) until the owner decides on P1/P2.
