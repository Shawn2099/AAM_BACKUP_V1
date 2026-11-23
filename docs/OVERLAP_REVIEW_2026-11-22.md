# FY overlap - logical review and test design (2026-11-22)

Scope: branch `fy-overlap` at 36953aa. Read: plan rev 4, task list (incl. "Found while building"), ARCHITECTURE 3.4/4/10,
`core/fy_overlap.py`, `core/fy_rollover.py`, `core/rollover_journal.py`, `flow.py` (backup, `_run_leg_folders`, LAN/cloud
pipelines, `_rollover_resume`, `_run_final_backups`, `integrity_audit_flow`), `core/manifest.py`, `core/backup_repository.py`,
`core/attention.py`, `core/report.py`, `core/notify.py`, `core/guards.py`, `ui.py`, `watchdog.py`, `models/config.py`,
`deploy/gcs_lifecycle.json`. Existing tests checked by name (ov1..ov12, break_fy_overlap, w25, property_release_*) so the
cards below do not repeat them. I did not change any code and did not run tests.

Already known and not repeated as new findings: OV-2.3 #1 (a junction placed AFTER activation is not refused), WP5-9 N1
(the audit can leave the NAS on), N5 (JUMP count), N6 (overlap off hides a PENDING early approval from the banner), I2
(first night after the fresh DB is BLOCKED).

---

## 1. Logical defect candidates

**D1 - A flow that loaded config before the April switch runs the closed FY after it (stale active set).** MEDIUM
- Where: `flow.py:3346` (config loaded once), `:3388` (`_active_fys` worked out before the slot), `:3280` (slot taken
  later in `_run_leg_folders`). The rollover holds the slot from the switch through the archive (`:2905` -> `:2806`
  switch, `:2813` archive). Mode `all` also releases the slot between the cloud and LAN loops (`:3401-3413`). The
  integrity audit has the same pattern (`:3031` before `:3134`).
- Condition: a backup or audit flow starts while `rollover_check_flow` is running. This can happen with a late scheduled
  run after a reboot, an audit cron near 06:00, or a long final backup. The flow computes `[FY27-28, FY26-27]` from the
  old config, waits, and gets the slot after `config_switch` + `archive` + `_close_old_fy`.
- Harm:
  - FY26-27 is synced again into `gs://bucket/FY26-27` after the prefix was moved to ARCHIVE. Changed files are
    re-uploaded as STANDARD, and deletes and overwrites hit ARCHIVE objects (early-deletion charges, mixed storage
    class). The archive step never runs again because the journal is finished.
  - A run row and possibly a FAILURE incident are written for an FY that is already closed, and nothing closes that
    incident later.
  - Not a wrong-folder write. The FY27-28 view is identical before and after the switch.
- Smallest repro (unit): patch `_backup_slot.__enter__` with a hook that runs `fy_rollover.update_config_yaml(...)` and
  `db.close_fy("FY26-27")`. Then call `backup(mode="cloud")` on 2027-04-01 with an activation row. Today the fake
  pipeline is called for FY26-27. Expected: the active set is re-derived after the slot (or the configured FY is
  re-checked against the file), and FY26-27 is skipped.

**D2 - `_close_old_fy` is never retried once the journal is complete.** MEDIUM
- Where: `flow.py:2331-2340` swallows every error. `:2916-2919` is reached only from `_rollover_resume`. After the
  journal is complete, `:2665-2672` returns `NO_ROLLOVER_NEEDED` before reaching it.
- Condition: `db.close_fy` raises once (DB locked or busy), or the process dies after `step_announce` and before
  `_close_old_fy`.
- Harm:
  - FY26-27 approvals stay PENDING/APPROVED for good, and `/approvals` (`ui.py:912`) still lists them (it is not
    filtered by active FY).
  - Approving one returns "started" (`ui.py:941`), but `backup()` raises a ValueError at `flow.py:3394` before any run
    row or alert, so nothing happens and nobody is told.
  - Its cloud/lan incidents stay open. That is cosmetic, but they still feed any lookups that cover all FYs.
- Repro: make `ManifestDB.close_fy` raise on the first call, run the full rollover, then call `rollover_check_flow`
  again the next day. Approvals of FY26-27 are still PENDING. Fix idea: run the close as an idempotent journal step,
  or call it at the top of every `rollover_check_flow` for each FY that has a finished switch.

**D3 - A deferred activation is signalled by ONE email only: no dashboard item, no repeat, no RESOLVED.** MEDIUM
- Where:
  - `fy_overlap.py:239-256` and `:283-284`: `overlap_alert_sent:<fy>` is set forever after the first SENT or
    SUPPRESSED mail.
  - `core/attention.py` has no item for a deferred activation (no use of `overlap:activation` anywhere).
  - `sibling_findings` skips links (`fy_overlap.py:450`) and folders without real files.
- Condition: before activation, `D:\FY27-28` is a junction or symlink (staff point it at another disk or share), or the
  parent ACL stops the service account from creating folders while staff create it by hand later. A changed reason
  (clock first, then rights) is never mailed again.
- Harm: **silent miss for up to 3 months.** Staff save into FY27-28 from January. One email arrives about 24 h later,
  then nothing: one green card, no red item, no overdue. On the April switch the configured source becomes that
  junction.
- Repro: junction `FY27-28` -> a temp folder holding 10 files. Call `ensure_overlap_activated` daily from 2027-01-01 to
  2027-02-01 with a CLOCK_OK fake. Result: exactly 1 `overlap:activation:FY27-28` notification, and `compute_attention`
  has no item that names FY27-28. Expected: a red attention item while it is deferred and in window, a daily repeat
  like the sibling alarm, and a RESOLVED mail on activation.

**D4 - New-first order plus a shared leg budget lets the early FY's first big upload starve the configured FY.** DECISION RISK (D-4)
- Where: `flow.py:3285-3294` (order and shared budget) and `:491-519` (`_missed_if_no_budget` for folder 2).
- Condition: on 1 January an adopted `FY27-28` already holds a lot of data (staff pre-filled it in December, or
  migrated it). Its cloud pipeline uses the whole budget (`budget_stopped`, PARTIAL "catching up"), so FY26-27 is
  `CLOUD_MISSED` every night until the upload finishes.
- Harm: the FY that is busiest in Q4 (Indian year end) gets no cloud copy for several nights. There are MISSED warning
  mails and amber items, and the red overdue item comes only after the deadline. It is truthful, but it is data at
  risk.
- Repro (unit): a fake cloud pipeline for FY27-28 that consumes `budget.remaining()`, run on 3 consecutive nights.
  FY26-27 is MISSED 3 times. Ask the owner whether the configured FY should go first while the early FY has no success
  yet, or whether a reserve should be kept for it.

**D5 - The early FY's NAS folder is re-created, marker included, on every LAN run after activation.** MEDIUM-LOW
- Where: `fy_overlap.py:561-564`, called from `flow.py:2056-2057` on every early-FY LAN pipeline.
- Condition: the early NAS folder is gone, for example after a NAS reset, the wrong volume mounted at the same share
  path, or a DFS failover to an empty target.
- Harm: the G5 canary is defeated for that FY only. A new marker is written and a full mirror lands on the wrong (or
  empty) volume, while FY26-27 on the same share correctly fails G5 or preflight. The two cards then disagree (early
  green, current red). The plan says "never recreated silently" for the source; the NAS side has no such rule.
- Repro: an activation row, the NAS share root present but empty, run the early-FY LAN pipeline. Result: state
  `created`, G5 passes. Expected (proposal): `created` only on the first LAN run after activation (record it, for
  example an `overlap_activations.nas_created` meta). After that, a missing folder is a G5/preflight failure that
  needs approval.

**D6 - `switch_pending` checks `state == DONE` while everything else treats SKIPPED as finished.** LOW (latent)
- Where: `fy_overlap.py:150` compared with `rollover_journal.py:30` (`FINISHED_STATES`), `flow.py:2629`, and
  `report.py:351,361`.
- Condition: `config_switch` is ever recorded SKIPPED (no path does that today, but the supersede path at
  `flow.py:2650` skips open steps generically).
- Harm: the overlap layer, the reports and the rollover would disagree about whether the switch happened. Use
  `rj.is_finished` in one place.

**D7 - The two-folder LAN night makes a final-backup SLOT_TIMEOUT on 1 April likelier, and G6 then invites "approve anyway".** LOW-MEDIUM
- Where: `flow.py:2960-2974` (any non-GuardBlocked exception becomes `<LEG>_NOT_COMPLETE`) and `guards.py:290-300`.
- Condition: the LAN loop of 31 March (two folders under one slot) is still running when the 06:00 final backup asks
  for the slot.
- Harm: ROLLOVER_BLOCKED, plus an approval request whose text says "approve to roll over anyway". A human could skip
  FY26-27's last LAN copy for something that a retry would have fixed. Suggest: a final-backup SLOT_TIMEOUT or MISSED
  is "retry tomorrow" (WARNING), not an approvable G6 block.
- Repro: hold the slot past `slot timeout` and run `rollover_check_flow`. The result is a G6 block with
  `lan=LAN_NOT_COMPLETE`.

**D8 - `close_fy` closes only `cloud` and `lan` incidents.** LOW
- Where: `manifest.py:1238`.
- Condition: at the switch, FY26-27 has an open `audit` incident (mode `audit`, key `audit:FY26-27:...`) or an
  `overlap:activation` incident (mode `rollover`).
- Harm: the incident stays open. An event without an FY (`fy=ALL` in `notify._incident`) can thread into it, or find
  it as the root.
- Repro: an audit FAILED for FY26-27, then the switch. `last_open_incident("audit", fy=ALL)` still returns it.

**D9 - An approval of an FY that is not active can be approved and "run", and then nothing happens.** LOW-MEDIUM
- Where: `ui.py:919-955` never checks `req["fy"] in _active_fy_list()`. The flow refuses at `flow.py:3393`
  (ValueError) before any row or alert.
- Condition: overlap switched off mid-window (N6), `early_start_month` raised, the clock moved back, or D2.
- Harm: the UI answers 200 "started", the Prefect run FAILS without a row, mail or attention item, and the approval
  stays APPROVED.
- Repro: TestClient. A PENDING approval for FY27-28 with overlap off, POST approve: 200 and `arun_deployment` is
  called. Expected: 409 "this year folder is not backed up now".

**D10 - The report window of an FY that left the active set WITHOUT a switch runs to the period end.** LOW
- Where: `report.py:332-367` ends the window only at a `config_switch`.
- Condition: overlap off, `early_start_month` raised, or the clock moved back after activation.
- Harm: the report counts "no backup ran" nights for FY27-28 while the dashboard shows no card and no overdue (the two
  disagree). Decide which is true and make the report and attention agree.

**D11 - The NAS share-root validator also refuses a mapped drive.** LOW
- Where: `models/config.py:1308-1309`. `Z:\FY26-27` splits into `["Z:", "FY26-27"]`, so `len <= 2` and it is refused.
- This only matters if mapped drives are allowed as `lan_destination` (services usually cannot see them). Check with
  the owner, or exclude drive letters.

**D12 - Changing `early_start_month` mid-window silently drops the activated early FY.** LOW
- Where: `fy_overlap.py:134`.
- Condition: the value is raised from 1 to 3 in February after activation.
- Harm: FY27-28 leaves the active set until 1 March. Its approvals and overdue are hidden. Only the sibling alarm
  fires, with the misleading text "only backed up from March on". Suggest: once activated, stay active until the
  switch (ignore later changes to `early_start_month`), or refuse the change on the Settings page while an activation
  row of the current cycle exists.

**D13 - The GCS lifecycle in git still has an unscoped `age: 365 -> ARCHIVE` rule** (`deploy/gcs_lifecycle.json`). LOW, known (WP12, D-13)
- With a 15-month life, objects uploaded in Jan-Mar of the early year reach 365 days while their FY is still active.
- Harm: retrieval cost and ARCHIVE class inside an active prefix. Apply WP12 before January 2028; it does not block
  this release.

Checked and found sound: per-FY run ids, rows, alerts, approvals and incidents; per-FY guard baselines carry over at
the switch (G4 destination strings are the same, built by `_child_path` on both sides); the archive step touches only
`gs://bucket/<from_fy>/`; a scheduled-late run keeps its scheduled date; one NAS decision per LAN loop (a missing vote
means stay on); the R3 crash path closes the old FY; the second folder gets MISSED when the budget is used up; the
audit gives one alert per FY; the overdue base for the early FY is its activation.

---

## 2. Scenario matrix (gaps only)

Model: H = HAIKU-writable (clear oracle, existing fixtures); S = NEEDS-SONNET (multi-flow timing, patching inside
flows). Layer: U = unit (tests/, fake_run_tool, sandbox config, fake clock), L = audit lab (real robocopy/rclone),
X = live stack (Prefect server + services, trial box). The oracles never call the code under test: they are fixed
dates, hand-counted rows, folder listings and SMTP-server mail counts.

### Timelines (run in order; each step is one card; fake clock IST)

| ID | Setup | Action | Expected (observable) | Oracle | Target | M | L |
|---|---|---|---|---|---|---|---|
| T-a1 | cfg `D:\FY26-27`, overlap on, start=1; no `FY27-28`; DB empty | LAN flow scheduled 2026-12-31 21:01, ends 2027-01-01 00:40 | only FY26-27 row; no activation row; no `D:\FY27-28` | scheduled date Dec 31 is out of window | tests/test_ov_timeline_a.py | S | U |
| T-a2 | after T-a1 | cloud flow scheduled 2027-01-01 02:01 | activation row (created_by_app=1); folder + placeholder; 1 INFO mail `overlap:start:FY27-28`; rows run order FY27-28 then FY26-27; prefixes `FY27-28`,`FY26-27` | rclone argv captured by fake | same | S | U |
| T-a3 | after T-a2 | rollover check 06:00 | no 2nd row/mail; sibling: 0 mails | `notifications` count unchanged | same | H | U |
| T-a4 | after T-a3 | LAN 2027-01-01 21:01 | NAS `\\nas\share\FY27-28` + marker created; exactly 1 wake, 1 shutdown call; 2 LAN rows | call counters on WoL/shutdown fakes | same | S | U |
| T-a5 | after T-a4, `/status` at 2027-01-02 09:00 | GET /status | `folders[0].fy=FY27-28 role=early active_since=2027-01-01T02:01..`; both green | hand-written expected JSON subset | tests/test_ov_timeline_a_ui.py | H | U |
| T-a6 | as T-a1 but clock UNVERIFIED on Jan 1-2, OK on Jan 3 | 3 days of flows | no row Jan 1-2; ACTION_REQUIRED mail once after 24 h; activation Jan 3; **dashboard item while deferred (D3)** | mail count from SMTP server | tests/test_ov_review_d3.py | H | U |
| T-b1 | activated; Jan 5-7 nightly cloud | FY26-27 fake BLOCKED (G3) on Jan 6 only | Jan 6: FY27-28 COMPLETE, FY26-27 BLOCKED, approval fy=FY26-27, flow FAILED; FY27-28 card green; no RECOVERED for FY27-28; Jan 7 FY26-27 success -> RECOVERED key `recovered:FY26-27:cloud` | hand counts | tests/test_ov_timeline_b.py | S | U |
| T-b2 | activated; LAN Jan-Mar 10 nights | FY27-28 PARTIAL on night 4 only | 10 wakes, 9 shutdowns, night 4 NAS on; `nas_shutdown` attention item names last run; audit Sunday both FYs | WoL/shutdown call log | same | S | U |
| T-b3 | activated; real rigs | 3 lab nights, delete 5 files in FY27-28 (9-file folder) night 2 | G3 small-folder passes 1 rename; blocks 11 of 99 | file counts on disk | audit_lab/test_w26_overlap_nights.py | S | L |
| T-b4 | D4 setup: FY27-28 adopted with 2 GB, `cloud_max_hours` tiny | 3 nights | FY26-27 `CLOUD_MISSED` x3 (documents D4); owner decision recorded | rows | tests/test_ov_review_d4.py | H | U |
| T-c1 | FY27-28 active since Jan 1 with 90 days success rows, 1 PENDING FY27-28 approval, 1 open FY27-28 incident, 1 PENDING FY26-27 approval | cloud 2027-04-01 02:01 | both FYs backed up (pending branch) | dates | tests/test_ov_timeline_c.py | S | U |
| T-c2 | after T-c1 | rollover 06:00 | final backup FY26-27 only (1 cloud + 1 LAN row with `-final-`); config -> FY27-28; archive argv `gs://b/FY26-27/` only; FY26-27 approvals CLOSED; FY27-28 approval still PENDING and runnable with fy=FY27-28 | argv + DB | same | S | U |
| T-c3 | after T-c2 | LAN 2027-04-01 21:01 | one folder FY27-28; G4 passes (same dest string); G2 baseline = Mar 31 FY27-28 row; no first-run block | `guard_baseline` row id | same | S | U |
| T-c4 | after T-c3 | /status + weekly report Mon 2027-04-05 | one card; FY26-27 in report only up to switch time; RECOVERED of FY27-28 incident threads into its own old thread | report body grep; Message-ID headers | same | H | U |
| T-c5 | **D1**: backup flow started 06:05 Apr 1 while rollover runs | hook switch+close inside slot wait | no pipeline call and no row for FY26-27 after close | fake pipeline call list | tests/test_ov_review_d1.py | S | U |
| T-d1 | overlap on, activation deferred all quarter (no create right), `FY27-28` absent | 2027-04-01 06:00 rollover | `create_source_folder_early` fails -> WARNING; final backups run; switch blocked until folder exists; FY26-27 still backed up nightly | journal states | tests/test_ov_timeline_d.py | S | U |
| T-d2 | overlap OFF all year | 1 Apr rollover | old behaviour: folder + NAS folder created, switch, first FY27-28 run no first-run block (empty prefix); report has no false "no backup ran" | rows/report | same | H | U |
| T-e1 | activated Jan 1; 20 days of rows | overlap off on Jan 21 (Settings, typed phrase) | Jan 21 runs: FY26-27 only; sibling alarm daily `overlap:unprotected:FY27-28` (files); early PENDING approval hidden in banner (N6) but **approve returns 409 (D9)** | SMTP count; HTTP code | tests/test_ov_timeline_e.py | S | U |
| T-e2 | after T-e1 | overlap back on Feb 10 | FY27-28 active at once (row kept), no 2nd INFO mail; G2 compares to Jan 20 baseline; sibling RESOLVED once | mails | same | H | U |
| T-e3 | after T-e1 | weekly report Mon Feb 1 | **D10**: FY27-28 section absent or no "no backup ran" after Jan 21 (owner picks the rule) | report text | same | H | U |
| T-f1 | Apr 1: crash after `config_switch` write, before journal update (R3) | LAN 21:01, then rollover Apr 2 06:00 | Apr 1 night: FY27-28 only; Apr 2: switch closed, FY26-27 closed, archive done | journal + approvals | tests/test_ov_timeline_f.py | S | U |
| T-f2 | Apr 1: archive fails (gcloud exit 1) | Apr 1-3 rollover checks | WARNING then retries `gs://b/FY26-27/` only; never FY27-28 argv; overlap inactive after switch | argv list | same | H | U |
| T-f3 | **D2**: `close_fy` raises once at Apr 1 | rollover Apr 2 | FY26-27 approvals CLOSED on Apr 2 (today: stay PENDING) | DB | tests/test_ov_review_d2.py | H | U |
| T-f4 | trial box, fake clock not possible -> test prefixes | real Prefect: start rollover, trigger scheduled cloud run during final backup | D1 on the live stack: run rows + GCS listing of `test-FY26-27/` storage classes | `gcloud storage ls -L` | agent_runs live script | S | X |

### Single-point cards

| ID | Setup | Action | Expected | Oracle | Target | M | L |
|---|---|---|---|---|---|---|---|
| P-1 | early folder adopted, holds `a.xlsx`, case `fy27-28` | activation + cloud | no placeholder written; prefix `FY27-28` (upper); row source_path keeps real spelling | listdir | tests/test_ov_review_points.py | H | U |
| P-2 | source created, placeholder write raises, next night OK | 2 nights | night 1 no row, meta pending; night 2 row created_by_app=1, one INFO mail | DB | same | H | U |
| P-3 | activation done; NAS share reachable, early NAS folder deleted later | LAN run | **D5**: G5 asks (not silently re-created) | marker absent | same | S | U |
| P-4 | NAS `FY27-28` exists with files, no marker | first LAN night | FY27-28 BLOCKED G5 AAM-NAS-10 + approval fy=FY27-28; FY26-27 COMPLETE; NAS shutdown not voted -> stays on | rows + shutdown calls | same | S | U |
| P-5 | cloud prefix `FY27-28` pre-filled (3 objs) not in source | first cloud night | G3 first-run block; one approval; after approve: 3 deletes, next night normal | rclone dry-run counts | same | H | U |
| P-6 | `config_switch` journal state forced SKIPPED | `active_folders` on Apr 2 | **D6**: same answer as `rj.is_finished` | constant | same | H | U |
| P-7 | LAN loop holds slot past 06:00 Apr 1 | rollover | **D7**: G6 block kind; proposal: WARNING + retry, no approval | approvals table | tests/test_ov_review_d7.py | S | U |
| P-8 | audit FAILED FY26-27 on Mar 28 | switch | **D8**: audit incident closed for FY26-27 | notifications | same file as D2 | H | U |
| P-9 | `Z:\FY26-27` lan path, overlap on | load_config | **D11** decision | ValueError or not | tests/test_ov_review_points.py | H | U |
| P-10 | activated Jan 1; early_start_month 1 -> 3 on Feb 5 | Feb 6 run | **D12**: FY27-28 still active (proposal) | rows | same | H | U |
| P-11 | 2027-03-31 21:01 LAN runs to 2027-04-01 03:10 | rows + report March monthly | both FY rows dated Mar 31 start; March report counts both full month | dates | same | H | U |
| P-12 | junction `FY27-28` pre-activation | 30 days | **D3** item + daily repeat | attention codes | tests/test_ov_review_d3.py | H | U |
| P-13 | integrity audit Sun 2027-04-04 after switch | audit flow | only FY27-28 audited; no row for FY26-27 | integrity_audits | tests/test_ov_review_points.py | H | U |

---

## 3. Verdict

- **Release-blocking for a December 2026 deploy: none.** Every finding is either about 1 January (fix before then) or
  about 1 April 2027 (fix before then). None touches the current FY's data on a normal night.
- **Must be fixed before 1 January 2027 (blocking for the January activation):** D3 (silent 3-month miss with a single
  mail). D4 needs an explicit owner decision recorded (keep D-4 or protect the configured FY while the early FY has no
  success yet).
- **Must be fixed before 1 April 2027:** D2 (closed FY never cleaned up, leading to approvals that silently do nothing)
  and D1 (closed FY synced into the archived prefix after the switch). D7 and D9 are strongly recommended.
- **Owner decision or nice to have:** D5, D6, D8, D10, D11, D12, D13 (WP12 already planned).
