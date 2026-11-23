# Investigation: cloud-only schedule, LAN on demand (2026-11-23)

Read-only code investigation on branch `fy-overlap`. No tracked file edited, no tests run, no services touched, nothing committed.
Citations are `file:line` in this checkout. Each answer is marked **CONFIRMED-FROM-CODE** or **UNCLEAR**.

Owner use case: the client may disable the scheduled LAN backup. The cloud leg still runs nightly. When the client wants a LAN copy they press the dashboard button; the server must be woken (WoL) if needed, back up and verify, then be shut down again. The same wish may apply to the cloud leg.

---

## 0. Short answer

- **The use case cannot be expressed today.** There is one switch for the LAN leg, `lan.enabled`. It controls the nightly schedule, the manual button, the WoL/shutdown path, the weekly audit, the rollover final backup and the NAS folder steps together. Turning it off removes the manual button too (ui.py:835-836). Keeping it on makes the LAN leg an expected nightly run.
- **If the LAN leg is kept on for manual use, the code treats every missed night as a failure.** That gives a red banner, a red `/health` (503), an ACTION REQUIRED "overdue" mail, and "action needed: N of M nights have no backup record" in the weekly report. The rollover and the Sunday audit also wake the NAS on their own.
- **Cloud-only (`lan.enabled: false`) is mostly clean** for alerts and the banner. Two things still need work: the config still demands a UNC `lan_destination` and `wol.enabled: false`, and the report says nothing about the LAN leg being off.
- **The NAS is powered off after a successful manual run even if it was already on.** A failed manual run leaves a woken NAS on, and the mail says the next scheduled run will re-sync, which is false in this mode.

---

## 1. What `lan.enabled: false` does, and whether a manual LAN trigger can exist

**Q1a. Is the LAN deployment schedule paused?** CONFIRMED-FROM-CODE. It is not registered at all. `serve.py:46-55` adds the `backup-lan` deployment (with `lan_cron`) only when `config.lan.enabled`. On a boot that finds an old registration, `launch.py:271-330` (`_reconcile_disabled_legs`) pauses it (`launch.py:325-327`), and that reconcile runs at start (`launch.py:407`). The `integrity-audit` deployment is registered whenever LAN or cloud is on (`serve.py:93-101`, `launch.py:295-301`).

**Q1b. Does a manual LAN trigger still work?** CONFIRMED-FROM-CODE: **no**. Every path refuses:
- Dashboard: `ui.py:835-836` returns 409 "turned off" before any WoL.
- Flow: `flow.py:3501-3502` raises `ValueError` for `mode=lan` with LAN disabled.
- Approval re-run: `ui.py:907` refuses a disabled leg.
- Manual verify: `ui.py:1546-1547` (`mode=lan`) and `ui.py:1550` (`mode=all` with both off).
- The page hides the LAN button and says so (`templates/dashboard.html:69-71`, "manual runs are rejected by the server").

**Q1c. Any way today to have "no schedule but manual allowed"? CONFIRMED-FROM-CODE: no.** Options checked:
- `lan.enabled: true` with an impossible `schedule.lan_cron`: UNCLEAR. Either the cron validator / `schedule_problems` (`models/config.py:1173-1181`, `1239-1243`) rejects it, or `core/overdue.py:77-83` (`next_firing`) fails and `core/attention.py:188-225` turns it into a red "schedule cannot be read" item. Either way it is not a usable mode.
- A separate "manual only" flag does not exist. Nothing in `config.yaml` can express it.

**Minimal config change (proposal only, not implemented):**
- Key: `lan.scheduled` (bool), default `true` (today's behaviour, no migration), bounds: boolean.
- Companion key (optional): `lan.on_demand_warn_after_days`, int, default 30, bounds 1-365. It sets the age after which the dashboard shows an amber "last office copy N days ago" item. It never makes a red item.
- With `lan.scheduled: false`:
  - `serve.py` registers `backup-lan` without a `Cron` (or keeps it paused but resumable) so `arun_deployment` still works; `launch._reconcile_disabled_legs` must not pause it.
  - `core/attention.compute_attention` (`attention.py:262-263` loop) and `watchdog._check_stale_legs` skip the schedule-based overdue, `first_run` and `last_run` checks for LAN. `/health` (`ui.py:724-743`) follows, since it uses `compute_attention`.
  - `core/report._shortfall` (`report.py:328-365`) and `_scheduled_counts` are skipped for LAN.
  - `models/config.py:1239-1243` drops the LAN job from the schedule-budget check.
  - The manual trigger keeps its `lan.enabled` gate. Only the schedule changes.
- Owner decisions still needed (see §6): whether the April 1 rollover and the weekly audit may wake the NAS on their own when `lan.scheduled` is false.

---

## 2. LAN leg scheduled off, but `lan.enabled: true` (the only way to get a manual button)

Each row says what the code does with the missing nightly LAN runs.

| Area | What happens | Evidence | Status |
|---|---|---|---|
| Overdue / stale rule | Deadline = next firing after last success + LAN budget (4 h) + `stale_grace_hours` (2 h), about 26 h after a daily cron. After that the LAN card is RED `stale:<fy>:lan`. Before the first run it is AMBER `first_run`, and RED once overdue (clock starts at the later of first known run or the `lan.enabled` change). | `attention.py:123-158`, `188-225`, `core/overdue.py:77-83`, `models/config.py:1042-1046` | CONFIRMED-FROM-CODE |
| Dashboard banner and `/health` | RED item -> `/health` returns 503 with the reason. A monitoring system would alarm every day. | `ui.py:724-743`, `attention.py:238` | CONFIRMED-FROM-CODE |
| Watchdog mail | `_check_stale_legs` sends ACTION REQUIRED "LAN backup overdue" per (leg, FY), repeating every `alerts.stale_repeat_hours`. The body says "Scheduled backups are not completing" and tells the owner to check services. Both are wrong for on-demand use. | `watchdog.py:470-505` (message at 500) | CONFIRMED-FROM-CODE |
| Weekly / monthly report | `_shortfall` counts every scheduled firing with no run row inside `[firing, firing + budget + grace)`. A manual run at 15:00 matches no night. The subject and top line become "action needed: Office copy (NAS) - N of M scheduled nights have no backup record". Scheduled and dashboard reports pass `schedule`, so this shows in both. | `report.py:249-257`, `265-268`, `328-365`; `flow.py:2403-2404`; `ui.py:1635`, `1713` | CONFIRMED-FROM-CODE |
| Integrity audit (weekly) | `integrity_audit_flow(mode="all")` audits every enabled leg, so LAN is included. The Sunday run wakes the NAS and shuts it down again even when nobody asked for it. | `serve.py:93-101`, `flow.py:3107` | CONFIRMED-FROM-CODE |
| Rollover final backup (1 April) | `rollover_check_flow` runs daily 06:00. On the switch day `_run_final_backups` runs the LAN pipeline for the closing FY, which wakes the NAS, and G6 needs its result. A NAS that is off or unreachable gives LAN_BLOCKED / NOT_COMPLETE, so the switch is blocked until approval, or waits (see 2.1). | `flow.py:2817-2836`, `3027-3046`, `guards.py:272-301` | CONFIRMED-FROM-CODE for the path; UNCLEAR for the exact status a NAS-off final run gets (not traced to the end) |
| Switch step `nas_folder` | Wakes the NAS (if WoL on) and creates the new NAS FY folder. This is an automatic wake, not a client request. | `flow.py:2893-2899` | CONFIRMED-FROM-CODE |
| Switch step `nas_shutdown` | Shuts the NAS down after the rollover if configured. | `flow.py:2880-2886` | CONFIRMED-FROM-CODE |
| 1 January overlap activation | Source-side only (creates and checks the next FY folder). No LAN check. The early FY is backed up by cloud from day one. The NAS folder of the early FY is created only by a LAN pipeline run (`early_nas_folder`), so it waits for the first manual LAN run. | `fy_overlap.py:360-398` (no `lan` reference), `flow.py:2055-2068`, `fy_overlap.py:607-617` | CONFIRMED-FROM-CODE |
| Activation mail | "Since <date> the nightly backups copy both folders". With a manual-only LAN leg the word "nightly" is false for LAN. | `fy_overlap.py:352` | CONFIRMED-FROM-CODE |
| Overlap second folder | `active_folders` depends only on the activation row, the switch state and the calendar, never on LAN success. So the next FY stays in the active set. | `fy_overlap.py:154-168` | CONFIRMED-FROM-CODE |
| Switch blocked by LAN? | Yes, if `lan.enabled`: G6 fails when any enabled leg's final backup is not a success. A manual-only LAN leg with a NAS that is off on 1 April blocks the switch until approval. | `guards.py:290-300`, `flow.py:3033-3035` | CONFIRMED-FROM-CODE |
| Archive step | Cloud only, independent of LAN. | `flow.py:2869-2873` | CONFIRMED-FROM-CODE |
| G1-G6 baselines | Each guard compares with the last successful run of the same leg and FY. After weeks without a LAN run, the baseline is old. A legitimate source shrink or mass delete in that gap is blocked (LAN_BLOCKED + approval). This is the designed behaviour, but it is more likely in on-demand use. | `ARCHITECTURE.md` section 4 (G2-G4, baseline) | CONFIRMED-FROM-CODE (rule); UNCLEAR whether this is acceptable to the owner |
| G5 canary on a new FY NAS folder | Missing folder or marker is not traced to the end. The documented rule is "missing canary -> LAN_BLOCKED + approval". A missing folder is a different path. | `ARCHITECTURE.md` 10.4 LA5 | UNCLEAR |
| Manual run covers both FYs | `trigger_lan` passes no `fy`, so `backup(mode="lan")` runs `_active_fys` = both folders, one slot, one wake, one shutdown decision. | `ui.py:830-850`, `flow.py:3566`, `3369-3380` | CONFIRMED-FROM-CODE |
| "Check now" / backup-first prompt | Prompts "back up now first" when the last LAN success is older than `audit.backup_first_prompt_minutes`. In on-demand use this prompt is always true. | `ui.py:1507-1530` | CONFIRMED-FROM-CODE |

### 2.1 Rollover on 1 April with on-demand LAN

If the NAS is off at 06:00 on 1 April, the daily check wakes it for the final LAN backup. If the wake fails, G6 blocks and the switch waits for the owner. This is the one case where an on-demand LAN setting can block a scheduled FY change. Owner decision needed (see §6, D-1).

---

## 3. Cloud-only deployment (`lan.enabled: false`, no WoL, no NAS)

**Q3a. What changes.** CONFIRMED-FROM-CODE, with these effects:
- No LAN deployment, no LAN pipeline, no WoL, no shutdown (`serve.py:46`, `flow.py:2338`, `flow.py:1394-1396`).
- Overdue, banner, `/health`, watchdog: skip LAN (`attention.py:262-263`, `watchdog.py:470-505` via `compute_attention`, `ui.py:724-743`).
- Report legs: cloud only (`flow.py:2338`, `ui.py:1697-1699`). Integrity audit: cloud only (`flow.py:3107`).
- G6 ignores LAN (`flow.py:3033-3035`).
- Rollover: LAN steps skipped (`flow.py:2893-2895`, `2880-2886`).
- Overlap activation runs and backs up the next FY in cloud (`fy_overlap.py:360-398`).
- `ensure_overlap_nas_folder` returns "unreachable: the NAS leg is switched off" and is never called (`fy_overlap.py:615-616`).

**Q3b. What do mails and reports say about the LAN leg? CONFIRMED-FROM-CODE: silence, not "off".**
- The weekly/monthly report omits the LAN section entirely. The text "off" does not appear for LAN in `core/report.py`. `report.py:637` only says "These emails are switched off" for alert mails. A reader cannot tell "LAN is off by choice" from "LAN is missing".
- The dashboard card says "Disabled" (`static/js/dashboard.js:265-296`, `templates/dashboard.html:69-71`), but it still prints "Next scheduled backup: <cron>" for the disabled leg (`templates/dashboard.html:62`). That is misleading.
- `core/email_compose.py:117-124` (`_facts`) takes the NAS host from `paths.lan_destination` even when LAN is off, so the placeholder host can appear in NAS text. Which mails use `_facts` is UNCLEAR.

**Q3c. Config burden for cloud-only. CONFIRMED-FROM-CODE:**
- `paths.lan_destination` is still required and must be a UNC path (`models/config.py:79` `Field(...)`, validator `117-121`), even with LAN off.
- `wol.enabled` defaults to `true` and then requires `mac_address` and `server_ip` (`models/config.py:179-182`, `242-248`). The validator does not look at `lan.enabled`. A cloud-only config must set `wol.enabled: false` explicitly, or it fails to load.
- Whether the installer or Settings writes `wol.enabled: false` for cloud-only is UNCLEAR.
- The FY-name and share-root checks are already skipped when LAN is off (`models/config.py:1276-1277`, `1307`, `1314`, `1324`), so the rules are consistent with a placeholder.

---

## 4. Manual LAN run after long gaps (weeks)

**Q4a. Robocopy /MIR.** Each folder runs `robocopy /L` first, then guards G1-G5, then `/MIR` (`ARCHITECTURE.md` 3.3). A long gap means more deletes on the source are mirrored. G3 blocks when planned deletes exceed `guards.max_delete_files` or `max_delete_percent` of the destination (`ARCHITECTURE.md` 10.4). The run then waits for approval. The blocked run is designed behaviour, but it is more likely after a gap.

**Q4b. The manifest.** `file_entries` (`UNIQUE(fy, relative_path)`) is refreshed by `lan_record_task`. "Files/bytes copied" is the diff of two snapshots taken around this run only (`lan_snapshot_before_task` / `lan_snapshot_after_task` inside `_run_lan_pipeline`, `flow.py:1086-1103`, `1298-1321`). After a gap, the copied count is the real delta since the last run, not a per-night number.

**Q4c. The run record.** One `run_history` row per folder, `mode='lan'`, `fy` set, `run_id = <flow run>-lan-<fy>` (`flow.py:142-146`). `extended_metrics` holds guards, budget, sync attempts, `nas_shutdown` and artefacts (`finally` block of `_run_lan_pipeline`, `flow.py:2246-2285`). `GuardBlocked` gives `LAN_BLOCKED` with the guard record.

**Q4d. Shortfall rule after a manual run.** A manual run covers a night only when it starts in `[firing, firing + 6 h)` and matches one-to-one in time order (`report.py:328-365`). A manual run at 15:00 covers no night. So after a manual-only month, the report still says every night is missing, and the subject says "action needed". This is a false alarm.

**Q4e. Does a manual run cover both FYs during an overlap? CONFIRMED-FROM-CODE: yes** (`flow.py:3566`, `_run_leg_folders` `flow.py:3382-3455`). Both folders are backed up under one slot, one wake and one shutdown decision.

---

## 5. WoL and shutdown for a manual run

**Q5a. Who shuts the server down.** CONFIRMED-FROM-CODE: the LAN pipeline does, after `LAN_COMPLETE` (`flow.py:2166-2169`), only if `lan.shutdown_after_backup` and `wol.enabled` (`flow.py:1394-1396`). For several folders, `_lan_shutdown_decision` shuts down once, and only when every folder voted yes (`flow.py:3369-3380`). After `LAN_PARTIAL` or failure it shuts down only when `shutdown_on_all_retries_exhausted` is true (default false; `models/config.py:138-141`, `flow.py:2172`, `2222-2226`).

**Q5b. Was the server already on? CONFIRMED-FROM-CODE: not respected.** `ensure_server_online` returns True when the SMB probe is already ready and sends no magic packet (`core/wol.py:131` onward). Nothing records "was already on". After `LAN_COMPLETE` the NAS is shut down anyway (`flow.py:2166-2169`). If staff were using the NAS, that is a surprise.

**Q5c. Click during the nightly cloud leg. CONFIRMED-FROM-CODE: refused, not queued.** `_busy_detail` (`ui.py:761-780`) checks cloud, then LAN, then rollover, then the safety check. A running cloud backup returns 400 "Wait for the running backup to finish". The race window is only the gap between the check and the Prefect run creation. If it is lost, the LAN run waits for the slot with no WoL until the slot is free (`flow.py:201-236`, `_run_leg_folders` `3382-3455`). A slot wait longer than `maintenance.concurrency_wait_seconds` (3600 s) gives `LAN_SLOT_TIMEOUT` and an alert (`flow.py:3313-3330`). A long manual LAN run started just before the 02:00 cloud firing makes the cloud run time out the same way. Banner colour for that status: UNCLEAR (depends on membership of `MISSED_STATUSES`, not checked).

**Q5d. What if shutdown fails. CONFIRMED-FROM-CODE:** `lan_shutdown_task` never raises. It records `FAILED` / `NOT_VERIFIED` in `extended_metrics.nas_shutdown` and sends a WARNING mail keyed `nas_shutdown:<ip>` (global, not per FY). The dashboard shows an amber `nas_shutdown` item (`flow.py:1386-1417`, `attention.py:322-324`). The backup status is unchanged.

**Q5e. What if the WoL wake itself fails? CONFIRMED-FROM-CODE:** `wait_for_server` raises `WolTimeout` (`core/wol.py:121`, code AAM-NAS-02). The run fails, `_vote(False)` is recorded, and the NAS **stays on**. The failure mail says "The NAS was NOT shut down; the next scheduled run will re-sync" (`flow.py:2222-2226`, `flow.py:371-382`). In on-demand use there is no next scheduled run, so that text is false. No attention item says "NAS left on" (only `faults.py:112` text).

**Q5f. WoL off, LAN on.** Shutdown is skipped (`flow.py:1394-1396`), so the NAS is never powered off by the backup. No alarm.

---

## 6. Owner decisions needed before building

- **D-1.** On 1 April and on the Sunday audit, should a LAN leg that is manual-only wake the NAS on its own? Proposal: no. The rollover final LAN backup becomes an approval step or a warning when the NAS is unreachable, not a G6 block.
- **D-2.** After a manual LAN run, should the NAS be shut down if it was already on before the click? Proposal: leave it on when it was already on. This needs a "was on" flag from `ensure_server_online`.
- **D-3.** For a failed manual run that woke the NAS, should the owner be asked to shut it down, or should the server shut it down after a timeout? Proposal: an ACTION REQUIRED item "NAS left on" with a dashboard "shut down now" button.
- **D-4.** Name and bounds of `lan.scheduled` and `lan.on_demand_warn_after_days` (see §1).

---

## 7. Every place that raises a false alarm or hides a real problem

**False alarms (manual-only LAN, `lan.enabled: true`, no nightly run):**
1. Banner red `stale:<fy>:lan` (`attention.py:188-225`).
2. `/health` 503 (`ui.py:724-743`).
3. Watchdog ACTION REQUIRED "LAN backup overdue" mail, repeating (`watchdog.py:470-505`).
4. Weekly / monthly report "action needed: ... N of M nights have no backup record" (`report.py:249-268`, `328-365`).
5. Report headline "no backup ran" when LAN has no rows (`report.py:265-266`).
6. Text "Scheduled backups are not completing" and "the next scheduled run will re-sync" (`watchdog.py:500`, `flow.py:378-382`, `2222-2226`).
7. Activation mail "the nightly backups copy both folders" (`fy_overlap.py:352`).
8. Backup-first prompt always true (`ui.py:1507-1530`).
9. Switch blocked by G6 if the NAS is off on 1 April (`flow.py:2823`, `guards.py:290-300`).
10. Dashboard "Next scheduled backup: <cron>" shown for a disabled LAN leg (`templates/dashboard.html:62`).
11. `*_SLOT_TIMEOUT` mail for the nightly cloud run when a manual LAN run is long (§5c).

**Hidden real problems:**
1. NAS powered off after a successful manual run even if it was already on (`flow.py:2166-2169`, `core/wol.py:131-161`).
2. NAS left on after a failed or interrupted manual run, with no attention item (`flow.py:2222-2226`, `3369-3380`).
3. WoL off with LAN on: the NAS is never shut down and no alarm is raised (`flow.py:1394-1396`).
4. Rollover journal marks `nas_folder` SKIPPED when LAN is off; when LAN is switched back on, the new FY's NAS folder is never created by the rollover, because `_complete_pending_rollover_nas_folder` treats a skipped step as finished (`core/rollover_journal.py:30`, `flow.py:1965-1970`, `2893-2895`). The first LAN run then depends on G5/preflight for a missing folder (UNCLEAR outcome).
5. Cloud-only "LAN off" is silent in reports: no "off" line, so a missing LAN section looks like nothing was scheduled (`report.py`, `flow.py:2338`).
6. `_facts` in `core/email_compose.py:117-124` shows the placeholder NAS host when LAN is off (which mails: UNCLEAR).

---

## 8. Proposed test cards (for the owner's approval, not written)

| # | Name | Setup | Expected truth |
|---|---|---|---|
| T1 | cloud-only-clean | `lan.enabled: false`, `wol.enabled: false`, placeholder UNC, 7 nights cloud OK | No LAN card red; `/health` 200; no watchdog mail; report has no LAN section and says LAN is OFF |
| T2 | cloud-only-config-load | `lan.enabled: false`, no `wol` section | Loads, or fails with a clear message naming `wol.enabled` (today it fails at 242-248) |
| T3 | manual-lan-refused-when-off | `lan.enabled: false`, POST `/trigger/lan` | 409 "turned off"; no WoL packet; no Prefect run |
| T4 | manual-only-stale | `lan.enabled: true`, `lan.scheduled: false` (after the change), last LAN success 10 days ago | Amber "last office copy 10 days ago" at most; no red; `/health` 200; no watchdog mail |
| T5 | manual-run-covers-both-fy | Overlap window, LAN enabled, POST `/trigger/lan` | Two `run_history` rows (new FY, configured FY), one WoL, one shutdown decision |
| T6 | manual-run-already-on | NAS answers 445 before the click, run succeeds | Decide per D-2: NAS stays on (proposed) or shut down (today) |
| T7 | manual-run-wol-timeout | NAS does not wake within `wake_timeout_seconds` | `LAN_FAILED` with AAM-NAS-02; alert text does NOT say "next scheduled run" (today it does); NAS-left-on item appears (today none) |
| T8 | click-during-cloud | Cloud backup running, POST `/trigger/lan` | 400 "Wait for the running backup"; no WoL |
| T9 | long-manual-vs-cloud-firing | Manual LAN starts at 01:50 and runs 80 min; cloud fires 02:00 | Cloud waits; if >3600 s, `CLOUD_SLOT_TIMEOUT` mail (confirm banner colour) |
| T10 | shortfall-after-manual | `lan.scheduled: false`, no LAN run for 3 weeks, one manual run at 15:00 | Report shows no "action needed" for LAN (today it does) |
| T11 | rollover-nas-off-on-april-1 | `lan.enabled: true`, NAS off, 1 April | Decide per D-1: no automatic wake, or an approval card (today: G6 block) |
| T12 | rollover-cloud-only-then-lan | Rollover with LAN off, then LAN re-enabled | New FY NAS folder exists or G5 asks; not a silent `LAN_FAILED` (today UNCLEAR) |
| T13 | audit-no-wake-when-off | `lan.enabled: false`, Sunday audit | Cloud audit only; no WoL packet |
| T14 | nas-shutdown-failed | Shutdown command returns 5 (access denied) | WARNING mail `nas_shutdown:<ip>`; amber banner item; backup status unchanged |
| T15 | wol-off-lan-on | `wol.enabled: false`, NAS on | Shutdown skipped; no false alarm; decide whether a "NAS not switched off by the backup" note is wanted |

---

## 9. Ranked suspected defects

1. **No "scheduled off, manual on" mode.** `lan.enabled` couples schedule, manual button, WoL, audit and rollover. Blocks the owner's use case. (CONFIRMED-FROM-CODE, `ui.py:835`, `serve.py:46`, `models/config.py:133`.)
2. **Manual-only LAN is treated as a failing nightly leg** (banner, `/health`, watchdog mail, report shortfall). (CONFIRMED-FROM-CODE, §7 items 1-4.)
3. **NAS left on after a failed manual run, no attention item, and a false "next scheduled run will re-sync" text.** (CONFIRMED-FROM-CODE, `flow.py:2222-2226`, `371-382`.)
4. **NAS powered off after a manual run even if it was already on.** (CONFIRMED-FROM-CODE, `flow.py:2166-2169`, `core/wol.py:131-161`.)
5. **Automatic NAS wake on 1 April and on the Sunday audit for a LAN leg the client disabled.** (CONFIRMED-FROM-CODE, `flow.py:2817-2836`, `3107`.)
6. **G6 blocks the switch if the NAS is off on 1 April while LAN is "on demand".** (CONFIRMED-FROM-CODE, `guards.py:290-300`.)
7. **Shortfall rule ignores manual runs outside the 6 h window after each firing.** (CONFIRMED-FROM-CODE, `report.py:328-365`.)
8. **Rollover `nas_folder` SKIPPED while LAN is off is treated as finished; the new FY's NAS folder is not created when LAN is re-enabled.** (CONFIRMED-FROM-CODE for the journal logic; outcome UNCLEAR, `rollover_journal.py:30`, `flow.py:1965-1970`.)
9. **Cloud-only config needs `wol.enabled: false` and a UNC placeholder.** (CONFIRMED-FROM-CODE, `models/config.py:79`, `117-121`, `179-182`, `242-248`.)
10. **Reports say nothing about LAN being off** (no "off" line; section missing). (CONFIRMED-FROM-CODE, `report.py`, `flow.py:2338`.)
11. **Dashboard prints "Next scheduled backup" for a disabled LAN leg.** (CONFIRMED-FROM-CODE, `templates/dashboard.html:62`.)
12. **Activation mail says "nightly" for both folders, including LAN.** (CONFIRMED-FROM-CODE, `fy_overlap.py:352`.)
13. **`_facts` shows the placeholder NAS host when LAN is off.** (CONFIRMED-FROM-CODE for the code; which mails: UNCLEAR, `core/email_compose.py:117-124`.)
14. **WoL off with LAN on: NAS never shut down, no alarm.** (CONFIRMED-FROM-CODE, `flow.py:1394-1396`.)
15. **Backup-first prompt always true in on-demand use.** (CONFIRMED-FROM-CODE, `ui.py:1507-1530`.)
16. **`/status` still computes LAN `timing` for a disabled leg** (cosmetic; the JS ignores it). (CONFIRMED-FROM-CODE, `ui.py` `/status` builds `"timing": _leg_timing(db, cfg, "lan")` without an `enabled` check.)

**Correct behaviour (no change needed):**
- Banner, `/health`, watchdog and report skip LAN when `lan.enabled` is false (`attention.py:262-263`, `flow.py:2338`).
- G6 ignores a disabled leg (`flow.py:3033-3035`).
- Refused manual runs during any backup, rollover or safety check (`ui.py:761-780`).
- A manual run covers both FYs (`flow.py:3566`).
- A failed shutdown is always alerted (`flow.py:1386-1417`).
