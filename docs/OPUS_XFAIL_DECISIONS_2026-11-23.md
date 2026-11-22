# Decisions on the 12 xfailed tests (Opus review, 2026-11-23)

Scope: the 12 `xfail(strict=False)` tests listed in the task. For each one I read the test and the production code.
Context: `docs/OVERLAP_REVIEW_2026-11-22.md` (D1-D14), `docs/RELEASE_BREAK_SESSION_2026-11-22_PART2.md`/`PART3.md`,
and plan rev 4 (binding). No code was changed. I judged every item on two things: the harm a customer would
actually see, and the risk of changing the code just before release. The real deploy is a **fresh install**
(no old DB). Overlap first matters on **2027-01-01** (activation) and again on **2027-04-01** (switch).

Verdict words: FIX NOW = before this release. FIX BEFORE <date>. ACCEPT = turn the test into a documented
characterization test, or delete it. OWNER = a business rule only the owner can set. My recommended option is given.

---

## A1, A2 - `purge_older_than(days=0 | -1)` deletes today's folder -> FIX NOW (bundled with A3, A4)
- Code: `core/run_artefacts.py:263`. When `days <= 0` the cutoff is now or later, so every entry goes, including
  the folder of a run that is still in progress.
- Reachable? Not through config: `run_logs.retention_days` and `service_log_retention_days` are bounded `ge=7, le=3650`.
  It is a defence-in-depth gap in the one shared retention function, which other callers will reuse later.
- Why now anyway: the function is pure, has 2 production call sites (`flow.log_retention_task`, flow.py:2471/2474)
  and many unit tests. The fix cannot change behaviour for any valid value. Blast radius is about zero.
- Design: at the top, `days_i = int(days)` inside `try` (on TypeError, ValueError or OverflowError: warn and return 0).
  If `days_i < 1`: `logger.warning(...)` and return 0 (fail closed: refuse and keep everything). No config key:
  this is an invariant, not a tunable. The config bounds stay the real limit.
- Test: remove the xfail marks so both cases are hard tests. Add an assert that `removed == 0`.

## A3 - absurd retention raises OverflowError -> FIX NOW (same commit)
- `cutoff = now - int(days)*86400` raises outside the try when `days=10**400`, although the docstring says
  "Never raises". The caller wraps the call in `try` (flow.py ~2470), so the only harm is a WARNING line.
- Design: compute the cutoff inside `try/except OverflowError` and return 0 with a warning.
  `test_huge_retention...` (`10**9`, line 162) must stay green.

## A4 - an old loose file at the run-logs root is deleted -> FIX NOW
- Code: run_artefacts.py:273-274. Any old plain file that matches the glob is unlinked. The run-logs call uses
  glob `*`. Only the service-log calls (`agent_svc-*.log` etc.) need file deletion.
- **This is more than cosmetic.** `run_logs.dir` is a free config key with no validator (models/config.py:694,
  `run_logs_dir` at 1235). If someone sets it to `C:\BackupAgent` (runtime dir: GCS key, rclone.conf), to a share,
  or worst of all to `D:\` or `D:\FY26-27`, then every plain file at that level that is older than 90 days is
  deleted once a day. In the live FY folder, `/MIR` then carries the deletion to the NAS. That is a data-loss path
  from a one-key mistake, in the function whose comment says "only ever remove our own day folders".
- Design: add the keyword `files: bool = False` to `purge_older_than`. When it is False, plain files are skipped
  (only `_DATE_DIR` folders are removed). `flow.log_retention_task` passes `files=True` for each `_SERVICE_LOG_GLOBS`
  pattern. The run-logs call stays as it is.
- Existing tests that encode the old API: `tests/test_m1_tool_runner.py:114` (service-log files) needs `files=True`,
  a one-word change that is allowed because it states the new contract. `test_f1_run_artefact_names.py:48` and
  `test_ov3_run_artefacts_fy.py:88` delete day folders and stay green. Add one test that runs `log_retention_task`
  on a sandbox where an old `agent_svc-1.log` is removed and an old `notes.txt` in the run-logs root is kept.
- What could regress: old NSSM logs stop being purged if the flag is forgotten at a call site. The new
  `log_retention_task` test covers that.
- Follow-up (owner, before 2027-01-01, not this release): a validator that refuses a `run_logs.dir` equal to, or
  inside, the source parent, the NAS path or the runtime dir root. Add it to "Found while building".

## B - launch continues to `prefect serve()` on an old-schema DB -> ACCEPT for this release (one check pending)
- Code: launch.py:123-141 catches everything as "non-fatal".
- Not reachable on the fresh install: v6 is created fresh. Someone would have to copy a v5 `manifest.db` in.
- The test's expectation (exit non-zero before serve) is **not what I want**. Exiting puts the agent in an NSSM
  restart loop, takes the dashboard down with it, and leaves only the watchdog to speak. Its `_notify` also needs
  the DB. Staying up is the better fail-closed state if every flow then refuses. `_record_running` hits the DB
  before any sync, so `ManifestSchemaError` stops the run before any transfer. `notify()` then falls back to
  `_fallback_alert` (D1 short notice, no DB, notify.py:532-571), so the human should get one mail per failed run.
- **Check before closing the item:** live runbook LS8b (old-schema DB at boot, PART3 TODO 3). Confirm that (1) no
  sync starts and (2) a short-notice mail arrives on the first scheduled run.
  - If LS8b shows no mail (silence): FIX BEFORE 2027-01-01. Give the watchdog a raw-config mail like SILENCE-2
    (`watchdog._alert_invalid_config`): "manifest.db has an old schema - rename it". Use a marker file and the
    cooldown. Do not exit the launcher.
  - If LS8b shows a mail: rewrite the test as a characterization test. `main()` reaches `serve()`, the boot output
    contains "schema version 5", and the DB file is byte-identical with no sidecars.

## C - `restore_fy()` defaults to `forbidden_paths=()` -> ACCEPT AND DOCUMENT
- The only production caller is `restore.py:238`, and it always passes `_forbidden_paths(config) + --protect`. That
  list holds the live source folders and the NAS twins and follows the overlap switch (`test_ov10_tools_active_fy`,
  `test_nas_twins_...`). `upload_fy.py` passes the protected list too.
- Harm today: none. The engine is only reached through the CLI. Making the argument required now would break
  dozens of rig tests that call the engine directly (`_restore(rig, ...)`). That is a high-churn change for no
  customer gain this release.
- Rule to record (ARCHITECTURE file map, restore row): "any new caller of `restore_fy`/`restore_from_cloud` (for
  example a dashboard restore) must pass `fy_overlap.protected_folders`; make the parameter keyword-required in
  that same commit". This is a FIX BEFORE trigger, not a date.
- Test: delete the xfail. In its place, add one CLI test that patches `restore_cli.restore_fy` with a MagicMock,
  runs `main([...])` and asserts that `forbidden_paths` contains the configured live source folder (and the early
  folder while overlap is on).

## D8 - `close_fy` closes only cloud/lan incidents -> FIX BEFORE 2027-04-01 (XS, may ride with batch 1)
- Plan §4/§5.4 (binding): at the switch the old FY's "incidents are closed silently". manifest.py:1245 hard-codes
  `("cloud", "lan")`. This is a bug inside the plan's intent, not an owner rule.
- Harm: low. An FY-less audit event (`fy=ALL`) can thread into a dead FY26-27 audit incident. It is only reachable
  from 1 April 2027.
- Design: change the tuple to `("cloud", "lan", "audit")`. **Do not** close `rollover` or `rollover_files`:
  1. D3 already ends the activation incident with its own RESOLVED mail (mode `rollover`).
  2. The rollover's own follow-ups (archive retries, mode `rollover`) can still mail for FY26-27 after the switch.
  3. D2's `_close_finished_old_fys` re-runs `close_fy` every morning. If rollover-mode incidents were closed, every
     archive-retry mail would look like the first mail of a new incident, and the reminder limits would never apply
     (mail flood).
  After the switch no audit of FY26-27 runs (P-13), so re-closing audit every day is a no-op.
- Test: remove the xfail. Add one assert that a FY26-27 `rollover`-mode SENT row is **not** closed. That pins the
  boundary.

## D12 - raising `early_start_month` after activation drops the early FY -> OWNER (decide before 2027-01-01)
- Code: fy_overlap.py:127-134. `in_window` reads the month on every run. Plan D-1 defines the window as "from
  `early_start_month` until the switch". Which way it should work is a business rule, so the owner sets it.
- Harm: FY27-28 stops being backed up until the new month. Its card, approvals and overdue disappear. It is not
  silent: the sibling alarm mails daily, but with the misleading text "only backed up from March on". It needs a
  Settings change by an admin.
- Options:
  - (a) Once activated, a year stays active until the switch. Only `overlap_enabled=false` stops it.
  - (b) The Settings page refuses the change while this cycle's activation row exists.
  - (c) Keep it as it is and document it.
- **Recommend (a).** It is the fail-closed choice: data keeps being protected. It needs no UI work. It also removes
  most of the D10 cases.
- Design for (a): in `active_folders` (fy_overlap.py:159) keep the existing condition, and add a case for the
  configured calendar FY in month 1..3 with an activation row for `next_fy`: then the year is active even when
  `in_window` is False. `in_window` itself does not change, so activation timing, `deferred_state` (D3) and
  `ensure_overlap_activated` keep honouring the month. No config key.
- Test: the xfail becomes hard. Keep `test_p10_control...`. Add one test where `early_start_month` is raised
  **before** activation: no activation and no deferred alarm in February. Check the effect on D9: approvals of
  FY27-28 stay runnable, which is correct.

## D14 - the second folder's run row is dated at its own pipeline start -> FIX BEFORE 2027-01-01
- Code: flow.py:3372 passes `now_iso()` per folder, while the loop already has `started_at` (3336). Plan §3: one
  date per flow run.
- Harm: from 1 January, any LAN night where the first folder (the early FY, which runs first) crosses midnight
  writes the second folder's row (the configured FY) with the next day's date. The weekly or monthly report then
  shows a "no backup ran" night that did run (a false alert), plus a double the next day. On 31 March this
  falsifies the **final monthly report of the closing FY**. It is likely on big Jan-Mar nights.
- Design: pass the loop `started_at` instead of `now_iso()` at 3372. Duration is unaffected (monotonic,
  `_record_run` 2298). Run ids and artefact folder names already contain the FY, so they cannot collide.
- To check: grep the LAN and cloud pipelines for `started_at` used in an `fy=ALL` query, for example
  `sent_in_mode_after` or `told_about_leg_since` with global keys. `nas_shutdown` is decided at loop level, so it
  is not affected. If such a query exists, it now looks a little further back, which is harmless (more inclusive).
- Test: the xfail (row assertion and March report 31/31) becomes hard. Regression risk: low. No existing test
  pins the per-folder timestamp (only `test_break_flow`/`test_ov_review_points` touch `_run_leg_folders`).

## D5 - the early FY's NAS folder and marker are re-created on every LAN run -> FIX BEFORE 2027-01-01
- Code: fy_overlap.py:614-626, called from flow.py:2057 for the early FY on every LAN night.
- Why this is a fix and not an owner rule: G5 (the mount canary) is an owner-approved safeguard. For the configured
  FY a vanished or empty destination already blocks with AAM-NAS-09 "approve to initialise" (guards.py:262).
  Re-writing the marker every night disables that safeguard for the early FY only, and the early card shows a
  green mirror onto a reset or wrong volume. Project rule: wrong-folder writes must ask. The fix only makes the
  early FY behave like the configured FY; it adds no new rule. **Tell the owner** in the report.
- Design (no schema change): give `ensure_overlap_nas_folder` a parameter `established: bool`. The flow computes
  it before the call: "this FY has at least one LAN success row" (use the existing per-FY
  last-successful-run query on `ManifestDB` and check its name). When `established` is True:
  - missing folder: create nothing and return `needs_confirmation`, reason "NAS folder of an established year
    vanished";
  - empty folder without marker: write nothing and return `needs_confirmation`.
  G5 then asks with AAM-NAS-09/10, and approving re-creates the marker through the existing approval path. On the
  first nights (no success yet) the behaviour stays as it is today. The configured FY is unaffected.
- To check: robocopy `/MIR` creates a missing destination. Confirm that G5 (or the LAN preflight) runs **before**
  robocopy on a missing destination. P-3 asserts this end to end (`last_b != LAN_COMPLETE`, A still complete).
- Tests: the P-3 xfail becomes hard. `test_ov2_activation.py:357-404` stays valid with `established=False` (the
  default). Add `established=True` with a missing folder and with an empty folder, both expecting
  `needs_confirmation` and nothing written. Regression risk: the T-a4/T-b2 timelines (first night creates). Run
  `test_ov_timeline_a/bc` and lab `test_w25_fy_overlap.py`.

## D10 - the report window only ends at a config switch -> ACCEPT AND DOCUMENT
- The case: overlap switched off mid-window (typed phrase), or the clock moved back. After D12(a), raising the
  start month no longer causes it. In the transition week the report counts FY27-28 nights after the switch-off as
  "no backup ran". The dashboard shows no card, but it does show the daily sibling ACTION REQUIRED item.
- The report is the **truthful, loud side**: those files really were not backed up. Making the two agree needs a
  durable "left the active set at" record, which does not exist. Using the last run row as the window end would
  hide real missed nights (fail open). The week after already has no section (`test_T_e3_week_after...`, green).
- Test: rewrite as a characterization test. The transition week report `act is True` names FY27-28's missing
  nights. Docstring: "intended - switching overlap off leaves that year's files unprotected; the report says so
  until the week is past". Put one line in the owner report.

## D7 - a final-backup slot timeout becomes an approvable G6 "roll over anyway" -> OWNER (decide before 2027-04-01)
- Code: flow.py:2986-2989 (non-GuardBlocked becomes `<LEG>_NOT_COMPLETE`). G6 (guards.py:290-300, owner DEC-4)
  asks a human for anything that is not a clean final backup.
- Facts from `test_P7_control...` (green): the switch is already held, the rows say `*_SLOT_TIMEOUT`, and the next
  morning completes on its own. There is no automatic data loss. The risk is a human who approves "roll over
  anyway" and so skips FY26-27's last LAN copy for a problem that waiting would fix.
- Options:
  - (a) Keep it as it is. The text already says "the daily check retries".
  - (b) A final backup that only failed to get the slot (`*_SLOT_TIMEOUT`, or `*_MISSED`) is a WARNING "retry
    tomorrow" with no approval for the first N mornings. After that, G6 asks as it does today.
  - (c) Let the final backup wait longer for the slot.
- **Recommend (b), N=2.**
- Design for (b): `_run_final_backups` keeps the pipeline's recorded status (`<LEG>_SLOT_TIMEOUT`) instead of
  mapping it to NOT_COMPLETE.
  - In the rollover, before `g6_fy_rollover`: if every failed leg is SLOT_TIMEOUT/MISSED and the count of earlier
    `-final-` rows of that FY with that status is below `rollover.final_backup_wait_retries`, then leave
    FINAL_BACKUPS pending, `notify` a WARNING (key `rollover:<fy>:final_wait`, mode `rollover`), and return
    without opening an approval.
  - Config key `rollover.final_backup_wait_retries: int`, default 2, `ge=0, le=7`. 0 = today's behaviour.
  - Tests: P-7 becomes hard. Add a case where N mornings pass and G6 then asks. `test_P7_control` stays green.
  - Risk: medium, because it touches the rollover state machine. Write it only after the owner says yes. Run the
    T-c/T-d/T-f timelines and lab w25.

---

## Interactions checked
- D8 is limited to `audit`, so it never touches D3's rollover-mode RESOLVED or the archive-retry incidents that
  D2's daily re-close would otherwise reset.
- D12(a) leaves `in_window` unchanged, so D3 `deferred_state` and activation timing do not change. It shrinks the
  D10 surface and keeps D9 approvals of the early FY valid.
- D5 reuses G5 as it is (AAM-NAS-09/10 approval re-creates the marker). It has no effect on G6 or D7.
- D14 changes only the timestamp that is passed in. The budget (`budget_monotonic_start`) and the NAS vote are
  unchanged.
- A1-A4 change only `purge_older_than` and one call site. While the audit lab is running, do not edit modules it
  imports.

## Summary

| Item | Verdict | Priority | Effort | Risk | Reason |
|---|---|---|---|---|---|
| A1/A2 days<=0 | FIX NOW | P1 | XS | very low | fail closed in the shared delete function; unreachable via config |
| A3 overflow | FIX NOW | P1 | XS | very low | "never raises" contract; same commit |
| A4 loose files | FIX NOW | P1 | S | low | a misset `run_logs.dir` deletes customer files older than 90 days |
| B old schema boot | ACCEPT (pending LS8b) | P3 | S | - | fresh install; staying up plus a short-notice mail beats an NSSM crash loop |
| C restore default | ACCEPT AND DOCUMENT | P4 | XS | - | the only caller passes the list; make it required when a 2nd caller appears |
| D8 audit incident | FIX BEFORE 2027-04-01 | P2 | XS | low | plan §5.4 says all incidents close; exclude rollover modes |
| D12 start month | OWNER, rec. (a) | P2 | S | low | keep protecting an activated year until the switch |
| D14 row date | FIX BEFORE 2027-01-01 | P2 | XS | low | false "no backup ran" in reports, incl. the FY's last March report |
| D5 NAS recreate | FIX BEFORE 2027-01-01 | P2 | M | medium | the early FY bypasses the G5 canary; align it with the configured FY |
| D10 report window | ACCEPT AND DOCUMENT | P4 | XS | - | the report is the truthful loud side; matching the dashboard would fail open |
| D7 G6 on slot wait | OWNER, rec. (b) N=2 | P3 | M | medium | stop inviting "roll over anyway" for a wait; touches DEC-4 |

## D10 re-decision (2026-11-23, after the engineer's finding) -> FIX BEFORE 2027-01-01 (replaces ACCEPT)
- **Correction:** my premise was wrong. For the week 18-24 Jan, the report says `act=False`, "all backups OK",
  and has an empty problems list. The gap is visible only as `scheduled=7` vs `total=3`. That is false assurance
  (R13) while the year's files really are unprotected.
- Why: `report_headline` (report.py:249-273) flags a leg only when `total == 0` with nights scheduled, or when there
  is an open problem. A shortfall (`total < scheduled`) is never a problem. That is true for the configured FY too.
- Options considered:
  - (a) Keep ACCEPT: rejected, because the report states something false.
  - (b) A general "scheduled > total = act": right in principle, but it gives false alerts in the install week
    (`fy_window` has no start for the configured FY, so the whole week counts as planned) and for a run that starts
    after midnight at the end of a period. It needs its own design. See the new finding below.
  - (c) End the window, or say "no longer backed up": ending the window needs a "left at" time that is not stored.
    The statement itself needs none.
- **Decision: (c) as a statement, with the window left as it is.** An FY that has a section in the report, is not
  the configured FY, is not in the active set now, and has no finished `config_switch` from it, is a problem.
- Minimal design:
  1. `fy_sections` already calls `fy_overlap.active_folders`. Return that active set, or compute it once in
     `build_report_model` with the same call. Do this only when `config` is given.
  2. On each summary of such a section, set `s["not_backed_up"] = True`.
  3. In `report_headline`, add a first loop: `action needed: <FY> is no longer backed up - files saved in it are
     not protected` returns `True`.
  4. The section detail in the renderer gets the same sentence.
  5. `fy_window` stays unchanged. No config key.
- Cases: a closed FY after the April switch has a finished switch, so it is not flagged. During a pending switch
  both years are active. With no config (old callers and tests) nothing changes.
- Tests: `test_T_e3_d10_*` becomes a hard test: `act is True` and the headline names FY27-28 and "no longer
  backed up". `test_T_e3_week_after...` stays green: that week has no FY27-28 section, and the daily sibling
  ACTION REQUIRED is the channel there, which is acceptable. Run all report and `test_ov9*` tests, plus T-c4 (the
  post-switch report must not flag FY26-27).
- Risk: low. It changes only the headline when config is given and a non-active, non-switched FY has rows.
- **New finding for the owner (before 2027-01-01, not this release):** the general shortfall rule (b). Today an
  agent that was down for 4 of 7 nights gives a weekly report that says "all backups OK". Overdue and watchdog
  mails went out at the time, but the report is wrong. A safe design needs:
  - a window start at the first run row ever (install week);
  - a tolerance of 1 run at the period edge, or counting runs by scheduled night.
  Add it to "Found while building".

## Report shortfall rule (2026-11-23) -> FIX BEFORE 2027-01-01 (one rule for every FY; D10 is its special case)
- **Gap:** `report_headline` (report.py:249-273) flags a leg only when it has no run at all. If 4 of 7 nights
  have no run row (agent or scheduler down, box off), the weekly report still says "all backups OK". The
  overdue/watchdog mails warned at the time, but the report is the owner's record of whether every night happened.
  That is R13 (false assurance).
- **Rule:** for each section FY and each reported leg, when `scheduled` is known, `missing = scheduled - covered`.
  If `missing > 0`, the headline says `action needed: <leg> (<FY>) - N of M scheduled nights have no backup
  record`, and `act` is True. This check comes right after the existing "no backup ran" check and before
  open problems. A night that has a row with a bad status (MISSED, BLOCKED, FAILED, SLOT_TIMEOUT, ABANDONED)
  counts as covered, because the existing problem logic already reports it. The new rule catches only nights
  with **no record**.
- **How a night counts as covered:** take each cron firing `f` in the section window (the same
  `overdue.firings_between` / CronSim as today). It is covered when a run row of that leg and FY has `started_at`
  in `[f, f + budget.<leg>_max_hours + dashboard.stale_grace_hours)`. A late-scheduled run or an approval re-run
  of that night therefore counts. Match runs to firings one-to-one, in time order.
  - To do this, the runs for the section are read up to `window_end + budget + grace`, not only up to the period
    end. That way the last night of the period, which runs after midnight, is still found. D14 makes a midnight
    crossing harmless too.
  - This reuses existing keys (the overdue logic already uses this budget plus grace), so it needs **no new
    config key** and **no tolerance**. A tolerance of 1 would hide exactly the single missed night the owner
    wants to see.
- **Window start and end (to avoid false alarms):**
  - Early FY: the window starts at activation. This is already in `fy_window`, and
    `test_first_january_weekly_counts_from_activation` (3 of 3) shows it.
  - Closed FY: the window ends at the switch (already in `fy_window`).
  - **New, for the install week:** for each (leg, FY), the window also starts no earlier than that leg's first
    run row ever for that FY. Add a new `ManifestDB.first_run_started_at(leg, fy)` (a MIN query; `fy` is
    required, per plan §5.4). This covers a fresh install mid-week and a leg switched on mid-week.
  - A leg with no row ever keeps today's "no backup ran" rule, so an install that never ran still says so.
  - A leg that was switched off and on again within the period: the nights while it was off count as missing.
    That is loud but true, so it is accepted.
- **D10 folded in:** when the FY with missing nights is not configured, not active now, and was not switched, use
  the wording `<FY> is no longer backed up - N of M nights have no record`. This replaces D10's separate check,
  so there is one code path.
- **Scope:** `build_report_model` applies to weekly, monthly and the on-demand dashboard report (ui.py:1621-1705),
  because they all render the same model. When `schedule` is None, `scheduled` is unknown, so the rule does not
  run and old callers are unchanged.
- **Tests:**
  - Fixtures that are already complete stay green: `test_ov9_reports_per_fy.py` (7/7, 3/3, 31/31, 0 scheduled),
    and P-11 (31/31 after D14).
  - `test_m5_reports_plain.py` passes no schedule, so it is unaffected.
  - `test_break_release_reports.py` only asserts "not all OK", which is the same direction as the new rule.
  - Check the `test_ov_timeline_def.py` report cards (lines 335 and 462: T-c4, T-e3). Complete any fixture that
    leaves a night without a row while asserting OK, and do not weaken any assertion.
  - Run `pytest tests -k "report or ov9 or timeline"`.
  - New hard tests:
    1. 4 of 7 rows: act is True, and the headline names the leg and FY with "4 of 7".
    2. Install on Wednesday with first rows from Wednesday: OK.
    3. Sunday 21:01 run that starts Monday 00:30: covered.
    4. A blocked night: still the existing problem line, no double count.
    5. D10 week: "no longer backed up".
- **Risk:** medium-low.
  - It changes the verdict of every report that has a schedule.
  - A false alarm is possible only if a row's `started_at` falls outside budget plus grace of its firing. That
    happens after a manual run at an odd hour while the scheduled run did not happen, and in that case the
    alarm is true.
  - The first days after activation are handled by `fy_window`. The first days after install are handled by the
    first-run start.
- **Why not FIX NOW:** the release runs a single FY, and the overdue/watchdog mails cover outages in real time.
  The change touches the verdict of every report and needs its own test round. **Why before 1 Jan:** from
  activation onward there are two FYs and the D10 case, and the report becomes the only per-FY retrospective.
  Effort M (one DB method, matching in `leg_summaries`/`_scheduled_counts`, one headline branch, renderer line).

## Ordered fix list (Sonnet engineer, own worktree, one commit per item, test first)
1. **A1-A4** `core/run_artefacts.purge_older_than`: days validation, overflow-safe cutoff, `files=False` keyword;
   `flow.log_retention_task` passes `files=True` for service logs; update `test_m1_tool_runner.py:114`; un-xfail
   the 4 tests; add the `log_retention_task` test. Run the full unit suite. **This release.**
2. **D14** flow.py:3372, pass `started_at`; un-xfail P-11; do the grep check. (Batch 1, before 1 Jan.)
3. **D8** manifest.py:1245, add `"audit"`; un-xfail P-8 and add the rollover-not-closed assert. (Batch 1.)
4. **D5** the `established` parameter plus the flow call site; un-xfail P-3; new unit cases; then
   `test_ov_timeline_a/bc`, `test_ov2_activation`, lab `test_w25`. (Batch 1.)
5. **D12** only after the owner picks (a): `active_folders` activated case; un-xfail P-10. (Batch 1.)
6. **C** convert the test as described; add the ARCHITECTURE restore-caller rule. **D10**: see the re-decision above
   (headline "no longer backed up"; un-xfail T-e3-d10). Batch 1, before 1 Jan. Implement it as part of the
   **Report shortfall rule** (one path, D10 wording), after D14.
7. **B** after LS8b: either a characterization test, or the watchdog old-schema mail (before 1 Jan).
8. **D7** only after the owner picks (b): config key, rollover deferral, P-7 hard. (Before 1 Apr.)
Every batch: full unit suite, then the audit lab in the background, then `detect_changes`, then merge, then
redeploy the test stack, and record the owner decisions in the task list's "Found while building".
