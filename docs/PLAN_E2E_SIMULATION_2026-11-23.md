# Plan - end-to-end calendar simulation, lab data integrity, remaining live steps (2026-11-23)

Status: PLAN (test architect, Opus). Owner decisions (a)-(e) of 2026-11-23 are fixed: no upgrade-path test; lab cards
L1-L7 + T-b3; live LS2b, LS5, LS7, LS6, LS8b; calendar sims around Dec 30-Jan 2 and Mar 30-Apr 2; HAIKU implements,
SONNET triages/fixes in a worktree, OPUS verifies last. **The system clock is never changed.**
Not duplicated (already green): `tests/test_ov_timeline_{a,bc,def}.py` (T-a1..a6, T-b1/b2/b4, T-c1..c5, T-d1/d2,
T-e1..e3, T-f1..f3, P-*), `tests/test_ov_review_points.py`, `audit_lab/test_w25_fy_overlap.py` (15 months, nights at
01:00, mode all), `tests/test_break_release_*.py`, live LS1-LS4, LS8-LS13 (other agent). The new sims differ: REAL
pipelines with a scripted fake `run_tool` (not faked pipelines), separate legs at the real cron instants, faults
COMBINED across the boundary, and a power cut emulated by a DB snapshot.

## 0. Rules for every card (Haiku reads this first)

- Calendar (IST, checked): 2026-12-30 Wed, 12-31 Thu, 2027-01-01 Fri, 01-02 Sat, **01-03 Sun (audit)**, **01-04 Mon
  (weekly)**; 2027-03-28 Sun, **03-29 Mon (weekly)**, 03-30 Tue, 03-31 Wed, 04-01 Thu, 04-02 Fri, **04-04 Sun (audit)**,
  **04-05 Mon (weekly)**. Monthly report on the 1st at 08:30.
- Sim config schedule (set explicitly, the model defaults differ): `cloud_cron "0 2 * * *"`, `lan_cron "0 21 * * *"`,
  `rollover_cron "0 6 * * *"`, `audit_cron "0 8 * * SUN"`, `weekly_cron "0 8 * * MON"`, `monthly_cron "30 8 1 * *"`.
  A LAN run at D 21:00 and a cloud run at D 02:00 both have run date D (plan section 3: the SCHEDULED time decides).
- Clock, unit-e2e layer: `time_machine.travel(dt, tick=False)` AND `pendulum.travel_to(dt, freeze=True)` together,
  only through the rig helpers `go()` / `jump()` (one helper sets both; never call either library directly).
  Flows are called as `.fn` (no Prefect engine under a moved clock). Lab layer: `audit_lab/labclock.fake_clock` only
  (proven with the Prefect harness and real subprocess tools; do NOT use time-machine inside `prefect_test_harness`).
- Moved-clock gotcha: file mtimes stay REAL (Nov 2026). Keep `log_retention_task` and
  `cleanup_orphaned_robocopy_logs` stubbed (the rigs already do), else fresh files look months old and are purged.
- Independent oracles ONLY for pass/fail: hand-written tables in the card (dates, FY names, counts), raw `sqlite3`
  SQL on `manifest.db` (never `ManifestDB` getters), the SMTP server's own `srv.messages`, folder listings and
  sha256/MD5 computed by the test, argv lists captured by the fake `run_tool`, the local rclone remote's directory.
  Product views (`notifications` rows, `/api/status` JSON, journal API) may be asserted only against hand-written
  values and are labelled `# product-view` in the test.
- Known owner-decision defects (D5, D7, D8, D10, D12, D14) are asserted in their OWN small test function marked
  `xfail(strict=False, reason="Dn ...")`; never xfail a whole calendar.
- `OK = {"CLOUD_COMPLETE","CLOUD_NO_CHANGES","LAN_COMPLETE","LAN_NO_CHANGES"}` - copy the `_OK` set of
  `audit_lab/test_w25_fy_overlap.py` by value; "failure" in a card = status not in OK and not RUNNING.
- Each Haiku agent: writes only its own files, runs only its own files
  (`.venv\Scripts\python.exe -m pytest <file> -q -p no:cacheprovider -o addopts="" --timeout=900`), never edits a
  product or existing test file, never commits; a red that is not a typo in the test is reported, not "fixed".
  Report: file, pass/fail per test, the first failing assertion verbatim. Write logs to `agent_runs/e2e/<card>.txt`.

## 1. Card list

Format: **ID [layer] model -> file** | setup | action | expect | oracle | deps | runtime.

### 1a. Infrastructure

**S0 [unit-e2e] HAIKU (Sonnet reviews before fan-out) -> `test_support/sim_calendar.py` + `tests/test_sim_e2e_00_rig.py`**
Setup: class `Sim` composed from existing rigs, no new logic: plumbing of `tests/test_ov_timeline_a.py::rig`
(lines 145-196: real `_run_cloud_pipeline`/`_run_lan_pipeline`, DEFAULTS stubs, `_plain` sync tasks, WoL/shutdown
counters, real `smtp_server()`), plus from `tests/test_ov_timeline_def.py::Site` the `_write_config` / `_archive` /
`slot_busy` / `point_config_at` behaviour (lines 129-167) for the rollover, plus the audit wiring of
`tests/test_ov_timeline_bc.py::test_t_b2...`. Config as in section 0. Scripted run_tool: one patch per module
(`core.cloud_sync`, `core.lan_sync`, `core.integrity`) whose answer is `script.get((leg, fy, run_date_iso))` or OK
(OK logs = `OK_CLOUD` and `robocopy_log(copied=1)`); FY taken from argv by regex `FY\d\d-\d\d`; an entry may carry
`hook=callable` run before returning (used for `jump()` and power cuts). Helpers: `go(dt, scheduled=None)`,
`jump(dt)`, events `lan(d)`, `cloud(d)`, `rollover(d)`, `audit(d)`, `weekly(d)`, `monthly(d)`, `status(dt)` (TestClient
GET `/api/status`, login via `test_support.ui_login`), `play(events)`, `CAL_J` (Dec 30 21:00 -> Jan 4 08:00) and
`CAL_A` (Mar 29 08:00 -> Apr 5 08:00) event lists in section 0 order, `seed_to_mar28()` (= play CAL_J, then raw-SQL
copy the 4 rows of the Jan 3/4 night for each day Jan 5..Mar 28 with shifted `started_at`/`ended_at`, keeping
`extended_metrics`), `smtp_down()` / `smtp_up()` (point `notifications.smtp_port` at a closed port / back),
`power_cut()` (sqlite backup API copy of `manifest.db` to `snap.db`, then raise `_Crash(BaseException)`),
`reboot(dt)` (restore `snap.db` over `manifest.db`, then `launch._mark_abandoned_runs()` with config patched).
Oracle helpers: `runs()`, `acts()`, `subjects()`, `argv(leg)`, `tree(path)` sha256 map, `approvals()`, `sql(q)`.
Self-test: (1) under `go(2026-12-31 21:00 IST)`: `pendulum.now`, `datetime.now(timezone.utc)`, `time.time()`,
`core.time_utils.now_iso()` all equal hand value `2026-12-31T15:30:00Z` (+-1 s); (2) `jump()` inside a hook moves
`now_iso()`; (3) one `lan()` event = 1 row, 1 wake, 1 shutdown; (4) each event < 5 s.
Deps: none. Runtime: build 1 h, run 1 min. Sonnet review: confirms no product module is patched beyond the listed seams.

### 1b. Calendar simulations, unit-e2e (time-machine, real flows/DB/notify/approvals/journal, scripted tools)

**S1 BEST-J [unit-e2e] HAIKU -> `tests/test_sim_e2e_01_best_jan.py`** | Sim fresh, source `FY26-27` 1 file |
`play(CAL_J)`, `status(Jan 4 09:00)` | after each event the hand table: Dec 30 21:00 +1 lan A; Dec 31 02:00 +1 cloud A;
Dec 31 06:00 none; Dec 31 21:00 +1 lan A, `acts()` empty; Jan 1 02:00: `acts()==[(FY27-28,1,...)]`, `src/FY27-28/`
holds only the placeholder, rows cloud B then cloud A (id order), 1 mail subject with `FY27-28`; Jan 1 08:30 1
monthly mail; Jan 1 21:00 `nas/FY27-28/.AAM_TARGET_MOUNTED` exists, rows lan B then lan A, wakes+1 shutdowns+1; Jan 3
08:00 audit rows for both FYs; Jan 4 08:00 weekly mail whose body contains both `FY26-27` and `FY27-28`. Totals: A lan
5/cloud 5, B lan 3/cloud 4, all OK; wakes 5, shutdowns 5; mails = 3 (INFO, monthly, weekly); no subject with
FAILURE/WARNING/ACTION; approvals 0; cloud argv dests end `/FY27-28` and `/FY26-27`; status has 2 folders
(`# product-view`). | raw SQL, srv.messages, listings, argv | S0 | 2 min

**S2 BEST-A [unit-e2e] HAIKU -> `tests/test_sim_e2e_02_best_apr.py`** | `seed_to_mar28()`; `tree(src/FY26-27)` saved |
`play(CAL_A)`, `status(Apr 5 09:00)` | Mar 29-31 21:00: 2 lan rows each; Mar 30, 31, Apr 1 02:00: 2 cloud rows each;
Apr 1 06:00: 1 cloud + 1 lan row for A whose run_id contains `final`, `switch_writes==[("FY27-28","FY26-27")]`,
archive calls == 1 for FY26-27, journal finished (raw SQL); Apr 1 21:00 .. Apr 5 02:00: B only (4 lan, 4 cloud);
Apr 4 08:00 audit rows B only; A row count never grows after Apr 1 06:00; `tree(src/FY26-27)` and `tree(nas/FY26-27)`
unchanged; no FAILURE/ACTION subject; rollover mail subjects recorded once and frozen by the Sonnet reviewer against
PLAN section 4 "1 Apr" row (golden, `# product-view`). Window totals: A lan 4 cloud 4; B lan 7 cloud 7. | as S1 | S0 | 3 min

**S3 WORST-J NAS down on Dec 31 [unit-e2e] HAIKU -> `tests/test_sim_e2e_03_nas_down.py`** | Sim | `play(CAL_J)` with
Dec 31 21:00: `wake` returns False and `lan_preflight` stub returns `{"ok": False, "exit_code": 1, "error": "NAS
unreachable"}` for that event only | Dec 31: exactly 1 lan row A, status failure, robocopy argv count unchanged, >=1
FAILURE mail naming LAN; Jan 1 02:00 activation unaffected (as S1); Jan 1 21:00: NAS `FY27-28` + marker created, lan
B and A OK, exactly 1 RECOVERED subject for lan A, 0 for lan B. | raw SQL, srv.messages | S0 | 2 min

**S4 WORST-J cloud auth failure on activation night + mail down [unit-e2e] HAIKU -> `tests/test_sim_e2e_04_auth.py`** |
Sim; script cloud for run date Jan 1, both FYs: exit 1, log = U14 case (c) lines (`Error 401: Invalid Credentials`,
`fatalError:true`) from `tests/test_break_release_cloudlog.py`; `smtp_down()` Jan 1 00:00, `smtp_up()` Jan 2 00:00 |
`play(CAL_J)`, `status(Jan 1 09:00)` | activation row still exactly 1, folder + placeholder created; cloud B and A
failure; exactly 1 `sync` argv per FY that night (no retry on auth); no mail reaches srv between the two times;
Jan 1 09:00 status has the undelivered-alert item (`# product-view`, hand text "alerts_undelivered"); Jan 2 02:00 both
OK; after Jan 2 00:00 srv gets >=1 RECOVERED per failed FY; INFO activation mail: record whether it is ever delivered
(if never: finding E2E-MAIL-1 for the owner, not a test failure). | argv, raw SQL, srv | S0 | 2 min

**S5 WORST-J LAN straddles midnight into the cloud slot [unit-e2e] HAIKU -> `tests/test_sim_e2e_05_straddle_jan.py`** |
Sim; Dec 31 21:00 lan A hook `jump(Jan 1 02:30)`; cloud Jan 1 run with `go(Jan 1 02:30, scheduled=Jan 1 02:00)` |
play CAL_J | lan row A only, its `started_at` date 2026-12-31 (IST), no activation after the LAN flow; cloud Jan 1:
not MISSED, activation 1, rows B and A OK; totals as S1. | raw SQL, hand dates | S0 | 1 min

**S6 WORST-A straddle Mar 31 -> Apr 1, cloud overruns into the rollover [unit-e2e] HAIKU -> `tests/test_sim_e2e_06_straddle_apr.py`** |
seed; Mar 31 21:00 lan B hook `jump(Apr 1 00:30)`; Apr 1 02:00 cloud A hook `jump(Apr 1 06:30)`; `slot_busy=True`
for the Apr 1 06:00 rollover only | play CAL_A | Mar 31 night: lan rows B and A both exist (A ran after midnight);
Apr 1 06:00: `switch_writes==[]`, no archive; Apr 1 21:00 and Apr 2 02:00: B AND A backed up (switch pending);
Apr 2 06:00: switch 1x, archive FY26-27 1x; afterwards B only. Separate xfail tests: D14 (row A of Mar 31 dated
2027-03-31), D7 (no approval requested for the slot timeout). | raw SQL, switch/archive lists | S0 | 2 min

**S7 WORST-A power loss during the final backup [unit-e2e] HAIKU -> `tests/test_sim_e2e_07_powercut.py`** | seed;
Apr 1 06:00 final cloud A: hook `power_cut()` | play CAL_A to Apr 1 06:00 (expect `_Crash`), `reboot(Apr 1 06:20)`,
continue CAL_A | after reboot: the final cloud row is `CLOUD_ABANDONED` (raw SQL), 1 mail for it; no switch on Apr 1;
Apr 1 21:00 and Apr 2 02:00 back up B and A; Apr 2 06:00: final backup re-run (new row with `final`), switch 1x,
archive FY26-27 1x; `backup.lock` absent at the end; no second activation row. | raw SQL, lists, srv | S0 | 2 min

**S8 WORST-A config-switch crash + mail server down [unit-e2e] HAIKU -> `tests/test_sim_e2e_08_switch_crash_mail.py`** |
seed; Apr 1 06:00 `update_config_yaml` = write then raise `_Crash` (copy T-f1 `write_then_die`); `smtp_down()`
Apr 1 00:00 -> Apr 2 12:00 | play CAL_A, `status(Apr 2 09:00)` | Apr 1 21:00: lan B only; Apr 2 06:00 journal
finished, archive FY26-27 1x, FY26-27 approvals CLOSED (raw SQL); undelivered-alert item at Apr 2 09:00
(`# product-view`); no backup status differs from S2 after Apr 2 06:00; srv receives 0 mails during the outage. | raw
SQL, srv | S0 | 2 min

**S9 WORST-A guard block approved + archive failing [unit-e2e] HAIKU -> `tests/test_sim_e2e_09_block_approved.py`** |
seed; Apr 1 02:00 cloud B: `cloud_guards` stub raises the G3 block exactly as `tests/test_ov_timeline_bc.py` T-b1;
archive returns False Apr 1 and Apr 2, True Apr 3 | play to Apr 1 10:00; approve via TestClient POST
`/api/approvals/<id>/approve` (wiring copied from `tests/test_ov_timeline_def.py::test_T_e1_d9...`, deployment run
mocked), then `cloud(Apr 1)` with `approval_id`; continue CAL_A | 1 PENDING approval (cloud, FY27-28) before; after:
approval USED, row cloud B OK; switch Apr 1; archive argvs/calls only FY26-27 (3 calls Apr 1-3), never FY27-28;
WARNING subjects for archive <= 2; FY26-27 rows frozen after the switch. | raw SQL, archive list | S0 | 2 min

**S10 WORST-A guard block never approved [unit-e2e] HAIKU -> `tests/test_sim_e2e_10_block_unapproved.py`** | seed;
lan A G3 block from Mar 31 21:00 on (every night) | play CAL_A to Apr 3 09:00 | exactly 1 PENDING approval (lan,
FY26-27) ever (no duplicates); Apr 1-3 06:00: no switch, ROLLOVER_BLOCKED subject >= 1, <= 1 per
`alerts.stale_repeat_hours`; B backed up every night both legs; A cloud backed up every night (switch pending). Then
approve at Apr 3 10:00, run the Apr 4 06:00 rollover: expected per PLAN section 4 row "Switch blocked for days" (leg
approval, then a G6 approval, then switch); assert only what that row states; anything else -> Sonnet triage. | raw
SQL, srv | S0 | 2 min

**S11 WORST one-night-late scheduler, both boundaries [unit-e2e] HAIKU -> `tests/test_sim_e2e_11_late.py`** | (J) the
Dec 31 21:00 LAN starts `go(Jan 1 21:05, scheduled=Dec 31 21:00)` with `_scheduled_lateness_seconds` = 86700, then the
regular Jan 1 21:00 run at 21:06 (lateness 360); (A) seed, the Mar 31 21:00 LAN starts Apr 1 21:05 AFTER the Apr 1
06:00 switch | play | (J) LAN_MISSED for A dated Dec 31, 0 robocopy argvs for it, 1 subject for `missed`; Jan 1 21:06
run: B and A OK. (A) the late Mar 31 run produces NO row for FY26-27 (closed year, D1 fix) and no robocopy argv
containing `FY26-27`; Apr 1 21:00 run B OK. | argv, raw SQL | S0 | 2 min

**S12 WORST three failing LAN nights across Jan 1 [unit-e2e] HAIKU -> `tests/test_sim_e2e_12_three_nights.py`** | Sim;
LAN robocopy exit 16 (fatal) for run dates Dec 31, Jan 1, Jan 2, all FYs; OK Jan 3 | play CAL_J;
`watchdog._check_stale_legs(cfg)` at Jan 2 09:00 and Jan 3 09:00 (clock via `go`) | rows: Dec 31 A fail; Jan 1, 2 B
and A fail; Jan 3 both OK; shutdowns == 2 (Dec 30, Jan 3; NAS stays on after failing nights); per (lan, FY) FAILURE
subjects >= 1 and <= 1 + ceil(48 / cooldown_hours) (cooldown read from the sim config literal); exactly 1 RECOVERED per
FY on Jan 3; stale list at Jan 2 09:00 contains lan A (last OK Dec 30 21:00). | raw SQL, srv, hand bound | S0 | 2 min

**S13 WORST overlap toggled off/on at both boundaries [unit-e2e] HAIKU -> `tests/test_sim_e2e_13_toggle.py`** | (J) set
`rollover.overlap_enabled=False` Dec 31 18:00, True Jan 2 12:00 (same mechanism as T-e1/T-e2 in
`tests/test_ov_timeline_def.py`); (A) seed, off Mar 30 12:00, on Mar 31 12:00 | play | (J) Jan 1-2: no activation, no
`src/FY27-28`, A only; Jan 3 02:00: activation 1, INFO 1, B and A; Jan 3 21:00 NAS B created. (A) Mar 30 21:00 + Mar 31
02:00: A only, sibling subject >= 1; Mar 31 21:00: B and A, no 2nd INFO; Apr 1 switch as S2; total activation rows 1.
Separate xfail: D12 not touched here. | raw SQL, srv, listings | S0 | 2 min

### 1c. Lab cards (real robocopy + local rclone remote; clean PREFECT_HOME each)

L1-L7 are specified in `docs/TEST_DESIGN_RELEASE_2026-11-22.md` section B (setup/action/expect/oracle, cleanup);
implement them exactly, on the WAVE 6 rig (`audit_lab/test_w6_flow.py::rig`) unless the card says otherwise. Split
into distinct files (replaces the single `test_w26_release_data.py` named there):

| ID | Model | File | Deps | Runtime |
|---|---|---|---|---|
| L1 same-size/mtime corruption caught by the weekly MD5 audit (LAN + cloud) | HAIKU | `audit_lab/test_w26a_corruption.py` | - | 5 min |
| L2 unicode/case/normalization churn; L3 300/400-char cloud paths | HAIKU | `audit_lab/test_w26b_names.py` | - | 8 min |
| L4 file growing during upload; L5 exclusively locked file (pywin32 `win32file` is installed) | HAIKU | `audit_lab/test_w26c_upload.py` | - | 10 min |
| L6 source swapped for a smaller tree; T-b3 (below) | HAIKU | `audit_lab/test_w26d_guards.py` | - | 8 min |
| L7 ACL-denied subfolder, `lan.use_backup_mode=false` (icacls cleanup in `finally`) | HAIKU | `audit_lab/test_w26e_acl.py` | - | 5 min |

**T-b3 [lab] HAIKU -> `audit_lab/test_w26d_guards.py`** | `audit_lab/test_w25_fy_overlap.py::lab` fixture (import
fixture + `Lab`); activate FY27-28 by `lab.backup(_at(2027,1,1,2))`; put 9 files in `src/FY27-28/Clients` | night 1
(Jan 2 02:00 cloud, Jan 2 21:00 LAN) OK; night 2: delete 5 of the 9, rename 1; night 3: no change | night 2 both legs
OK (5 deletes <= `guards.small_folder_max_deletes` 10), NAS and remote `FY27-28` == source (`_tree_map`); night 3
`NO_CHANGES`/0 copied; second variant: 99-file folder, delete 11 -> both legs `*_BLOCKED`, 1 approval each, NAS and
remote trees unchanged (sha256 before/after). | `_tree_map`, raw SQL | - | 8 min

**LC1 BEST lab across both boundaries at cron times [lab] HAIKU -> `audit_lab/test_w27a_calendar_best.py`** | `lab`
fixture of w25 (real tools, labclock) | events: Dec 31 21:00 lan, Jan 1 02:00 cloud, Jan 1 21:00 lan, Jan 2 02:00 cloud,
Jan 3 08:00 audit; then move labclock straight to Mar 31 (no nights in between, no seeding): Mar 31 21:00 lan, Apr 1 02:00 cloud,
Apr 1 06:00 rollover, Apr 1 21:00 lan, Apr 2 02:00 cloud; `lab.work()` before every night | after each LAN+cloud pair
`lab.converged(fy)` for every active FY; after Apr 1 06:00 `archived==[("lab-bucket","FY26-27")]`; FY26-27 NAS and
remote trees frozen from Apr 1 06:00 (sha256) although `lab.work("FY26-27", ...)` still edits the source on Apr 1-2. |
`_tree_map` | - | 15 min

**LC2 WORST lab data path at the boundary [lab] HAIKU -> `audit_lab/test_w27b_calendar_worst.py`** | `lab` fixture;
one test per variant, each from Dec 31 21:00 to Jan 1 21:00: (a) `src/FY27-28` is a junction (`mklink /J`) to
`sandbox/elsewhere` with 5 files; (b) hand-made `FY27-28` with 20 files, mtime Dec 20; (c) empty: app-created folder
(placeholder only); (d) Mar 31 21:00 LAN where the first robocopy call's hook (wrap `run_tool` in `core.lan_sync`)
rewrites `src/FY26-27/Clients/a_000.txt` with new bytes before the second folder runs, then Apr 1 02:00 cloud | (a)
never a success row for FY27-28 whose destination has 0 files while the junction target has 5 (silent false success);
acceptable = adopted + `converged`, or BLOCKED/failure + mail; which one = Sonnet records for the owner. (b)
adopted (`created_by_app=0`), source files' sha256 unchanged, converged. (c) both legs not BLOCKED, remote prefix
holds only the placeholder, NAS likewise. (d) cloud Apr 1 has the new bytes (MD5), LAN row of Mar 31 OK or partial,
the next LAN converges. | `_tree_map`, hashlib | - | 15 min

### 1d. Live steps (C:\AAMTest stack; runbook `docs/TEST_DESIGN_RELEASE_2026-11-22.md` section C, helpers `h.ps1`)

Model: SONNET runs them (elevated queue, timing, judgement on evidence); a Haiku may only re-run a step whose script
Sonnet wrote, and collect evidence. Scripts and evidence only in `agent_runs/live/` (gitignored).

**LS7a Prefect server stopped while idle** | snapshot n | [ELEV] `Stop-Service AamPrefectServer` | watchdog restarts it
within `check_interval_seconds * failure_threshold + 60 s`; `/api/health` true; no backup row created | `sc query`,
watchdog log | after the live-stack agent | 10 min
**LS7b Prefect server killed mid-LAN** | `Run-Leg lan` | when robocopy runs: `taskkill /F` the server python PID | watchdog
log shows DEFER while lock/robocopy alive; the run writes a terminal row (any status, never RUNNING after 30 min); no
second robocopy; server RUNNING after the run; next `Run-Leg lan` OK, `Tree-Hash` equal | LS7a | 30 min
**LS2b agent dies, robocopy survives** | per runbook row LS2b (kill python PID only, no /T); while the orphan runs,
`Run-Leg lan` again | `Get-Process robocopy` count never > 1; the second run is refused/deferred (lock or transfer
check) with a row or log line; watchdog restart deferred; after the orphan exits: next run OK, `Tree-Hash` equal | LS7b | 30 min
**LS5 NAS volume full mid-copy** | per runbook row LS5 (400 MB VHDX, `AAMTestNASsmall`, config COPY) | LAN_PARTIAL or
LAN_FAILED, robocopy error 112 in the log, never COMPLETE, no shutdown claim, 1 mail, source hash unchanged | LS2b | 40 min
**LS6 overlap Jan-Mar on the live stack** | snapshot 1; [ELEV] stop AamWatchdog THEN AamBackupAgent (the watchdog would
restart the agent); Server stays up | `agent_runs/live/run_with_date.py`: `AAM_CONFIG` + `PREFECT_API_URL` set,
`mock.patch("core.fy_overlap.run_date", return_value=date(2027,1,2))`, call `flow.backup(config_path, mode="all")` as a
real flow (real slot on the live server); repeat with `date(2027,3,31)`; then the second invocation for Jan 2 again |
`C:\AAMTest\source\FY27-28` + NAS `FY27-28` + marker created once; 1 activation row; COMPLETE rows for both FYs on
both legs per invocation; bucket prefix `FY27-28/` exists (`rclone lsf` as the service account); the repeat creates
nothing new; `mails.jsonl` +1 INFO only; dashboard (browser) shows 2 cards | restore: per runbook row LS6, then start
Agent, then Watchdog | LS5 | 45 min. The Apr 1 switch is NOT run live (it needs the real clock date; covered by S2/LC1).
**LS8b old-schema manifest.db at boot** | stop 3 services; move the DB aside; create a v5 file (`CREATE TABLE
run_history(id INTEGER)`, `PRAGMA user_version=5`); start all | within 15 min: >= 1 ACTION_REQUIRED/CRITICAL line in
`mails.jsonl` naming the database; restarts bounded by `max_starts_per_window`; <= 1 mail per window; v5 file sha256
unchanged. Zero mails = SILENCE-3, release-blocking | restore snapshot | LS6 (last: most invasive) | 30 min

## 2. Run order and parallelism

1. **Wave 0** (now): S0 by one Haiku -> Sonnet review (only seams listed in S0; self-test green) -> fan-out.
2. **Wave 1** (parallel with the running live-stack agent; fake tools only, no real robocopy/rclone): 6 Haiku agents,
   one pytest process each (time-machine is process-wide: no xdist): H1 S1+S5, H2 S2+S6, H3 S3+S4+S12, H4 S7+S8,
   H5 S9+S10, H6 S11+S13. Same time: 4 Haiku agents WRITE lab files only (H7 L1+L6/T-b3, H8 L2+L3, H9 L4+L5+L7,
   H10 LC1+LC2) and may run `--collect-only`, nothing else.
3. **Hard exclusion**: no audit lab test and no lab card may RUN while any live step runs (the other agent's or ours):
   the stack watchdog's `_transfer_process_running` sees any `robocopy`/`rclone` process on the PC and defers, and
   LS2b/LS5/LS7b look up robocopy by name. Start live work only after the orchestrator has the live-stack agent's
   hand-back AND `prefect flow-run ls --state RUNNING` is empty AND `C:\AAMTest\runtime\backup.lock` is absent AND a
   snapshot is taken.
4. **Wave LS** (after the live-stack agent): Sonnet, serially LS7a -> LS7b -> LS2b -> LS5 -> LS6 -> LS8b, snapshot
   restore between steps that say so. ~3 h.
5. **Wave L** (after Wave LS): lab files, at most 2 pytest processes at once, each with its own empty
   `PREFECT_HOME=%TEMP%\ph_<card>`; e.g. `w26a+w26b`, then `w26c+w26d`, then `w26e+w27a`, then `w27b`; finally the full
   audit lab once (`--ignore=audit_lab/test_w7_deployed.py`, ~90 min, background). While Wave L runs, nobody edits or
   merges into product modules of the main checkout (fixes live in the worktree; merging waits).
6. **Fix loop** (Sonnet) runs in its worktree in parallel with Waves LS/L; merges only between waves.
7. **Final**: full unit suite in main after the last merge; Opus verification (section 4).

## 3. Triage of a red result (Sonnet)

1. Re-run the single test alone; delete `__pycache__`; check the rig: wrong date/weekday, missing PREFECT_HOME,
   moved clock vs real mtime (purge), invalid FY leaf, a stub missing for a new task -> **A: test artefact**, fix the
   test (Sonnet or send back to the Haiku with the exact correction).
2. Compare with the binding text: PLAN_FY_OVERLAP rev 4 (sections 2-5 and the calendar table), ARCHITECTURE section 10
   gates, the known list (D4, D5, D7, D8, D10, D12, D13, D14, BUG-05 robocopy 4-7 = COMPLETE, I2 first-night approval,
   locked-file PARTIAL). Documented behaviour or an owner-decision item -> **C: owner decision**: move the assertion
   into its own xfail(strict=False, reason="E2E-<n>/Dn") test, add a line to the run report for the owner.
3. Otherwise reproduce with the smallest standalone script against current `fy-overlap` HEAD; state the customer harm
   in one sentence (data loss / silent false success / silence / wrong folder / storm) -> **B: real bug**.
4. Fix B: `git worktree add .claude/worktrees/e2e-fix -b e2e-fix fy-overlap`; regression test = the red card (must be
   red on old code); minimal fix inside the plan's intent; every new tunable a config key; run the card + the module's
   unit tests + `tests/test_ov_*`; commit per fix citing the card and `E2E-<n>`, attribution lines per CLAUDE.md;
   GitNexus `impact` before the edit and `detect_changes` before the commit.
5. Merge `e2e-fix` -> `fy-overlap` only between waves (no lab running); re-run the affected cards and the full unit
   suite. If a live re-test is needed: `git -C C:\AAMTest\code merge fy-overlap`, [ELEV] restart Agent + Watchdog.
6. Semantic change (anything the plan does not already intend) -> never fixed by Sonnet; owner question.

Non-vacuity (Sonnet, per committed card; TEST_DESIGN D1 procedure, working-tree mutation, never committed):
S1/S2 `active_folders` returns only the configured FY -> red; S3/S12 `decide_lan_result` maps exit 16 to COMPLETE ->
red; S4 make `rclone_retryable` True for 401 -> red (argv count); S5/S11 `flow._scheduled_time` -> `pendulum.now()` ->
red; S6/S7 drop the journal check in `switch_pending` -> red; S8 skip `_close_finished_old_fys` -> red; S9/S10 archive
with the new FY -> red / approval created per night -> red; S13 activation without the `overlap_enabled` check -> red;
L/LC as in TEST_DESIGN D1 plus LC1 "copy FY26-27 after the switch" -> frozen-tree red.

## 4. Release-gate additions (on top of TEST_DESIGN D3)

Must be green: S0-S13 (only the listed per-defect xfails), L1-L7, T-b3, LC1, LC2, LS7a, LS7b, LS2b, LS5, LS6, LS8b;
the mutation list above red for each; full unit suite; full audit lab (known red `test_w16_schedule_collisions_across_year`).
Release-blocking from these cards: any success row while a destination tree differs from the source (L1 audit miss,
L4/L5/L7 COMPLETE, LC2a empty success); any write into FY26-27 destinations after the switch (S2/S11/LC1); any write
outside active FY folders/prefixes; a second concurrent robocopy (LS2b); a full NAS reported COMPLETE (LS5); boot with
an old DB and zero mail (LS8b SILENCE-3); a power cut that leaves the switch never completing (S7); unbounded mails
(S10/S12 bounds).
Accepted / owner decision (not blocking, listed in the owner report): D4, D5, D7, D8, D10, D12, D13, D14; LC2a
junction policy (adopt vs block); E2E-MAIL-1 (INFO mail lost while SMTP was down, if found); rollover mail wording.
Unverifiable here: real runs on Windows Server 2016 / 2019 (NSSM, SMB defaults, per-wheel OS floors; trial box
is the only Server 2016 evidence); the April 1 switch on a live stack with the real date (only in-process/lab);
real GCS ARCHIVE transition (gcloud recorder only); real NAS power-off (deferred until the owner is at the NAS);
DST zones (U3).

## 5. Open questions (not guessed; Sonnet answers from code or the owner decides)

1. Is the activation of FY27-28 expected to happen when the cloud leg fails for auth on Jan 1 (S4)? T-a2 shows it
   happens at flow start; if the product defers it, is that intended?
2. After an SMTP outage, are undelivered alerts re-sent (S4/S8)? If not, is the dashboard banner enough (owner)?
3. LC2a: should a junction as the early FY folder be adopted (content backed up) or refused? The plan (D-9) is silent.
4. S10: the exact approval sequence after a final-backup G3 block (leg approval, then G6) is only one table row in
   the plan; the expected mails per day are not written down.
5. S2: the rollover mail set on Apr 1 is not specified in the plan (T-d1 already showed near-duplicates): golden
   recorded once, owner to judge wording/count.
6. Does pendulum 3 follow time-machine on this venv? S0 self-test answers it; the rig sets both clocks anyway.
7. Production schedule: the owner's 21:00/02:00/06:00/Sunday 08:00 differ from the model defaults (01:00 LAN,
   18:00 cloud, Sunday 10:00 audit, WP14 comment). Confirm which the customer config uses; the sims use the owner's.
