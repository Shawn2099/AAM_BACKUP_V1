# Plan - FY overlap on the baseline (two active FYs from 31 Dec to the April switch)

Status: **FINAL** (rev 2, 2026-11-23; owner answers to the rev 1 questions are in section 8). Branch
`baseline-v1-overlap`, base tag `baseline-v1` = `9461ac2` (the version on the trial box). Build only after the owner's
go; new findings go to the owner, not into silent changes of this plan.

Replaces, for this branch, the heavy attempt on `fy-overlap` (`docs/PLAN_FY_OVERLAP_2026-11-20.md`: schema v6, fresh
DB, dependency upgrades, small-folder guards). Ideas kept from it: the per-FY "folder view" config, run-id / log-folder
naming per FY, NAS folder adopt rules, per-FY approvals. Everything else is dropped.

Line numbers below are from the base commit `9461ac2` and were checked while writing this plan.

**Owner's layout (the reference case for every example and test):** source = sibling year folders on a drive root,
`E:\FY26-27`, `E:\FY27-28`, ... (parent `E:\`); NAS = `\\nas\backup\FY26-27`, `\\nas\backup\FY27-28`, ... (parent
`\\nas\backup`, which exists). Verified with the base code: `_parent_path(r"E:\FY26-27")` = `E:\` (the drive root
keeps its backslash, `core/fy_rollover.py:100-109`), `_child_path(r"E:\", "FY27-28")` = `E:\FY27-28`;
`_parent_path(r"\\nas\backup\FY26-27")` = `\\nas\backup`, child = `\\nas\backup\FY27-28`. The config validator
(`models/config.py:1193-1254`) accepts this pair: NAS path starts with `\\`, both leaves are `FY26-27`.

---

## 1. Design summary and key decisions

**One sentence:** while an overlap is active, every backup flow run (and the weekly audit) runs the **unchanged**
pipeline twice per leg - first for the configured FY, then for the next FY with a copied config whose two paths point
at the next FY's folders - and every row, alert, approval and log of the second pass is tagged with its FY by a
`-FYxx-yy` suffix on its id. No database schema change. The April rollover keeps the baseline journal; its final-copy
gate learns to count a verified copy made after the closing date (also a manual one), so the switch happens by itself
as soon as the last blocker is gone. A leg can be enabled but unscheduled (manual only).

| ID | Decision | Reason |
|---|---|---|
| D-1 | **Active set** = `[configured FY]`, plus `[next FY]` when `rollover.overlap_enabled` and the next FY has an **activation record**. Order in backup runs: configured first, then next (owner order). | One rule, no date logic in the nightly runs. The overlap ends by itself at the config switch: the new configured FY's next FY has no activation record. |
| D-2 | **Activation** (create `E:\FY27-28` + placeholder, write the record) runs in the daily `rollover_check_flow`, on or after `rollover.overlap_activate_on` (default `12-31`), **only on a proven clock** (waits and mails otherwise). `\\nas\backup\FY27-28` is made by the first next-FY LAN pass (NAS is awake then). | The 06:00 check is the existing place for FY work; no WoL / shutdown logic in activation; NAS offline cannot block it. |
| D-3 | **Per-pass config** = `config.model_copy` with `paths.source_drive` / `paths.lan_destination` moved to the next FY (`fy_rollover._parent_path` / `_child_path`, the same rule `update_config_yaml` uses). | Every task reads its paths from `config`; cloud prefix (`_effective_fy_prefix`), guard FY, verify, LAN snapshot, health check follow with zero engine change. |
| D-4 | **No schema change.** The FY of a next-FY pass is in its ids: run_id `<flow-run>-<leg>-FY27-28`, audit_id `<uuid>-<leg>-full-FY27-28`. The activation record and the "scheduled" state in effect live in the existing `db_meta` key/value table. Every row also gets `"fy"` in `extended_metrics`. | The baseline DB (user_version 5) opens unchanged; rolling the code back is safe; RUNNING / ABANDONED rows carry the FY from the first insert. |
| D-5 | ManifestDB "latest / last success" lookups gain `fy: str \| None = None`: `None` = rows **without** an FY suffix (the configured FY's pass, i.e. all rows today), a value = that FY's suffixed rows. | With no suffixed rows every existing call returns exactly what it returns today. |
| D-6 | Guard baselines unchanged: `guard_baseline(leg, fy)` already filters on `$.guards.fy` (`core/manifest.py:845-867`). | Per-FY baselines for free; after the switch FY27-28 inherits its three months of baselines (G2/G4 stay armed). |
| D-7 | Both passes of a leg **share the leg's run budget** (same start); only the first pass gets `late_seconds`. A pass with no budget left is `<LEG>_MISSED` for its FY. | `AppConfig._schedule_fits_the_budgets` (`models/config.py:1166-1184`) stays a true proof that a leg run never runs into the next job. |
| D-8 | LAN: WoL once (first pass); earlier passes run with `allow_shutdown=False`; the last pass may shut the NAS down only if every earlier pass ended `LAN_COMPLETE` (or `lan.shutdown_on_all_retries_exhausted` is on - today's rule). | One wake, one shutdown, today's "do not power off after a failure" rule applied across both folders. |
| D-9 | The next-FY pass **does not write `file_entries`** and skips the cloud bucket-vs-DB cross-check. | `file_entries.relative_path` is UNIQUE per path, not per FY (`core/manifest.py:42`); a second FY would prune the first FY's entries (`prune_stale_synced` `:676`). The table only feeds metrics cross-checks and the "files" counters. |
| D-10 | April: baseline journal steps and order. The final-copy gate is the state machine of section 4: a leg is done when a **verified final copy of the old FY started on/after the closing moment** exists (this morning's own run, a nightly run or a manual run). The daily check detects it and switches with no extra click. The forced switch (G6 approval with the typed word) stays. | Owner answer A2. Both FYs stay protected while waiting (the config still points at the old FY, the overlap keeps the new one). |
| D-11 | `rollover.overlap_enabled` default **true** in the model and in every shipped config (owner A1). A config without the key = enabled. Nothing changes before the activation date. | Owner decision; the activation date is the only switch-on moment. |
| D-12 | New per-leg switch **`cloud.scheduled` / `lan.scheduled`** (default true) beside the existing `enabled`; crons stay in `schedule.*_cron` and are kept for when the leg is scheduled again. | Baseline has only `enabled` (registered + scheduled, `serve.py:35-55`) and crons that must have 5 fields (`models/config.py:1108-1114`) - no way to say "manual only". Owner answer A3. |
| D-13 | Weekly audit covers both FYs inside the existing `budget.audit_max_hours`; per leg the **next FY first** (small), then the configured FY. No new budget key. | Raising the audit budget collides with the default schedule (audit Sun 10:00 + 8 h ends exactly at the 18:00 cloud run; the budget validator would refuse more). The next FY holds at most three months, so its audit is short and always fits; the configured FY gets the rest, as today. |

## 2. The mechanism: one pipeline pass per active FY

### 2.1 New module `core/fy_overlap.py` (pure helpers + the activation step)

- `next_fy("FY26-27") -> "FY27-28"` (2-digit, modulo 100: `FY99-00 -> FY00-01`).
- `activation_date(configured_fy, "12-31") -> date(2026, 12, 31)`: month >= 4 -> FY start year, else start year + 1.
- `closing_moment(from_fy)` = 1 April 00:00 IST of the next FY's start year (`2027-04-01T00:00+05:30` for FY26-27).
- `fy_of_id(run_or_audit_id) -> "FY27-28" | None` (regex `-(FY\d{2}-\d{2})$`), `pass_run_id(base, fy)`,
  `artifact_key(base, fy)` (`cloud-backup-summary-fy27-28`, Prefect keys are lowercase).
- `config_for_fy(config, fy)`: `E:\FY26-27` -> `E:\FY27-28`; `\\nas\backup\FY26-27` -> `\\nas\backup\FY27-28`
  (`_child_path(_parent_path(x), fy)`). The NAS path is moved only when its leaf is an FY folder (a cloud-only
  placeholder stays, as `update_config_yaml` does at `core/fy_rollover.py:410,422`).
- `FyPass(fy: str, extra: bool, config)`; `active_passes(config, db) -> list[FyPass]`: `[FyPass(configured, False,
  config)]` always first; the next FY is added when `overlap_enabled` and `db_meta['overlap:<next>']` exists without
  `ended_at`. Folder existence does **not** decide (fail closed: a deleted next folder makes that pass fail loudly in
  its health check, it is never dropped silently).
- `row_fy(row)`: suffix, else `extended_metrics.fy`, else `extended_metrics.guards.fy`, else None.
- `overlap_state(config, db, today)` for the dashboard / rollover card.
- `activate_if_due(config, db, now)`, `ensure_nas_folder(lan_root, fy)` (section 3), `final_copy_state(...)` (section 4).

### 2.2 `flow.backup()` (`flow.py:2972-3080`)

- New parameter `fy: str | None = None` (card buttons, approved re-runs). Must be in the active set, else `ValueError`
  (Prefect FAILED, no row) - the pattern of the disabled-leg guard at `flow.py:3012-3015`.
- `passes = fy_overlap.active_passes(config, db)` once at flow start, filtered by `fy`.
- One active pass -> the pipelines are called with `fy_pass=None`: **exactly the baseline call**.
- For each leg (cloud then LAN, as today), `leg_start = time.monotonic()`; for each pass:
  `pipeline(p.config, run_id, now_iso(), time.monotonic(), budget_start=leg_start, late_seconds=late_s (first pass
  only), approval_id=approval_id, fy_pass=p, allow_shutdown=<D-8 rule>)`. Exceptions collected exactly like the two
  legs today (`excs`, `ExceptionGroup`); a failed first pass never stops the second.
- The same loop runs for a scheduled run and for a manual run of an unscheduled leg (D-12): manual runs take part in
  the overlap like scheduled ones.
- Race guard: before every pass after the first, re-read `configured_fy` from `config.yaml`; if it changed since the
  flow started (the rollover switched meanwhile), stop the loop with a log line.

### 2.3 Inside the two pipelines (`_run_cloud_pipeline` `flow.py:1536`, `_run_lan_pipeline` `flow.py:1902`)

New keyword parameters, both defaulting to baseline behaviour: `fy_pass: FyPass | None = None`,
`budget_start: float | None = None` (`RunBudget(..., (budget_start or monotonic_start) - late_seconds)` at
`flow.py:1545-1548` / `1913-1916`). Everything below is driven by `fy_pass` and is a no-op when it is None:

| Item | Configured-FY pass | Next-FY pass (`fy_pass.extra`) | Where |
|---|---|---|---|
| Paths, cloud prefix, guard FY, verify, LAN dest | `E:\FY26-27`, `gs://bucket/FY26-27`, `\\nas\backup\FY26-27` | `E:\FY27-28`, `gs://bucket/FY27-28`, `\\nas\backup\FY27-28` | all tasks read `config.paths.*`; `_effective_fy_prefix` `flow.py:102` |
| run_id | `<flow-run>-cloud` (unchanged) | `<flow-run>-cloud-FY27-28` | `backup()`; `_stable_run_id` `flow.py:130` unchanged |
| run_history row | mode `cloud`/`lan`, statuses unchanged, + `"fy"` in metrics | same mode/statuses, suffixed run_id, + `"fy"`, `"overlap": true` | `_record_run` `flow.py:2173`; RUNNING row `start_run_row` `core/backup_repository.py:122` (status built from mode, so mode stays the leg) |
| Run-log folder and file names (A3b) | folder `<date>_<HHmm>_cloud_<id8>`, files `<date>_<HHmm>_cloud_<step>.log` (unchanged) | folder `<date>_<HHmm>_cloud_<id8>_FY27-28`, files `<date>_<HHmm>_cloud_FY27-28_<step>.log` (the FY sits between the leg and the step, so a mailed log says which year it is) | `core/run_artefacts.py` (`_RUN_DIR` regex with the FY group, `_leg_of` strips the suffix first, `__init__` builds `_prefix` with the FY tail, `open()` keeps it); readers walk the folder by name (`core/support_bundle.py` `_log_files`), so they are unaffected |
| Prefect artifact key | `cloud-backup-summary` | `cloud-backup-summary-fy27-28` | `flow.py:1378`, `flow.py:1414` (key becomes a parameter) |
| Slot + `backup.lock` | per pipeline, unchanged | per pipeline, unchanged (passes are sequential) | `_pipeline_slot` `flow.py:494`, `_backup_slot` `:194` |
| Alert keys | unchanged (`backup:cloud:<status>`, `blocked:cloud`, `missed:cloud`, `recovered:cloud`) | `backup:cloud:FY27-28:<status>`, `blocked:cloud:FY27-28`, `missed:cloud:FY27-28`, `recovered:cloud:FY27-28` | `_send_run_alert` `:325`, `_open_block_request` `:1082`, `_missed_if_no_budget` `:450`, `_send_recovery_if_needed` `:355`, slot timeout `:516`; all prefixes already in `alert_class.KNOWN_PREFIXES` |
| Alert title | `CLOUD backup failed` | `CLOUD backup (FY27-28) failed` | while the overlap is active BOTH passes show their FY in the title (text only) |
| Notification incident group | `notifications.mode = cloud` | `notifications.mode = cloud:FY27-28` | `AlertEvent.fy` (new, default None); `core/notify.py` uses `group = mode if not fy else f"{mode}:{fy}"` in `_incident` `:623-642`, `_incident_closed` `:589-616` (`leg_success_after(leg, since, fy=fy)`) and the record `:677-686`. `AlertEvent.mode` stays the leg, so `email_compose` / `support_bundle` rendering is unchanged |
| Approvals | `approvals.fy` already stored from `guard_record["fy"]` (`flow.py:1101`) | same | supersede per (leg, FY) - 2.4; `_apply_approval` `:1042` also refuses an approval whose `fy` differs from the pass FY |
| Guard baseline | `guard_baseline(leg, FY26-27)` | `guard_baseline(leg, FY27-28)` | unchanged code (D-6) |
| Verify (daily size check + evidence gate) | unchanged | unchanged, on `gs://bucket/FY27-28` | `cloud_verify_and_report_task` `:863`, gate `:1675-1699` |
| `file_entries` + cross-check | unchanged | skipped (D-9); metrics `"cross_check": "skipped (overlap pass)"` | `flow.py:1570-1577`, `:1713`, `:1720`, `:2009` |
| Pending rollover NAS step | `_complete_pending_rollover_nas_folder` `:1957` | `fy_overlap.ensure_nas_folder(r"\\nas\backup", "FY27-28")` after WoL, before preflight; failure raises `faults.coded(..., "AAM-LAN-01")` -> that pass is `LAN_FAILED` with its own alert | `flow.py:1954-1958` |
| Recovery mail | `previous_run_status(leg, run_id)` | `previous_run_status(leg, run_id, fy=...)` | `flow.py:355-375` |
| Abandoned sweep | unchanged | FY parsed from run_id -> alert with `fy` | `core/backup_repository.py:130-155` |

### 2.4 Database (no schema change, no version bump - re-verified for rev 2)

`SCHEMA_VERSION` stays 5 (`core/manifest.py:25`); no DDL, no ALTER. Re-checked for the rev 2 additions: the
`scheduled` switch is config only; the "scheduled in effect" flag is two extra fields in the existing
`db_meta['schedule_in_effect']` JSON (`set_schedule_in_effect` `:1261-1279`; `_stored_schedule` `:1245-1259` reads
them with default true, so an old record = scheduled); the final-copy state machine reads existing `run_history`
columns (`mode`, `status`, `started_at`, `run_id`, `extended_metrics.guards.fy`). Additions to `ManifestDB` only:

- SQL helper `_fy_clause(fy)`: `None` -> `substr(run_id, -8) NOT GLOB '-FY[0-9][0-9]-[0-9][0-9]'`; a value ->
  `substr(run_id, -8) = '-' || ?`. Baseline ids end in `-cloud`/`-lan`/`-rollover`, so every existing row is "None".
- `fy=None` keyword on: `last_run` `:790`, `last_successful_run` `:804`, `previous_run_status` `:823`,
  `previous_run_fault_code` `:832`, `first_partial_after` `:1286`, `leg_success_after` `:1308`, `latest_audit` `:919`
  (same clause on `audit_id`). `told_about_leg_since` `:1339` is queried with the group string `cloud:FY27-28`.
- `final_copy_since(leg, fy, since_iso) -> dict | None`: newest row of `mode = leg` with `status IN
  statuses.FINAL_COPY_STATUSES`, `json_extract(extended_metrics,'$.guards.fy') = fy`, started at or after `since`
  (compared as instants in Python, like `leg_success_after`). Used only by the April gate.
- `get_runs_since` / `get_runs_between` / `get_recent_runs` unchanged (all rows); callers filter with `fy_of_id`.
- `open_approval_request` `:956-974`: keyword `keep_fy: str | None = None`; supersede becomes
  `... WHERE leg = ? AND status = 'PENDING' AND COALESCE(fy,'') != COALESCE(?, '#')` - `keep_fy=None` has today's
  effect. The pipelines pass the *other* active FY.
- `supersede_approvals_of_fy(fy)` (used at the switch); `overlap_record(fy)` / `set_overlap_record(fy, dict)` on
  `_meta` / `_set_meta` `:1213-1222` (key `overlap:FY27-28`, JSON `{activated_at, from_fy, source_folder, clock,
  ended_at}`).
- Compatibility test: a baseline DB written by the `9461ac2` code (runs, approvals, journal, schedule record) opens
  with the new code: no migration, `user_version` stays 5, old queries return the same rows, old schedule record =
  both legs scheduled.

### 2.5 Overdue, attention, watchdog, reports, audit

- `core/attention.py`: `_leg_clock(db, config, leg, sched, fy=None)` `:93` uses `last_successful_run(leg, fy=fy)`;
  for the next FY with no success yet the base is `activated_at`. `compute_attention` `:195` loops `(leg, None)` plus
  `(leg, next_fy)` when active; codes get `:FY27-28` (`stale:cloud:FY27-28`, `first_run:`, `last_run:`, `audit:`).
  **Unscheduled legs (D-12) get no `stale:` / `first_run:` / `catching_up:` item for any FY**, so the watchdog never
  mails them. `unprotected_new_fy` (`:321-331`, `core/fy_rollover.py:262`) returns None while the overlap is active
  for that FY.
- `watchdog._check_stale_legs` `watchdog.py:399-416`: `rest = code.split(":",1)[1]`, `leg, _, fy = rest.partition(":")`;
  `AlertEvent(key=f"stale:{rest}", mode=leg, fy=fy or None)`; `told_about_leg_since(rest, ...)`.
- Reports (`core/report.py:201-268`): sections per (leg, FY) using `row_fy(row)`; a next-FY section counts scheduled
  runs only from `activated_at`; an unscheduled leg has 0 scheduled runs (never "no backup ran") and is labelled
  "run by hand". `email_compose._data_safety` `:125-139` lists one line per active (leg, FY); `_next_text` `:142-157`
  says "this backup runs only when started from the dashboard" for an unscheduled leg.
- Integrity audit (`flow.py:2818-2965`): inside the one slot, per leg the next FY then the configured FY (D-13),
  one shared `audit_max_hours` budget (as today), one NAS wake/shutdown; suffixed audit ids;
  `_pending_context(..., fy)` `:1435`; outcome strings `cloud@FY27-28=...` in the alert key. The **scheduled** weekly
  audit skips unscheduled legs (a manual-only NAS may be off); the card's "Check now" audits them. New flow parameter
  `fy: str | None = None`.

## 3. Activation (31 December) - idempotent, proven clock, NAS-independent

`fy_overlap.activate_if_due(config, db, now)` is called by `rollover_check_flow` (`flow.py:2340-2377`) right after log
retention and **before** `_alarm_unprotected_new_fy` and `_rollover_resume`. Never raises. Steps:

1. Not enabled, record present, or `configured_fy` None -> return. Due when `today (IST) >= activation_date` and
   `classify_rollover(configured, get_fy_prefix())` is `NONE` (Dec 31 - Mar 31) or `FORWARD` (after 1 April while
   the switch is pending: a late enable or a server that was off on Dec 31 still activates). `JUMP` / `CLOCK_BEHIND`
   -> never (the rollover check already alerts).
2. Clock proof: `check_clock(...)` exactly as the rollover does (`flow.py:2560-2566`). Not `CLOCK_OK` -> nothing is
   created; mail `rollover:activation:FY27-28` (new fault AAM-ROL-07 "Next financial year not added yet - clock not
   proven"), WARNING on the activation day, ACTION_REQUIRED from the next day; cooldown `alerts.stale_repeat_hours`;
   retried every morning (and by "Run FY rollover now"). A clock that is behind makes "today" earlier: nothing happens.
3. Source folder: `fy_rollover.create_source_folder(r"E:\", "FY27-28", placeholder)` `core/fy_rollover.py:353` -
   idempotent (`mkdir exist_ok`, placeholder opened `"x"`). An office-made `E:\FY27-28` is adopted. A link /
   junction is refused (`_new_fy_root` rule `:234-239`) -> same mail with the reason.
4. Write `db_meta['overlap:FY27-28'] = {activated_at, from_fy, source_folder, clock}` **after** step 3. Crash between
   3 and 4 -> next morning repeats 3 (no-op) and writes 4; crash after 4 -> nothing to redo.
5. INFO mail `rollover:activated:FY27-28` ("From today FY27-28 is backed up as well as FY26-27"), `audience=reports`,
   sent once (checked with `last_notification`, like `_resolve_unprotected_new_fy` `flow.py:2388-2390`).

**NAS folder (offline-safe):** made by the next-FY LAN pass after `wol_check_task` with
`ensure_nas_folder(r"\\nas\backup", "FY27-28")` (stricter than `create_nas_folder` `core/fy_rollover.py:376-390`,
which stays for the rollover): parent `\\nas\backup` not a directory (NAS off, share missing) -> `OSError` (pass
FAILED, AAM-LAN-01); folder missing -> create + `MOUNT_MARKER`; marker present -> OK; exists and empty -> marker;
exists with files and no marker -> left alone (G5 blocks that FY's pass and asks; the approval creates the marker as
today, `flow.py:1193-1203`). The configured FY's pass is never affected.

**Config without the key / overlap switched off:** a config.yaml without `rollover.overlap_enabled` is enabled (model
default, D-11). Switching it off after activation stops the passes at once; `compute_attention` adds a RED item
`overlap_off:FY27-28` while the record has no `ended_at` and the folder holds files ("files in FY27-28 are no longer
backed up").

## 4. April 1 rollover: the final-copy state machine

The journal (`flow.py:2441-2752`, `core/rollover_journal.py`) keeps its steps and order. Only the `final_backups` step
changes how it decides each leg; G6 (`core/guards.py:229-258`) keeps its verdict logic and gets the per-leg result.

**Definitions** (`from_fy` = configured FY being closed, `to_fy` = next FY):

- **Closing moment C** = `closing_moment(from_fy)` = 1 April 00:00 IST.
- **Required legs** = every leg with `enabled: true`. A disabled leg is never required, never run, never waited for.
- **Verified final copy of leg L** = a `run_history` row with `mode = L`, `guards.fy = from_fy`, `started_at >= C`
  and status in `statuses.FINAL_COPY_STATUSES` = `{CLOUD_COMPLETE, CLOUD_NO_CHANGES_COMPLETE, LAN_COMPLETE}` (new set
  in `core/statuses.py`, legacy statuses excluded). A cloud row reaches those statuses only after the verify evidence
  gate passed (`flow.py:1675-1699`); `LAN_COMPLETE` needs the exit bitmask **and** the completed job summary
  (`lan_sync.decide_lan_result`). PARTIAL, BLOCKED, FAILED, VERIFY_FAILED, MISSED never count. It does not matter how
  the run started: this check's own final run, a scheduled nightly run, a dashboard run (FY26-27 card), or an
  approved re-run.
- A manual run of the old FY before C never counts (started before the closing date).

**Each daily check while the switch is pending** (06:00, or "Run FY rollover now"):

| Step | Scheduled required leg | Unscheduled required leg |
|---|---|---|
| 1. Run | runs its final backup now, as today (`_run_final_backups` `flow.py:2774-2795`, LAN `allow_shutdown=False`) | **not run** by the check (manual only: no automatic NAS wake) |
| 2. Decide | done if step 1 succeeded, else done if `final_copy_since(L, from_fy, C)` finds an earlier verified copy | done if `final_copy_since(L, from_fy, C)` finds one |
| 3. Result per leg | `final_status[L]` = the counted row's status, or the failed status of step 1 | counted status, or `<LEG>_MANUAL_FINAL_NEEDED` (a non-success marker used only inside `final_status`, never stored as a run status) |

Then `g6_fy_rollover(from_fy, to_fy, kind, final_status)` as today:

- **Pass (all required legs done, kind FORWARD)** -> the remaining steps run in the slot (folder steps are no-ops on
  the existing `E:\FY27-28` / `\\nas\backup\FY27-28`, config switch, archive of `gs://bucket/FY26-27/` only, NAS
  shutdown, announce). No click needed: a manual final copy made yesterday is picked up by this morning's check.
- **Block (a required leg not done, or kind JUMP)** -> `ROLLOVER_BLOCKED` row, approval request, ACTION_REQUIRED mail
  (existing `blocked:rollover`, cooldown as today). Config stays on FY26-27, so nightly runs keep backing up **both**
  FYs. The mail and the rollover card list each outstanding leg with what to do: "Cloud: tonight's run or 'Run
  backup' on the FY26-27 cloud card", "NAS (run by hand): switch the NAS on and press 'Run backup' on the FY26-27 NAS
  card". The G6 reason text (`:254-255`) gets a text-only keyword `overlap_active`: "until then both FY26-27 and
  FY27-28 are backed up every night".
- **Refused, not approvable** (baseline, unchanged): clock behind (AAM-CLK-03), clock not proven (AAM-ROL-03), settings
  switched by hand (ROL-02 path), re-entered journal.
- **Forced switch** (escape, unchanged): approving the G6 request on the dashboard (typed word `APPROVE`) and running
  it -> switch without the missing copy; the announce mail and the rollover row name the legs that were forced.

**Warns only (never blocks):** a leg counted from a copy older than this check (its own run failed or it was not run)
-> the announce mail and the rollover row say "the NAS copy of FY26-27 is from <time>; changes saved in FY26-27 after
that are not on the NAS". After the switch FY26-27 is not backed up any more (baseline).

**Change from baseline (scheduled legs too):** baseline counted only this check's own run; now an earlier verified
copy after C also counts when this morning's own run fails. Owner answer A2; covered by tests and the warning above.

Right after `config_switch` is done (in `_rollover_resume`, next to `_resolve_unprotected_new_fy` `:2739-2743`,
never raising): `supersede_approvals_of_fy(from_fy)`, stamp `overlap:<to_fy>.ended_at`.

| Day | Example (cloud scheduled, NAS run by hand) |
|---|---|
| 31 Mar 18:00 | Cloud FY26-27 + FY27-28 (nightly). |
| 1 Apr 06:00 | Cloud final runs and succeeds; NAS has no copy since C -> blocked; mail lists "NAS FY26-27: run by hand". |
| 1 Apr 14:00 | Office switches the NAS on, presses "Run backup" on the FY26-27 NAS card -> `LAN_COMPLETE` (and the FY27-28 NAS card if wanted). |
| 2 Apr 06:00 | Cloud final runs again; NAS counted from 1 Apr 14:00 -> switch, archive, one FY. Warning line about the NAS copy time. |

## 5. Per-leg "scheduled" switch (D-12)

- Keys `cloud.scheduled`, `lan.scheduled` in `CloudConfig` / `LanConfig` (`models/config.py:303`, `:132`), default
  `true`. `enabled: false` still means off everywhere (baseline). `enabled: true, scheduled: false` = manual only.
- Validation: refuse a config where no enabled leg is scheduled ("nothing would ever back up by itself" - same style
  as the both-off refusal at `models/config.py:1199-1200`). Cloud keeps running by its own schedule when the LAN leg
  is disabled or unscheduled (separate deployments; test).
- `serve.py:35-55`: an enabled unscheduled leg is still registered (`backup-<leg>` deployment, so card buttons,
  approvals and per-FY runs work) with `schedules=[]`. `launch._reconcile_disabled_legs` (`launch.py:252`) keeps it
  **resumed** (it is enabled). Task C1 proves on the local Prefect (and the box) that re-serving with `schedules=[]`
  removes the old cron and that `arun_deployment` still runs it; if the old schedule survives, the reconcile step
  deletes it through the Prefect client (no pausing: a paused deployment's manual runs are not relied on).
- `_schedule_fits_the_budgets` (`models/config.py:1166-1184`) skips unscheduled legs. `set_schedule_in_effect` stores
  `cloud_scheduled` / `lan_scheduled` (old records read as true) so the dashboard judges what the scheduler really runs.
- Unscheduled leg: no overdue / first-run / catching-up items, no watchdog mail, skipped by the scheduled weekly
  audit, 0 scheduled runs in reports, April gate as in section 4. A failed manual run still shows red (`last_run:` item)
  and sends its normal failure mail - that is a real failure, not a schedule alarm.
- Baseline behaviour changes with `scheduled: true` (the default): **none**.

## 6. Dashboard and API

**`/status`** (`ui.py:514-592`): `cloud` / `lan` keep their meaning (configured FY, `fy=None` queries) and gain `fy`
and `scheduled`. When the overlap is active, `cloud_next` / `lan_next` with the same shape (`last_run`,
`last_success`, `timing`, `integrity`, `nights`, `running`, `enabled`, `scheduled`, `fy`) and `overlap: {enabled,
active, next_fy, activates_on, activated_at, note}`. `_get_last_success` `:1553`, `_integrity_summary` `:1561`,
`_last_run_summary` `:1603`, `_nights` `:595`, `_leg_timing` `:607` take `fy`. "Running" for a card = the leg's
Prefect run is active **and** that FY's last row is `*_RUNNING`; leg active but this FY not yet -> "Queued (after
FY26-27)". `recent_runs` rows carry `fy`.

**Triggers:** `/trigger/cloud` `:648`, `/trigger/lan` `:686`, `/trigger/verify/{mode}` `:1361` accept `?fy=` (must be
active, else 409) and pass `parameters={"fy": ...}`; without `fy` they run every active FY. Unscheduled legs are
accepted like scheduled ones. `_start_approved_run` `:777` passes `{"approval_id", "fy": req["fy"]}` for cloud/LAN; a
request whose FY is no longer active -> 409.

**Cards** (`templates/dashboard.html:54-91`, `static/js/dashboard.js:246-307`): the duplicated per-leg blocks become
one `renderLegCard(key, d)` (`cloud`, `lan`, `cloud-next`, `lan-next`); two extra cards in a row hidden unless
`overlap.active`. Titles "Online copy (cloud) - FY26-27" / "- FY27-28". Each card: badge, last backup (with the row's
FY), last success, strip, safety check, **Run backup** and **Check now** for its FY. Schedule line: "Next scheduled
backup: ..." or "Runs only when you press Run backup" for an unscheduled leg.

| State | What the user sees |
|---|---|
| Before activation | 2 cards (as today); rollover card: "FY27-28 will be added on 31 Dec 2026". |
| Activation waiting for the clock | 2 cards + attention item (amber on the day, red after) and the mail. |
| Active (Jan-Mar) | 4 cards; next-FY cards "first backup pending" until their first success (unscheduled: "not backed up yet - press Run backup"). |
| 1 Apr, switch pending | 4 cards; rollover card lists each required leg: "done (<time>, <how>)" or "outstanding - <what to do>", plus the force option (approval). Hero: both years are backed up. |
| After switch | 2 cards (FY27-28); FY26-27 rows stay in history and reports, labelled. |
| Leg disabled | one "Disabled" card for that leg. |

`/api/rollover` `:1234-1261` adds `overlap_state(...)` and `final_copy_state(...)` per required leg;
`fy_rollover.manual_run_state` also allows "Run FY rollover now" when an activation is due but not done.

## 7. Config keys

| Key | Default | Bounds / validation | Meaning |
|---|---|---|---|
| `rollover.overlap_enabled` | `true` | bool; missing key = `true` | Back up the next FY's folder too, from the activation date until the April switch. |
| `rollover.overlap_activate_on` | `"12-31"` | `^\d{2}-\d{2}$`, real date in a non-leap year (`02-29` refused), not `04-01` | Day (MM-DD, IST) from which the daily check activates the next FY. |
| `cloud.scheduled` | `true` | bool; at least one enabled leg must be scheduled | `false` = manual only (no cron run; cron kept). |
| `lan.scheduled` | `true` | same | same |

All four: `config_help.py` labels (next to `:290-295`), `config_edit._RISKY_KEYS` (`core/config_edit.py:48-53`,
typed phrase - each can stop a backup), `config.example.yaml` + `deploy/config.install.yaml` written explicitly. No
new budget key (D-13); retries, cooldowns and clock checks reuse existing keys.

## 8. Owner answers (rev 1 questions)

- **A1** `overlap_enabled` default true in code and in every config; a config without the key is enabled.
- **A2** After 1 April a person may run either or both legs for the old FY from the dashboard; when a missing final
  copy was the only blocker the next daily check switches by itself; the forced switch (approval) stays. -> section 4.
- **A3** Per-leg `scheduled` switch is in scope. -> sections 4 and 5, tasks A6, C1-C2.
- **A4** Weekly audit covers both FYs; a budget key may be added if needed. -> D-13: not needed (and would collide
  with the default schedule).
- **A5** Activation needs a proven clock; waits and mails otherwise. -> section 3 step 2.
- **Layout** source `E:\FY..` siblings on a drive root, NAS `\\nas\backup\FY..` -> verified in the header; tests A2.

No new open questions.

## 9. Task list (test first; one commit per task; cite the task ID)

Each task: GitNexus `impact` on every symbol before editing (CLAUDE.md), write the failing test, implement, run the
file + `tests/test_flow_orchestration.py` + `tests/test_flow_status_semantics.py`, `detect_changes` before commit.
After each Part, the full unit suite. **Default-on:** the sandbox config (`test_support/sandbox_config.py`) does not
set `overlap_enabled`, so every test runs with the default (on). Until B1 wires activation nothing reads it; in B1 the
rollover / calendar tests that cross 31 Dec are reviewed one by one - each either asserts the activation or sets the
key to false in its own fixture with a comment why. No test may call the real `check_clock` (fake it, as today).
Overlap tests use the owner layout (`E:\FY26-27` + `\\nas\backup\FY26-27`, mapped to temp folders where files are
touched) plus one non-root source (`D:\Data\FY26-27`) as a control.

**Part A - building blocks (no behaviour change)**

- [x] **A1 Overlap keys.** `tests/test_fy_overlap_config.py`: defaults (on, `12-31`); a config without the keys is on;
  `01-15`, `10-01` accepted; `02-29`, `04-01`, `13-01`, `12-32`, `1231` refused with a plain message; help + risky
  lists; `test_fallback_defaults_from_model.py`, `test_config_validation.py` green; the owner pair
  `E:\FY26-27` / `\\nas\backup\FY26-27` loads.
- [x] **A2 `core/fy_overlap.py` helpers.** `tests/test_fy_overlap_unit.py`: `next_fy` (FY26-27, FY99-00);
  `activation_date` (FY26-27 -> 2026-12-31, `01-15` -> 2027-01-15, FY27-28 / leap year 2028); `closing_moment`;
  `fy_of_id` on `x-cloud`, `x-cloud-FY27-28`, `x-rollover-final-cloud`, `u-cloud-full-FY27-28`; `config_for_fy`:
  `E:\FY26-27` -> `E:\FY27-28` (not `E:FY27-28`, not `E:/FY27-28`), `\\nas\backup\FY26-27` -> `\\nas\backup\FY27-28`,
  the per-pass config passes `AppConfig` cross-field validation when re-validated, cloud-only placeholder kept, other
  sections identical; `ensure_nas_folder` parent = `\\nas\backup`; `row_fy` fallbacks.
- [x] **A3 Run-log folder names.** (A3b, owner request after A6: the FY also goes into the file names, section 2.3.) extend `tests/test_f1_run_artefact_names.py`: suffixed id -> `..._FY27-28` folder,
  re-open keeps the file prefix, two passes in the same minute get two folders, `purge_older_than` unaffected.
- [x] **A4 DB filters, final copy, approvals, records.** `tests/test_fy_overlap_db.py`: each `fy=` method with suffixed
  and unsuffixed rows; `guard_baseline` sees both passes of its FY; `final_copy_since` (status set, `guards.fy`,
  started before / at / after C, PARTIAL / VERIFY_FAILED / legacy `SUCCESS` ignored); `keep_fy` supersede;
  `supersede_approvals_of_fy`; overlap record round trip; schedule record with/without `*_scheduled`; **baseline DB
  compatibility** (fixture written by the base-commit DDL, user_version 5).
- [x] **A5 Alerts per FY.** `tests/test_fy_overlap_alerts.py`: `AlertEvent.fy=None` records today's mode; a FY27-28
  RECOVERED does not close a FY26-27 incident and vice versa; cooldown per group; watchdog parses
  `stale:cloud:FY27-28`; `test_alert_class.py` scan green; AAM-ROL-07 in the fault catalog.
- [x] **A6 `scheduled` keys + statuses set.** `tests/test_manual_only_leg.py` (part 1): defaults true; no enabled leg
  scheduled -> refused; budget validator skips an unscheduled leg (a schedule that only fits without it is accepted);
  `FINAL_COPY_STATUSES` in `core/statuses.py`.

**Part B - overlap wiring**

- [x] **B1 Activation.** `tests/test_fy_overlap_activation.py` (fakes for `check_clock`, `notify`; `pendulum.travel_to`):
  Dec 30 not due; Dec 31 due -> folder + placeholder + record + one INFO mail; Jan 1 catch-up; second call no-op;
  crash after the folder -> rerun completes; crash after the record -> nothing redone; clock UNVERIFIED -> nothing
  created, WARNING, ACTION_REQUIRED next day, then success once proven; system clock behind -> nothing;
  `CLOCK_BEHIND` / `JUMP` -> nothing; overlap off -> nothing; office-made folder adopted; junction refused;
  `ensure_nas_folder` cases (missing, empty, marker, non-empty without marker, `\\nas\backup` unreachable). Review of
  the existing tests that cross 31 Dec (see default-on note).
- [x] **B2 Pipelines take `fy_pass` / `budget_start`.** `tests/test_fy_overlap_pipelines.py` with `fake_run_tool`:
  next-FY cloud pass uses `E:\FY27-28` -> `gs://bucket/FY27-28`, LAN `E:\FY27-28` -> `\\nas\backup\FY27-28`,
  suffixed run_id, row `fy`/`overlap`, artifact key, no `file_entries` write, alert keys/titles, other FY's approval
  not applied; LAN next pass creates the NAS folder after WoL; unreachable parent = `LAN_FAILED` AAM-LAN-01;
  `fy_pass=None` produces exactly today's task calls.
- [ ] **B3 `backup()` loop.** `tests/test_fy_overlap_backup_flow.py`: order per leg; `fy=` runs one pass, inactive `fy`
  raises; first pass fails, second still runs, flow FAILED with both; cloud OK both FYs while LAN FY27-28 fails ->
  rows/alerts only for it; shared budget, `CLOUD_MISSED` for FY27-28 with its key; one WoL, shutdown rule D-8; race
  guard; overlap inactive -> one baseline call; manual run of an unscheduled leg runs both FYs.
- [ ] **B4 Final-copy state machine.** `tests/test_fy_overlap_rollover.py` (fixtures of
  `test_a6_rollover_journal_flow.py`): all scheduled legs OK -> switch, FY26-27 approvals superseded, record
  `ended_at`, config now `E:\FY27-28` / `\\nas\backup\FY27-28`; LAN final fails -> blocked, both FYs still active,
  mail lists LAN; manual LAN `LAN_COMPLETE` after C -> next check switches with no approval, warning line names the
  copy time; manual run **before** C -> still blocked; nightly cloud success at 1 Apr 01:00 + 06:00 cloud attempt
  fails -> counted with warning; `CLOUD_VERIFY_FAILED` / `CLOUD_PARTIAL` / `LAN_PARTIAL` after C -> not counted;
  unscheduled LAN not run by the check, outstanding until a manual copy; cloud-only (`lan.enabled false`) -> only cloud
  required; JUMP -> blocked even with copies; forced approval -> switch, forced legs named; G6 text keyword keeps
  verdicts identical; `unprotected_new_fy` silent while active; activation runs before the alarm.
- [ ] **B5 Calendar simulation.** `tests/test_fy_overlap_calendar.py` (pattern of `test_calendar_simulation.py`):
  2026-12-30 -> 2027-04-03 day by day, default schedule: 1 pass per leg until Dec 31 06:00, then 2, back to 1 after the
  switch; Feb 29 2028 in the FY27-28 -> FY28-29 cycle; clock behind on Dec 31; server off Dec 31 (activation Jan 1);
  switch blocked 1-2 Apr by a failed LAN final, manual LAN run 2 Apr, switch 3 Apr 06:00; FY27-28 overdue counted from
  activation; an unscheduled LAN leg never overdue in the whole run.
- [ ] **B6 Integrity audit per FY.** `tests/test_fy_overlap_audit.py`: next FY then configured FY per leg in one slot,
  suffixed audit ids, `latest_audit(leg, fy=)`, alert key `@FY27-28`, one NAS wake/shutdown, shared budget (budget
  used up -> configured FY `AUDIT_INCOMPLETE`, next FY verified), scheduled audit skips an unscheduled leg, `fy=`.
- [ ] **B7 Attention / overdue / watchdog.** `tests/test_fy_overlap_attention.py`: per-FY items; next FY "waiting"
  from activation; watchdog mails `stale:lan:FY27-28` once; `overlap_off:` RED item; activation-waiting item.
- [ ] **B8 Reports.** `tests/test_fy_overlap_report.py`: sections per (leg, FY); first January week no false "no
  backup ran" for FY27-28; 28 Mar - 3 Apr report labels both FYs across the switch; legacy rows without `fy` fall
  into the configured-FY section.
- [ ] **B9 Dashboard API + UI.** `tests/test_fy_overlap_ui.py` (TestClient, fake `arun_deployment`): `/status` with and
  without overlap; triggers with `?fy=` (active / inactive / disabled / running); approved re-run passes `fy`;
  `/api/rollover` with `final_copy_state`; extend `tests/test_e8_dashboard_e2e.py` for 4 cards and per-card buttons.

**Part C - manual-only legs (in scope)**

- [ ] **C1 Registration.** `tests/test_manual_only_leg.py` (part 2): `serve._deployments()` registers an unscheduled
  enabled leg with no schedule, a scheduled cloud leg with its cron while LAN is unscheduled or disabled;
  `_reconcile_disabled_legs` keeps it resumed; schedule-in-effect record carries the flags. Probe against a local
  Prefect server (clean `PREFECT_HOME`): re-serve removes the old cron, no scheduled runs appear, `arun_deployment`
  starts it; if the cron survives, add the delete to the reconcile step (with its test).
- [ ] **C2 Behaviour.** same file (part 3): no overdue / first-run items and no watchdog mail for an unscheduled leg
  (both FYs); failed manual run still red + failure mail; reports "run by hand", 0 scheduled; email `_next_text`
  wording; card schedule line; April gate covered in B4.

**Part D - finish**

- [ ] **D1 Docs.** `docs/ARCHITECTURE.md` §2, §3.1, §5.1, §5.2, §10.7 (gates R9 activation, R10 overlap passes, R11
  final-copy rule, R12 manual-only legs); `SESSION_HANDOFF.md`; `docs/EMAIL_CONTENT_SPEC.md` (keys, AAM-ROL-07).
- [ ] **D2 Audit lab (targeted).** `audit_lab/test_fy_overlap_lab.py`: real robocopy + rclone (`deploy\bin\rclone.exe`,
  test bucket prefix) with two sibling FY folders and a `backup` parent on the fake share: two mirrors, two cloud
  prefixes, a delete in FY27-28 never touches FY26-27, G3/G5 per FY. Clean `PREFECT_HOME`.
- [ ] **D3 Trial box.** Deploy by git bundle; for the test only set `overlap_activate_on` = today with test prefixes,
  one night of 4 runs, one manual-only LAN run from the card; put `12-31` back. Tell the owner before the restart.

## 10. Baseline behaviours that must stay unchanged (and how)

| Behaviour | How the plan keeps it |
|---|---|
| rclone / robocopy commands, flags, retries, `--max-delete` backstop | engine modules (`cloud_sync`, `lan_sync`, `cloud_verify`, `cloud_reporter`, `tool_runner`) are not edited |
| Guards G1-G6 verdicts, approval rules | `core/guards.py` logic untouched (G6 gets a text-only keyword, test proves equal verdicts); baselines already per FY |
| Verify / evidence gate, terminal-status policy | same pipeline code per pass (`flow.py:1675-1699`, `:1784-1788`, `:2088-2092`) |
| WoL, NAS shutdown rules | tasks unchanged; only *when* they are called across passes (D-8) |
| RUNNING row before the slot, one outcome row per run, ABANDONED sweep | `_pipeline_slot` / `_record_run` / `start_run_row` per pass |
| Slot + `backup.lock` serialisation | each pass takes them like any pipeline; passes are sequential |
| Database file and schema | no DDL, user_version 5, compatibility test (A4) |
| Before the activation date (default on) | `active_passes` returns one pass, pipelines get `fy_pass=None`, every `fy=None` query sees all existing rows |
| `scheduled: true` (default) | deployments, crons, overdue, watchdog, audit, reports exactly as today |
| Rollover journal steps, compare-and-set switch, archive of the old prefix only, D-17 repeat, clock refusals | unchanged; changed: the per-leg final-copy decision (section 4, owner A2) and a post-switch cleanup hook |
| Schedules and budget proof | shared leg budget keeps `_schedule_fits_the_budgets` valid; unscheduled legs are simply not in it |
| Separate cloud prefixes, GCS lifecycle | each FY syncs only `gs://bucket/<fy>`; `deploy/gcs_lifecycle.json` unchanged (its 365-day ARCHIVE rule cannot reach overlap objects) |
| Alert classes, switches, cooldowns, recipients | new keys use existing prefixes and kinds; incidents grouped per FY only for next-FY passes |
| Settings never change the FY | the overlap never writes `config.yaml`; only the April switch does |

## 11. Risks (accepted unless the owner says otherwise)

- Files edited in FY26-27 after the April switch are not backed up (baseline). With the A2 rule, a leg counted from an
  earlier copy can also miss changes made between that copy and the switch; the announce mail says so per leg.
- A flow run waiting for the slot while the rollover switches can still run one pass with the old view (baseline has
  the same window; the race guard in 2.2 closes it between passes).
- Next-FY passes do not fill `file_entries`, so the "files on NAS / in the cloud" counters show the configured FY only;
  the next-FY cards show `total_files` from their last run.
- Note: a NAS FY folder directly at a share root (`\\nas\FY26-27`) would have no parent to create siblings in; not the
  owner's layout (`\\nas\backup\FY..`), so out of scope.
