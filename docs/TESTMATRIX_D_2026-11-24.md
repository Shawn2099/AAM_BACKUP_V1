# Test matrix - Part D (docs, targeted audit lab, trial box) - written 2026-11-24, before the build

Binding plan: `docs/PLAN_BASELINE_OVERLAP_2026-11-23.md` (rev 2 FINAL) section 9 Part D, plus sections 2-6, 10-12.
Inputs: `docs/PROGRESS_BASELINE_OVERLAP_2026-11-23.md`, `docs/TESTMATRIX_B7_2026-11-24.md` (decisions Q1-Q12),
`audit_lab/labconf.py`, `audit_lab/conftest.py`, `audit_lab/test_w6_flow.py`, `audit_lab/test_w6b_rollover_nas_off.py`,
`audit_lab/test_w5c_ops.py`, `core/fy_overlap.py`, `SESSION_HANDOFF.md` (deploy recipe lines 154-176, 13-28).
Line numbers are from worktree HEAD `505afce` (B7 uncommitted). Ambiguities are decided here (marked **[DEC]**);
owner-only decisions are listed at the end (section 6) and are NOT decided here.

**Order and entry gate for Part D.** D1 is written after B7, B8, B9, C1, C2, C3 are committed (D1 text describes
built code only; any point below whose task is not committed yet is written as "planned (task Xn)"). D2 can be built
in parallel with D1 once B9 is in (it drives `flow.backup` / `rollover_check_flow` / `integrity_audit_flow` only, no
UI). D3 needs: full unit suite green (only the 2 known failures of PROGRESS "Setup facts"), D2 green, the Opus review
of Parts B/C passed, and the owner's explicit go (PROGRESS: "the trial box is untouched until the owner says we are
confident"). Opus review fact kept: **do not run the box past 31 Dec without B8 and B9** (reports and the night strip
would mix both FYs).

---

## 1. Facts found while writing this matrix (they drive D2 and D3)

| # | Fact | Where | Consequence |
|---|---|---|---|
| F-D1 | The overlap is active while `db_meta['overlap:<next>']` exists **without `ended_at`** (and `overlap_enabled`). The activation date is read only by `activate_if_due`. | `core/fy_overlap.py:127` `active_passes`, `:343` `active_next_fy`, `:261` | Putting `overlap_activate_on` back to `12-31` on the box does **not** end the test overlap. D3 must remove the record (DB restore, section 4). |
| F-D2 | `activate_if_due` returns ALREADY_ACTIVE whenever the record exists, even with `ended_at`. | `core/fy_overlap.py:261-262` | Stamping `ended_at` on the test record would block the real 31 Dec activation forever. Never use it as the D3 cleanup. |
| F-D3 | `guard_baseline(leg, fy)` = newest success row whose `$.guards.fy` = fy, no date limit. | `core/manifest.py:858-880` | Test rows of FY27-28 left in the box DB become the G2 / G4 baseline on 31 Dec (real folder = placeholder only) -> false `*_BLOCKED` on the first real night. They must not stay in the live DB. |
| F-D4 | The activation INFO mail is sent once, checked with `last_notification`. | plan section 3 step 5 | A test notification row left in the live DB suppresses the real 31 Dec INFO mail. Same cleanup as F-D3. |
| F-D5 | `core/fy_overlap.py` imports `pendulum`, `check_clock`, `notify`, `get_fy_prefix`, `now_iso` by name. | `core/fy_overlap.py:19-29` | Lab fakes must patch `core.fy_overlap.check_clock` / `core.fy_overlap.notify`; `labclock.fake_clock(..., modules=CLOCK_MODULES + ("core.fy_overlap",))` when the clock is pinned. |
| F-D6 | Cloud preflight probe B lists `aam_gcs:<bucket>/<FY>`; the comment says GCS returns `[]` exit 0 for an empty prefix. The lab's local backend fails on a missing folder. | `core/cloud_preflight.py:99-145`, `test_w6_flow.py:62-64` | D2 pre-creates `buckets/lab-bucket/FY27-28` (lab artefact, documented in the test). The empty-prefix first run on real GCS is proven only in D3 (check D3-N1.4). |
| F-D7 | The backup slot is taken **per pipeline pass** (`_pipeline_slot` `flow.py:623-632`, used at `:1695`, `:2081`); the audit takes it once (`:2936`, `:3203`). | flow.py | A manual run queued for the slot can run between pass 1 and pass 2 of a nightly run (writes stay serialised). Not a bug; D1 writes it as a fact. |
| F-D8 | Old code (`9461ac2`) has `extra="forbid"` models and no overlap / `scheduled` keys. | `models/config.py` StrictModel | A rollback with a config.yaml that contains the new keys fails at load (services do not start). D3 rollback restores the pre-deploy `config.yaml` copy. |
| F-D9 | The lab `build_config` forces `lan.shutdown_after_backup: False`, `wol.enabled: False`, and replaces `core.shutdown.shutdown_server` with a raising guard. | `audit_lab/labconf.py:176-195, 246-256` | The D2 rig must keep both and assert them (never power off this PC; the loopback "NAS" is this PC). |
| F-D10 | The existing `rig` fixture (`test_w6_flow.py:44`) uses `FY26-26` and a NAS folder directly under `nas\` (no `backup` parent). | `test_w6_flow.py:49-52` | D2 builds its own fixture with the owner layout (`source\FY26-27`, `nas\backup\FY26-27`); it may copy code from `rig`, not import it. |
| F-D11 | No `.venv` in the worktree; the main checkout's venv is used with `PYTHONPATH=.`. | PROGRESS "Setup facts" | D2 must assert `flow.__file__` is inside the worktree before anything runs (else it silently tests the `fy-overlap` branch code of the main checkout). |

---

## 2. D1 - Docs (Haiku, effort medium; Opus fact-checks the diff)

Rules for every D1 chunk: edit only the sections named; describe built code only (check each sentence against the
code by grep, cite `file:function`); keep the existing style (tables, plain words, IST); no new section numbers
except where named; never open `SESSION_HANDOFF.md`, `flow.py`, `ui.py` whole (grep / offset). Git Bash heredocs
mangle backslashes: edit with the Edit tool only.

### 2.1 `docs/ARCHITECTURE.md`

| Chunk | Section | What to write (text points) |
|---|---|---|
| D1-a | §2 Schedules (after the cron table, before the slot paragraph) | (1) "FY overlap": from `rollover.overlap_activate_on` (default `12-31`, IST) until the April switch, every `backup-cloud` / `backup-lan` run runs one pass per active FY, configured FY first then the next FY; the weekly audit runs next FY first. Active = `rollover.overlap_enabled` and `db_meta['overlap:<next FY>']` without `ended_at` (D-1). (2) Both passes share the leg's run budget (same start, `late_seconds` only on the first pass, D-7), so `schedule_problems` stays a true proof; no new budget key (D-13). (3) Manual-only leg: `cloud.scheduled` / `lan.scheduled` false = deployment registered with no schedule (`serve.py`), kept resumed by `launch._reconcile_disabled_legs`, skipped by the budget check; at least one enabled leg must be scheduled (config refuses otherwise). (4) Slot fact F-D7. |
| D1-a | §3.1 Entry `backup()` | Replace the tree with the built signature `backup(config_path, mode, approval_id, budget_hours, fy)`. New nodes: `fy` guard (not active -> ValueError, FAILED, no row); `fy_overlap.active_passes(...)` read once (DB error reading the record -> configured FY only, flow ends FAILED with a note, review F2); per leg `for p in passes` with `fy_pass`, `budget_start`, `allow_shutdown` (D-8: earlier passes never shut down; the last pass only if every earlier LAN pass ended `LAN_COMPLETE`, or `lan.shutdown_on_all_retries_exhausted`); race guard (configured FY re-read before every later pass; a switch skips every later pass of both legs -> `PartialRun`, FAILED); a failed pass never stops the next; one active pass -> `fy_pass=None` = exactly the baseline call. Then a short table "what differs for a next-FY pass" condensed from plan 2.3: paths (`fy_overlap.config_for_fy`), run id `-FY27-28`, `extended_metrics.fy` / `overlap`, run-log folder `..._FY27-28` and file names `<date>_<HHmm>_<leg>_FY27-28_<step>.log`, artifact key `<leg>-backup-summary-fy27-28`, alert keys `backup:<leg>:FY27-28:<status>` / `blocked:` / `missed:` / `recovered:`, titles "CLOUD backup (FY27-28) failed" (both passes name their FY while active), incident group `<leg>:FY27-28`, approvals per (leg, FY) (`keep_fy`, FY-mismatch refused), no `file_entries` / cross-check (D-9), NAS folder by `ensure_pass_nas_folder` after WoL (parent unreachable or wrong leaf -> `LAN_FAILED` AAM-LAN-01; files without marker -> G5), MISSED / SLOT_TIMEOUT rows carry `fy` (F5), abandoned sweep keyed with FY (F7). |
| D1-a | §3.2, §3.3 (one line each, top) | "During the FY overlap this pipeline runs once per active FY on a per-pass config copy; see §3.1 for what differs." |
| D1-b | §5.1 `integrity_audit_flow` | Built shape (B6): signature `(config_path, mode, fy)`; one slot, one `RunBudget` (`budget.audit_max_hours`); per leg next FY first, then configured; LAN one wake per leg, a failed wake not retried for the second pass, one shutdown after the last pass only if a wake happened; suffixed audit ids `<uuid>-<leg>-full-FY27-28`, `latest_audit(leg, fy=)`; outcome keys `cloud@FY27-28=<status>`; `_pending_context(config, db, leg, fy)` uses that FY's last success; the scheduled cron run (`_auto_scheduled_run()`) skips `scheduled: false` legs, a run started by hand ("Check now") audits every enabled leg; `fy=` audits one active FY (inactive -> ValueError); DB error reading the record -> configured FY audited, FAILED. |
| D1-b | §5.2 `rollover_check_flow` | Insert after `log_retention_task`: "`fy_overlap.activate_if_due` (before `_alarm_unprotected_new_fy` and the journal; never raises)": conditions (enabled, FY source folder, no record, today IST >= activation date, classify NONE or FORWARD, `check_clock` = CLOCK_OK), effect (source folder + placeholder only, link / junction refused, record written last, INFO mail `rollover:activated:<fy>` to the reports list, once), waiting (AAM-ROL-07 `rollover:activation:<fy>`, WARNING on the day, ACTION_REQUIRED after, cooldown `alerts.stale_repeat_hours`, retried every morning and by "Run FY rollover now"), and the E1 rule: no backup, NAS, WoL, approval or failed run can delay it, only an unproven clock. Step 1 `final_backups` while the overlap is open: per-leg `fy_overlap.final_copy_state` (required = enabled legs; scheduled leg runs its final now and, if that fails, an earlier verified copy since C counts with a warning; unscheduled leg is not run and needs a manual copy, marker `<LEG>_MANUAL_FINAL_NEEDED`; `<LEG>_NOT_RUN`), G6 gets `overlap_active` + hints (text only, same verdicts), the next daily check switches by itself when the last blocker clears, forced switch = G6 approval (typed `APPROVE`) and the switch mail names forced legs (`announce_text`); state kept in the overlap record for a resume (`save_final_copy` / `load_final_copy`, review G3). After `config_switch`: `end_overlap` (old-FY approvals superseded, `ended_at` stamped). `unprotected_new_fy` is silent while the overlap of that FY is open. Overlap off = baseline (a manual-only NAS leg is then still run by the check, plan §12). |
| D1-c | §10.1 | C2: add "`fy=` must be an active FY (else ValueError, FAILED, no row)". C6: "per FY: `stale:<leg>:<fy>` for the next FY (counted from `activated_at`, B7 Q5), titles name the FY while the overlap is active (Q8); manual-only legs never get a stale mail (C2)". |
| D1-c | §10.7 rows R3, R7 | R3 Plan column: "while the overlap is open: R11". R7: "silent while the overlap of that FY is open (B4)". |
| D1-c | §10.7 new rows R9-R12 (columns #, Condition, Today, Plan; Today = "yes (task)") | **R9 Activation**: due on/after `rollover.overlap_activate_on` (IST), only NONE / FORWARD, only on CLOCK_OK; creates the source folder + placeholder only (never the NAS folder, never `config.yaml`); idempotent (record last; crash before it repeats safely); link / junction refused; waits and mails AAM-ROL-07 otherwise; never blocked by backups. **R10 Overlap passes**: active set D-1 (folder existence does not decide: a deleted next folder fails loudly in its health check); every next-FY pass passes all gates of 10.1-10.6 for its own FY (G1-G5 with `guard_baseline(leg, fy)`); the next-FY NAS destination must end in its FY or the pass is refused (last barrier against a mirror into the share parent - keep it); `ensure_nas_folder` rules (missing -> created + marker, empty -> marker, files without marker -> G5, parent missing -> AAM-LAN-01); shared budget -> `<LEG>_MISSED` for that FY; D-8 shutdown; no `file_entries`; race guard. **R11 Final-copy rule**: a leg counts when a row `mode = leg`, `guards.fy = old FY`, `started_at >= 1 Apr 00:00 IST` (boundary included), status in `FINAL_COPY_STATUSES` (`CLOUD_COMPLETE`, `CLOUD_NO_CHANGES_COMPLETE`, `LAN_COMPLETE`); PARTIAL / BLOCKED / FAILED / VERIFY_FAILED / MISSED / legacy statuses never count; a next-FY row never counts; a run before C never counts; how it started does not matter; switch automatic at the next daily check; forced switch only by approval; a copy older than this check = warning in the switch mail. **R12 Manual-only legs**: `enabled: true, scheduled: false`; registered without cron, resumed (C1); no `stale:` / `first_run:` / `catching_up:` item and no watchdog mail for any FY; a failed manual run is still red + failure mail; skipped by the scheduled audit; reports "run by hand", 0 scheduled; April: not run by the check, needs one manual final copy. |
| D1-d | §6 file map (`core/` table) | Row `core/fy_overlap.py`: pure helpers (`next_fy`, `prev_fy`, `activation_date`, `closing_moment`, `fy_of_id`, `config_for_fy`, `active_passes`, `row_fy`), activation (`activate_if_due`), NAS folder (`ensure_nas_folder`, `ensure_pass_nas_folder`), April (`final_copy_state`, `outstanding_legs`, `announce_text`, `end_overlap`, `save_final_copy`, `load_final_copy`), attention helpers (`overlap_open`, `active_next_fy`). |
| D1-d | §7 Data written | `run_history`: next-FY rows have run id suffix `-FY27-28`, `extended_metrics.fy` / `overlap` (no schema change, user_version 5); `integrity_audits`: audit id suffix; `notifications.mode` = `<leg>:<fy>` for next-FY incidents; `approvals.fy` per (leg, FY); new row "`manifest.db` > `db_meta`": `overlap:<fy>` (`activated_at`, `from_fy`, `source_folder`, `clock`, `ended_at`, `final_copy_state`) and `schedule_in_effect` (+ `cloud_scheduled` / `lan_scheduled`); run_logs folder / file name with `_FY27-28`. |
| D1-d | §8 Tunables | `rollover`: + `overlap_enabled` (default true; missing = true), `overlap_activate_on` (`MM-DD`, default `12-31`, `02-29` and `04-01` refused); `cloud` / `lan`: + `scheduled` (default true, manual only when false). All four are risky keys (typed phrase). |
| D1-d | §9 Accepted risks (new bullet list "FY overlap - accepted gaps") | (1) **F4** NAS left on for a day when the last LAN pass is missed / skipped / slot timeout (owner decision 2026-11-23). (2) **Q10** after the switch the new configured FY's `fy=None` lookups show the old FY's rows until its first run (one day) and the old FY's audit until the next weekly audit; texts name the row's real FY (`prev_fy`), never the new one. (3) Plan §11: files edited in the old FY after the switch are not backed up; a leg counted from an earlier copy misses changes after that copy (the switch mail says so); a flow waiting for the slot during the switch can run one pass with the old view (race guard closes it between passes); next-FY passes do not fill `file_entries` (the files counters show the configured FY); a NAS FY folder at a share root is out of scope. (4) Review G4: after a DB error at `end_overlap` the record can stay without `ended_at` (harmless: the config already points at the new FY). (5) Slot between passes (F-D7). (6) B5 limits: the calendar simulation uses temp sibling folders, not a drive root, and the clock skew direction is not modelled. (7) Lab vs GCS: the empty next-FY prefix is proven on real GCS only in D3 (F-D6). (8) Real NAS shutdown not tested (owner deferred until near the NAS). (9) Q9 fail-closed: a record without a readable `activated_at` = RED `stale:<leg>:<fy>`; a DB error reading the record = RED `overlap:unknown`. |

### 2.2 `docs/EMAIL_CONTENT_SPEC.md` (chunk D1-e)

| Section | Text points |
|---|---|
| §4.6 AAM-ROL-07 row (already there) | Check against `core/faults.py:267` and `fy_overlap._not_added`: the code sends ROL-07 for three reasons (clock not proven, the folder is a link / junction, the folder could not be made); write "{reason}" in "What happened", key `rollover:activation:<fy>`, WARNING on the activation day, ACTION NEEDED from the next day, cooldown `alerts.stale_repeat_hours`. Do not change the fault catalog title. |
| §4.6 AAM-ROL-02 row | While the overlap is open: "until then both {from} and {to} are backed up every night" and one line per outstanding leg from `fy_overlap._hint` (quote the four built strings exactly). |
| §4.6 AAM-ROL-06 row | "Not sent while the overlap that adds {to} is active (both years are backed up)." |
| §4.3 / §4.2 AAM-LAN-01 | Add: also used when the next year's NAS folder cannot be made (share parent not reachable) or the destination is not that year's folder. |
| §7.1-7.4, §7.12 | Per-FY titles and keys: while the overlap is active the title names the FY ("CLOUD backup (FY27-28) failed"); keys `backup:<leg>:<fy>:<status>`, `blocked:<leg>:<fy>`, `missed:<leg>:<fy>`, `recovered:<leg>:<fy>`; cooldown and incident per `<leg>:<fy>`, so a FY27-28 RESOLVED never closes a FY26-27 problem and vice versa. Configured-FY keys unchanged. |
| §7.7 overdue | `stale:<leg>:<fy>`, title "LAN backup (FY27-28) overdue" (configured: "LAN backup (FY26-27) overdue" during the overlap, "LAN backup overdue" outside it); next FY counted from activation; never for a manual-only leg. |
| §7.8 audit | Outcome keys `cloud@FY27-28=<status>`; the text names the FY. |
| §7.10 Financial year | New INFO "FY27-28 is added to the backups" (`rollover:activated:<fy>`, reports list, once; quote the built message); waiting mail = ROL-07; the "New financial year started" mail adds `announce_text` lines (copy time per leg, forced legs). |
| §7.13 reports | Sections per (leg, FY), next-FY counts from `activated_at`, manual-only leg "run by hand" with 0 scheduled, `_next_text` "this backup runs only when started from the dashboard" (write only what B8 / C2 built). |
| §7 samples note | Check that `core/email_samples.py` has samples for ROL-07, `rollover:activated:` and a next-FY title (the sample test fails when a fault code has none); report a gap, do not add samples in D1 (code change). |

### 2.3 `SESSION_HANDOFF.md` (chunk D1-f; read only lines 1-60; insert a new block after line 11 `---`)

New top block "2026-11-2x - FY overlap on the baseline (branch `baseline-v1-overlap`)": (1) where: worktree path,
base tag `baseline-v1` = `9461ac2` = box version; read order PLAN -> PROGRESS -> TESTMATRIX_B7 / this file;
(2) status: tasks done with commits (copy from PROGRESS), suite count, the 2 known failures + 1 flaky; (3) box
state: untouched at `9461ac2` until D3 (or the D3 result once run); (4) owner decisions: A1-A5, F4 accepted,
manual-only NAS with overlap off kept, Q-decisions of TESTMATRIX_B7 (one line, pointer); (5) how to run tests
without a worktree venv (PROGRESS line 8) and D2 (section 3.4 below); (6) D3 recipe pointer (section 4 here) and the
gotchas F-D1, F-D2, F-D3, F-D8; never power off this PC; clean `PREFECT_HOME`. Also update the "Last updated" line 3
to point at the new block. Do not edit older blocks.

### 2.4 D1 checks (Opus, after the Haiku edits)

| # | Check | Pass when |
|---|---|---|
| D1-K1 | Every function / key / code / status named in the new text exists | `grep` hits in code for each name (Haiku lists them in its report) |
| D1-K2 | No text for an uncommitted task presented as built | each "planned (Xn)" maps to an open plan checkbox |
| D1-K3 | R9-R12 match plan §3-§5 and the B4/B6/B7 decisions | side-by-side read |
| D1-K4 | Diff only in the named sections | `git diff --stat` = 3 files; hunks inside the sections |
| D1-K5 | No mangled backslashes (`E:\FY26-27`, `\\nas\backup`) | grep the diff for `\n`/`\t` artefacts |

---

## 3. D2 - targeted audit lab `audit_lab/test_fy_overlap_lab.py` (Sonnet, effort medium)

### 3.1 Rig (fixture `overlap_rig`, in the new file; copy patterns from `test_w6_flow.rig`, do not import it)

| Item | Value / rule |
|---|---|
| Layout (owner layout under the sandbox) | `<case>\source\FY26-27\Clients\...` (60 files, 2 sub-folders, one 0-byte, one Unicode name); `<case>\nas\backup\FY26-27\` + `.AAM_TARGET_MOUNTED` (`L.make_canary`); `nas\backup` is the share parent; `<case>\buckets\lab-bucket\FY26-27\` and `...\FY27-28\` pre-created (F-D6, comment why); `<case>\runtime\`. |
| Config | `L.build_config(sandbox, source=<case>\source\FY26-27, lan_dest=<case>\nas\backup\FY26-27, runtime=..., rollover={"overlap_enabled": True})` -> NAS path becomes `\\127.0.0.1\C$\...\nas\backup\FY26-27`; written to `config.yaml` with `write_lab_config` logic (the flows load the file). Default guards (G1-G3 defaults: 500 files / 10 % / 20 % shrink; only `forbid_local_destination: False`). |
| Tools | real `deploy\bin\rclone.exe` with the local `[aam_gcs]` remote (`L.write_local_rclone_config`, `L.patch_cloud_modules_to_local`, `flow.temp_rclone_config` patched, `chdir(buckets)`); real robocopy with `/ZB` stripped (`L.strip_zb` on `ls.build_robocopy_command`); restore both in teardown. |
| Prefect | `PREFECT_TEST_MODE` removed, `prefect_test_harness()`, `launch._ensure_concurrency_limit()` (real `aam-backup` slot). `PREFECT_HOME` must be a clean temp dir (set by the runner, section 3.4; the fixture asserts it is set and not `~/.prefect`). |
| Fakes (only these) | `flow.notify` and `core.fy_overlap.notify` -> recorder lists; `core.fy_overlap.check_clock` (and `flow.check_clock` when the rollover flow is run) -> CLOCK_OK result object (build it with the real `core.health` result type); `flow.wol_check_task` -> no-op recorder; `flow.lan_shutdown_task` -> recorder MagicMock (`with_options` returns itself). |
| Never power off this PC | Fixture asserts before yielding: `cfg.lan.shutdown_after_backup is False`, `cfg.wol.enabled is False`, `core.shutdown.shutdown_server.__name__ == "<lambda>"` (the lab guard), and `flow.lan_shutdown_task` is the recorder. No scenario may set either key true. |
| Code under test | Assert `Path(flow.__file__).resolve()` is inside the worktree (F-D11). |
| Activation helper | `_activate(rig)`: `fy_overlap.activate_if_due(cfg, db, now=datetime(2027, 1, 5, 10, 0, tzinfo=IST))` with the fakes; asserts `ACTIVATED`. **[DEC]** No pinned clock except in S1: the helper passes `now`; `activated_at` is the real time (harmless in the lab). |
| Helpers | `_rows(cfg_path)` (all `run_history` rows, oldest first, with `fy_of_id`), `_tree_md5(path)` (relative path -> md5, canary and placeholder excluded), `_bucket(fy)`, `_nas(fy)`, `_approvals()`. Every scenario records evidence with `L.record("FYOV.<id>", ...)`. |
| Timeout | `pytestmark = pytest.mark.timeout(1800)`; one Prefect harness per test (6 tests). |

### 3.2 Scenario matrix

| ID | Test function | Setup | Action | Must hold (assert) |
|---|---|---|---|---|
| S0 | `test_fyov_before_activation_is_baseline` | rig, no record | `flow.backup(mode="all")` | exactly 2 new rows (`cloud`, `lan`), unsuffixed run ids, no `fy`/`overlap` metric; `source\FY27-28` and `nas\backup\FY27-28` do not exist; `buckets\lab-bucket\FY27-28` still empty; flow COMPLETED. |
| S1 | same test, second half: `..._then_activation_by_daily_check` | after S0 | `flow.rollover_check_flow()` under `labclock.fake_clock("2027-01-05 06:00 IST", modules=CLOCK_MODULES + ("core.fy_overlap",))` | `source\FY27-28\README_FY.txt` exists; `overlap_record("FY27-28")` has `activated_at`, `from_fy == "FY26-27"`; one INFO event key `rollover:activated:FY27-28`; `nas\backup\FY27-28` NOT made (NAS-independent); `config.yaml` bytes unchanged (sha256); a second call: no second mail, record unchanged. |
| S2 | `test_fyov_two_mirrors_two_prefixes` | rig + `_activate`; 20 files in `source\FY27-28\` incl. `Clients\f_0001.txt` (same relative path as a FY26-27 file, different content) | `flow.backup(mode="all")` | 4 new rows, order by `started_at`: `cloud`, `cloud-FY27-28`, `lan`, `lan-FY27-28`; all `*_COMPLETE`; suffixed rows have `fy == "FY27-28"`, `overlap is True`; `_tree_md5(bucket FY26-27) == _tree_md5(source FY26-27)`, same for FY27-28; NAS FY26-27 and FY27-28 mirrors equal their sources; `nas\backup\FY27-28\.AAM_TARGET_MOUNTED` made by the pass; the shared file has each FY's own content in each target; no FY27-28-only name in any FY26-27 target and vice versa; `file_entries` count after == count after an FY26-27-only run (D-9; take the count after S0-style run first); run-log folders `*_FY27-28` and log files `*_cloud_FY27-28_*.log`, `*_lan_FY27-28_*.log` exist, the robocopy log reads as UTF-16 and holds the FY27-28 paths; flow COMPLETED; `lan_shutdown_task` recorder not called (shutdown off). |
| S3 | same test, continued: `..._delete_in_next_fy_never_touches_current` | after S2: snapshot `_tree_md5` + file mtimes of bucket FY26-27 and NAS FY26-27 | delete `source\FY27-28\Clients\f_0001.txt` (1 of 20 = 5 %, under G3), `flow.backup(mode="all")` | 4 rows COMPLETE; the file is gone from bucket FY27-28 and NAS FY27-28; FY26-27 targets byte-identical to the snapshot (md5 map, count and mtimes) and still hold their own `Clients\f_0001.txt`. |
| S4 | `test_fyov_g3_blocks_per_fy` | rig + `_activate`, first `backup(all)` (4 COMPLETE) | delete 10 of 20 files in `source\FY27-28` (50 % > 10 %), `backup(all)` | `cloud` and `lan` COMPLETE; `cloud-FY27-28` and `lan-FY27-28` `*_BLOCKED` with guard G3; FY27-28 targets unchanged (count before == after); 2 open approvals with `fy == "FY27-28"`; flow FAILED (Prefect state) while FY26-27 rows COMPLETE. Then delete 10 of 60 in FY26-27 too (>10 %), `backup(all)`: 4 BLOCKED, 4 open approvals, one per (leg, FY) (the FY26-27 block did not supersede the FY27-28 ones, `keep_fy`). (a) Approve the cloud FY27-28 request (`db.approve_request`, pattern `test_w5c_ops.py:98-107`) and run `backup(mode="cloud", approval_id=<it>)` without `fy`: the FY26-27 pass is NOT unblocked by the FY27-28 approval (row `cloud` BLOCKED again, bucket FY26-27 unchanged), the FY27-28 pass uses it (`cloud-FY27-28` COMPLETE, bucket FY27-28 == source FY27-28, approval USED by that run id). (b) Approve the LAN FY26-27 request and run `backup(mode="lan", approval_id=<it>, fy="FY26-27")`: exactly one new row `lan` COMPLETE (NAS FY26-27 == source), no `lan-FY27-28` row, the LAN FY27-28 approval still PENDING. |
| S5 | `test_fyov_g5_per_fy` | rig + `_activate`; pre-create `nas\backup\FY27-28\office.xlsx` (no marker); 5 files in source FY27-28 | `backup(mode="lan")` | `lan` COMPLETE; `lan-FY27-28` `LAN_BLOCKED` guard G5_CANARY; `office.xlsx` untouched and nothing else written in `nas\backup\FY27-28`; one approval fy FY27-28. Approve, `backup(mode="lan", approval_id=..., fy="FY27-28")`: marker made, mirror == source (office file removed by the mirror only after approval - assert it is gone and the run is COMPLETE), approval USED. Then remove the FY26-27 marker, `backup(mode="lan")`: `lan` BLOCKED (G5), `lan-FY27-28` COMPLETE (a block in pass 1 never stops pass 2). |
| S5b | same test or own: `test_fyov_empty_nas_folder_is_marked` | fresh rig + `_activate`; empty `nas\backup\FY27-28\` | `backup(mode="lan")` | `lan-FY27-28` COMPLETE without approval, marker written (`NAS_MARKED`). |
| S6 | `test_fyov_weekly_audit_both_fys` | after a `backup(all)` with 4 COMPLETE | `flow.integrity_audit_flow(mode="all")`; then overwrite one FY27-28 NAS file with same-size different bytes, run again | first run: 4 `integrity_audits` rows, ids `...-cloud-full-FY27-28` / `...-lan-full-FY27-28` and 2 unsuffixed, all VERIFIED; order next FY first per leg; `wol_check_task` recorder called once for LAN; second run: `lan` FY27-28 `VERIFICATION_FAILED`, FY26-27 `lan` VERIFIED, alert key contains `lan@FY27-28=VERIFICATION_FAILED`. |
| S7 | `test_fyov_manual_only_lan_and_overlap_off` | rig with `lan={"scheduled": False}` + `_activate` | `backup(mode="lan")` (as the card does) | 2 rows `lan`, `lan-FY27-28` COMPLETE (manual runs take part in the overlap). Then rewrite `config.yaml` with `rollover.overlap_enabled: false`, `backup(mode="all")`: exactly 2 rows, unsuffixed; FY27-28 targets unchanged (md5 map). |

**Not in the lab [DEC]** (unit-tested already, or unsafe here): D-8 NAS shutdown and WoL with real tools (would
address this PC - fakes in B3/B6 cover it); race guard, shared budget / MISSED, abandoned sweep (B3, F5, F7);
April final-copy state machine (B4/B5); attention / watchdog / reports / UI (B7-B9, C2); a real empty GCS prefix (D3).

### 3.3 Chunks (Sonnet builds the test; Haiku runs it; no production edits in D2)

| Chunk | Content | Done when |
|---|---|---|
| D2-a | fixture `overlap_rig`, helpers, safety asserts, S0 + S1 | S0/S1 pass alone |
| D2-b | S2 + S3 | pass alone |
| D2-c | S4 | pass alone |
| D2-d | S5 + S5b | pass alone |
| D2-e | S6 + S7 | pass alone; whole file green twice in a row |
| D2-run | Haiku runs the file (3.4) and reports rows, durations and every `[EVIDENCE]` line | report in `agent_runs/` |

A red scenario is never "fixed" by loosening an assertion: the Sonnet agent records it under PROGRESS "Found while
building" and in plan §12, and stops; Opus decides whether it is a product bug (separate Sonnet fixer with a unit
regression test first) or a wrong expectation.

### 3.4 Runner (PowerShell, cwd = worktree)

```
$env:PREFECT_HOME = "$env:TEMP\aam_prefect_home_d2_$(Get-Date -Format yyyyMMddHHmm)"
$env:PYTHONPATH = "."
C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe -m pytest audit_lab/test_fy_overlap_lab.py -q -s --timeout=1800 -p no:cacheprovider -o addopts=""
```

Expected about 15-25 min (6 harness starts, ~10 runs with 2-4 passes each); run in the background; do not edit
modules it imports while it runs. A Prefect `409 Conflict` is a known lab flake: re-run that single test. Also run
`audit_lab/test_w6_flow.py` and `audit_lab/test_w6b_rollover_nas_off.py` once (baseline flows still green with the
overlap code; `w6b` crosses an FY switch). Not the full lab (owner rule).

---

## 4. D3 - trial box checklist (main session; worker agents read-only on the box)

Box facts to re-read first (they may have changed): code `C:\AAMBackup` at `9461ac2`, runtime `C:\BackupAgent`,
schedule LAN 21:00 / cloud 02:00 / rollover 06:00 / audit per box config, NSSM services AamPrefectServer,
AamBackupAgent, AamWatchdog. Times below are IST.

### 4.1 Before anything (tell the owner first - section 6 decisions answered)

| # | Step | Pass when |
|---|---|---|
| P1 | PC: entry gate of Part D met; `git diff --stat 9461ac2 HEAD -- pyproject.toml uv.lock` empty (no dependency change; if not empty STOP - new dependency needs owner approval and `uv sync` on the box) | empty |
| P2 | PC: `git bundle create <scratch>\overlap.bundle baseline-v1-overlap ^9461ac2`; `git bundle verify`; note HEAD sha | verify OK |
| P3 | Box read-only: `git -C C:\AAMBackup rev-parse HEAD` = `9461ac2`, `git status --porcelain` empty | else STOP, ask |
| P4 | Box read-only: read the live config with the OLD code: source path, NAS path, bucket, `lan.shutdown_after_backup`, `wol.enabled`, recipients, crons | recorded in the session Part doc |
| P5 | Box read-only, the three FY27-28 places are empty / absent: source sibling `<parent>\FY27-28`, NAS sibling `<share parent>\FY27-28`, `rclone lsf aam_gcs:<bucket>/FY27-28` (as the service account's rclone config) | all absent / empty; else STOP and ask (never sync over unknown data) |
| P6 | Window: not within a leg's budget of a scheduled run, not Sunday audit window, not Monday 08:00 report, not 05:45-06:30 | chosen e.g. 10:00-17:00 |

### 4.2 Deploy (recipe of SESSION_HANDOFF 2026-10-22 / 2026-10-23)

| # | Step | Pass when |
|---|---|---|
| E1 | Idle check: no RUNNING Prefect flow run, `aam-backup` active slots 0, no `C:\BackupAgent\backup.lock` with a live PID | idle |
| E2 | Copies: `config.yaml` -> `config.yaml.bak-before-overlap-<date>`; DB with `sqlite3.connect(src).backup(dst)` -> `C:\BackupAgent\manifest.db.bak-before-overlap-<date>` | both files exist, DB copy opens, `PRAGMA user_version` = 5 |
| E3 | Transfer: Python script (Write tool, bundle as base64) piped `ssh innoviztawinserver2016 'C:\AAMBackup\.venv\Scripts\python.exe -' < script.py`; it writes the bundle, `git bundle verify`, `git fetch <bundle> baseline-v1-overlap:refs/heads/baseline-v1-overlap`, `git checkout baseline-v1-overlap` | `rev-parse HEAD` = PC sha |
| E4 | New code loads the live config: `load_config(...)` prints `overlap_enabled True`, `overlap_activate_on 12-31`, `cloud.scheduled True`, `lan.scheduled True`, and `fy_overlap.config_for_fy(cfg, "FY27-28").paths` = the expected FY27-28 siblings | as expected; else rollback 4.6 |
| E5 | Restart (owner told): stop AamWatchdog, restart AamBackupAgent, start AamWatchdog (Prefect server not restarted); no old python PIDs left | 3 services Running, dashboard `/status` OK, deployments registered with the same crons, no new ABANDONED rows |

### 4.3 Night 0 - baseline night with the overlap not active **[DEC: recommended, owner may skip]**

| # | Check | Pass when |
|---|---|---|
| N0.1 | 21:00 LAN, 02:00 cloud | exactly 2 rows, unsuffixed, COMPLETE; no FY27-28 folder / prefix; no new mail kinds; dashboard 2 cards; rollover card "FY27-28 will be added on 31 Dec 2026" |

### 4.4 Activation day + night 1 (four runs)

| # | Step / check | Pass when |
|---|---|---|
| A1 | Second DB copy right before activation: `manifest.db.bak-before-overlap-activation-<date>` (this is the one restored at the end) | exists, opens |
| A2 | Set `rollover.overlap_activate_on` = today `MM-DD` in Settings (typed phrase; proves the risky-key path). Today must not be `04-01` | saved; `config_changes` row |
| A3 | Run the daily check now: "Run FY rollover now" (if B9 allows it while activation is due) else `prefect deployment run rollover-check/rollover-check` | `source\FY27-28\README_FY.txt` made; `overlap:FY27-28` record; one INFO mail `rollover:activated:FY27-28` to the reports list; NAS FY27-28 not made yet; `config.yaml` paths unchanged |
| A4 | Put test files into the new source folder (about 20 small files, 1 sub-folder, office-like names) | files there |
| A5 | Dashboard: 4 cards, FY27-28 cards "first backup pending"; attention: `first_run:*:FY27-28` AMBER only, no RED | as stated |
| N1.1 | 21:00 LAN: rows `lan` then `lan-FY27-28`, both `LAN_COMPLETE`; one WoL; NAS `FY27-28` + marker made by the pass; NAS FY27-28 mirror == source (file list + sizes); shutdown, if the box has it on (owner decision 6.4), only after the FY27-28 pass and recorded on its row (`nas_shutdown`) | as stated |
| N1.2 | 02:00 cloud: rows `cloud` then `cloud-FY27-28`, COMPLETE / NO_CHANGES_COMPLETE after verify | as stated |
| N1.3 | `rclone check --one-way <source FY27-28> aam_gcs:<bucket>/FY27-28` 0 differences; FY26-27 prefix object count and size unchanged except the night's normal changes; no FY27-28 name under FY26-27 | as stated |
| N1.4 | The first FY27-28 cloud pass passed preflight probe B on an empty GCS prefix (log `rclone-preflight-probe` exit 0) - proves F-D6 | exit 0 |
| N1.5 | Both passes inside the leg budget (`extended_metrics.budget`), no MISSED; run-log folders / files carry `_FY27-28`; no failure mails; `notifications` has no unexpected rows; weekly / night strip (B8/B9) shows the FYs apart | as stated |

### 4.5 Manual-only LAN run (next day, inside the day window, done before 21:00)

| # | Step / check | Pass when |
|---|---|---|
| M1 | Set `lan.scheduled: false` (Settings, typed phrase); restart agent (re-serve) - owner told | config saved; validator accepts (cloud scheduled) |
| M2 | Prefect: `backup-lan` deployment has no schedule, not paused, no scheduled LAN runs listed; cloud deployment keeps its cron | as stated |
| M3 | Dashboard NAS cards: "Runs only when you press Run backup"; no LAN overdue / first-run item | as stated |
| M4 | Press "Run backup" on the **FY27-28 NAS card** **[DEC: the next-FY card, proves `?fy=`]** | exactly one row `...-lan-FY27-28` `LAN_COMPLETE`, NAS folder state ready (no new marker), one WoL; no FY26-27 LAN row from this press |
| M5 | Watchdog for at least one check interval: no stale mail for LAN | none |
| M6 | Put `lan.scheduled: true` back, restart agent | `backup-lan` cron back (`0 21 * * *` on the box) |

### 4.6 Put back (same day, before 21:00 and before the next 06:00 check)

Order matters: the config date goes back first (else the next 06:00 check re-activates, F-D1), the record and the
test rows go with the DB restore (F-D1..F-D4).

| # | Step | Pass when |
|---|---|---|
| B1 | Set `overlap_activate_on` back to `12-31` (Settings) | saved |
| B2 | Idle check (E1); stop AamWatchdog and AamBackupAgent | stopped |
| B3 | Evidence: copy the live `manifest.db` (sqlite backup) and the `run_logs` folders named `*_FY27-28` to `C:\BackupAgent\overlap_test_evidence_<date>\` | copied |
| B4 | Restore `manifest.db.bak-before-overlap-activation-<date>` over the live DB (owner decision 6.2) | `overlap_record("FY27-28")` is None; no `-FY27-28` rows; no `rollover:activated:FY27-28` notification |
| B5 | Delete test data (owner told): list then delete `source\FY27-28` (only placeholder + our test files - else STOP), NAS `FY27-28` (only marker + our files - else STOP), `rclone purge aam_gcs:<bucket>/FY27-28` (owner decision 6.3) | all three absent / empty |
| B6 | Start AamBackupAgent then AamWatchdog | Running; dashboard 2 cards, rollover card "FY27-28 will be added on 31 Dec 2026"; attention has no FY27-28 item |
| B7 | Night 2 | 2 rows, unsuffixed, COMPLETE |
| B8 | Write the result into the session Part doc / SESSION_HANDOFF and tick D3 in the plan | committed |

### 4.7 Rollback (any step fails, or the owner says stop)

1. Idle check (E1); stop AamWatchdog, AamBackupAgent.
2. `git -C C:\AAMBackup checkout --detach 9461ac2` (or `git reset --keep 9461ac2` on the branch, recipe 2026-10-23).
3. Restore `config.yaml.bak-before-overlap-<date>` (F-D8: the old code refuses the new keys).
4. Restore the DB copy matching the moment (before activation: E2 or A1 copy; the schema is unchanged, but the
   restore removes FY27-28 rows and the record, F-D3). Keep the replaced DB in the evidence folder.
5. Remove the FY27-28 test folders / prefix as in B5 (only if they were made by the test).
6. Start AamBackupAgent, then AamWatchdog; check `/status`, deployments, the next scheduled run.

---

## 5. Risks

| # | Risk | Mitigation |
|---|---|---|
| K1 | Overlap stays active after the test because the record persists (F-D1); re-activation at the next 06:00 if the date is not put back | 4.6 order: B1 first, then DB restore |
| K2 | Test FY27-28 rows become G2/G4 baselines and suppress the real INFO mail on 31 Dec (F-D3, F-D4) -> false BLOCKED on the first real night | DB restore B4; never stamp `ended_at` as cleanup (F-D2) |
| K3 | Leftover FY27-28 objects / NAS files / source test files on 31 Dec: mass delete (G3 block) or test files backed up as real data | B5 with listing first; P5 pre-check |
| K4 | Rollback with new keys in `config.yaml` -> services do not start (F-D8) | restore the pre-deploy config copy |
| K5 | A lab or box action powers off this PC (loopback NAS) or the real NAS | lab: shutdown keys forced off, guard asserted, shutdown task faked; box: owner decision 6.4 |
| K6 | Lab hangs on the PC's migrated `~/.prefect` DB | clean `PREFECT_HOME`, fixture asserts it |
| K7 | Lab silently imports the main checkout's `fy-overlap` code (no worktree venv) | `PYTHONPATH=.`, cwd worktree, `flow.__file__` assert |
| K8 | Lab local backend differs from GCS for an empty prefix (F-D6) | pre-created folder documented; D3 N1.4 |
| K9 | Restarts or config edits during a running backup | idle check E1 before every restart; day window P6 |
| K10 | Real mails reach the office during the test (INFO activation, any failure) | owner told; decision 6.5 |
| K11 | Deploying past 31 Dec without B8/B9 mixes FYs in reports / night strip | entry gate of Part D |
| K12 | A small model loosens a lab assertion to make it green | chunk rule 3.3; Opus checks `git diff --stat` vs scope |
| K13 | Lab runtime / Prefect 409 flakes | 1800 s timeout, re-run the single test, never the full lab |

---

## 6. Owner-only decisions (not decided here)

1. **Go for D3** and the dates / day windows (box untouched until the owner says we are confident); OK for the
   service restarts at E5, M1, M6, B2/B6.
2. **Live DB after the test**: restore the pre-activation copy (recommended; the live DB then lacks the test night's
   real FY26-27 rows, which are kept in the evidence copy) **or** keep the live DB and delete only the FY27-28 rows,
   the `overlap:FY27-28` record and its notification rows (touches history; owner rule "DB shows the actual truth").
3. **Real GCS**: the next-FY prefix of the box bucket is `FY27-28` (the prefix is always the FY name; there is no
   test-prefix key). Confirm the box bucket is test-only and that `gs://<bucket>/FY27-28/` may be written and purged
   afterwards (noncurrent versions then expire by the lifecycle).
4. **NAS shutdown during the test**: if the box has `lan.shutdown_after_backup` on, night 1 really powers the NAS off
   after the FY27-28 pass (owner deferred real shutdown tests until near the NAS). Keep it on (owner near the NAS) or
   switch it off for the test window.
5. **Mails**: let the test mails go to the real recipient lists, or point both lists to a test address for the test
   window (config change + restore).
6. **Night 0** (baseline night before activation): run it (recommended, one extra day) or skip.
7. **Deleting the test folders** on the source and the NAS after the test (data deletion on the box).
8. Gaps reported for decision if D1 finds them: missing email samples (2.2 last row) and any D2 red scenario that is a
   product bug.
