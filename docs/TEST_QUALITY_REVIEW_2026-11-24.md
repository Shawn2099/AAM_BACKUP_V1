# Test quality review - FY overlap tests (2026-11-24)

Independent audit of: `tests/test_fy_overlap_*.py` (activation, alerts, attention, audit, backup_flow, calendar,
command_lines, config, db, pipelines, report, rollover, ui, unit), `tests/test_manual_only_leg.py`,
`tests/test_alert_class.py`, `audit_lab/test_fy_overlap_lab.py`, `audit_lab/test_c1_prefect_schedules_lab.py`.
Code under test: branch `baseline-v1-overlap` (code identical at b89bb82 and 4282473; the later commit is docs only).
Nothing tracked was edited. Mutants were applied in a throw-away detached worktree (removed afterwards).

## 1. Verdict

**The tests are real. Confidence: high for the business rules, medium for the dashboard JavaScript.**

- 475 unit tests pass (non-slow scope, 59 s), the 9 slow calendar tests pass (370 s), the 13 audit-lab tests pass
  (185 s, real robocopy / rclone loopback rig and a real local Prefect server, 0 skipped).
- 45 deliberate one-line bugs were put into the product code: **41 killed, 4 survived** by the scope unit tests.
  Of the 4, one is killed by the audit lab (M34), two by existing baseline tests (M41, M45), and one by nothing (M27).
  So there is **1 real hole (M27)** and **3 places where overlap-specific unit coverage is missing**.
- The April gate (R11), the activation rules (R9), the pass order and the per-FY alert keys are strongly proven:
  every mutant on those was killed by a test whose name and assertion match the rule. The previous author had already
  mutation-tested the gate (a section in `test_fy_overlap_rollover.py` says "gaps found by mutation").
- No `xfail`, no `skip` marker, no loosened assert was found. The only skips are conditional (`importorskip("time_machine")`
  in 5 files, loopback-share and Prefect-server-health skips in the slow/lab files). All of them ran (0 skipped) here.
- The weak spots are: (a) all dashboard JS tests read source text, nothing executes the JS; (b) the real
  `_auto_scheduled_run` helper is faked in every test; (c) the `keep_fy=` call sites in the two pipelines are
  only proven by the audit lab; (d) a few untested error branches in activation.

## 2. Fake / weak tests (file:line, why, concrete fix)

| # | Where | Problem | Fix |
|---|---|---|---|
| W1 | `tests/test_fy_overlap_audit.py:104` (and `:228-239`) | `flow._auto_scheduled_run` is patched with a MagicMock (`create=True`, comment "absent on the base code" is stale: the helper exists at `flow.py:542`). The test of "a scheduled audit skips an unscheduled leg" therefore never runs the real detection. Mutant M27 (`_auto_scheduled_run` always False) **survives every test** including the lab (the lab calls the flow directly, never as an auto-scheduled Prefect run). If Prefect renamed or changed `auto_scheduled`, the weekly audit would wake a manual-only NAS and nobody would notice. | Add a unit test of the real helper: patch `prefect.context.FlowRunContext.get` to return `SimpleNamespace(flow_run=SimpleNamespace(auto_scheduled=True))` -> True; `False` -> False; `None`/raises -> False. Add one lab test that starts `integrity-audit` as a scheduled run (like C1 p1) with `lan.scheduled: false` and asserts no LAN audit row. Remove `create=True` and the stale comment. |
| W2 | `flow.py:2009-2011` and `flow.py:2334-2336` (call sites) vs `tests/test_fy_overlap_pipelines.py:429-446` (`TestBlockRequestOfAPass`) | `_open_block_request(..., keep_fy=_other_fy(fy_pass))` at the two pipeline call sites is never executed by any unit test (coverage: lines 2004-2009 and 2329-2334 uncovered). `TestBlockRequestOfAPass` calls `_open_block_request` directly with `keep_fy`, so the wiring is unproven. Mutant M34 (`_other_fy` returns None) survives the unit tests; only `audit_lab/test_fy_overlap_lab.py::test_fyov_g3_blocks_per_fy` kills it. A regression would let a blocked FY27-28 pass supersede the owner's pending FY26-27 approval. | Add to `TestCloudNextPass` / `TestLanNextPass` / `TestCloudConfiguredPassWithOverlap`: guards task raises `GuardBlocked`, assert `_open_block_request` is called with `keep_fy="FY26-27"` (next pass) and `keep_fy="FY27-28"` (configured pass), and `keep_fy=None` for `fy_pass=None`. Add a direct unit test of `flow._other_fy` (lines 450-454 are uncovered). |
| W3 | `tests/test_fy_overlap_ui.py:999-1083` (`test_js_one_card_renderer`, `test_js_next_card_hidden_when_leg_disabled`, `test_js_approval_card_escapes_fy`, `test_js_hero_warns_for_next_fy_without_run`, `test_js_buttons_send_fy`, `test_template_next_cards_hidden_by_default`, `test_js_labels_and_states`, `test_settings_js_final_copy`) | These assert on the **text of `dashboard.js`, `settings.js`, `dashboard.html`** (regex / `in`). Not proven: that `renderLegCard` really renders four cards from a `/status` payload, that next cards are hidden/shown at runtime, that the escaped FY is really escaped in the DOM, that the hero shows the warning, that a click really sends `?fy=`, that the rollover card shows `outstanding` / `hint` / `done_at`. A refactor that keeps the strings but breaks the logic passes; a harmless rename fails. `:1073` (`assert "manual" not in body`) is a negative text check that can never fail for a real behaviour change. The server half of the same features (E01-E03 `:1126-1185`, `test_status_*`, `test_trigger_*`) is real. | Run the JS for real: either a small Node/`jsdom` test (feed a canned `/status` JSON, assert the DOM: 4 cards, next cards hidden when `overlap.active` false, buttons' `data-fy`, hero class) or a headless-browser smoke test in the audit lab (the project already has a real uvicorn rig in `server` fixture, `:1100`). At minimum replace `:1073` with a behavioural check of the manual-only text rendering. |
| W4 | `tests/test_fy_overlap_ui.py:1041` (`test_status_reads_overlap_record_once_per_call`) | `assert len(reads) <= 12` is a magic bound (was 51); a regression to 12 reads per row-set still passes; the number is not derived from the rows. | Assert `reads.count("FY27-28") <= 1` (one read per FY per call), derived not hard-coded. |
| W5 | `tests/test_fy_overlap_calendar.py:15-16` | Module docstring says "Known gaps are xfail(strict=True) ..." but there is **no xfail** in the file (grep). Stale, misleading to a reader who thinks gaps are tracked. Also the whole file is `pytest.mark.slow` (`:42`): a run with `-m "not slow"` silently omits the only end-to-end April calendar tests (6 min). | Delete the xfail paragraph. Make sure the release gate runs `-m slow` at least once (put it in the `SESSION_HANDOFF` test checklist). |
| W6 | `tests/test_fy_overlap_command_lines.py:58` and `:76` | `with contextlib.suppress(Exception):` around the whole pipeline: a pipeline that crashes **after** the command line was built still passes; only the argv is asserted, not the outcome of the pass. Acceptable for "command line" intent but it hides a failing pass. | Narrow to the expected exception type, or assert `flow._record_run` status afterwards. |
| W7 | `tests/test_fy_overlap_rollover.py:191, :316` (`_OverlapRig(approved=True)`) | The forced switch is proven with a rig flag standing in for "owner approved with the typed word". The typed phrase is **not** exercised in any overlap test: mutant M12 (`phrase_matches` always True) is killed only by baseline `tests/test_m2_approvals.py::test_one_word_confirms_every_approval`, not by any scope file. The rule "forced rollover requires the typed phrase" is therefore proven only by baseline tests. | Add one dashboard test: POST the G6 rollover approval with a wrong word -> refused, with `APPROVE` -> accepted, while the overlap is active (the plan section 4 says the forced switch "stays"). |
| W8 | `tests/test_fy_overlap_rollover.py` (whole `_OverlapRig`), `:46-47` | The rig returns a status from mocked `_run_cloud_pipeline`/`_run_lan_pipeline` and stubs `activate_if_due` and `_alarm_unprotected_new_fy`. That is fine for the gate (the gate is real: real DB, real `final_copy_since`, real `g6`), but nothing in scope proves the claim "a cloud row reaches CLOUD_COMPLETE only after the verify evidence gate". The word "verified" in R11 rests on baseline tests of `flow.py:1849` (BUG-01). | Add one scope test that drives `_run_cloud_pipeline` with a failing verify and asserts the recorded status is `CLOUD_VERIFY_FAILED` (so not in `FINAL_COPY_STATUSES`). `test_fy_overlap_pipelines.py::test_failure_alert_has_its_own_key_group_and_title` already runs that path (`verified=False`); add the status assert there. |
| W9 | `tests/test_fy_overlap_activation.py:22`, `attention.py:34`, `report.py:28`, `ui.py:41`, `calendar.py:44`, `test_manual_only_leg.py:282,523,645,658` | `pytest.importorskip("time_machine")` at module level: if `time_machine` is not installed, whole files (about 300 tests incl. the April gate calendar) are **silently skipped** and the suite is green. It is installed here (0 skipped). | Make `time_machine` a pinned dev dependency (it is only an implicit dependency of `pendulum.travel_to`) and replace `importorskip` with a plain import; or add a CI step that fails when `pytest -rs` prints any skip for these files. |
| W10 | `tests/test_manual_only_leg.py:307` (`_FakeClient`, reconcile tests `:347-379`) | The "re-serve with no schedule removes the cron / manual run still starts" behaviour is proven with a fake Prefect client. The truth is only in `audit_lab/test_c1_prefect_schedules_lab.py` (p1-p6; it `skip`s silently at `:187` if the local Prefect server is not healthy in 120 s). | Keep, but record in the release checklist that C1 lab must show 6 passed and 0 skipped. |

Sampled for reality (all judged REAL unless listed above): activation (`test_dec_31_creates_folder_placeholder_record_and_one_info_mail`,
`test_jan_1_catches_up...`, `test_crash_after_the_folder_rerun_completes`, `test_crash_after_the_record_redoes_nothing`,
`test_unproven_clock_creates_nothing_warns_then_asks_next_day_then_succeeds`, `test_activation_never_touches_the_nas`,
`test_office_made_folder_is_adopted`, `test_link_or_junction_is_refused_with_the_reason`, `test_rollover_check_runs_activation_before_the_unprotected_alarm`),
attention (`T20`, `T54_activation_ignores_backup_state`: five real DB variants incl. failed/running/blocked/NAS-off/manual),
rollover (15 tests: all-scheduled switch, failed NAS blocks, manual copy after the closing moment, copy before it, nightly copy, only verified copies
(4 params), unscheduled NAS, NAS disabled, JUMP, forced switch, G6 text-only, unprotected alarm, next-FY row not a final copy, closing moment (4 instants)),
backup_flow (order, config per pass, shared budget/lateness, selection by fy, 4 shutdown-rule tests, 2 race-guard tests, baseline call),
pipelines (the 9 `TestCloudNextPass` tests), command_lines (2: real `fake_run_tool` argv of rclone and robocopy: asserts the FY26-27 path never appears),
audit (3), report (R01-R10), db/unit/alerts (real SQLite, real functions), alert_class (AST scan of every `AlertEvent` key: a real consistency test),
manual-only (serve registration x6, flag record), UI e2e (E01-E03 over a real uvicorn server), lab (S4 G3 per FY, 4 approvals per (leg, FY)).
Tautologies found: none. Asserts that cannot fail: `:1073` only (W3). Over-mocking that fakes the function under test: W1 only.

## 3. Mutation table

Each mutant: one deliberate bug in a copy of the code, only the listed files run (`-x`); if all green the full scope set was run too.
KILLED = a test failed (the killing test is named); SURVIVED = everything green.

| ID | Mutant (rule it breaks) | Files run | Result (killing test) |
|---|---|---|---|
| M01 | April gate: `>=` becomes `>` (boundary 1 Apr 00:00 excluded) | db, rollover, unit | KILLED `test_fy_overlap_db::TestFinalCopySince::test_counts_only_verified_final_copies_on_or_after_the_closing_moment` |
| M02 | April gate accepts a final copy 60 s before 1 Apr 00:00 IST | db, rollover, unit | KILLED `rollover::test_the_closing_moment_is_1_april_midnight_ist_through_the_whole_check[23:59-False]` |
| M03 | April gate accepts a copy 6 h before the closing moment | db, rollover, unit | KILLED `db::test_nothing_before_the_closing_moment` |
| M04 | Activation refused while the cloud backup is failing | activation, attention | KILLED `attention::test_T54_activation_ignores_backup_state` |
| M05 | Activation on an unproven clock | activation | KILLED `activation::test_unproven_clock_creates_nothing...` |
| M06 | Activation due a day early (30 Dec) | activation | KILLED `activation::test_dec_30_is_not_due` |
| M07 | Cloud leg skips the next-FY pass | backup_flow, pipelines | KILLED `backup_flow::test_each_leg_runs_the_configured_fy_then_the_next` |
| M08 | Configured FY pass runs second | backup_flow, pipelines | KILLED `backup_flow::test_each_leg_runs_the_configured_fy_then_the_next` |
| M09 | Alert key `backup:cloud:FY27-28:<status>` loses the FY | pipelines, alerts | KILLED `pipelines::test_failure_alert_has_its_own_key_group_and_title` |
| M10 | `blocked:<leg>:<FY>` key loses the FY | pipelines, alerts | KILLED `pipelines::test_supersedes_only_its_own_fy_and_names_it` |
| M11 | Unscheduled leg gets an overdue item again | attention, manual_only_leg | KILLED `manual_only_leg::test_C2_01_manual_leg_has_no_overdue_items` |
| M12 | Forced rollover/approval accepts any typed phrase | m2_approvals, rollover, ui | KILLED `test_m2_approvals::test_one_word_confirms_every_approval` (baseline test only; see W7) |
| M13 | Report counts both FYs' rows in one section | report | KILLED `report::test_R06_first_january_week_two_sections_per_leg` |
| M14 | `?fy=` of an inactive FY accepted | ui | KILLED `ui::test_trigger_inactive_fy_is_409` |
| M15 | `flow.backup(fy=...)` accepts an inactive FY | backup_flow, audit | KILLED `backup_flow::test_inactive_fy_raises_and_runs_nothing` |
| M16 | Next-FY pass keeps the configured FY's NAS path (mirror/delete could hit FY26-27) | unit, backup_flow, command_lines | KILLED `unit::test_config_for_fy_moves_both_paths_to_the_next_year_folders` |
| M17 | Manual run of an unscheduled leg not counted as a final copy | rollover, manual_only_leg | KILLED `rollover::test_an_unscheduled_nas_is_not_run_by_the_check_and_waits_for_a_manual_copy` |
| M18 | Overlap switched off still runs two passes | backup_flow, config | KILLED `backup_flow::TestBaselineCall::test_overlap_switched_off_is_one_baseline_call_per_leg` |
| M19 | A PARTIAL run counts as a verified final copy | db, rollover, manual_only_leg | KILLED `db::test_status_set_is_the_plan_set` |
| M20 | Last LAN pass shuts the NAS down after an earlier failure | backup_flow | KILLED `backup_flow::test_last_pass_does_not_shut_down_after_an_earlier_failure` |
| M21 | Next-FY pass writes `file_entries` (would prune FY26-27 entries, D-9) | pipelines | KILLED `pipelines::test_run_id_is_suffixed_and_the_row_is_tagged` |
| M22 | A disabled leg is required for the April switch | rollover | KILLED `rollover::test_with_the_nas_disabled_only_the_cloud_copy_is_required` |
| M23 | NAS folder with files and no marker gets a marker (G5 bypass) | activation, unit, pipelines | KILLED `activation::test_nas_folder_with_files_and_no_marker_is_left_alone` |
| M24 | Switch does not stamp `ended_at` (overlap never ends) | rollover | KILLED `rollover::test_all_scheduled_copies_switch_by_itself...` |
| M25 | Watchdog stale mail of the next FY drops the FY | attention, alerts | KILLED `attention::test_T39_watchdog_mails_next_fy_stale_once` |
| M26 | Approved re-run of FY27-28 loses its `fy` parameter | ui | KILLED `ui::test_approved_rerun_passes_fy[FY27-28]` |
| M27 | Weekly audit never sees "scheduled" (`_auto_scheduled_run` returns False) | audit, then full scope | **SURVIVED** (W1). Real helper has no test. |
| M28 | Report counts scheduled runs for a manual-only leg | report, manual_only_leg | KILLED |
| M29 | `serve` gives the manual-only NAS a cron | manual_only_leg | KILLED |
| M30 | `launch` reconcile does not recognise a manual-only leg | manual_only_leg | KILLED |
| M31 | Incident group ignores the FY (next-FY recovery closes the configured-FY incident) | alerts | KILLED |
| M32 | Audit trigger drops the `fy` parameter | ui | KILLED |
| M33 | An approval made for the other FY is applied to this pass | pipelines | KILLED `pipelines::test_other_fy_approval_is_not_offered` |
| M34 | `_other_fy` returns None (blocked next-FY pass supersedes the configured FY's pending approval) | pipelines, then full scope | **SURVIVED** in unit scope (W2). KILLED by `audit_lab/test_fy_overlap_lab.py::test_fyov_g3_blocks_per_fy` (run with `-k g3`). |
| M35 | `recovered:<leg>:<FY>` key loses the FY | pipelines, alerts | KILLED |
| M36 | `missed:<leg>:<FY>` key loses the FY | pipelines | KILLED |
| M37 | Switch does not supersede the old FY's approvals | rollover | KILLED |
| M38 | Race guard never sees a switch | backup_flow | KILLED |
| M39 | Next-FY LAN pass does not create its NAS folder | pipelines | KILLED |
| M40 | Next-FY pass keeps the configured source folder | unit, pipelines, backup_flow, command_lines | KILLED |
| M41 | Disabled NAS leg still gets FY passes in `backup()` | backup_flow, pipelines, then full scope | **SURVIVED** in scope. KILLED by baseline `tests/test_flow_orchestration.py` (M41b). No overlap-specific "NAS disabled + overlap" test. |
| M42 | April check runs an unscheduled (manual-only) leg itself | rollover | KILLED |
| M43 | Weekly audit does configured FY first, next FY second | audit | KILLED |
| M44 | Recorded "scheduled in effect" flag ignored | attention, manual_only_leg | KILLED |
| M45 | Disabled cloud leg still gets FY passes in `backup()` | backup_flow, pipelines, then full scope | **SURVIVED** in scope. KILLED by baseline `tests/test_flow_orchestration.py` (M45b). Same gap as M41. |

**Totals: 45 mutants, 41 killed, 4 survived in scope** (M27, M34, M41, M45). After adding the audit lab and the baseline
suite: 44 killed, 1 survivor (M27).

## 4. Coverage gaps

Run: scope unit tests (475 tests, slow calendar excluded), `--cov=core --cov=flow --cov=ui --cov=serve --cov=launch --cov=watchdog`.
Whole-file percentages are low for `flow.py` (60 %), `ui.py` (62 %), `launch.py` (21 %), `watchdog.py` (23 %) because the scope tests do not
run baseline code; only lines inside overlap-related functions are listed.

Overlap modules: `core/fy_overlap.py` 96 %, `core/attention.py` 92 %, `core/report.py` 85 %, `core/overdue.py` 97 %, `core/alert_class.py` 98 %,
`serve.py` 84 %.

Uncovered, overlap-related (all lines checked against source):

| File : lines | What the line does | Risk |
|---|---|---|
| `core/fy_overlap.py:294-297` | activation: `OSError` while making `E:\FY27-28` -> `_not_added` mail + `FOLDER_FAILED` | MEDIUM: the only activation failure path with no test. A bug here could silently skip activation or crash the daily check. |
| `core/fy_overlap.py:338, 355, 358, 371, 417, 448, 451-452, 460, 560, 593-594, 623-624` | defensive returns / the cloud hint text `"press 'Run backup' on the FY26-27 cloud card"` (460, used when the cloud leg is the missing one) / unreadable time fallbacks | LOW; line 460 is owner-facing text: add an assert in `test_final_copy_state_reports_the_source_the_hint_and_the_manual_leg` for the cloud hint. |
| `core/attention.py:46-48, 141-148, 156, 204, 224, 311-312, 341, 346, 365` | schedule-in-effect read error, `_progressing_partial` branches, `leg_timing` states (`on_time` default, `catching_up`), next-FY `audit:` items (341, 346), `_settle_repeats` | MEDIUM: lines 341/346 are the per-FY `audit:<leg>:<FY>` AMBER items (T19 covers one variant only); `catching_up` (224) is hit by T18 for the next FY only if the line numbers are read as the leg_timing state, so verify. |
| `core/report.py:239-242, 305-306, 344-345, 394-395, 409-410, 429-440, 454-467, 504-505, 617-619, 650-653, 675-678` | report FY view error returns, `_open_decisions`, headline "a backup is waiting for your decision", audit-row wording, "alerts off" paragraph, "no backup was scheduled" text | MEDIUM for 429-440 / 454-467 (headline and audit wording in an FY view), LOW otherwise. |
| `flow.py:450-454` (`_other_fy`), `:2004-2009`, `:2329-2334` | `keep_fy` wiring at both pipeline call sites (W2) | HIGH-ish: approvals of the other FY. Covered only by the lab. |
| `flow.py:1241-1246`, `:1582-1584` | block-request record failure, "last successful backup unknown" fallback in `_pending_context` | LOW |
| `flow.py:2137-2145` | LAN pass `preflight / snapshot / guards / access` retry closure of a LAN pass | MEDIUM (baseline path, but runs for each FY pass; only the audit lab runs it). |
| `flow.py:2541-2542, 2563, 2567`, `:2630-2655`, `:2691-2761`, `:2779-2781`, `:2818-2819` | rollover check: log retention failure, resume of a finished/aborted journal, hand-switched config, clock refusals, reading/saving the saved final-copy state | MEDIUM: `:2779-2781` / `:2818-2819` are the overlap's `load_final_copy` / `save_final_copy` error branches (a resumed announce); the clock refusals are baseline. |
| `flow.py:3013-3017` | `_run_final_backups` exception branch (a final backup that raises -> `<LEG>_NOT_COMPLETE`/`_BLOCKED`) | MEDIUM: this is how a failing NAS final copy reaches the gate in production (the rollover rig returns statuses instead of raising). Add one test where the pipeline raises. |
| `flow.py:3062-3081, 3100, 3128-3130, 3178-3179, 3221-3227` | audit: overlap records unreadable (3080-3081, 3100: fail-closed message), audit row persistence failure, NAS shutdown failure after audit, slot not obtained -> rows for every pass | MEDIUM: 3080-3081/3100 is the fail-closed path of the overlap in the weekly audit. |
| `flow.py:3335-3337, 3351-3352` | `backup()` refuses a disabled leg; overlap record unreadable names "the next financial year" | LOW (baseline guard); 3351 is overlap-specific wording. |
| `ui.py:723-724, 771, 779, 811-813, 880-909, 922-944` | `_leg_card` audit read error, `_legs_plan` "no copy switched on" / "both manual" summaries, `_leg_timing` fallback, trigger rate-limit / disabled / already-running / start-failure branches | MEDIUM: `ui.py:771` and `:779` are two of the four config-state summaries the owner asked for ("nothing on", "both manual"; the validator forbids them, so low). |
| `ui.py:1479-1481, 1507, 1519-1521, 1542-1543, 1559-1567` | unfinished-rollover message, rollover card read error, `_activation_is_pending` error -> not allowed, "Run FY rollover now" error branches | MEDIUM |
| `ui.py:1632-1640, 1648-1692, 1846-1849, 1860-1861` | check-now pre-check, verify trigger error branches, `_get_last_success` (1846-1849: the per-FY last success for a card) | LOW-MEDIUM |
| `serve.py:128-129`, `launch.py:308-317, 336-339`, `watchdog.py:430-432` | record-schedule failure, reconcile "absent"/pause branches, stale-check exception | LOW; `launch.main()` and `watchdog.main()` (21 % / 23 %) are never run: the real services' wiring (reconcile call order, watchdog loop calling `_check_stale_legs`) is not tested in scope. |
| `core/guards.py` | no overlap-related line uncovered (G6 overlap branch fully covered); the rest of the file is baseline G1-G5 | - |

## 5. Business-rule map

REAL = a test that fails when the rule is broken (mutant-proven where a mutant is named); WEAK = tested only indirectly, by
text, by fakes, or only in the lab / baseline; MISSING = no test.

### Owner expectations

| Rule | Test(s) | Rating |
|---|---|---|
| New FY folder created on 31 Dec (and 1 Jan catch-up if the server was off) | `activation::test_dec_31_creates_folder_placeholder_record_and_one_info_mail`, `test_jan_1_catches_up...`, `test_dec_30_is_not_due`, `attention::T32`; calendar `test_server_off_all_of_31_dec_activates_at_the_first_check_on_1_jan` (M06) | REAL |
| ...with NO blockers: failed / running / blocked backups, NAS off, a manual-only leg do not stop it | `attention::test_T54_activation_ignores_backup_state` (M04 killed), `activation::test_activation_never_touches_the_nas`, `test_office_made_folder_is_adopted` | REAL |
| The only deliberate blockers: unproven clock, clock behind, jump > 1 FY, link/junction (documented D-2) | `test_unproven_clock_...`, `test_skewed_clock_waits_too`, `test_clock_behind_...`, `test_jump_of_more_than_one_year_is_refused`, `test_link_or_junction_is_refused_with_the_reason` (M05) | REAL (owner should know the clock is a blocker by design) |
| Folder creation failure (`mkdir` raises) is mailed and retried | none (`fy_overlap.py:294-297`) | **MISSING** |
| Activation is idempotent and crash-safe | `test_second_call_is_a_no_op`, `test_crash_after_the_folder_rerun_completes`, `test_crash_after_the_record_redoes_nothing` | REAL |
| Both FYs backed up every night until 1 April (cloud and NAS) | `backup_flow::test_each_leg_runs_the_configured_fy_then_the_next` (M07, M08), `command_lines` x2 (argv of real rclone/robocopy), lab `test_fyov_two_mirrors_two_prefixes` (real tools), calendar `test_one_pass_per_leg_until_31_dec_then_two_then_one_after_the_switch` | REAL |
| Configured FY first, next FY second | M08 | REAL |
| A failing pass does not stop the other pass; the flow still fails loudly | `backup_flow::TestFailures` x3 | REAL |
| Switch only after the old FY's final backup AND its verification, on every enabled leg | gate: `rollover` tests + `db::TestFinalCopySince` (M01-M03, M19, M22); "verification" = statuses only reachable after the evidence gate: baseline tests only | REAL for the gate, WEAK for the verification link (W8) |
| Final copy must be from the closing moment on (1 Apr 00:00 IST, boundary included, UTC form) | `rollover::test_the_closing_moment_is_1_april_midnight_ist_through_the_whole_check` (4 instants), `db::test_nothing_before_the_closing_moment`, `test_newest_qualifying_copy_wins_compared_as_instants` (M01-M03) | REAL |
| A manual run counts; a nightly run counts; a next-FY run does not; PARTIAL/BLOCKED/FAILED never | `test_a_manual_nas_copy_after_...`, `test_a_nightly_cloud_copy_counts_...`, `test_a_run_of_the_next_fy_pass_after_april_is_not_a_final_copy...`, `test_only_verified_copies_count...` x4 (M17, M19) | REAL |
| Unscheduled leg is never run by the April check; blocks the switch until a manual copy; both FYs keep being backed up while blocked | `rollover::test_an_unscheduled_nas_is_not_run_by_the_check...` (M42), `test_a_failed_nas_final_copy_blocks_and_both_years_stay_active`, calendar `test_failed_lan_final_blocks_the_switch_until_a_manual_copy_then_switches_on_3_apr`, `test_unscheduled_lan_leg_is_never_run_by_the_check_and_blocks...` | REAL |
| Switch happens by itself (no click) once the last copy exists; old approvals superseded; overlap ended | `rollover::test_all_scheduled_copies_switch_by_itself...` (M24, M37) | REAL |
| Forced switch (typed word) still available and names the forced legs | `rollover::test_a_forced_switch_goes_through...`, `test_a_resumed_announce_still_names_the_forced_legs`; typed phrase itself only in baseline `test_m2_approvals` (M12) | WEAK (W7) |
| After the switch only the new FY is active | `rollover::test_the_overlap_is_not_open_once_ended_at_is_stamped`, `attention::T24`, calendar | REAL |
| Switch during a running backup does not run stale passes | `backup_flow::TestRaceGuard` x2 (M38) | REAL |
| Config shows cloud only / NAS only / both / manual only, and behaviour honours it | text: `ui::test_legs_plan_texts` (5 states + record wins + overlap sentence), `manual::test_leg_plan_text`; registration: `manual::test_manual_*_registered_without_schedule` x4 (M29), reconcile x5 (M30), lab C1 p1-p6 (real Prefect); reports `R29`, `R30`, `R31-R34`; attention `T22`, `C2_01-C2_14` (M11, M44); April gate (M17, M22, M42); audit (M27 survives) | REAL, except the audit part (W1) and the JS that displays the text (W3) |
| A manual-only leg still takes part in a hand-started overlap run | `backup_flow::test_manual_run_of_an_unscheduled_leg_runs_both_years`, lab `test_fyov_manual_only_lan_and_overlap_off` | REAL |
| Disabled leg takes no passes at all (with overlap on) | only baseline `test_flow_orchestration` (M41, M45 survive in scope) | WEAK |
| Dashboard shows 2 or 4 cards, per-card `Run backup` / `Check now` send the card's FY; `?fy=` of an inactive FY refused | server: `ui::test_status_*`, `test_trigger_*` (M14, M26, M32), E01-E03 over real uvicorn; browser rendering: source-text tests only | REAL (server) / WEAK (rendering, W3) |

### Plan rules and gates R9-R12 (ARCHITECTURE section 10.7)

| Gate / rule | Test(s) | Rating |
|---|---|---|
| R9 activation: on/after `overlap_activate_on` IST, only NONE/FORWARD, proven clock, SOURCE folder + placeholder only, record written last, link refused, INFO mail once (`rollover:activated:<fy>`), never NAS / config.yaml | activation file (all of the above), `test_activation_never_touches_the_nas`; "never writes config.yaml" has no direct test | REAL (config.yaml untouched: WEAK) |
| R10 each next-FY pass passes G1-G5 for its own FY (`guard_baseline(leg, fy)`), within the leg's shared budget; missing next folder fails that pass; NAS dest must end in its FY | `db::test_guard_baseline_sees_both_passes_of_its_fy`, `pipelines::test_uses_the_next_fy_folder_and_prefix` (guards called with FY27-28), `test_destination_of_another_year_is_refused`, `test_no_budget_left_is_*_missed_*` x2, lab `test_fyov_g3_blocks_per_fy`, `test_fyov_g5_per_fy`, `test_fyov_empty_nas_folder_is_marked` | REAL |
| R10 (alert side) per-FY keys, titles, incident groups, recovery, approvals per (leg, FY) | M09, M10, M31, M33, M35, M36; `keep_fy` wiring: lab only (W2) | REAL / WEAK for `keep_fy` wiring |
| R11 final-copy rule (statuses, fy, started_at >= closing moment, boundary included, legacy statuses excluded, next-FY row never counts, switch warns when counted copy is older) | see above; warning text: `test_a_nightly_cloud_copy_counts_when_this_mornings_cloud_run_fails_and_the_mail_warns`, `test_a_manual_nas_copy_after...` | REAL |
| R12 manual-only legs: no cron; no overdue / first-run / catching-up item; no watchdog mail for any FY; registered and kept resumed; skipped by scheduled audit; "(run by hand)" in reports; failed manual run still red and mailed | serve (M29), launch (M30), attention C2_01-C2_14 (M11), watchdog `C2_08`, `C2_09`, report R31-R33 (M28), `C2_19`/`C2_20` mail wording, lab C1; **skipped by scheduled audit: M27 survives** | REAL except "skipped by scheduled audit" = WEAK/MISSING for the real detector |
| D-7 shared budget, first pass only late | `backup_flow::test_both_passes_of_a_leg_share_the_budget_origin_and_only_the_first_is_late`, `pipelines::test_no_budget_left...` | REAL |
| D-8 one NAS wake, last pass may shut down only after all earlier LAN_COMPLETE | shutdown rule: `TestLanShutdownRule` x4 (M20); audit: `test_one_nas_wake_and_one_shutdown_for_both_fys`; **backup flow: no test that the second LAN pass does not wake the NAS again** (code calls `wol_check_task` in every pass, `flow.py:2116`, plan says once) | REAL (shutdown) / WEAK (wake once) |
| D-9 no `file_entries` and no cross-check on the next-FY pass | `pipelines::test_writes_no_file_entries`, `test_run_id_is_suffixed_and_the_row_is_tagged` (M21) | REAL |
| D-4 / section 2.4 no schema change, baseline DB opens unchanged | `db::TestBaselineDatabaseCompatibility::test_opens_unchanged_with_the_same_answers`, `TestRecords` | REAL |
| Section 10 "overlap off / before activation = exact baseline call" | `backup_flow::TestBaselineCall` x3 (M18), report R01-R05 byte-identical, ui `test_status_without_overlap_matches_baseline`, lab `test_fyov_before_activation_is_baseline` | REAL |
| Weekly audit covers both FYs, next first, one slot, one wake/shutdown, `fy=` selects | `audit` file (M43), lab `test_fyov_weekly_audit_both_fys` | REAL |
| Reports: sections per (leg, FY), no summing across FYs, activation window, switch week, overlap off | report R06-R44 (M13, M28) | REAL |

## 6. Ranked list of tests to add

1. **Real `_auto_scheduled_run` test** (M27, W1): unit test of the helper with a stubbed `FlowRunContext` (True / False / None / raises) plus one lab run of `integrity-audit` started by its Prefect schedule with a manual-only NAS asserting no LAN audit and no WoL.
2. **`keep_fy` wiring at both pipeline call sites** (M34, W2): guard-blocked next-FY and configured-FY pass in `cloud_env` / `lan_env`, assert `keep_fy` and that a pending approval of the other FY survives (use a real in-memory `ManifestDB`, not the mock); direct test of `_other_fy`.
3. **Run the dashboard JS** (W3): jsdom/Node or headless-browser test of `renderLegCard`, `updateStatus`, `renderHero`, `triggerBackup` against a canned `/status` and `/api/rollover` payload (2 cards, 4 cards, disabled leg, manual-only leg, final-copy block); delete the text-grep tests it replaces.
4. **Activation `mkdir` failure** (`fy_overlap.py:294-297`): `create_source_folder` raises `OSError` -> `FOLDER_FAILED`, no record written, WARNING mail `rollover:activation:FY27-28` naming the reason, retried next day and then succeeds.
5. **Overlap + one leg disabled through `backup()`** (M41, M45): `owner_config` with `lan.enabled=False` (and one with cloud off), overlap active: only cloud passes (configured then next), no LAN call; same with `scheduled=False` on the other leg.
6. **Final backup raises** (`flow.py:3013-3017`): the NAS pipeline raises (not returns a status) during `_run_final_backups` in the overlap rollover; assert `LAN_NOT_COMPLETE` blocks the switch, both FYs keep running, and a later manual `LAN_COMPLETE` switches.
7. **Typed phrase on the forced rollover** with the overlap active (W7, M12): wrong word refused, `APPROVE` accepted, forced legs named.
8. **Wake-once in a hand/cron LAN run with two passes** (D-8): second pass does not send a magic packet when the NAS is already up (or document and test the actual behaviour: `wol_check_task` probes first).
9. **Verify-failed is not a final copy** (W8): assert the recorded status of a failed-verify next-FY / configured pass is outside `FINAL_COPY_STATUSES`.
10. **Report and attention branches**: report FY-view headline "a backup is waiting for your decision" and the audit-row wording in an FY view (`report.py:429-467`), per-FY `audit:` items (`attention.py:341,346`), cloud hint text (`fy_overlap.py:460`).
11. **Housekeeping**: delete stale xfail docstring (W5), drop `create=True` + stale comment (W1), narrow `suppress(Exception)` (W6), tighten `<= 12` (W4), make `time_machine` a hard dev dependency and add a "0 skipped" check for the slow, lab and C1 runs (W9, W10).
12. **Watchdog / launch entry points**: a smoke test that `launch.main` calls the reconcile and `serve` in order and that the watchdog loop calls `_check_stale_legs` once per cycle (21 % / 23 % covered).

Commands used (for reproduction): scope unit run with `--cov` in a detached worktree; mutants via a one-line text replace and
restore; `audit_lab/test_fy_overlap_lab.py audit_lab/test_c1_prefect_schedules_lab.py` with a clean temp `PREFECT_HOME`
(13 passed); `tests/test_fy_overlap_calendar.py` (9 passed).
