# HANDOFF - FY overlap on the baseline (written 2026-11-24, end of session)

Read this first in the next session, then `docs/PROGRESS_BASELINE_OVERLAP_2026-11-23.md` (older facts), the binding plan
`docs/PLAN_BASELINE_OVERLAP_2026-11-23.md` (rev 2 FINAL) and `CLAUDE.md` (build process, escalation, scope guard).

## Where
- Worktree `C:\Users\malliath\Desktop\aam_baseline_overlap`, branch `baseline-v1-overlap` (cut from tag `baseline-v1` = 9461ac2 = trial box version).
  The main checkout `C:\Users\malliath\Desktop\aam_backup` (branch `fy-overlap`) is reference only. Not pushed to git (push only when the owner asks).
- No `.venv` in the worktree. Tests: cwd = worktree,
  `PYTHONPATH=. C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe -m pytest <files> -q -p no:cacheprovider -o addopts="" --timeout=600`. Full suite about 5.5 min.
  Known NOT ours: `tests/test_v113_round3.py` (2 tests, need the untracked real config.yaml). Two tests in
  `tests/test_flow_orchestration.py::TestCloudPipelineSkipsRecordOnManifestError` fail only in a combined run (order dependence; an Opus review was checking whether it pre-exists).
- Project plugins/skills irrelevant to this repo were switched off in `.claude/settings.local.json` (gitignored, both folders); restart Claude Code to apply.

## Owner rules and permissions (all recorded in memory too)
- Expectations: the new FY folder is created on 31 Dec with NO blockers (whatever the backup state); both FYs are backed up until 1 April; the switch to the single latest FY only after the old FY's final backup AND verification; config must show which legs are scheduled/selected (cloud only / LAN only / both / manual only) and the behaviour must honour it.
- Models (owner, 2026-11-24): Sonnet 5.5 at effort medium builds core logic and its tests (two SEPARATE agents: tests vs code); Haiku at medium only for mechanical work (docs, config comments, running suites, lint, search); Opus writes test matrices, decides and reviews. Pass `effort: medium` on Agent calls. Escalation: a chunk failing twice -> stop and report; builders never edit tests; no commits by builders (the main session commits after checking `git diff --stat` against scope).
- Permissions: may build, test, delete test files, deploy to the trial box (`ssh innoviztawinserver2016`) and restart SERVICES there. NEVER reboot/shut down a whole system (box or this PC), never power off the NAS. Tell the owner before a service restart on the box; follow the deploy runbook (no backup running, copy Prefect DB + manifest DB first, order: AamPrefectServer, AamBackupAgent, AamWatchdog).
- Never open SESSION_HANDOFF.md, flow.py, ui.py whole in subagents (token bloat): grep / offset+limit.

## Done and committed (newest first)
| Commit | Task |
|---|---|
| d1fbebe | D2 audit lab `audit_lab/test_fy_overlap_lab.py` (7 scenarios, real robocopy/rclone, passed twice) + `audit_lab/test_c1_prefect_schedules_lab.py` (P1-P6 pass) + `docs/TESTMATRIX_D_2026-11-24.md` |
| e6239ba | B8 reports per (leg, FY). Renamed first param of `send_summary_report/send_weekly_report/send_monthly_report` from `config` to `notifications` (new `config=` keyword). Opus review of this commit was RUNNING when the session ended. |
| 5e1da37 | C1 + C3: `serve` registers manual-only leg without schedule, records `cloud_scheduled/lan_scheduled` (bug F-C1-1 fixed), flag flip restarts the overdue clock, `launch._reconcile_disabled_legs` removes the old cron (harmless safety: the probe proved Prefect 3.9.4 deletes it itself), shipped configs explain off/scheduled/manual only, `core/config_help.leg_plan`, `deploy/test_config.py` prints the plan |
| f9ea772 | B7 attention / overdue / watchdog per FY; Opus review fixes 2-7 applied |
| earlier | A1-A6, B1-B6 (see PROGRESS doc) |
Suite at B7 commit: full unit suite exit 0. Not re-run after B8/C1/C3.

## In flight when the session ended (check `git status` first; uncommitted work may exist)
1. **B9 dashboard API + UI** - Sonnet builder was editing `ui.py`, `core/attention.py`, `core/fy_overlap.py`, `static/css/dashboard.css`, `templates/dashboard.html` (uncommitted). Tests: `tests/test_fy_overlap_ui.py` (66 tests; 14 pass on baseline, 52 must go green), matrix `docs/TESTMATRIX_B9_2026-11-24.md` (uncommitted, untracked). Builder rules: no test edits, no commits. If its work is half done: run `tests/test_fy_overlap_ui.py`, finish remaining chunks with a new Sonnet builder, then commit.
2. **C2 behaviour tests** - a Sonnet test-writer was appending C2 rows to `tests/test_manual_only_leg.py` and removing the strict xfail marker of `test_unscheduled_lan_leg_is_never_overdue_in_the_whole_window` in `tests/test_fy_overlap_calendar.py`. C2 production code is NOT built yet: banner, watchdog, emails for an unscheduled leg (no overdue/first-run item, no watchdog mail, failed manual run still red + mail). Build it AFTER B9 (shares the scheduled-flag helper in `core/attention.py`, matrix DC8). Matrix: `docs/TESTMATRIX_C_2026-11-24.md`.
3. **Opus review of B8 (commit e6239ba)** - result unknown; if lost, re-run it (check: baseline reports byte-identical, all callers of the renamed params, no loosened asserts).

## Remaining work, in order
1. Finish B9 (tests green) -> Opus review of the B9 diff (also re-check the decision that "Run FY rollover now" is allowed when next-FY activation is due but not done) -> fixes -> commit.
2. C2 build -> flip the last strict xfail -> commit.
3. Full unit suite (about 5.5 min), then targeted audit lab: `audit_lab/test_fy_overlap_lab.py` and `audit_lab/test_c1_prefect_schedules_lab.py` (clean PREFECT_HOME, command in CLAUDE.md; skip the full lab). Also `audit_lab/test_w6_flow.py`, `test_w6b_rollover_nas_off.py`, `test_w19_ui.py` (they share rigs).
4. Tick B7-B9, C1-C3, D2 in the plan section 9; commit the matrices (`TESTMATRIX_B9`, `_C`, `_D`, `_B8` are committed or listed above; B9's is still untracked).
5. **D1 docs** (Haiku medium): per `docs/TESTMATRIX_D_2026-11-24.md` section D1: `docs/ARCHITECTURE.md` sections 2, 3.1, 5.1, 5.2, 10.7 (gates R9 activation, R10 overlap passes, R11 final-copy rule, R12 manual-only legs), `docs/EMAIL_CONTENT_SPEC.md` (keys, AAM-ROL-07), `SESSION_HANDOFF.md` block. Record the accepted gaps: F4 NAS shutdown gap (owner accepted), B7 Q10 (after the April switch the new configured FY's queries return old-FY rows for about a day), B7 Q1/finding 1 (activation text says 'the mail gives the reason', the mail body is not stored), plan section 11 and G4, the free slot between FY passes.
6. **D3 trial box** (owner allowed deploy + service restarts; never a system reboot): read the D3 section and risks K1-K13 of the D matrix first. Key facts: setting `overlap_activate_on` back to `12-31` does NOT end the test overlap (the `overlap:FY27-28` record keeps it on; stamping `ended_at` would block the real 31 Dec activation); cleanup order = date back, then restore the DB copy taken just before activation, then delete the FY27-28 source folder, NAS folder and GCS prefix; rollback must restore the pre-deploy `config.yaml` (old code refuses new keys). There is no test-prefix key: real writes to `gs://<bucket>/FY27-28`. Run Prefect probe P7 on the box (re-serve with schedules=[] removes the old cron). Deploy by git bundle piped over ssh (recipe in SESSION_HANDOFF 2026-10-22 block); remote PowerShell: `;` not `&&`, UTF-8 console. Tell the owner before restarting services and before anything that touches backup data.

## Owner decisions still open
- None blocking. Owner-only items listed in the D matrix section 6 (D3 go and restarts; restore DB vs delete test rows; real FY27-28 cloud writes; real NAS shutdown during the test = NOT allowed by the owner's rule; test mails; optional baseline night 0; deleting test folders) were largely pre-approved on 2026-11-24 (everything except system restart/shutdown); the NAS shutdown stays forbidden.

## Facts found this session
- Prefect 3.9.4 probe (this PC, clean PREFECT_HOME): re-serving with `schedules=[]` deletes the old cron and its 61 future runs; manual `arun_deployment` still runs; re-adding the cron restores them. P7 (box) not run.
- `format_ist` appends " IST" to times; the closed-FY note has no IST because the test requires that.
- `notifications` table stores no mail body, so the activation-waiting text cannot quote the mail's reason.
- Haiku built B7 acceptably but wrote two bad test helpers and took 12-15 min per job at 220-290k tokens; the owner moved to Sonnet medium for core work.

## Baseline bugs found, deliberately not fixed yet (owner: fix later)

- **Baseline bug 1, NAS error classification (found 2026-11-24 via release-candidate tests, NOT fixed, owner: fix later).** `core/faults.py` has no mapping for raw Windows network errors (WinError 53, 67, 1222 and similar) on the LAN leg, so they classify as `AAM-SYS-06` instead of `AAM-NAS-01` (NAS unreachable). The branch `release-candidate` adds `UNREACHABLE_WINERRORS`; its test `tests/test_l3_nas_unreachable_label.py` (14 tests) fails on the baseline for this reason. Fix later as a test-first commit.
- **Baseline bug 2, zero / negative retention deletes today's log folder (found 2026-11-24, NOT fixed, owner: fix later).** `core/run_artefacts.purge_older_than(root, 0)` deletes today's day folder; `release-candidate` refuses a retention below 1 day. Its test is in `tests/test_break_db_artefacts.py` (zero/negative retention). Fix later as a test-first commit (also check the config bounds of the retention key so 0 cannot be configured).
- Other `release-candidate` tests (104 added files) need branch-only code and were not copied; four test-only files that pass on the baseline were copied (commit e94e21f).
