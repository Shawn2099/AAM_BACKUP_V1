# Plan - FY overlap (the next FY folder is backed up from 1 January)

**Date:** 2026-11-20 - **Branch:** `mvp-hardening` - **Revision:** 4 (single consolidated spec) - **Status:** for owner
sign-off, NO CODE YET.
Rev 1 design, rev 2 three hostile code reviews (R-1..R-25), rev 3 owner answers (A-1..A-7), rev 4 two more reviews
(open questions answered from the code + a day-by-day calendar walkthrough). Earlier FY docs were deliberately not used.
This revision replaces all earlier text: if something is not in here, it is not part of the plan.

---

## 1. Requirement

US clients work in the NEXT financial-year folder from 1 January; Indian clients stay in the current one until
31 March and move on 1 April. Both folders must be backed up (cloud + NAS) for those three months, and the dashboard
and emails must show both.

**In one line:** each FY folder is backed up for 15 months - its own 12 (April-March) plus the 3 months before
(January-March), when it is backed up next to the previous FY's folder.

| Period (example) | Folders backed up | config.yaml points at |
|---|---|---|
| 1 Apr 2026 - 31 Dec 2026 | `FY26-27` | `FY26-27` |
| 1 Jan 2027 - April switch | `FY27-28` **and** `FY26-27` | `FY26-27` |
| April switch - 31 Dec 2027 | `FY27-28` | `FY27-28` |

**Principle (owner):** engineer only the overlap; reuse the existing, proven pipeline unchanged.

## 2. Decisions (final)

| ID | Decision | Source |
|---|---|---|
| D-1 | Overlap window: from `rollover.early_start_month` (default **January**) until the April config switch is done. Config keys, Advanced. | owner |
| D-2 | 1 April rollover unchanged: final backup of the old FY, switch, archive. Afterwards the old FY is no longer backed up. | owner |
| D-3 | Dashboard: **one card per active FY** (2 in Jan-Mar, 1 otherwise), each with its own **Back up now** (cloud / NAS) and **Verify now**. | owner |
| D-4 | Order inside each leg's flow run: **new FY first**, then the configured FY. **One NAS wake + one shutdown per LAN flow run.** | owner A-3 |
| D-5 | The weekly integrity audit checks every active FY in the same run, result + alert per FY. | owner |
| D-6 | Weekly / monthly report: one email, **one section per FY**. | owner |
| D-7 | **Fresh database** (schema v6). No data migration; a v5 file is refused with "rename it and start fresh". | owner A-5 |
| D-8 | Whole-disk mode: out of scope. | owner |
| D-9 | The app creates the new FY folder on activation, or **adopts** one the office made with the correct name. Never recreated silently afterwards. | owner A-2 |
| D-10 | Nothing is moved by the app; whatever is in either folder is backed up; mirror semantics stay. | owner A-3 |
| D-11 | Small folders: below `guards.small_folder_files` (100) the percentage limits are replaced by `guards.small_folder_max_deletes` (10). | delegated A-1 |
| D-12 | Folder-creation rights are tested on the trial box; the service account is created / adjusted at deployment if needed. | owner A-4 |
| D-13 | GCS lifecycle is aligned with the overlap at the end (WP12): an active FY prefix is never ARCHIVEd. Until then the git version of `deploy/gcs_lifecycle.json` is used. | owner A-6 |
| D-14 | Prefect stays the orchestrator for this work (`docs/AUDIT_TOOLING_2026-11-20.md`). | rev 4 |
| D-15 | **Dependencies are upgraded FIRST (WP0)**: every minor / patch (incl. Prefect 3.9.x, FastAPI, Starlette, pydantic, uvicorn, loguru, pendulum, certifi security fix, rclone 1.76); majors excluded (wakeonlan 4, cron-descriptor 2, SQLAlchemy 2.1, websockets 17, httpx 1.0 dev). | owner 2026-11-20 |
| D-16 | **httpx2** is added and used by OUR code (launch.py, watchdog.py health checks, our tests where they call HTTP themselves); `httpx` stays installed because Prefect and Starlette's TestClient need it. | owner 2026-11-20 |
| D-17 | Trial box and this PC: full control for this work (folder-creation test, service restarts, DB rename, deploy). | owner 2026-11-20 |

## 3. Definitions (used everywhere below)

- **IST date of a run:** `pendulum` date in Asia/Kolkata, taken **once at flow start**; a scheduled run that starts late
  uses its **scheduled** time (`flow._scheduled_time`). Every decision in one flow run uses this one date.
- **configured FY:** the leaf of `paths.source_drive` (unchanged rule, `fy_rollover.configured_fy`).
- **next FY:** FY after the configured one (`FY26-27 -> FY27-28`, `FY99-00 -> FY00-01`).
- **calendar FY:** `get_fy_prefix(date)` (April start, unchanged).
- **in window:** calendar FY == configured FY **and** `early_start_month <= month <= 3`.
- **switch pending:** calendar FY == next FY **and** no FINISHED journal for `configured -> next` whose `config_switch`
  step is done. Open follow-up steps (archive, NAS shutdown, announce) do **not** count. No new clock proof is needed for
  this branch. A JUMP (more than one FY) or a clock behind is never "pending" for the overlap.
- **activated:** a durable row in the new table `overlap_activations (fy PRIMARY KEY, activated_at, source_path,
  created_by_app, clock_state)`, never purged.
- **active set:** `[next FY]` if overlap enabled **and** (in window **or** switch pending) **and** activated - followed by
  the configured FY. Order = run order (new first). Outside that: `[configured FY]` only.
- **folder view:** `config.model_copy` with `source_drive` = `parent(source)\<fy>` and, when the LAN leg is on,
  `lan_destination` = `parent(nas)\<fy>` (existing `_parent_path` / `_child_path`, drive-root and UNC safe). The
  configured FY's view is the config itself. The pipelines read every path from the view, so the cloud prefix
  (`gs://bucket/<fy>`), guard baselines and folder names follow automatically.
- **closed FY:** an FY that left the active set because the config switch moved past it. Its history stays readable;
  it is never red, never overdue, never alerting (§5.4).
- **leg flow run:** one Prefect run of `backup-cloud` or `backup-lan` (separate deployments, separate times - box: LAN
  21:01, cloud 02:01; defaults: LAN 01:00, cloud 18:00). Nothing in this plan depends on which runs first.

## 4. Behaviour over the year (box schedule; the rules hold for any schedule)

| When | What happens |
|---|---|
| Any day Apr-Dec | As today: one folder, one card. Daily sibling check (§5.2) alerts on a correctly named **future** FY folder that holds files (e.g. staff start `FY27-28` in December) and on FY-looking misnamed folders. |
| First flow run on/after 1 Jan (02:01 cloud, Fri 1 Jan 2027) | **Activation** (§5.1): clock proof, create or adopt `D:\FY27-28`, record the row, INFO mail "FY27-28 is now backed up as well". Then cloud FY27-28, then cloud FY26-27. If activation cannot run (clock unproven, parent missing) FY27-28 is simply not active yet: no failure, retried by every later flow run; ACTION REQUIRED mail after `rollover.activation_alert_after_hours` (24). |
| 06:00 rollover check | Retries activation if still missing. Runs the sibling check. Otherwise unchanged. |
| 21:01 LAN | NAS woken once; `\\nas\share\FY27-28` created / adopted (§5.1); NAS FY27-28, NAS FY26-27; one shutdown decision at the end. |
| Sun 3 Jan audit | Both FYs, new first, one NAS wake, shared `audit_max_hours`. |
| Mon 4 Jan weekly / 1 Feb monthly | One section per FY; FY27-28 counted only from its activation. |
| Jan-Mar nights | Two cards. Each FY has its own runs, alerts, approvals, overdue clock (FY27-28 from activation). |
| 31 Mar | Last in-window night: both folders. |
| 1 Apr onwards, switch pending | Both folders keep being backed up nightly (pending branch). 06:00 rollover: final backup of **FY26-27 only** (G6 covers only the configured FY), folder steps succeed on the existing FY27-28 folders, switch, archive `gs://bucket/FY26-27` only. |
| Switch blocked for days | Both folders backed up every night; existing ROLLOVER_BLOCKED mails; a final-backup G3 block needs the per-leg approval (FY26-27, still active) first, then the G6 approval. |
| After the switch | FY26-27 is closed (§5.4): its card disappears, its open approvals are closed "FY closed", its incidents are closed silently, its history stays in the run list and reports. One card (FY27-28). |
| Next 1 Jan | Same cycle with FY28-29 (new activation row; nothing from the previous cycle interferes). |

## 5. Changes by area (each maps to a work package in §6)

### 5.1 Activation and folders (WP2)
- `ensure_overlap_activated(config, run_date)` runs at the start of **every backup flow run** and of `rollover_check_flow`;
  idempotent; needs no slot; whichever flow comes first activates.
- Conditions: overlap enabled; in window or switch pending; `check_clock(...).state == CLOCK_OK` called directly (works
  with cloud off; the cloud health verdict is not used); `parent(source)` exists; the target is not a link / junction.
- Source folder: exists with the canonical name (case-insensitive) -> **adopt** (no placeholder written into a
  non-empty folder); missing -> create **without `parents=True`** + placeholder; record `created_by_app`.
- NAS folder (in the LAN flow, after the NAS is awake, before the first pipeline): missing -> create without parents +
  mount marker; exists with marker -> OK; exists and **empty** -> marker; exists, **non-empty, no marker** -> leave it,
  G5 asks (AAM-NAS-10) for that FY only. The rollover's `nas_folder` / `source_folder` steps treat "exists (+ marker)" as done.
- After activation, a missing / unreadable early folder is a normal failure of that FY's legs (existing health check,
  per-FY alert). It is **never recreated**.
- An early folder that is a link / junction is refused: that FY's legs fail with a clear alert.

### 5.2 Daily sibling check (WP2) - replaces the ROL-06 "unprotected" alarm
Runs in `rollover_check_flow`; lists only the direct children of `parent(source_drive)`:
- **FY-looking but not canonical** (case-insensitive `FY\s*\d{2,4}\s*[-_ ]\s*\d{2,4}`, e.g. `FY 27-28`, `FY2027-28`)
  -> ACTION REQUIRED "rename to FY27-28 - it is not backed up" (`overlap:sibling:<name>`).
- **Canonical, newer than the configured FY, not in the active set, holds real files** -> ACTION REQUIRED "files in
  FY27-28 are not backed up yet" (covers December use, overlap switched off, clock not proven).
- Canonical **past** FYs are ignored. Names listed in the new key `rollover.sibling_ignore` (default empty) are ignored.
- The old `unprotected_new_fy` alarm (ROL-06) is removed; its cases are covered by the second rule.

### 5.3 Orchestration (WP3)
- `backup(mode, approval_id=None, budget_hours=None, fy=None)`: computes the active set once; `fy` (manual / approval
  runs) must be in it, else the run is refused; loops **new FY first** per leg; `mode="all"` = cloud loop then LAN loop.
- **Slot + `backup.lock` held once around the whole loop of a leg**; the pipelines get a `slot_held=True` path (nesting
  the limit-1 slot would deadlock). Nothing can run between the two folders.
- **Budget:** both folders of a leg share the leg budget (same monotonic start, same `late_seconds`); a folder that has
  no budget left is recorded `<LEG>_MISSED` for its FY (the `late_s = 0` shortcut in `_missed_if_no_budget` is fixed).
- **NAS:** the first LAN pipeline wakes the NAS; every pipeline runs with `allow_shutdown=False`; a loop-level
  `finally` makes **one** shutdown decision with today's rules applied to all folders (stay on if any folder's own rule
  says stay on). A folder that was skipped, MISSED or crashed before its pipeline is covered by that `finally`.
- Crash / restart between folders: the second folder is skipped that night and caught by its own overdue check; the NAS
  stays on until the next LAN flow run's shutdown (same as a crash today; accepted).
- Run id `<flow run>-<leg>-<fy>`; run-log folder `<date>_<HHmm>_<leg>_<fy>_<short id>`; `run_artefacts` parser fixed;
  Prefect artifact keys per FY (`cloud-backup-summary-fy27-28`).
- `_run_final_backups` unchanged: configured FY only.

### 5.4 Database v6, alerts, approvals, closed FY (WP1, WP4)
- **v6 fresh schema:** `fy` column on `file_entries` (`UNIQUE(fy, relative_path)`), `run_history` (NOT NULL),
  `integrity_audits`, `notifications` (nullable - mails without an FY), `approvals` (required for backup approvals);
  new `overlap_activations`. A v5 file -> refused with the rename instruction.
- Every `ManifestDB` method that reads `file_entries`, `run_history`, `integrity_audits` or per-mode notifications takes
  `fy` with **no default** (global lists take an explicit `fy=ALL`). Covers: `last_successful_run`, `last_run`,
  `previous_run_status`, `previous_run_fault_code`, `leg_success_after`, `first_partial_after`, `latest_audit`,
  `audit_verified_after`, `told_about_leg_since`, `sent_in_mode_after`, `last_open_incident`, `get_recent_runs`,
  `file_count`, `get_cloud_synced_entries`, `prune_stale_synced`, `clear_mode_status`, `guard_baseline`;
  `start_run_row` / `record_run_history` / `insert_run` / `sweep_abandoned_runs` (FY from the RUNNING row).
- **Alerts:** `AlertEvent.fy`; keys `backup:<fy>:<leg>:<status>`, `blocked:<fy>:<leg>`, `missed:<fy>:<leg>`,
  `recovered:<fy>:<leg>`, `stale:<fy>:<leg>`, `audit:<fy>:<mode>:...`, abandoned-run key with FY; subject label
  `[FY27-28]` added in `email_compose.compose` (+ notify fallback subject). `nas_shutdown:` and `watchdog:*` stay global.
  Incident closure, RECOVERED and cooldowns per (mode, fy).
- **Approvals:** supersede per (leg, fy); `evaluate_approval` requires `approval.fy == run fy`; the approved re-run is a
  new flow run with `fy` = the approval's FY (that folder only, the leg budget). An approval of a closed FY cannot be used.
- **Closed FY (at the config switch):** pending approvals -> `CLOSED` ("FY closed"); open incidents closed without a
  RESOLVED mail; overdue / audit / attention items only for active FYs; dashboard shows it as history.

### 5.5 Guards (WP5)
- **Small folders (D-11)**, implemented only in `guards.max_delete_allowance` (so the rclone `--max-delete` backstop and
  `remaining_delete_allowance` agree) and the G2 check:
  - G3: if `destination_files + already_deleted < small_folder_files` -> allowance `min(max_delete_files,
    small_folder_max_deletes)`; else today's rule. At 100 files 10 % = 10: no jump.
  - G2: if the baseline source count `< small_folder_files` -> block when the drop `> small_folder_max_deletes`; else
    today's 20 % rule (at 100 files the allowed drop rises from 10 to 20 - accepted, documented).
  - G1 (empty source) and the absolute cap are unchanged. Applies to every folder.
- **First run of a leg in an FY** (no baseline) with planned deletes > 0 -> DECISION (approval), so an already filled
  `gs://bucket/FY27-28` or NAS folder is never trimmed unasked.

### 5.6 Overdue, attention, watchdog (WP6)
- Overdue clock per (leg, FY); the early FY's base is its `activated_at` (not `first_known_at`).
- Attention codes `stale:<fy>:<leg>`, `approval:<fy>:<leg>`, `last_run:<fy>:<leg>`; watchdog parser and
  `told_about_leg_since` per FY. `backup.lock`, restarts and `watchdog:recovered` stay global.

### 5.7 Dashboard and settings (WP7)
- `/status` adds `folders[]` (fy, role current/early, active since, per leg: last run, last success, overdue,
  integrity, approval, running). Old top-level fields stay (= configured FY) for compatibility.
- One card per FY; per-card **Back up now** (cloud / NAS) and **Verify now** -> `/trigger/cloud?fy=`,
  `/trigger/lan?fy=`, `/trigger/verify/<mode>?fy=` (FY must be active). "Running" per (leg, FY) comes from the RUNNING
  `run_history` row.
- Manual triggers are **refused while any backup or audit flow run is active** (instead of queueing into a
  SLOT_TIMEOUT false alarm).
- Rollover card (Settings tab) also shows the active set and "FY27-28 backed up since <date>".
- Settings: `rollover.overlap_enabled`, `rollover.early_start_month`, `rollover.activation_alert_after_hours`,
  `rollover.sibling_ignore`, `guards.small_folder_files`, `guards.small_folder_max_deletes` - Advanced, risky (typed
  phrase), `config_help` labels; effective from the next flow run. Switching the overlap off is allowed; the sibling
  check then alerts if the dropped folder holds files.
- Reworded texts that say the new folder is "NOT backed up until the switch" (models/config.py, config_help.py).

### 5.8 Integrity audit (WP8)
Loops the active set, new FY first; one NAS wake and one shutdown; the FYs share `audit_max_hours`; `fy` on every
audit row and alert; `_pending_context` per FY; manual Verify now per FY.

### 5.9 Reports (WP9)
Sections = FYs with runs in the period plus the active set; per FY the scheduled-run count covers
`max(period start, activation)` to `min(period end, switch)`, so the first January report and the first April report
do not raise a false "no backup ran".

### 5.10 Tools, readiness, deployment (WP10, WP11)
- `upload_fy.py` and `restore.py` refuse **every active FY** (not only the configured one).
- Readiness (`04_check_readiness` / `probe_service_account`): create + delete a test folder in `parent(source_drive)`
  and in the NAS share root **as the service account**. Tested on the trial box (D-12).
- Config validator: refuses a NAS FY folder directly at a share root with no parent to create siblings in
  (`\\nas\FY26-27`) while the LAN leg and the overlap are on.
- Deploy on any date: inside the window the first flow run activates. Fresh DB: stop the services, checkpoint, rename
  `manifest.db` together with its `-wal` / `-shm` files (never delete). Accepted: the old finished rollover journal is
  gone (nothing is in production).

### 5.11 Unchanged (confirmed by the reviews)
rclone / robocopy commands, cloud verify, the guard logic beyond §5.5, config path rules, the April rollover steps
(archive touches only `gs://bucket/<old_fy>/`), Prefect deployments and schedules, `serve.py`, separate cloud prefixes
(one FY's sync can never delete the other FY's objects), FY-scoped guard baselines (the April switch inherits three
months of FY27-28 history).

## 6. Work packages (test first, commit per WP / sub-step, GitNexus impact before each edit)

| WP | Content | Review items closed |
|---|---|---|
| WP0 | Dependency upgrade (D-15) + httpx2 for our own calls (D-16): `uv lock --upgrade` with majors pinned, Prefect server DB backed up first, unit suite + audit lab (clean `PREFECT_HOME`) green before any overlap code; rclone 1.76 into `deploy/bin` after a sandbox run | tooling audit |
| WP1 | DB schema v6 (fresh), `fy` everywhere, `overlap_activations`, v5 refusal, every listed method takes `fy` | R-1 (part), R-2, R-6, R-8, R-11 |
| WP2 | Config keys; `core/fy_overlap.py` (dates, active set, folder views); activation (§5.1); sibling check replacing ROL-06 (§5.2) | R-12, R-15, R-21, R-22, R-23, R-24, R-25 |
| WP3 | Orchestration (§5.3): loop new-first, slot once, shared budget + MISSED, NAS once, run ids, log folders, artifact keys, `fy` parameter | R-3, R-4, R-7, R-16, R-18 |
| WP4 | Alerts, approvals, closed FY (§5.4) | R-1, R-2 |
| WP5 | Guards: small folders, first-run deletes (§5.5) | R-14, A-1 |
| WP6 | Overdue, attention, watchdog (§5.6) | R-5, R-8 |
| WP7 | Dashboard, triggers, rollover card, settings, texts (§5.7) | R-8, R-13 |
| WP8 | Integrity audit per FY (§5.8) | R-9 |
| WP9 | Reports per FY (§5.9) | R-10 |
| WP10 | upload / restore refuse active FYs; readiness create probes; NAS-at-share-root validator (§5.10) | R-17, R-20, R-25 |
| WP11 | Audit-lab 15-month fake-clock scenario; ARCHITECTURE + SESSION_HANDOFF; trial-box deploy with fresh DB and a real folder-creation test | all |
| WP12 | GCS lifecycle aligned (no ARCHIVE of an active FY prefix), owner review, then `gcloud storage buckets update` | R-19 |

## 7. Acceptance tests (must pass before the trial-box deploy)

1. Active set: every month; 1 Jan / 31 Mar / 1 Apr; switch pending for 5 days; after the switch; overlap off; clock
   behind; JUMP; century wrap; drive-root, nested and UNC paths; cloud-only placeholder untouched.
2. Activation: created vs adopted; clock unproven -> retried, alert after 24 h; parent missing; junction refused; a missing
   folder after activation fails, is not recreated.
3. Same relative path in both FYs: catalog rows independent; pruning one FY leaves the other.
4. Two-folder LAN night: exactly one wake and one shutdown, including folder 2 MISSED / crashing / slot timeout paths.
5. FY26-27 BLOCKED while FY27-28 succeeds: per-FY approval, no false RECOVERED, FY27-28 card green, approval of one FY
   never usable by the other.
6. Small-folder rule: 9-file folder with 1 rename passes; 99 files with 11 deletes blocks; 100 files at 10 % boundary.
7. Reports: first January weekly and first April weekly/monthly have no false "no backup ran".
8. Audit lab: 15-month fake clock Dec -> Jan -> Apr switch with both legs and the audit, real robocopy/rclone.
9. Full unit suite green; audit lab green except the known intentional red.

## 8. Accepted risks

- A crash between the two folders skips folder 2 for that night (caught by overdue) and leaves the NAS on until the next
  LAN flow run.
- The fresh DB loses old history and the finished rollover journal (nothing in production).
- The G2 allowed drop jumps from 10 to 20 files at 100 files.
- A JUMP rollover (more than one FY, approved) does not back up a skipped FY.
- Files edited in a closed FY after the switch are not backed up (unchanged rule).

## 9. Review trace

R-1..R-25 (rev 2) and A-1..A-7 (rev 3) are all closed by §5 / §6. Rev 4 additions: IST run date fixed at flow start
(scheduled time); activation at the start of every flow run with a direct clock proof; durable activation table; pending =
switch step not done; rollover folder steps accept existing folders; final backups configured-FY only; report counts from
activation to switch; closed-FY clean-up; manual triggers refused during any backup/audit run; audit shares its budget;
sibling check covers correctly named future folders and ignores past ones; overlap-off alerting; first-run deletes need
approval; NAS-at-share-root validator; deploy on any date.
