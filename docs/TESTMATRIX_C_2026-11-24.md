# Test matrix - C1 Registration, C2 Behaviour, C3 Shipped configs (written 2026-11-24, before the build)

Reference for the C test writer and the C code writer (CLAUDE.md "Build process"). Binding plan:
`docs/PLAN_BASELINE_OVERLAP_2026-11-23.md` sections 2.5, 5, 6 (cards, state table), 7, 9 (C1, C2), 10 (row
"`scheduled: true` (default)"). B7 context: `docs/TESTMATRIX_B7_2026-11-24.md` (E3 paragraph, rows C01-C04, which
move into this file as C2-01..C2-04). Line numbers are from worktree HEAD `505afce` **plus the uncommitted B7 work**
in `core/attention.py` / `watchdog.py` (state of 2026-11-24); re-check them after the B7 commit. Prefect source lines
are from the venv in use (`prefect 3.9.4`, `C:\Users\malliath\Desktop\aam_backup\.venv\Lib\site-packages\prefect`).
Order in the plan: C1 and C2 come after B8 (reports) and B9 (dashboard). **Scope split (read with
`docs/TESTMATRIX_B8_2026-11-24.md` D8-5 and `docs/TESTMATRIX_B9_2026-11-24.md` Q6/Q7):** the manual-only report part
(rows R31-R33) is built in B8; the card schedule line (`schedule_text`), the card timing state `manual` and the
`/status` `scheduled` / `legs` fields are built in B9. C2 keeps attention, watchdog, `_next_text` / `_data_safety`,
the B5 xfail flip, and re-checks the B8/B9 rows as regression (C2-15..C2-18). When this file and B8/B9 disagree on a
surface they own, B8/B9 win.

Owner expectation (E3 in B7): the config makes clear which legs run and how (cloud only, LAN only, both; each on its
schedule or manual only), and the behaviour honours exactly that. Three states per leg, no fourth:

| `enabled` | `scheduled` | Meaning | Registered | Cron | Overdue / first-run / watchdog mail | Failure of a run |
|---|---|---|---|---|---|---|
| false | (ignored) | off | no (paused if left from an old boot) | - | none | - (never runs) |
| true | true (default) | on its schedule | yes | yes | yes (baseline) | red + mail (baseline) |
| true | false | manual only | yes (`backup-<leg>`, resumed) | **none** | **none, either FY** | red + mail (unchanged) |

## DECISIONS (Opus, 2026-11-24, binding for C1-C3)

- **DC1 Source of the flag.** Everything that judges "is this leg late" (attention items, watchdog, `leg_timing`, card
  schedule line) reads `<leg>_scheduled` from the **schedule-in-effect record** (what the scheduler really serves,
  plan 5 last bullet), falling back to `config.<leg>.scheduled` only when no valid record exists. Always tested as
  `is not False` (absent / `MagicMock` / `SimpleNamespace` without the key = scheduled = baseline). Reports and the
  email `_next_text` have no record at hand and read the config (they describe the plan, not the live scheduler; the
  Settings "Apply changes" restarts the agent, so the window is minutes).
- **DC2 F-C1-1 (finding): the record never carries the flags today.** `serve._record_schedule_in_effect`
  (`serve.py:117-119`) passes only the three cron/tz keys, so `set_schedule_in_effect` (`core/manifest.py:1314-1315`)
  writes `<leg>_scheduled: true` for every leg. Without fixing it in C1, C2 would never take effect on the box. C1 passes
  `cloud_scheduled = config.cloud.scheduled`, `lan_scheduled = config.lan.scheduled` (the literal flag; `enabled` is
  judged from the config as today).
- **DC3 A flip of the flag starts the leg's clock.** In `set_schedule_in_effect` a change of `<leg>_scheduled` (either
  direction) against the old record stamps `<leg>_since = since or now`, exactly like a cron change
  (`core/manifest.py:1307-1313`). Reason: a leg that was manual for months and is switched back to scheduled must be
  counted from the switch (first scheduled run + budget + grace), not go RED at once from a months-old manual success.
  Old record without flags = true: no flip, `since` kept (baseline unchanged).
- **DC4 `schedules=[]`, never `None`, for an unscheduled leg.** By reading Prefect 3.9.4: `Runner.aadd_deployment`
  calls `deployment.aapply()` with no arguments (`prefect/runner/runner.py:300`), so the deprecated "empty list
  preserves schedules" path (`prefect/deployments/runner.py:132-145`, only for `aapply(schedules=[])`) is NOT taken;
  `_update` sends `schedules: []` because the field was set (`deployments/runner.py:643-672`); the API falls back to
  "recreate" because cron schedules made by `Cron(...)` have no slug (`server/api/deployments.py:296-372`); the model
  deletes all schedules and creates none (`server/models/deployments.py:335`, `:387-405`) and deletes future
  auto-scheduled runs (`:371-377`). Expected: the old cron disappears. The probe (C1 P1-P7) proves it on this PC and on
  the box; it also asserts that no `FutureWarning` "Preserving existing schedules" was raised (we are on the clearing
  path). The probe stays as a lab file: re-run it on every Prefect upgrade (note for D1 / DEPLOY_RUNBOOK).
- **DC5 No pause for a manual-only leg** (plan 5). `_reconcile_disabled_legs` code is unchanged: it already keeps an
  enabled leg resumed (`launch.py:275-283`, `:303-305`). C1 adds tests only. Chunk K-C1-4 (delete the surviving cron in
  the reconcile step) is built **only if** probe P2 or P3 fails.
- **DC6 No flow-level guard** refusing an auto-scheduled backup run of a manual-only leg. If a cron ever survived, the
  result is one extra backup run under all guards (G1-G6, approvals) - safe for the data - and the probe catches it on
  the next Prefect upgrade. A guard would also mis-fire in the "saved but not applied yet" window (record still says
  scheduled, config already false). The audit flow keeps its own skip (`flow.py:3091-3094`), and `_run_final_backups`
  its `skip_unscheduled` (`flow.py:2997-3002`) - both unchanged.
- **DC7 Items of a manual-only leg.** Suppressed for both FYs: `stale:`, `first_run:`, `catching_up:` (the whole
  `_overdue_item` call, and the RED "schedule cannot be read" fallback, `core/attention.py:275-283`). Kept for both
  FYs: `last_run:` (RED; AMBER for MISSED), `run_logs:`, `clock`, `nas_shutdown`, `audit:`, `approval:`. A failed manual
  run is a real failure (plan 5). No new "never backed up" item for a manual leg (owner chose manual; the card says
  "not backed up yet - press Run backup", B9, and the April gate lists it as outstanding, B4).
- **DC8 Card timing state** is B9's (`ui._leg_timing(..., scheduled=)` returns state `manual`, B9 Q6 / K9). C2 does
  not change `core.attention.leg_timing`. One rule only: B9's in-effect read and C2's K-C2-1 `leg_scheduled` must be
  the same function - if B9 landed with its own helper, K-C2-1 reuses it (same semantics as DC1, no second copy); if
  not, B9's K11/K9 call K-C2-1.
- **DC9 Wording (fixed strings, tests compare exactly).**
  - Card schedule line: B9 Q6 (`"Next scheduled backup: <cron_to_human>"` / `"Runs only when you press Run backup"`).
  - Email `_next_text`, manual leg (mode `lan` / `cloud`, not RECOVERED/INFO, not a `_BLOCKED` action):
    `"This <LEG_RUN> has no schedule: it runs only when someone presses Run backup on the dashboard. You get one email
    when it works again."` (`LEG_RUN` = `core/email_compose.py:39`). Scheduled legs and `audit` unchanged.
  - Email `_data_safety` line of a manual leg gets the suffix `" Runs only by hand (no schedule)."`.
  - Reports: B8 D8-5 texts (label `"<leg label> (run by hand)"`, `scheduled` 0, empty detail "No backup was
    scheduled in this period - this backup runs only when started from the dashboard.").
- **DC10 Shipped configs (C3).** Both `config.example.yaml` and `deploy/config.install.yaml` carry one explanation
  block above `lan:` (the three states + "cloud only / NAS only / both"), and write `enabled` and `scheduled`
  explicitly for both legs. `deploy/test_config.py` (`:61-62`, prints only `Enabled`) prints one plan line per leg from
  a config-only helper (nouns as B9 Q7: "office NAS copy", "online copy"), so the install check shows the same three
  states as the dashboard.

No owner questions.

---

## 1. Current behaviour (found in the code)

| What | Where | Fact |
|---|---|---|
| Registration | `serve.py:35-55` `_deployments()` | an enabled leg is registered with `schedules=[Cron(<leg>_cron, tz)]` always; a disabled leg is not registered. No `scheduled` read anywhere in `serve.py`. Docstring `:22-29`. |
| Schedule record | `serve.py:103`, `:107-123` `_record_schedule_in_effect` | writes `{cloud_cron, lan_cron, timezone}` only -> flags default true (F-C1-1). Never raises. |
| Record store | `core/manifest.py:1276-1294` `_stored_schedule`, `:1296-1316` `set_schedule_in_effect` | reads flags as `is not False` (old record = scheduled); `<leg>_since` changes only on a cron/tz change (`:1307-1313`); a flag flip does not move it. |
| Reconcile | `launch.py:252-324` `_reconcile_disabled_legs(config, legs=None)` | legs map `backup-lan` / `backup-cloud` = `enabled`, `integrity-audit` = any enabled (`:275-283`); enabled + paused -> resume (`:303-305`); disabled + unpaused -> pause. Called before `serve()` (`launch.py:381-388`, serve `:406-409`, `pause_on_shutdown=False`). |
| Tests today | `tests/test_p2_sched_pause.py` (`serve.deployments()` with a tmp config via `monkeypatch.setattr(serve, "CONFIG_PATH", ...)`; live reconcile against `127.0.0.1:4200`, skipped when down), `tests/test_audit_remediation.py:266` (mocked `get_client`), `tests/test_overdue_schedule.py:310-329` (record written by serve) | no test of an unscheduled leg. |
| Schedule in effect (attention) | `core/attention.py:40-56` `_schedule_in_effect` | stored record returned as is (has flags); the config fallback `:51-54` builds only cron/tz keys - **no flags**. |
| Overdue item | `core/attention.py:219-266` `_overdue_item`; called from `_leg_items` `:269-283` | runs for every enabled leg and FY; no `scheduled` check. Missing schedule -> RED `stale:<leg>[:<fy>]` `:278-283`. |
| Banner loop | `core/attention.py:422-446` `compute_attention` | per enabled leg `_leg_items(..., fy=None)` then the next FY (B7). |
| Card timing | `core/attention.py:186-216` `leg_timing`; `ui.py:607` `_leg_timing` | states `on_time/late/overdue/waiting/catching_up`; no manual state. |
| Watchdog | `watchdog.py:382-433` `_check_stale_legs` | mails every `stale:` code of `compute_attention` (`:400`); so suppressing `stale:` there is enough. |
| Failure mail | `flow.py:340` key `backup:<leg>[:<fy>]:<status>` via `_record_run` | independent of `scheduled`; a manual run's failure is mailed today. |
| Reports | `core/report.py:201` `leg_summaries`, `:243-267` `report_headline`, `:293-304` `_scheduled_counts`, `:307` `build_report_model`, `:354-360` `_summary_html`, `:422-426` stats; `flow.py:2412-2426` passes `schedule=config.schedule` | `scheduled` = cron firings in the period for every leg -> a manual leg with no run gives "action needed: <leg> - no backup ran". |
| Email | `core/email_compose.py:125-139` `_data_safety`, `:142-157` `_next_text` | `_next_text` says "The next <run> runs automatically on <time>" for a manual leg too (wrong). |
| Card line | `ui.py:487-505` `dashboard()` context `cloud_schedule` / `lan_schedule`; `templates/dashboard.html:57`, `:76` `"Next scheduled backup: {{ lan_schedule or 'Not configured' }}"` | says a schedule for a manual leg (wrong). |
| B5 xfail | `tests/test_fy_overlap_calendar.py:338-350` `test_unscheduled_lan_leg_is_never_overdue_in_the_whole_window` (strict) | fails today (first_run/stale LAN codes); the calendar itself already skips cron jobs of an unscheduled leg (`:79`) and writes no schedule record (attention falls back to the config -> needs the DC1 fallback flags). |
| Shipped configs | `config.example.yaml:35-39`, `:125-128`; `deploy/config.install.yaml:20-22`, `:30-32`; tests `tests/test_manual_only_leg.py:97-108` | `scheduled: true` written with a comment; `enabled` has **no comment** in the example (only the install file says "false = cloud only" / "false = NAS only"); no block saying the three states in one place. `deploy/test_config.py:61-62` prints `Enabled` only. |

## 2. Behaviour to build

**C1 Registration.** `_deployments()`: per enabled leg `schedules=[Cron(...)] if config.<leg>.scheduled else []`,
description `"Manual-only <leg> backup - runs when started from the dashboard"` for the manual one (name, parameters,
tags unchanged: card buttons and approvals call `aam-backup/backup-<leg>`). Disabled leg: not registered (unchanged).
`_record_schedule_in_effect` passes both flags (DC2). `set_schedule_in_effect` stamps `since` on a flag flip (DC3).
Reconcile: unchanged code (DC5). Probe proves the cron is removed and manual runs still start (DC4).

**C2 Behaviour.** One helper decides "scheduled" (DC1); `_leg_items` skips the overdue part for a manual leg (DC7, both
FYs); the watchdog follows automatically; the email says "by hand" (DC9). Card line / timing (B9) and reports (B8)
are re-checked only. The strict B5 xfail is removed in the C2 commit.

**C3 Shipped configs.** Explanation block + explicit keys in both shipped configs; test; `deploy/test_config.py`
prints the plan per leg (DC10).

## 3. Test matrix

Fixtures: real `ManifestDB` on `tmp_path`; `AppConfig` built as `tests/test_manual_only_leg.py:21` `_config(...)`
(owner layout `E:\FY26-27` / `\\nas\backup\FY26-27`, `runtime_dir` in `tmp_path` so the record goes to a temp DB);
schedule record written with `db.set_schedule_in_effect({... "lan_scheduled": False})` unless the row says "no record";
overlap record as in B7 (`db.set_overlap_record("FY27-28", {"activated_at": "2026-12-31T06:00:00+05:30",
"from_fy": "FY26-27"})`); next-FY rows with run ids `x-lan-FY27-28`; `now=` or `pendulum.travel_to(..., freeze=True)`;
notify faked (records events). Default schedule. Layers: S = `serve._deployments` / `_record_schedule_in_effect`,
R = `launch._reconcile_disabled_legs` (mocked client as `tests/test_audit_remediation.py:266`), M = `ManifestDB`,
U = `compute_attention`, W = `watchdog._check_stale_legs`, P = pipeline with `fake_run_tool`,
E = `email_compose`, L = lab probe (real local Prefect server).
All C1/C2 unit rows in `tests/test_manual_only_leg.py` (part 2 = C1, part 3 = C2) unless stated.

### C1 Registration

| ID | Scenario | Inputs | Expected | Layer / test name |
|---|---|---|---|---|
| C1-01 | Defaults | both legs enabled + scheduled | exactly today: `backup-cloud` one schedule with `cron == cloud_cron` and `timezone == schedule.timezone`, `backup-lan` likewise; 6 deployments, same names/parameters/tags as before | S `test_default_config_registers_both_crons` |
| C1-02 | LAN manual, cloud scheduled | `lan.scheduled: false` | `backup-lan` registered, `schedules == []`, parameters `{"config_path", "mode": "lan"}`, name unchanged; `backup-cloud` keeps its cron | S `test_manual_lan_registered_without_schedule_cloud_keeps_cron` |
| C1-03 | Cloud manual, LAN scheduled | `cloud.scheduled: false` | mirror of C1-02 | S `test_manual_cloud_registered_without_schedule` |
| C1-04 | LAN disabled | `lan.enabled: false` (any `scheduled`) | no `backup-lan`; `backup-cloud` with its cron (cloud keeps running by its own schedule, plan 5) | S `test_disabled_lan_not_registered_cloud_keeps_cron` |
| C1-05 | Other deployments | C1-02 config | weekly, monthly, rollover-check, integrity-audit schedules identical to C1-01 | S `test_manual_leg_leaves_other_deployments_alone` |
| C1-06 | Description | C1-02 | `backup-lan` description says manual only; `backup-cloud` description unchanged | S `test_manual_leg_description` |
| C1-07 | Record carries the flags | C1-02 config, `serve._record_schedule_in_effect(cfg)` | stored `lan_scheduled is False`, `cloud_scheduled is True` | S+M `test_serve_records_the_scheduled_flags` (fails today: F-C1-1) |
| C1-08 | Old record without flags | raw meta JSON with only cron/tz | read back `cloud_scheduled is True`, `lan_scheduled is True` | M (already in `tests/test_fy_overlap_db.py`, A4; cite, do not duplicate) |
| C1-09 | Flip to manual stamps since | record (scheduled true, same cron) then `set_schedule_in_effect({... "lan_scheduled": False}, since="2027-01-10T09:00:00+05:30")` | `lan_since == "2027-01-10T09:00:00+05:30"`, `cloud_since` unchanged | M `test_flag_flip_stamps_since` (DC3) |
| C1-10 | Flip back to scheduled stamps since | record lan false -> true, same cron, `since=` given | `lan_since` = that time | M `test_flag_back_to_scheduled_stamps_since` (DC3) |
| C1-11 | Restart, same flags | write the same record twice | `lan_since` unchanged (None stays None) | M `test_same_flags_keep_since` |
| C1-12 | Switch back is counted from the switch | LAN manual, last success 60 days ago; flip to scheduled at 10 Jan 09:00 (C1-10); `now` = 10 Jan 12:00, then the deadline `overdue.deadline(lan_cron, tz, 10 Jan 09:00, 4, 2)` + 60 s | 12:00: no `stale:lan`; after the deadline: RED `stale:lan` | M+U `test_back_to_scheduled_is_counted_from_the_switch` (DC3) |
| C1-13 | Reconcile resumes a paused manual leg | mocked client: `backup-lan` paused, config lan enabled + manual | `resume_deployment` called; result `"resumed (leg enabled)"`; `pause_deployment` never called | R `test_reconcile_resumes_manual_leg` |
| C1-14 | Reconcile leaves an active manual leg | same, `paused=False` | no call; result `"active"` | R `test_reconcile_keeps_manual_leg_active` |
| C1-15 | Reconcile legs map | config cloud scheduled, lan manual | `legs == {"backup-lan": True, "backup-cloud": True, "integrity-audit": True}` (spy on the map via the mocked client reads) | R `test_reconcile_treats_manual_as_enabled` |
| C1-16 | Regression | `tests/test_p2_sched_pause.py`, `tests/test_audit_remediation.py`, `tests/test_launch_comprehensive.py`, `tests/test_overdue_schedule.py` | green, unchanged | existing |

### C1 Probe (lab, real local Prefect server, clean `PREFECT_HOME`)

File `audit_lab/test_c1_prefect_schedules_lab.py` (targeted lab file; not part of the unit suite). Setup: pick a free
port; start `.venv\Scripts\python.exe -m prefect server start --host 127.0.0.1 --port <port>` as a subprocess with
env `PREFECT_HOME=<tmp_path>\prefect_home` (empty), `PREFECT_API_URL=http://127.0.0.1:<port>/api`,
`PREFECT_SERVER_SERVICES_SCHEDULER_LOOP_SECONDS=2`, all other `PREFECT_*` removed; wait for `/api/health` (max 120 s,
else `pytest.skip`). The client part runs in a child script (pattern `tests/test_p2_sched_pause.py:58-82` `_live`) with
the same env. Use a **dummy flow** `@flow(name="aam-backup") def probe(mode: str = "lan"): return mode` (never
`flow.backup`: no real config, no real backup). Deployment name `backup-lan-c1probe`. Kill the server in a `finally`.

| ID | Step | Expected | Test name |
|---|---|---|---|
| P1 | `probe.to_deployment(name, parameters={"mode": "lan"}, schedules=[Cron("* * * * *", "Asia/Kolkata")])`, `await dep.aapply()` | deployment has 1 schedule; within 30 s at least one flow run with `auto_scheduled=True`, state SCHEDULED, start in the future | `test_p1_scheduled_deployment_gets_runs` |
| P2 | rebuild with `schedules=[]`, `await dep.aapply()` with no arguments (the exact `runner.py:300` call), inside `warnings.catch_warnings(record=True)` | `read_deployment_by_name(...).schedules == []`; no recorded `FutureWarning` containing "Preserving existing schedules" | `test_p2_reserve_with_empty_list_removes_the_cron` |
| P3 | read flow runs of the deployment, `auto_scheduled=True`, state SCHEDULED, start > now; wait 5 scheduler loops (>= 10 s) and read again | none both times | `test_p3_no_scheduled_runs_after_reserve` |
| P4 | `await arun_deployment("aam-backup/backup-lan-c1probe", parameters={"mode": "lan"}, timeout=0)` | a flow run is created, `auto_scheduled is False`; started by a `Runner` serving the deployment (subprocess `serve(dep)` for <= 60 s) it reaches COMPLETED | `test_p4_manual_run_still_starts` |
| P5 | rebuild with the cron again, `aapply()` | 1 schedule; auto-scheduled runs reappear within 30 s | `test_p5_back_to_scheduled_restores_the_cron` |
| P6 | after P2, `launch._reconcile_disabled_legs(cfg, legs={"backup-lan-c1probe": True})` (its `flow_map` resolves an unknown name to flow `aam-backup`, so the dummy flow is named `aam-backup` on this throwaway server, and the P4 name is `aam-backup/backup-lan-c1probe`) | result `"active"`, deployment not paused, `schedules` still `[]` | `test_p6_reconcile_keeps_the_manual_deployment_resumed` |
| P7 | the same file on the box (D3): run against the box's Prefect with deployment name `backup-lan-c1probe` and delete it after; no production deployment is touched | P1-P6 green; logged to `agent_runs/` | manual run in D3 (tell the owner before: none of it stops a backup) |

Run on this PC: `set PREFECT_HOME` to a clean temp folder (CLAUDE.md gotcha) and
`.venv\Scripts\python.exe -m pytest audit_lab/test_c1_prefect_schedules_lab.py -q -s --timeout=900 -p no:cacheprovider -o addopts=""`.
If P2 or P3 is red: build K-C1-4 (with C1-17, C1-18), re-run the probe with the reconcile step before the re-serve.

| ID | Scenario (only with K-C1-4) | Inputs | Expected | Test name |
|---|---|---|---|---|
| C1-17 | Reconcile deletes a surviving cron | mocked client: `backup-lan` with 1 schedule, lan manual | `delete_deployment_schedule(dep.id, s.id)` per schedule; result `"cron removed (manual only)"`; not paused | R `test_reconcile_deletes_cron_of_manual_leg` |
| C1-18 | Scheduled leg's cron never deleted | cloud scheduled with 1 schedule | no delete call | R `test_reconcile_keeps_cron_of_scheduled_leg` |

### C2 Behaviour

| ID | Scenario | Inputs | Expected | Layer / test name |
|---|---|---|---|---|
| C2-01 | Manual LAN, configured FY, never ran | record `lan_scheduled: false`, no LAN rows, `now` = a week after `first_known_at` | no code `first_run:lan`, `stale:lan`, `catching_up:lan`; cloud items as baseline | U `test_manual_leg_has_no_overdue_items` (B7 C01) |
| C2-02 | Manual LAN, old success | last `LAN_COMPLETE` 30 days ago | no `stale:lan` | U `test_manual_leg_old_success_not_overdue` |
| C2-03 | Manual LAN, next FY | C2-01 + active overlap record, `now` = 15 Jan | no `first_run:lan:FY27-28`, `stale:lan:FY27-28`, `catching_up:lan:FY27-28` | U `test_manual_leg_next_fy_has_no_overdue_items` (B7 C02) |
| C2-04 | Failed manual run, both FYs | `x-lan` `LAN_FAILED`, `x-lan-FY27-28` `LAN_FAILED`, overlap active | `last_run:lan` RED and `last_run:lan:FY27-28` RED; a `LAN_MISSED` row -> AMBER | U `test_manual_leg_failed_run_is_red` (B7 C03) |
| C2-05 | Failure mail of a manual run | `lan.scheduled: false`, manual (not auto-scheduled) LAN pipeline run with `fake_run_tool` exit 16 (fixtures of `tests/test_fy_overlap_backup_flow.py:156`) | one FAILURE event key `backup:lan:LAN_FAILED` sent; row `LAN_FAILED`; flow raises (not COMPLETED) | P `test_manual_leg_failure_is_mailed` |
| C2-06 | Other items kept | manual LAN: latest audit `VERIFICATION_FAILED`; last row metrics `nas_shutdown.outcome = UNVERIFIED`, `artefacts_degraded` | `audit:lan` RED, `nas_shutdown` AMBER, `run_logs:lan` AMBER | U `test_manual_leg_keeps_audit_shutdown_and_log_items` |
| C2-07 | Schedule unreadable | manual LAN, `_schedule_in_effect` -> None (no record, config without `schedule`) | no RED `stale:lan` "schedule cannot be read" for the manual leg (its flag comes from config: false); scheduled cloud still gets it | U `test_manual_leg_no_schedule_fallback_item` |
| C2-08 | Watchdog | manual LAN never ran, cloud scheduled and past its deadline | `_check_stale_legs(cfg)` returns `["cloud"]`; one `stale:cloud` mail; no event with mode `lan` | W `test_watchdog_never_mails_manual_leg` (B7 C02 mail part) |
| C2-09 | Watchdog, both FYs | C2-03 state, cloud on time | returns `[]`, no notify call | W `test_watchdog_manual_leg_both_fys_silent` |
| C2-10 | Record wins over config | record `lan_scheduled: true`, config `lan.scheduled: false` (saved, not applied), LAN past deadline | RED `stale:lan` (the scheduler still runs it) ; reverse (record false, config true) -> no `stale:lan` | U `test_record_in_effect_decides_scheduled` (DC1) |
| C2-11 | No record | config `lan.scheduled: false`, no record | as C2-01 (fallback carries the flags) | U `test_config_fallback_carries_flags` (DC1) |
| C2-12 | Test configs without the key | `SimpleNamespace` config from `test_support/attention_cfg.py`, `MagicMock` db | output identical to a baseline call (absent = scheduled) | U `test_missing_flag_is_scheduled` |
| C2-13 | Card timing (B9 regression) | `/status` with record `lan_scheduled: false` | `lan.timing.state == "manual"`, `due_at` / `overdue_at` null; next-FY LAN card the same; cloud unchanged | re-run B9 `tests/test_fy_overlap_ui.py::test_manual_only_leg_card` (U13); no new test |
| C2-14 | Default unchanged | both legs scheduled | `compute_attention`, `leg_timing`, watchdog output equal to a call with the flags absent | U `test_scheduled_default_is_baseline` |
| C2-15 | Report, manual leg (B8 regression) | B8 R31 (empty week), R32 (failed manual run), R33 (during the overlap) | as B8 | re-run `tests/test_fy_overlap_report.py` R31-R33; no new test |
| C2-18 | Report caller passes the config (B8 regression) | `flow._send_summary_report` with `lan.scheduled: false` | the report model sees the flag (B8 R6 `config=`); a manual LAN empty week is not "no backup ran" | covered by B8 R31; if B8 did not wire the flow caller, add `test_summary_report_honours_manual_leg` here |
| C2-19 | Email next text | FAILURE event mode `lan`, `lan.scheduled: false` | `_next_text` == DC9 text exactly; scheduled LAN: today's "The next office NAS backup runs automatically on ..."; mode `audit` unchanged; a `_BLOCKED` ACTION event: today's text | E `test_next_text_manual_leg` |
| C2-20 | Email data safety | manual LAN with a last success | its line ends with `" Runs only by hand (no schedule)."`; cloud line unchanged | E `test_data_safety_names_manual_leg` |
| C2-21 | Card schedule line (B9 regression) | GET `/` and `/status` with record lan false | LAN card "Runs only when you press Run backup", cloud card "Next scheduled backup: <cron text>" (B9 Q6 `schedule_text`, template K20) | re-run B9 U13 / J03; no new test |
| C2-22 | B5 calendar | `tests/test_fy_overlap_calendar.py::test_unscheduled_lan_leg_is_never_overdue_in_the_whole_window` | passes; strict xfail marker removed in the C2 commit (B7 C04) | existing file |
| C2-23 | Regression | `test_d2_attention_latest_run.py`, `test_m5_stale_alert.py`, `test_stale_mail_dedup.py`, `test_overdue_schedule.py`, `test_m4_watchdog.py`, `test_fy_overlap_attention.py`, `test_fy_overlap_calendar.py`, report and email tests (`test_report*`, `test_email_compose*`), `test_e8_dashboard_e2e.py` | green, unchanged | existing |

### C3 Shipped configs

| ID | Scenario | Inputs | Expected | Test name |
|---|---|---|---|---|
| C3-01 | Keys explicit | `yaml.safe_load` of `config.example.yaml` and `deploy/config.install.yaml` | for `lan` and `cloud`: keys `enabled` and `scheduled` present (not via defaults) and `bool` | `tests/test_manual_only_leg.py` `test_shipped_configs_write_enabled_and_scheduled` (parametrised over both files; replaces/extends `:97-108`) |
| C3-02 | Three states explained | raw text of both files | contains each of the exact lines of the K-C3-1 block (`enabled: false`, `scheduled: true`, `scheduled: false`, "manual only", "Run backup", "Cloud only", "NAS only") before the `lan:` line | `test_shipped_configs_explain_the_three_states` |
| C3-03 | Still loads | `config.example.yaml` through the existing loader test (if one loads it) | unchanged result | existing (`tests/test_config_validation.py` / `test_fallback_defaults_from_model.py`) |
| C3-04 | Install check prints the plan | `config_help.leg_plan(cfg, "lan")` / `(cfg, "cloud")` for the three states | `"switched off"`, `"runs only when you press Run backup"`, `"runs on its schedule (<cron_to_human(<leg>_cron, tz)>)"`; `deploy/test_config.py` output (capsys, test pattern of any existing `deploy/test_config.py` test, else call its `main` with a tmp config) has one `Office NAS copy: ` and one `Online copy: ` line with these texts | `tests/test_manual_only_leg.py` `test_leg_plan_text` |

## 4. Chunks (one function per chunk; tests by one Haiku, code by another)

Signatures are fixed (Opus-defined; nobody below Opus changes them). Before each: GitNexus `impact` upstream on the
symbol (`compute_attention` / `_leg_items` HIGH: read by `ui._attention`, `/health`, the watchdog; `_deployments` is read
by `launch.main`).

| Chunk | Function / change | Signature | Rows | Scope |
|---|---|---|---|---|
| K-C1-1 | registration | `serve._deployments()` (unchanged signature): `schedules=[Cron(...)] if config.<leg>.scheduled else []`, manual description; docstring adds the manual-only rule | C1-01..C1-06 | ~10 lines |
| K-C1-2 | record flags | `serve._record_schedule_in_effect(config) -> None` (unchanged): add `"cloud_scheduled": config.cloud.scheduled, "lan_scheduled": config.lan.scheduled` | C1-07 | 2 lines |
| K-C1-3 | since on flip | `ManifestDB.set_schedule_in_effect(self, schedule: dict, since: str \| None = None) -> None` (unchanged): a flag different from `old[f"{leg}_scheduled"]` stamps `since or now_iso()` | C1-09..C1-12 | ~4 lines |
| K-C1-4 | (only if P2/P3 red) | `launch._reconcile_disabled_legs(config, legs: dict \| None = None, manual: set[str] \| None = None) -> dict` - `manual` default = deployment names of enabled legs with `scheduled` false; for those, delete every schedule via `client.delete_deployment_schedule` | C1-17, C1-18 | ~15 lines |
| K-C1-5 | probe | `audit_lab/test_c1_prefect_schedules_lab.py` (test agent writes it; no product code) | P1-P6 | ~150 lines |
| K-C2-1 | the flag | `core/attention.py`: `leg_scheduled(sched: dict \| None, config, leg: str) -> bool` - `sched[f"{leg}_scheduled"]` when the key is in a dict, else `getattr(getattr(config, leg, None), "scheduled", True)`; result `is not False`; never raises. Plus `_schedule_in_effect` config fallback (`:51-54`) adds `cloud_scheduled` / `lan_scheduled` from the config the same way | C2-10..C2-12 | ~12 lines |
| K-C2-2 | banner | `_leg_items(db, config, leg, sched, now, fy=None, activated_at=None, active=_READ) -> list[dict]` (unchanged): when `not leg_scheduled(sched, config, leg)` skip the whole overdue block (`:275-283`); the rest unchanged | C2-01..C2-12, C2-14, C2-22 | ~3 lines |
| K-C2-6 | email | `core/email_compose._next_text(config, event, fault, now) -> str` and `_data_safety(config, db, now) -> list[str]` (unchanged signatures): DC9 texts, read `getattr(getattr(config, event.mode, None), "scheduled", True) is not False` | C2-19, C2-20 | ~8 lines |
| K-C2-8 | B5 file | remove the strict xfail marker of `test_unscheduled_lan_leg_is_never_overdue_in_the_whole_window` (Opus edits this, not the code agent) | C2-22 | 2 lines |
| K-C3-1 | example config | `config.example.yaml`: above `lan:` (`:32-35`) the block below; comment on each `enabled:` line; keep `scheduled: true` comments | C3-01, C3-02 | docs |
| K-C3-2 | install config | `deploy/config.install.yaml`: same block above `lan:` (`:20`) | C3-01, C3-02 | docs |
| K-C3-3 | plan text | `core/config_help.py`: `leg_plan(config, leg: str) -> str` (C3-04 texts; config only, uses `core.time_utils.cron_to_human`); `deploy/test_config.py:61-62` prints `Office NAS copy: <plan>` / `Online copy: <plan>` after the existing lines | C3-04 | ~12 lines |

Block for K-C3-1 / K-C3-2 (exact text; C3-02 checks these lines):

```yaml
# WHICH BACKUPS RUN, AND HOW - the same three choices for lan: and cloud: below
#   enabled: false                    = this backup is OFF (never runs, no alerts about it)
#   enabled: true,  scheduled: true   = runs by itself on its schedule (schedule: lan_cron / cloud_cron)
#   enabled: true,  scheduled: false  = manual only: runs when you press "Run backup" on the dashboard
# Cloud only = lan.enabled: false. NAS only = cloud.enabled: false. Both = both true.
# At least one switched-on backup must be scheduled (the settings are refused otherwise).
```

Order: K-C1-1 -> K-C1-2 -> K-C1-3 -> K-C1-5 probe (decides K-C1-4) -> [K-C1-4] -> C1 commit; then K-C2-1 -> K-C2-2 ->
K-C2-8 (flip, same commit as K-C2-2) -> K-C2-6 -> C2 commit (re-run the B8/B9 rows C2-13/15/18/21); C3 (K-C3-1..3)
its own commit. After K-C2-2 run every C2-23 file. Findings for D1 (`docs/ARCHITECTURE.md` gate R12): F-C1-1 (flags
never recorded before C1), DC3 (flip starts the clock), DC4 (probe is the Prefect-upgrade guard), DC6 (no flow guard,
and why).
