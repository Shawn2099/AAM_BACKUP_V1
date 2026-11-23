# Plan - FY overlap on the baseline (two active FYs from 31 Dec to the April switch)

Status: **DRAFT for owner review** (2026-11-23). Branch `baseline-v1-overlap`, base tag `baseline-v1` = `9461ac2`
(the version on the trial box). Planning only: nothing is built until the owner says go.

Replaces, for this branch, the heavy attempt on `fy-overlap` (`docs/PLAN_FY_OVERLAP_2026-11-20.md`: schema v6, fresh
DB, dependency upgrades, small-folder guards). Ideas kept from it: the per-FY "folder view" config, run-id / log-folder
naming per FY, NAS folder adopt rules, per-FY approvals. Everything else is dropped.

Line numbers below are from the base commit `9461ac2` and were checked while writing this plan.

---

## 1. Design summary and key decisions

**One sentence:** while an overlap is active, every backup flow run (and the weekly audit) runs the **unchanged**
pipeline twice per leg - first for the configured FY, then for the next FY with a copied config whose two paths point
at the next FY's folders - and every row, alert, approval and log of the second pass is tagged with its FY by a
`-FYxx-yy` suffix on its id. No database schema change. The April rollover is the baseline rollover; it simply keeps
the overlap running until it has switched.

| ID | Decision | Reason |
|---|---|---|
| D-1 | **Active set** = `[configured FY]`, plus `[next FY]` when `rollover.overlap_enabled` and the next FY has an **activation record**. Order: configured first, then next (owner order). | One rule, no date logic in the nightly runs. The overlap ends by itself at the config switch: the new configured FY's next FY has no activation record. |
| D-2 | **Activation** (create next source folder + placeholder, write the record) runs in the daily `rollover_check_flow`, on or after `rollover.overlap_activate_on` (default `12-31`), on a proven clock. The NAS folder is made by the first next-FY LAN pass (NAS is awake then). | The 06:00 check is the existing place for FY work; no WoL / shutdown logic is needed in activation; NAS offline cannot block it. |
| D-3 | **Per-pass config** = `config.model_copy` with `paths.source_drive` / `paths.lan_destination` moved to the next FY (`fy_rollover._parent_path` / `_child_path`, the same rule `update_config_yaml` uses). | Every task already reads its paths from `config`; cloud prefix (`_effective_fy_prefix`), guard FY, verify, LAN snapshot, health check follow with zero engine change. |
| D-4 | **No schema change.** The FY of a next-FY pass is in its ids: run_id `<flow-run>-<leg>-FY27-28`, audit_id `<uuid>-<leg>-full-FY27-28`. The activation record lives in the existing `db_meta` key/value table. Every row also gets `"fy"` in `extended_metrics` (display / reports). | The baseline DB (user_version 5) opens unchanged; rolling the code back is safe; RUNNING / ABANDONED rows carry the FY from the first insert. |
| D-5 | ManifestDB "latest / last success" lookups gain `fy: str \| None = None`: `None` = rows **without** an FY suffix (the configured FY's pass, i.e. all rows today), a value = that FY's suffixed rows. | With the overlap off there are no suffixed rows, so every existing call returns exactly what it returns today. |
| D-6 | Guard baselines unchanged: `guard_baseline(leg, fy)` already filters on `$.guards.fy` (`core/manifest.py:845-867`). | Per-FY baselines for free, and after the switch FY27-28 inherits its three months of overlap baselines (G2/G4 stay armed). |
| D-7 | Both passes of a leg **share the leg's run budget** (same start); only the first pass gets `late_seconds`. A pass with no budget left is `<LEG>_MISSED` for its FY. | `AppConfig._schedule_fits_the_budgets` (`models/config.py:1166-1184`) stays a true proof that a leg run never runs into the next job. |
| D-8 | LAN: WoL once (first pass); passes before the last run with `allow_shutdown=False`; the last pass may shut the NAS down only if every earlier pass ended `LAN_COMPLETE` (or `lan.shutdown_on_all_retries_exhausted` is on - today's rule). | One wake, one shutdown, today's "do not power off after a failure" rule applied across both folders. |
| D-9 | The next-FY pass **does not write `file_entries`** (`cloud_record_task` / `lan_record_task`) and skips the cloud bucket-vs-DB cross-check. | `file_entries.relative_path` is UNIQUE per path, not per FY (`core/manifest.py:42`); a second FY would prune the first FY's entries (`prune_stale_synced`). The table only feeds metrics cross-checks and the "files" counters. |
| D-10 | April 1 = baseline rollover (`_rollover_resume`, unchanged steps). Final backups cover the configured FY only. While G6 blocks, config stays on FY26-27, so **both FYs keep being backed up every night** with no new code. At the switch: pending approvals of the retired FY are superseded, the activation record is stamped `ended_at`. | Owner rule "old FY retired only after its final copy succeeded on every enabled leg" is exactly baseline G6 + D-17 retry; the overlap makes the waiting period safe. |
| D-11 | Default `rollover.overlap_enabled = false` in the model, `true` in `config.example.yaml` and on the trial box. | The box and every test behave exactly as today until a person switches it on (owner question Q1). |

## 2. The mechanism: one pipeline pass per active FY

### 2.1 New module `core/fy_overlap.py` (pure helpers + the activation step)

- `next_fy("FY26-27") -> "FY27-28"` (2-digit, modulo 100: `FY99-00 -> FY00-01`).
- `activation_date(configured_fy, "12-31") -> date(2026, 12, 31)`: month >= 4 -> FY start year, else start year + 1.
- `fy_of_id(run_or_audit_id) -> "FY27-28" | None` (regex `-(FY\d{2}-\d{2})$`), `pass_run_id(base, fy)`,
  `artifact_key(base, fy)` (`cloud-backup-summary-fy27-28`, Prefect keys are lowercase).
- `config_for_fy(config, fy)`: source -> `_child_path(_parent_path(source), fy)`; NAS path moved only when its leaf is
  an FY folder (cloud-only placeholder stays, as `update_config_yaml` does at `core/fy_rollover.py:410,422`).
- `FyPass(fy: str, extra: bool, config)`; `active_passes(config, db) -> list[FyPass]`. `[FyPass(configured, False,
  config)]` always first; the next FY is added when `overlap_enabled` and `db_meta['overlap:<next>']` exists and has no
  `ended_at`. Folder existence does **not** decide (fail closed: a deleted next folder makes that pass fail loudly in
  its health check, it is never dropped silently).
- `row_fy(row)`: suffix, else `extended_metrics.fy`, else `extended_metrics.guards.fy`, else None (reports, cards).
- `overlap_state(config, db, today)` for the dashboard / rollover card: enabled, configured, next, activates_on,
  activated_at, active, note.
- `activate_if_due(config, db, now)` and `ensure_nas_folder(lan_root, fy)` - see section 3.

### 2.2 `flow.backup()` (`flow.py:2972-3080`)

- New parameter `fy: str | None = None` (manual card buttons, approved re-runs). Must be in the active set, else
  `ValueError` (Prefect FAILED, no row) - the same pattern as the disabled-leg guard at `flow.py:3012-3015`.
- `passes = fy_overlap.active_passes(config, db)` once at flow start (one decision per flow run), filtered by `fy`.
- When the active set has one entry, the pipelines are called with `fy_pass=None`: **byte-for-byte the baseline call**.
- For each leg (cloud then LAN, as today), `leg_start = time.monotonic()`; for each pass:
  `pipeline(p.config, run_id, now_iso(), time.monotonic(), budget_start=leg_start, late_seconds=late_s (first pass
  only), approval_id=approval_id, fy_pass=p, allow_shutdown=<D-8 rule>)`. Exceptions are collected exactly like the
  two legs today (`excs`, `ExceptionGroup`); a failed first pass never stops the second.
- Race guard: before every pass after the first, re-read `configured_fy` from `config.yaml`; if it changed since the
  flow started (the rollover switched meanwhile), stop the loop with a log line (the next run uses the new config).

### 2.3 Inside the two pipelines (`_run_cloud_pipeline` `flow.py:1536`, `_run_lan_pipeline` `flow.py:1902`)

New keyword parameters, both defaulting to baseline behaviour: `fy_pass: FyPass | None = None`,
`budget_start: float | None = None` (`RunBudget(..., (budget_start or monotonic_start) - late_seconds)` at
`flow.py:1545-1548` / `1913-1916`). Everything below is driven by `fy_pass` and is a no-op when it is None:

| Item | Configured-FY pass | Next-FY pass (`fy_pass.extra`) | Where |
|---|---|---|---|
| Paths, cloud prefix, guard FY, verify, LAN dest | from `config` (unchanged) | from the per-pass config | all tasks read `config.paths.*`; `_effective_fy_prefix` `flow.py:102` |
| run_id | `<flow-run>-cloud` (unchanged) | `<flow-run>-cloud-FY27-28` | `backup()`; `_stable_run_id` `flow.py:130` unchanged |
| run_history row | mode `cloud`/`lan`, statuses unchanged, + `"fy"` in metrics | same mode/statuses, suffixed run_id, + `"fy"`, `"overlap": true` | `_record_run` `flow.py:2173`; RUNNING row `start_run_row` `core/backup_repository.py:122` (status from mode, so mode must stay the leg) |
| Run-log folder | `<date>_<HHmm>_cloud_<id8>` (unchanged) | `<date>_<HHmm>_cloud_<id8>_FY27-28` | `core/run_artefacts.py:39` (`_RUN_DIR` gets an optional `_FYxx-yy` tail), `_leg_of` `:49` strips the suffix first, `__init__` `:71-76` appends it |
| Prefect artifact key | `cloud-backup-summary` | `cloud-backup-summary-fy27-28` | `flow.py:1378`, `flow.py:1414` (key becomes a parameter) |
| Slot + `backup.lock` | per pipeline, unchanged | per pipeline, unchanged (passes are sequential) | `_pipeline_slot` `flow.py:494`, `_backup_slot` `:194` |
| Alert keys | unchanged (`backup:cloud:<status>`, `blocked:cloud`, `missed:cloud`, `recovered:cloud`) | `backup:cloud:FY27-28:<status>`, `blocked:cloud:FY27-28`, `missed:cloud:FY27-28`, `recovered:cloud:FY27-28` | `_send_run_alert` `:325`, `_open_block_request` `:1082`, `_missed_if_no_budget` `:450`, `_send_recovery_if_needed` `:355`, slot timeout `:516`; all prefixes already in `alert_class.KNOWN_PREFIXES` |
| Alert title | `CLOUD backup failed` | `CLOUD backup (FY27-28) failed` | while the overlap is active BOTH passes show their FY in the title (text only) |
| Notification incident group | `notifications.mode = cloud` | `notifications.mode = cloud:FY27-28` | `AlertEvent.fy` (new, default None); `core/notify.py` uses `group = mode if not fy else f"{mode}:{fy}"` in `_incident` `:623-642`, `_incident_closed` `:589-616` (`leg_success_after(leg, since, fy=fy)`) and the record `:677-686`. `AlertEvent.mode` stays the leg, so `email_compose` / `support_bundle` rendering is unchanged |
| Approvals | `approvals.fy` already stored from `guard_record["fy"]` (`flow.py:1101`) | same | supersede per (leg, FY) - section 2.4; `_apply_approval` `:1042` also refuses an approval whose `fy` differs from the pass FY |
| Guard baseline | `guard_baseline(leg, FY26-27)` | `guard_baseline(leg, FY27-28)` | unchanged code (D-6) |
| Verify (daily size check + evidence gate) | unchanged | unchanged, on `gs://bucket/FY27-28` | `cloud_verify_and_report_task` `:863` |
| `file_entries` + cross-check | unchanged | skipped (D-9); metrics say `"cross_check": "skipped (overlap pass)"` | `flow.py:1570-1577`, `:1713`, `:1720`, `:2009` |
| Pending rollover NAS step | `_complete_pending_rollover_nas_folder` `:1957` | `fy_overlap.ensure_nas_folder` after WoL, before preflight; failure raises `faults.coded(..., "AAM-LAN-01")` -> that pass is `LAN_FAILED` with its own alert | `flow.py:1954-1958` |
| Recovery mail | `previous_run_status(leg, run_id)` | `previous_run_status(leg, run_id, fy=...)` | `flow.py:355-375` |
| Abandoned sweep | unchanged | FY parsed from run_id -> alert with `fy` | `core/backup_repository.py:130-155` |

### 2.4 Database (no schema change, no version bump)

`SCHEMA_VERSION` stays 5 (`core/manifest.py:25`); no DDL, no ALTER. Additions to `ManifestDB` only:

- SQL helper `_fy_clause(fy)`: `None` -> `substr(run_id, -8) NOT GLOB '-FY[0-9][0-9]-[0-9][0-9]'`; a value ->
  `substr(run_id, -8) = '-' || ?`. Baseline ids end in `-cloud`/`-lan`/`-rollover`, so every existing row is "None".
- `fy=None` keyword added to: `last_run` `:790`, `last_successful_run` `:804`, `previous_run_status` `:823`,
  `previous_run_fault_code` `:832`, `first_partial_after` `:1286`, `leg_success_after` `:1308`,
  `latest_audit` `:919` (same clause on `audit_id`). `told_about_leg_since` `:1339` needs no change (it is queried with
  the incident group string `cloud:FY27-28`).
- `get_runs_since` / `get_runs_between` / `get_recent_runs` unchanged (all rows); callers filter with `fy_of_id`.
- `open_approval_request` `:956-974`: new keyword `keep_fy: str | None = None`; the supersede becomes
  `... WHERE leg = ? AND status = 'PENDING' AND COALESCE(fy,'') != COALESCE(?, '#')` - with `keep_fy=None` it is
  today's statement's effect (every pending request of the leg). The pipelines pass the *other* active FY.
- `supersede_approvals_of_fy(fy)` (used at the switch) and `overlap_record(fy)` / `set_overlap_record(fy, dict)` on top
  of `_meta` / `_set_meta` `:1213-1222` (key `overlap:FY27-28`, JSON `{activated_at, from_fy, source_folder,
  clock, ended_at}`).
- Compatibility test: copy a baseline DB (created by the `9461ac2` code with runs, approvals, journal), open it with the
  new code: no migration runs, `user_version` stays 5, every old query returns the same rows.

### 2.5 Overdue, attention, watchdog, reports, audit

- `core/attention.py`: `_leg_clock(db, config, leg, sched, fy=None)` `:93` uses `last_successful_run(leg, fy=fy)`;
  for the next FY with no success yet the base is `activated_at` (not `first_known_at`). `compute_attention` `:195`
  loops `(leg, None)` plus `(leg, next_fy)` when active; item codes get `:FY27-28` (`stale:cloud:FY27-28`,
  `first_run:...`, `last_run:...`, `audit:...`), messages name the FY. `unprotected_new_fy` (`:321-331`,
  `core/fy_rollover.py:262`) returns None while the overlap is active for that FY (its files ARE backed up).
- `watchdog._check_stale_legs` `watchdog.py:399-416`: `rest = code.split(":",1)[1]`, `leg, _, fy = rest.partition(":")`;
  `AlertEvent(key=f"stale:{rest}", mode=leg, fy=fy or None)`; `told_about_leg_since(rest, ...)`.
- Reports (`core/report.py:201-268`): sections per (leg, FY) using `row_fy(row)`; a next-FY section counts scheduled
  runs only from `activated_at`; "last good copy" per FY uses `guard_baseline`-style lookup on `$.guards.fy` (every
  success row has it). `email_compose._data_safety` `:125-139` lists one line per active (leg, FY).
- Integrity audit (`flow.py:2818-2965`): inside the one slot, per leg the configured FY then the next FY (per-pass
  config, one shared `audit_max_hours` budget as today, one NAS wake/shutdown at the end), audit_id suffixed,
  `_pending_context(..., fy)` `:1435`, outcome strings `cloud@FY27-28=...` in the alert key. New flow parameter
  `fy: str | None = None` for the per-card "Check now".

## 3. Activation (31 December) - idempotent, NAS-independent

`fy_overlap.activate_if_due(config, db, now)` is called by `rollover_check_flow` (`flow.py:2340-2377`) right after log
retention and **before** `_alarm_unprotected_new_fy` and `_rollover_resume`. Never raises (a failure is mailed and
retried next morning). Steps:

1. Not enabled, record already present, `configured_fy` None -> return. Due when `today (IST) >= activation_date` and
   `classify_rollover(configured, get_fy_prefix())` is `NONE` (normal, Dec 31 - Mar 31) or `FORWARD` (after 1 April
   while the switch is pending - so enabling the overlap late, or a server that was off on Dec 31, still activates).
   `JUMP` / `CLOCK_BEHIND` -> never (the rollover check already alerts for those).
2. Clock proof: `check_clock(...)` exactly as the rollover does (`flow.py:2560-2566`); not `CLOCK_OK` -> WARNING mail
   `rollover:activation:FY27-28` (AAM-ROL-07, new catalog entry), nothing created, retry tomorrow. A clock that is
   behind simply makes "today" earlier than the activation date (nothing happens, no harm).
3. Source folder: `fy_rollover.create_source_folder(_parent_path(source), next_fy, placeholder)` `core/fy_rollover.py:353`
   - idempotent (`mkdir exist_ok`, placeholder opened with `"x"`, never overwritten). An existing folder made by the
   office is adopted as is. A link / junction is refused (`_new_fy_root` rule `:234-239`) -> failure mail.
4. Write `db_meta['overlap:FY27-28'] = {activated_at, from_fy, source_folder, clock}` **after** step 3.
   Crash between 3 and 4 -> next morning repeats 3 (no-op) and writes 4. Crash after 4 -> nothing to redo.
5. INFO mail `rollover:activated:FY27-28` ("From today FY27-28 is backed up as well as FY26-27; save new-year files
   there from 1 January"), `audience=reports`, sent once (key + `last_notification` check, like
   `_resolve_unprotected_new_fy` `flow.py:2388-2390`).

Failure mail kind: WARNING while `today == activation date`, ACTION_REQUIRED from the next day (the office starts
saving into the new year). Cooldown = `alerts.stale_repeat_hours` (existing key).

**NAS folder (offline-safe):** made by the next-FY LAN pass after `wol_check_task`, with `ensure_nas_folder(lan_root,
fy)` (stricter than `create_nas_folder` `core/fy_rollover.py:376-390`, which is left unchanged for the rollover):
share root not a directory -> `OSError` (pass FAILED, AAM-LAN-01, "NAS share not reachable, FY27-28 not created");
folder missing -> create + `MOUNT_MARKER`; exists with marker -> OK; exists and empty -> marker; exists with files and
no marker -> leave it (G5 blocks that FY's pass and asks for approval, the approval creates the marker as today,
`flow.py:1193-1203`). The configured FY's pass is never affected by any of this.

Overlap switched off after activation: passes stop at once (D-1); `compute_attention` adds a RED item
`overlap_off:FY27-28` when the record has no `ended_at` and the folder holds files ("files in FY27-28 are no longer
backed up - switch the overlap back on or finish the year"); the `rollover:unprotected` alarm covers it after 1 April.

## 4. April 1 rollover with an overlap

The journal (`flow.py:2441-2752`, `core/rollover_journal.py`) is unchanged in its steps and their order:

- `source_folder` / `create_source_folder_early` `flow.py:2703-2718`: the folder exists already -> done (idempotent).
- `final_backups` `flow.py:2599-2614` -> `_run_final_backups` `:2774-2795`: configured FY only, every **enabled** leg,
  through the normal pipelines (`fy_pass=None`), LAN with `allow_shutdown=False`. Not waiting for any schedule: the
  rollover runs the legs itself.
- G6 `core/guards.py:229-258`: verdict logic unchanged. One text-only change: a keyword `overlap_active=False`; when
  True the last sentence of the AAM-ROL-02 reason (`:254-255`, "nothing saved in {to_fy} is backed up yet") becomes
  "until then both {from_fy} and {to_fy} are backed up every night; approving retires {from_fy} without its final copy
  on: {legs}". A test proves verdicts are identical for both values.
- `nas_folder`: exists (made by the overlap) -> `create_nas_folder` touches the marker (baseline); `config_switch`,
  `archive` (only `gs://bucket/FY26-27/`), `nas_shutdown`, `announce` unchanged. Announce text adds "FY26-27 is no
  longer backed up; FY27-28 has been backed up since <activated_at>".
- Right after `config_switch` is done (in `_rollover_resume`, next to `_resolve_unprotected_new_fy` `:2739-2743`,
  never raising): `supersede_approvals_of_fy(from_fy)`, stamp `overlap:<to_fy>.ended_at`.

**"Keep both until the final copy succeeded on every enabled leg" - the loop:**

| Day | What happens |
|---|---|
| 1 Apr 06:00 | Final backups FY26-27. All enabled legs succeed -> switch, archive, one FY from tonight. |
| 1 Apr 06:00, a leg fails (e.g. NAS off, LAN failed) | G6 blocks: `ROLLOVER_BLOCKED`, approval request, ACTION_REQUIRED mail (existing `blocked:rollover`) listing the outstanding legs. Config stays FY26-27, the activation record stays -> **18:00 cloud and 01:00 LAN back up FY26-27 and FY27-28** (normal nightly runs, normal alerts per FY). |
| Every next morning | D-17 (`reset_at_start` `core/rollover_journal.py:78-85`) repeats the final backups of every enabled leg; the first morning they all succeed the switch happens. |
| Any time | "Run FY rollover now" on the rollover card re-runs the check at once (e.g. after switching the NAS on). |
| Human override | Approving the G6 request switches without the missing final copy (baseline; owner question Q2). |

**Leg not enabled** (`lan.enabled: false`, cloud-only): no LAN passes, no NAS folder (`step_nas_folder` skips at
`flow.py:2621-2622`), not in `final_status`, so G6 never waits for it. Same for `cloud.enabled: false`.

**Leg enabled but run by hand only:** the baseline has no such setting (an enabled leg is always scheduled,
`serve.py:35-55`). The rollover never depends on a schedule (it runs the finals itself), so nothing waits forever and
nothing is skipped silently: a leg that cannot run (NAS off, no WoL) is listed in the G6 block mail and on the rollover
card, retried daily, and finished with "Run FY rollover now". If the owner wants real manual-only legs, Part C adds
them (section 7, owner question Q3).

Mails in this phase are all existing classes: per-FY run failures (FAILURE), `blocked:rollover` (DECISION),
`rollover:<to_fy>:<step>` (ACTION), `rollover:done:<to_fy>` (INFO). Nothing auto-proceeds.

## 5. Dashboard and API

**`/status`** (`ui.py:514-592`): existing `cloud` / `lan` objects keep their meaning (configured FY, `fy=None`
queries) and gain `"fy"`. When the overlap is active, `cloud_next` / `lan_next` objects with the same shape
(`last_run`, `last_success`, `timing`, `integrity`, `nights`, `running`, `enabled`, `fy`) and `overlap:
{enabled, active, next_fy, activates_on, activated_at, note}`. `_get_last_success` `:1553`, `_integrity_summary`
`:1561`, `_last_run_summary` `:1603`, `_nights` `:595`, `_leg_timing` `:607` take `fy`. "Running" for a card = the
leg's Prefect run is active **and** that FY's last row is `*_RUNNING`; leg active but this FY not yet -> badge "Queued
(after FY26-27)". `recent_runs` rows carry `fy` (`row_fy`).

**Triggers:** `/trigger/cloud` `:648`, `/trigger/lan` `:686`, `/trigger/verify/{mode}` `:1361` accept `?fy=`. It
must be in the active set (409 "FY27-28 is not backed up yet / any more"); passed to the deployment as `parameters={"fy":
...}`. Without `fy` they run every active FY (today's button behaviour). `_start_approved_run` `:777` passes
`{"approval_id", "fy": req["fy"]}` for cloud/LAN; a request whose FY is no longer active -> 409.

**Cards** (`templates/dashboard.html:54-91`, `static/js/dashboard.js:246-307`): the two duplicated per-leg blocks
become one `renderLegCard(key, d)` (`key` = `cloud`, `lan`, `cloud-next`, `lan-next`); two extra `<article>` cards
`card-cloud-next` / `card-lan-next` in a row hidden unless `overlap.active`. Titles: "Online copy (cloud) - FY26-27",
"... - FY27-28". Each card: own badge, last backup (with the row's FY, so on 1 April the FY27-28 card honestly shows
"FY26-27 final backup 06:00" until its first run), last success, strip, safety check, **Run backup** and **Check
now** with its FY. Source panel shows "FY26-27 + FY27-28".

What the user sees:

| State | Cards |
|---|---|
| Overlap off / not yet active | 2 cards (as today); rollover card: "FY27-28 will be added on 31 Dec 2026" or "overlap is off". |
| Active (Jan-Mar) | 4 cards; next-FY cards amber "first backup pending" until their first success, then normal. |
| Activation failed | 2 cards + attention item (amber on the day, red after) with the reason; rollover card shows it. |
| 1 Apr, switch blocked | 4 cards stay; rollover card lists outstanding final legs + the approval; hero says both years are backed up. |
| After switch | 2 cards, FY27-28; FY26-27 rows stay in history and reports, labelled. |
| Leg disabled | that leg shows one "Disabled" card (no FY pair). |

`/api/rollover` `:1234-1261` adds `overlap_state(...)`; `fy_rollover.manual_run_state` lets "Run FY rollover now" run
also when an activation is due but not done.

## 6. Config keys (all in `RolloverConfig`, `models/config.py:802-833`)

| Key | Default | Bounds / validation | Meaning |
|---|---|---|---|
| `rollover.overlap_enabled` | `false` (Q1) | bool | Back up the next FY's folder too, from the activation date until the April switch. |
| `rollover.overlap_activate_on` | `"12-31"` | `^\d{2}-\d{2}$`, a real date in a non-leap year (so `02-29` is refused), not `04-01` | Day (MM-DD, IST) from which the daily check activates the next FY. |

Both: `config_help.py` labels (next to `:290-295`), `config_edit._RISKY_KEYS` (`core/config_edit.py:48-53`, typed
phrase - switching off stops a backup), `config.example.yaml` + `deploy/config.install.yaml` (`true`, `12-31`).
Part C only: `schedule.cloud_scheduled`, `schedule.lan_scheduled` (bool, default `true`) and
`dashboard.manual_reminder_days` (int, default 7, 1-90). No other new keys: retries, cooldowns, budgets and clock
checks reuse existing keys.

## 7. Task list (test first; one commit per task; cite this plan's task ID)

Each task: GitNexus `impact` on every symbol before editing (CLAUDE.md), write the failing test, implement, run the
file + `tests/test_flow_orchestration.py` + `tests/test_flow_status_semantics.py`, `detect_changes` before commit.
After each Part, the full unit suite. With `overlap_enabled: false` every existing test must stay green unchanged.

**Part A - building blocks (no behaviour change while the overlap is off)**

- [ ] **A1 Config keys.** `tests/test_fy_overlap_config.py`: defaults; `12-31`, `01-15`, `10-01` accepted; `02-29`,
  `04-01`, `13-01`, `12-32`, `1231` refused with a plain message; keys present in `config_help`, risky list;
  `test_fallback_defaults_from_model.py` / `test_config_validation.py` still green.
- [ ] **A2 `core/fy_overlap.py` pure helpers.** `tests/test_fy_overlap_unit.py`: `next_fy` (FY26-27, FY99-00);
  `activation_date` for FY26-27 (2026-12-31), `01-15` (2027-01-15), FY27-28 around leap year 2028; `fy_of_id` on
  `x-cloud`, `x-cloud-FY27-28`, `x-rollover-final-cloud`, `u-cloud-full-FY27-28`; `config_for_fy` on `D:\FY26-27`,
  drive root `D:\` parent rule, UNC `\\nas\share\FY26-27`, cloud-only placeholder kept; result paths only changed,
  other sections identical; `row_fy` fallbacks.
- [ ] **A3 Run-log folder names.** extend `tests/test_f1_run_artefact_names.py`: `<id>-cloud-FY27-28` -> folder
  `..._cloud_<id8>_FY27-28`, re-open keeps the file prefix, two passes of one flow run in the same minute get two
  folders, `purge_older_than` unaffected.
- [ ] **A4 DB filters, approvals, overlap record.** `tests/test_fy_overlap_db.py`: each listed method with suffixed
  and unsuffixed rows (`None` sees only unsuffixed, `FY27-28` only its rows); `guard_baseline` sees both passes' rows
  of its FY; `open_approval_request(keep_fy=...)` keeps the other FY's PENDING request, `keep_fy=None` = baseline;
  `supersede_approvals_of_fy`; `overlap_record` round trip; **baseline DB compatibility** (fixture DB written by the
  base-commit DDL, user_version 5: opens, no migration, same results).
- [ ] **A5 Alerts per FY.** `tests/test_fy_overlap_alerts.py`: `AlertEvent.fy` default None keeps today's recorded
  mode; a FY27-28 RECOVERED does not close a FY26-27 incident (and vice versa); cooldown per group; watchdog parses
  `stale:cloud:FY27-28`; `test_alert_class.py` scan still green.

**Part B - wiring**

- [ ] **B1 Activation.** `tests/test_fy_overlap_activation.py` (fakes for `check_clock`, `notify`; `pendulum.travel_to`):
  Dec 30 not due; Dec 31 due -> folder + placeholder + record + one INFO mail; Jan 1 catch-up; second call no-op (no
  second mail); crash after the folder (record missing) -> rerun completes; crash after record -> nothing redone;
  clock UNVERIFIED -> WARNING, nothing created, ACTION_REQUIRED the day after; clock behind (system date Dec 30 on
  real Dec 31) -> nothing; `CLOCK_BEHIND` / `JUMP` classification -> nothing; overlap disabled -> nothing; existing
  office-made folder adopted (placeholder only if missing); junction refused. `ensure_nas_folder`: missing, empty,
  marker present, non-empty without marker (left), share root missing (OSError).
- [ ] **B2 Pipelines take `fy_pass` / `budget_start`.** `tests/test_fy_overlap_pipelines.py` with
  `test_support.fake_tools.fake_run_tool`: next-FY cloud pass uses `gs://bucket/FY27-28`, suffixed run_id, row has
  `fy`/`overlap`, artifact key `-fy27-28`, no `file_entries` write, alert keys/titles with FY, approval of the other
  FY not applied; LAN next pass creates the NAS folder after WoL, share-root failure = `LAN_FAILED` AAM-LAN-01;
  `fy_pass=None` produces exactly today's calls (snapshot of task arguments).
- [ ] **B3 `backup()` loop.** `tests/test_fy_overlap_backup_flow.py`: order FY26-27 then FY27-28 per leg; `fy=`
  runs one pass, an inactive `fy` raises; first pass failing, second still runs, flow FAILED with both errors; one
  leg failing while the other succeeds (cloud OK both FYs, LAN FY27-28 fails) -> per-FY rows/alerts only for it;
  shared budget: second pass gets the remainder, none left -> `CLOUD_MISSED` for FY27-28 with key
  `missed:cloud:FY27-28`; LAN: one WoL, `allow_shutdown` False on pass 1, True on pass 2 only if pass 1
  `LAN_COMPLETE`; race guard stops pass 2 when config.yaml switched meanwhile; overlap off -> single baseline call.
- [ ] **B4 Rollover with overlap.** `tests/test_fy_overlap_rollover.py` (reuse fixtures of `test_a6_rollover_journal_flow.py`):
  finals = configured FY only; all OK -> switch, approvals of FY26-27 superseded, record `ended_at`, next night one
  pass; LAN final fails -> blocked, both FYs still in `active_passes`; next morning success -> switch; G6 text with
  `overlap_active` (verdict identical); cloud-only (`lan.enabled false`) -> G6 needs cloud only; approval of G6 -> switch
  (baseline); `unprotected_new_fy` silent while active; activation runs before the alarm.
- [ ] **B5 Calendar simulation.** `tests/test_fy_overlap_calendar.py` (pattern of `test_calendar_simulation.py`,
  `pendulum.travel_to`): 2026-12-30 -> 2027-04-02 day by day with the default schedule: passes per night (1 until Dec 31
  06:00, then 2, back to 1 after the switch); Feb 29 2028 in the FY27-28 -> FY28-29 cycle; clock behind on Dec 31;
  server off Dec 31 (activation Jan 1); switch blocked 1-2 Apr then done 3 Apr; overdue of FY27-28 counted from
  activation, never red before its first due run.
- [ ] **B6 Integrity audit per FY.** `tests/test_fy_overlap_audit.py`: both FYs audited in one slot, suffixed
  audit ids, `latest_audit(leg, fy=...)`, alert key with `@FY27-28`, one NAS wake/shutdown, `fy=` parameter.
- [ ] **B7 Attention / overdue / watchdog.** `tests/test_fy_overlap_attention.py`: per-FY `stale:` / `first_run:` /
  `last_run:` items; next FY "waiting" from activation; watchdog mails `stale:lan:FY27-28` once; `overlap_off:` RED item.
- [ ] **B8 Reports.** `tests/test_fy_overlap_report.py`: sections per (leg, FY) with `row_fy`; first January week
  does not say "no backup ran" for FY27-28 before activation; the report covering 28 Mar - 3 Apr shows both FYs
  correctly labelled across the switch; legacy rows without `fy` fall into the leg's configured-FY section.
- [ ] **B9 Dashboard API + UI.** `tests/test_fy_overlap_ui.py` (FastAPI TestClient, fake `arun_deployment`):
  `/status` shape with/without overlap; triggers with `?fy=` (active / inactive / disabled leg / already running);
  approved re-run passes `fy`; `/api/rollover` overlap state; extend `tests/test_e8_dashboard_e2e.py` for 4 cards and
  the per-card buttons.
- [ ] **B10 Docs.** `docs/ARCHITECTURE.md` §3.1, §5.1, §5.2, §10.7 (new gates R9 activation, R10 overlap passes),
  `SESSION_HANDOFF.md`, `docs/EMAIL_CONTENT_SPEC.md` (new keys, AAM-ROL-07).
- [ ] **B11 Audit lab (targeted only).** `audit_lab/test_fy_overlap_lab.py`: real robocopy + rclone (`deploy\bin\rclone.exe`,
  test bucket prefix) with two FY folders: two LAN mirrors, two cloud prefixes, deleting a file in FY27-28 never
  touches FY26-27's copy, G3/G5 per FY. Clean `PREFECT_HOME`.
- [ ] **B12 Trial box.** Deploy by git bundle; set `overlap_enabled: true` and, for the test only,
  `overlap_activate_on` = today, with test prefixes; observe one night of 4 runs; put `12-31` back. Tell the owner
  before the restart (stops a backup slot).

**Part C - manual-only legs (only if the owner says yes, Q3)**

- [ ] **C1** `schedule.<leg>_scheduled`; `serve.py:35-55` registers the deployment with `schedules=[]` when false
  (buttons and approvals keep working); `_schedule_fits_the_budgets` skips it; `set_schedule_in_effect` records it.
  `tests/test_manual_only_leg.py`.
- [ ] **C2** Attention/report: no overdue for an unscheduled leg (any FY); AMBER `manual:<leg>[:<fy>]` when the last
  success is older than `dashboard.manual_reminder_days` or the next FY has none since activation; reports say
  "manual only" and never "no backup ran". Rollover unchanged (it runs the final itself; a failure is listed in the G6
  mail with "switch the NAS on, then press Run FY rollover now"). Tests in the same file.

## 8. Open questions for the owner (with recommended default)

- **Q1 Default of `overlap_enabled`.** Recommended: `false` in code (tests and an un-updated config behave as today),
  `true` in `config.example.yaml`, the install config and on the trial box. Alternative: `true` in code.
- **Q2 G6 approval while the overlap is active.** Today a person can approve the switch although a leg's final copy
  failed. With the overlap both years are protected while waiting, so the approval is only needed if a leg is broken
  for a long time. Recommended: keep it approvable (no endless double backup), with the new warning text (section 4).
- **Q3 Manual-only legs (Part C).** The baseline has no "enabled but not scheduled" leg. Recommended: build Part C
  only when a client needs it; Part B already handles disabled legs and legs that cannot run.
- **Q4 Weekly integrity audit of both FYs (B6).** Doubles the audit's work for three months inside the same
  `audit_max_hours`. Recommended: yes (owner asked for "everything per FY").
- **Q5 Activation needs a proven clock.** Recommended: yes (same rule as the switch); cost: a day's delay when the
  internet is down on 31 Dec, with a mail.

## 9. Baseline behaviours that must stay unchanged (and how)

| Behaviour | How the plan keeps it |
|---|---|
| rclone / robocopy commands, flags, retries, `--max-delete` backstop | engine modules (`cloud_sync`, `lan_sync`, `cloud_verify`, `cloud_reporter`, `tool_runner`) are not edited; the second pass calls them through the same tasks |
| Guards G1-G6 verdicts, approval rules | `core/guards.py` logic untouched (G6 gets a text-only keyword, test proves equal verdicts); baselines already per FY |
| Verify / evidence gate, terminal-status policy | same pipeline code per pass (`flow.py:1675-1699`, `:1784-1788`, `:2088-2092`) |
| WoL, NAS shutdown rules | `wol_check_task` / `lan_shutdown_task` unchanged; only *when* they are called across passes (D-8) |
| One RUNNING row before the slot, one outcome row per run, ABANDONED sweep | `_pipeline_slot` / `_record_run` / `start_run_row` per pass, unchanged |
| Slot + `backup.lock` serialisation | each pass takes them like any pipeline; passes are sequential |
| Database file and schema | no DDL, user_version 5, compatibility test (A4) |
| Overlap off (default) | `active_passes` returns one pass, pipelines get `fy_pass=None`, every `fy=None` query sees all existing rows |
| Rollover steps, compare-and-set config switch, archive of the old prefix only, D-17 repeat | `_rollover_resume` steps unchanged; only a post-switch cleanup hook and the activation call before it |
| Schedules, deployments, budget proof | `serve.py` untouched (except Part C); shared leg budget keeps `_schedule_fits_the_budgets` valid |
| Separate cloud prefixes, GCS lifecycle | each FY syncs only `gs://bucket/<fy>`; `deploy/gcs_lifecycle.json` unchanged (its 365-day ARCHIVE rule cannot reach overlap objects) |
| Alert classes, switches, cooldowns, recipients | all new keys use existing prefixes and kinds; incidents are grouped per FY only for the next-FY pass |
| Settings never change the FY | the overlap never writes `config.yaml`; only the April switch does (baseline) |

## 10. Risks (accepted unless the owner says otherwise)

- A NAS FY folder directly at a share root (`\\nas\FY26-27`) has no parent to create `FY27-28` in: the next-FY LAN pass
  fails loudly every night (the April rollover has the same limit today). Readiness/support fix: use `\\nas\share\FY..`.
- Files edited in FY26-27 after the April switch are not backed up (baseline, unchanged).
- A flow run that waits for the slot while the rollover switches can still run one pass with the old view (baseline has
  the same window for one pass; the race guard in 2.2 closes it between passes).
- Next-FY passes do not fill `file_entries`, so the "files on NAS / in the cloud" counters show the configured FY only;
  the next-FY cards show `total_files` from their last run instead.
