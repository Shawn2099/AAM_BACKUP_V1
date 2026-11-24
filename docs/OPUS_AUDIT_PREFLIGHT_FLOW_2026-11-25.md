# Opus audit: pre-flight checks, night flow, truthful reporting (baseline-v1-overlap)

Scope: committed code at `C:\Users\malliath\Desktop\aam_baseline_overlap` HEAD 10a4e29. Product code is identical
to dc50265, the commit running on the box (`git diff dc50265 HEAD` outside docs/tools/tests changes only CLAUDE.md).
I read flow.py, core/guards, health, cloud_preflight, lan_preflight, wol, smb_ready, notify, email_compose,
alert_class, approvals, audit_pending, integrity, fy_overlap, rclone_errors, statuses, faults, manifest (parts),
serve.py, watchdog.py (stale check) and models/config.py defaults. Line numbers refer to `git show HEAD:<file>`.
This was a read-only audit. Nothing was edited, and the box was not touched.

Lens: reliable backups and true reporting, with as little machinery as that needs.

---

## A) Flow and order as it is today

Schedules come from serve.py:37-105. The box runs LAN at 21:01, cloud at 02:01, the rollover check at 06:00 daily,
the audit on Sunday at 08:00, the weekly report on Monday at 08:00 and the monthly report on the 1st at 08:30 (IST).
The model defaults are different (config.py:1129-1140). Each leg is its own deployment (`mode=cloud` / `mode=lan`).

### A1. Cloud leg (`backup(mode=cloud)`, flow.py:3291-3409, then `_run_cloud_pipeline` flow.py:1674-2013)
1. Load the config and clean up orphaned robocopy logs (3321, 3370).
2. Work out the FY passes: the configured FY, plus the next FY while its overlap record is open (3343, fy_overlap.py:127-147).
   **Every step below repeats once per pass.**
3. Check the run budget. A late start with no budget left records MISSED plus one WARNING (571-599).
4. Write the RUNNING row. This also sweeps ABANDONED rows (522-539). Then take the Prefect slot `aam-backup` (wait up to 3600 s) and `backup.lock` (623-652, 194-266).
5. `health_check_task` (1748, health.py:192-266): the source exists and can be listed, source free space >= 1 GB, rclone binary present, GCS key file present, clock check (a Google HEAD request, then a w32tm fallback; UNVERIFIED only warns by default).
6. `cloud_preflight_task` (1749, cloud_preflight.py:33-146). Probe A repeats step 5's source check. Probe B is `rclone lsjson --max-depth 0` on the bucket prefix. A failure gets 1 Prefect retry after 30 s.
7. `cloud_guards_task` (1750, flow.py:730-818): `first_regular_file`, then **`rclone size` of the source (a full source walk)**, then **`rclone size` of the bucket (a full listing)**, then the DB baseline. G1, G2 and G4 are computed, then **`rclone sync --dry-run` (a full walk of both sides)** feeds G3. `_apply_approval`, then `enforce` raises GuardBlocked if any guard blocks.
8. `cloud_access_task` (1752, 821-838): reads 1 byte of a source file, then creates and deletes a probe object in the bucket.
9. `_sync_with_retries` (1773, 841-911): rclone sync with `--max-delete` = what is left of the G3 allowance. Up to 3 attempts, and each retry re-runs step 7 and step 8 (1757-1764).
10. If rclone stopped at the budget or its time limit: PARTIAL plus an alert, and verify is skipped (1785-1811).
11. `cloud_verify_and_report_task` (1814, 993-1098) runs **`rclone check --one-way --size-only`**, then **`rclone size` of the bucket**, then **`rclone lsjson -R` of the bucket**, then **`rclone check --combined --size-only`**. The whole task gets 1 Prefect retry.
12. Evidence gate (1821-1845). Any failure is CLOUD_VERIFY_FAILED plus one alert.
13. Record file_entries (1861) and compute the cross-check `_cloud_diff_count` (1870-1885). Write the summary artifact.
14. In `finally` (1978-2013), write one run_history row. BLOCKED opens an approval request and sends an ACTION mail (2008-2011). On success, send RECOVERED if the previous run was a failure (2012-2013).
15. Back in `backup()`, purge run_history **only if every pass of this flow succeeded** (3462-3468).

Tree work per cloud pass: 5 full source walks (size, dry-run, sync, check, check) and 7 full bucket listings.
From 31 Dec to 1 Apr all of it happens twice.

### A2. LAN leg (`backup(mode=lan)`, flow.py:3411-3440, then `_run_lan_pipeline` flow.py:2060-2338)
1-4. Same as cloud steps 1-4. In overlap, only the last pass may shut the NAS down, and only if every earlier pass ended LAN_COMPLETE (3426-3430).
5. `health_check_task(lan)` (2115): the source checks, the robocopy binary, and SeBackupPrivilege when /ZB is used.
6. `wol_check_task` → `ensure_server_online` (2116, wol.py:133-171). This is a TCP 445 probe plus a stat. If the NAS is not ready, it sends the magic packet and waits up to 900 s plus 90 s. **This runs once per pass** (P6). When the NAS is already up, it only costs a probe.
7. Next-FY pass: make the NAS FY folder (2117-2119). Otherwise finish a pending rollover NAS-folder step (2120-2123, 2020-2057).
8. `lan_preflight_task` (2124, lan_preflight.py:70-189): the SMB 445 probe (8 x 15 s) and the canary check, then **`robocopy /L /MIR` (both sides)**. 1 Prefect retry.
9. `lan_snapshot_before_task` (2125): **a full walk of the destination** to get the G3 denominator.
10. `lan_guards_task` (2126, 1264-1339): G1 (first file), G2 (the /L total against the baseline), G4 (DNS of the host, local IPs, last destination), G3 (`*EXTRA File` lines against the destination count), G5 (canary).
11. `lan_access_task` (2131, 1342-1354): read a source byte, write a probe file at the destination, check NAS free space >= the /L copied bytes plus the minimum.
12. `_sync_with_retries`: robocopy /MIR, up to 2 attempts. Each retry re-runs steps 8-11 (2135-2145).
13. `lan_snapshot_after_task` (2173): **another full walk of the destination**. Then file_entries, the cross-check diff and the artifact (2174-2203).
14. Shutdown (2219-2252): only after LAN_COMPLETE with allow_shutdown, or after PARTIAL/FAILED when `shutdown_on_all_retries_exhausted` (default False, config.py:143-149). **BLOCKED and FAILED leave the NAS on.**
15. `finally`: the row, then block request plus ACTION mail, or RECOVERED (2304-2338).

Tree work per LAN pass: 2 source walks and 4 destination walks over SMB.

### A3. Rollover check, daily 06:00 (`rollover_check_flow`, flow.py:2513-2553, `_rollover_resume` 2617-2966)
1. Log retention (2539-2542).
2. `fy_overlap.activate_if_due` (2549): from 31 Dec, after a proven clock, it creates the next-FY source folder, writes the overlap record and sends an INFO mail (fy_overlap.py:253-311).
3. `_alarm_unprotected_new_fy` (2550, 2575-2589).
4. Rollover decision: NONE returns early. On 1 Apr: the hand-switch, re-entry, clock-behind and clock-proof checks (2690-2756). Then open or resume the journal. `reset_at_start` resets the steps again **every morning** (2760).
5. `step_final_backups` (2786-2819) **runs the full cloud and LAN pipelines again** (`_run_final_backups` 2988-3018). The LAN run uses `allow_shutdown=False` (3006). Then G6 is checked against the final status. A block opens a request plus a mail, and RolloverError ends the run.
6. Inside the slot (2937): source folder, NAS folder (wakes the NAS, 2825-2830), config switch, archive, NAS shutdown, then the INFO announcement.

### A4. Weekly audit, Sunday 08:00 (`integrity_audit_flow`, flow.py:3041-3248)
It takes the slot once (3204). For each leg it audits the next FY first, then the configured FY (3085-3089), using a hash `rclone check` (integrity.py:455-506).
It wakes the NAS once (3162-3171) and shuts it down after the last LAN pass, **only if `lan.shutdown_after_backup`** is on (3175).
Differences are sorted into PENDING and REAL with `audit_pending` (integrity.py:363-372). It sends one mail if anything did not verify (3237-3243).

### A5. Watchdog and reports
The watchdog checks the stale legs (watchdog.py:382-432) and sends `stale:<leg>[:FY]` ACTION mails. It skips the mail when a mail about the missed run was already sent (419). Reports are built from run_history (flow.py:2412-2466).

---

## B) Checks table

| # | Check (where) | Purpose | Verdict | Why |
|---|---|---|---|---|
| 1 | Config validation incl. schedule fits budgets (config.py:1205-1224) | stops bad config and overlapping jobs | KEEP, FIX | Cheap and real. It leaves out the rollover check because it "does not take the slot". It does take the slot: while blocked, the rollover re-runs full final backups at 06:00 every day (A3.5). Those collide with the 02:01 cloud budget (6 h, ends 08:01) and the Sunday 08:00 audit. |
| 2 | Disabled-leg guard (flow.py:3334-3337) | an explicit run of a disabled leg must fail | KEEP | Trivial and correct. |
| 3 | RUNNING row + abandoned sweep (522-539) | a dead run is visible | KEEP | Gives the truth cheaply. |
| 4 | Slot + backup.lock (194-266, 623-652) | no two mirrors at once; the watchdog can see a run | KEEP | Real failure mode. Records SLOT_TIMEOUT correctly. |
| 5 | Run budget / MISSED (571-599) | stops a late start from running into office hours | KEEP | One warning per leg via the stable key `missed:<leg>`. |
| 6 | Source exists/listable/free space (health.py:31-70) | drive unmounted, or disk full on the server | KEEP | Cheap. Free space on the source protects little (rclone/robocopy only read the source), but it is harmless. |
| 7 | Cloud preflight probe A (cloud_preflight.py:70-97) | same as #6 | REMOVE (MERGE into #6) | An exact duplicate of health.py:47-56. |
| 8 | rclone/robocopy binary + GCS key file (health.py:233-263) | install broken | KEEP | Cheap and gives a clear cause. |
| 9 | Clock check (health.py:157-189) | GCS JWT rejects a skewed clock | KEEP, FIX | Cheap. When Google is unreachable and w32tm also fails, this is the best "no internet" signal the product has, and it is thrown away (warn only). It should feed the classification of the preflight failure (P2). |
| 10 | Preflight probe B `lsjson --max-depth 0` (cloud_preflight.py:100-146) | key, bucket and network work before a long run | KEEP, FIX | Cheap. Its error text is classified with AUTH before NETWORK (rclone_errors.py:29-36), which causes P2. The extra Prefect retry plus rclone `--retries` gives 2 levels of retry; one is enough. |
| 11 | G1 source has a file (guards.py:73-85, 381-413) | an empty or unmounted source would wipe the copy | KEEP | Stops at the first file, so it is cheap. It is also re-checked inside the builders (`require_source_files`), which is fine as a last line of defence. |
| 12 | G2 source shrink (guards.py:90-113) | wrong or half-mounted source | REMOVE on cloud, SIMPLIFY on LAN | For a mirror, any shrink that matters shows up as planned deletions, and G3 (10%) blocks before G2 (20%). On cloud it costs a **full `rclone size` source walk** (flow.py:757-758) for no extra protection. On LAN the count comes free from /L, so keeping it there costs nothing, but it is still redundant. |
| 13 | G3 mass delete via dry-run (guards.py:118-150, 325-336) | a mistake must not delete the backup | KEEP, FIX | This is the core protection. Faults: (a) allowance = `min(500, floor(dest * 10%))`, so a destination of fewer than 10 files may delete nothing at all (P3), and the new-FY folder trips it in its first weeks; (b) a rename counts as delete plus copy (R1); (c) a block stops **all copying too**, so new work is not protected until a person approves (the big reliability gap; see F1). |
| 14 | Cloud `--max-delete` backstop (flow.py:805-812) | a change between the dry-run and the sync | KEEP | Same allowance function as G3 (B1). Correct. |
| 15 | G4 destination identity + not this machine (guards.py:167-196) | config pointed at the wrong share or at loopback (a real incident) | KEEP, FIX | Cheap. A host name that cannot be resolved becomes BLOCK (176-179), which a person cannot sensibly approve. "Could not measure" should be FAIL and retry next night. |
| 16 | G5 mount canary (guards.py:201-224, lan_preflight.py:126-144) | NAS share not mounted, or a different share | KEEP | Cheap and real. An approval recreates the marker. |
| 17 | Unmeasured metric → BLOCK (G2 None, G3 None, G5 None) | fail closed | FIX | An approval can never cover it: `evaluate_approval` returns "cannot compare" when a metric is None (approvals.py:136-137). The run is then blocked until the next night measures it, yet the office gets an ACTION mail it cannot act on. Make it FAIL, which already fails closed and retries itself. |
| 18 | Cloud write probe (flow.py:821-838) | prove write rights | REMOVE (default off) | Redundant: on a 403, rclone sync deletes nothing ("not deleting files as there were IO errors", guards.py:423-425). It adds an API path and a nightly object in the bucket. It already has a switch (`cloud.write_probe_enabled`). |
| 19 | Source read probe, 1 byte (flow.py:826, 1350) | the service account cannot read files | KEEP | Cheap and gives a clear cause. |
| 20 | LAN write probe + NAS free space (1342-1354) | rights; a full NAS fails after hours | KEEP | Cheap. The free-space check saves a futile 4 h run. |
| 21 | WoL + smb_ready (wol.py:133-171, smb_ready.py) | NAS asleep or still booting | KEEP, SIMPLIFY | Correct readiness logic (TCP, then share root, then stability). It runs once per pass (P6), cheap when the NAS is up. It duplicates the lan_preflight SMB probe (lan_preflight.py:111-124) when WoL is on. Keep one call per night. |
| 22 | robocopy /L dry-run (lan_preflight.py:146-189) | G3/G2 counts, bytes for the free-space check | KEEP | robocopy has no `--max-delete`, so the /L listing is the only delete guard on LAN. |
| 23 | Pre-sync destination walk (flow.py:1155-1170) | G3 denominator | KEEP | Needed for the percent limit. |
| 24 | Post-sync destination walk + file_entries + cross-check (2173-2212, 1419-1441) | metrics cross-check | REMOVE | `metrics_consistent`/`diff_copied` and file_entries are read by **nothing** outside flow.py (git grep: no consumer in ui/report/js). It is a full SMB walk per pass, just for a log line. |
| 25 | Cloud post-sync: check one-way + size + lsjson -R + check combined (993-1098) | prove the upload | MERGE | Both checks are `--size-only` source↔bucket comparisons (cloud_verify.py:148-152, cloud_reporter.py:185-188). One `check --combined` gives verified/missing/changed. `lsjson -R` already gives count and bytes, which makes `rclone size` redundant, and lsjson exists mainly for the cross-check in #24. Keep **one check plus one size**. That saves 2 source walks and 2 bucket listings per pass. |
| 26 | Evidence gate (1821-1845) | never COMPLETE without proof | KEEP | Correct fail-closed decision point. |
| 27 | `_sync_with_retries` (841-911) | bounded repeat with cumulative G3 | KEEP, SIMPLIFY | Correct. On cloud, though, rclone `--retries` already repeats inside one attempt, and each flow attempt re-runs the full guard walk set. `cloud.max_attempts` 3 → 1 or 2. |
| 28 | Prefect retry of verify task (1707-1710) | transient listing errors | SIMPLIFY | It reruns 4 walks. rclone `--retries` inside the check is enough. |
| 29 | Overlap race guard `_switched_fy` (3274-3288) | the config switched mid-run | KEEP | Cheap re-read. Fails closed. |
| 30 | G6 rollover gate (guards.py:229-274, flow.py:2786-2813) | do not close the old year without its final copy | KEEP, FIX | Right rule, wrong mechanics: it re-runs full final backups every morning while blocked (2760 + 2791). That means daily daytime backups, the NAS left on (3006), new block requests and mails. It should count the verified nightly copies made after the closing moment (`final_copy_state` already can, fy_overlap.py:467-503) and re-run only a leg that has none and was not guard-blocked. |
| 31 | Rollover clock proof (2735-2756), clock-behind (2717-2729), hand-switch (2690-2704), re-entry (2706-2715) | the one-way switch must not act on a wrong date | KEEP | Cheap. These protect the irreversible step. |
| 32 | Overlap activation clock proof (fy_overlap.py:273-284) | do not add a year on a wrong clock | KEEP, FIX | Fine in itself, but its mail shows P1 and P8. |
| 33 | Audit pending classifier (audit_pending.py) | do not call "newer than the last backup" a failure | KEEP, FIX | Good idea. With no reference (a new FY that has never been backed up, or a reference purged by retention) every difference is REAL (151), which causes F-E1. |
| 34 | Audit LAN readiness (flow.py:3162-3171) | NAS reachable before the check | FIX | Only `wol_check_task` runs here, which is a no-op when WoL is off. There is no smb_ready, unlike the backup's preflight. A share that is not ready can then produce a verdict (see D4). |
| 35 | Watchdog stale check (watchdog.py:382-432) | runs that never start | KEEP | The only alarm for a silent scheduler. It already de-duplicates against the run's own mail. |

---

## C) WRONG now (ranked)

1. **R1/C1: a G3 block stops all backup of new work, and nothing ever clears it on its own.** G3 blocks the whole mirror (flow.py:813-818, 1322-1327), so new and changed files are not copied either. With a rename (316 files), the plan repeats the same deletes every night, so the block is permanent until a person approves each leg and each FY pass (`_approval_of_pass` 485-499, one approval per leg/FY). Lane A was blocked 2091-04-04 → end (1022 cloud plus 1020 LAN rows). This is the biggest reliability defect: one tidy-up by an accountant means no new backup on either leg. Confirmed.
2. **Rollover blocked → full backups every morning, NAS on, slot collisions (P5, "220 retries").** `reset_at_start` (2760) plus `step_final_backups` (2786) re-run both pipelines daily at 06:00, the LAN one with `allow_shutdown=False` (3006). Nightly blocked LAN runs never shut down either (2274-2296; `shutdown_on_all_retries_exhausted` defaults to False). So the NAS stays on around the clock while any LAN block lasts, not only for ~15 h. The 06:00 cloud final run competes with the 02:01 cloud run (budget to 08:01, slot wait 1 h → SLOT_TIMEOUT → G6 block) and with the Sunday 08:00 audit. The schedule validator does not see any of this (config.py:1205-1224). Confirmed in code; the collision is inferred from the defaults.
3. **P3: G3 on small trees blocks any single deletion.** `max_delete_allowance` = `min(500, floor(dest*10/100))` (guards.py:335-336) is 0 for fewer than 10 files and 3 for 30 files. The new-FY folder from 31 Dec, and a just-switched year in April, are exactly that small. Confirmed.
4. **P2: no internet is classified as a key/login problem.** AUTH (`oauth2: cannot fetch token`) is tested before NETWORK (rclone_errors.py:29-36). rclone wraps every token failure in that text, so `...cannot fetch token: Post ...: dial tcp ...: i/o timeout / connection refused` becomes AUTH. DNS failures are fine, because DNS is tested first and its pattern `dial tcp: lookup` matches. A firewall block, a WAN-down state with DNS still answering, or a timeout is misclassified. Confirmed in code; the exact text on the box is still unverified.
5. **P1: daily reminders and the escalation are dropped by the 24 h cooldown.** `_within_hours` uses a strict `<` on the real elapsed time (notify.py:702-709). A daily 06:00 job that starts a few ms earlier than the day before is inside 24 h and is SUPPRESSED (stale_repeat_hours=24 for `rollover:activation`, `rollover:unprotected`; fy_overlap.py:324, flow.py:2587). The kind change WARNING → ACTION_REQUIRED (fy_overlap.py:316) does not start a new incident, because "unchanged" compares only the fault_code (notify.py:645-648). So the escalation mail is lost twice over. Confirmed.
6. **P8: a WARNING is mailed as "ACTION NEEDED".** The subject severity comes from the fault code, not from the event kind (email_compose.py:232-235). AAM-ROL-07 is ACTION (faults.py:267), so the day-1 WARNING says ACTION NEEDED. Confirmed.
7. **R2/P7: duplicate mails per FY with identical subjects.** With a fault code present the subject is `severity: label - fault.title (code)` (email_compose.py:235). `event.title`, which carries the FY (`_leg_title`, flow.py:502-503), is ignored, so the two passes produce two identical subjects. Each pass also fails on its own for one environment cause (no internet, NAS down): pass 2 repeats pass 1's preflight about 31 s later (preflight retry 30 s). Confirmed.
8. **F-E1: a new FY audited before its first backup shows VERIFICATION_FAILED.** `_pending_context(fy=new)` has no last success, so reference=None and everything is REAL (flow.py:1585-1591, audit_pending.py:151). Activation at 06:00 on 31 Dec plus a Sunday 08:00 audit means a false ACTION mail and "action needed" in the 1 Jan reports. Confirmed.
9. **Retention can delete a leg's last success row.** `purge_old_runs` deletes `started_at < cutoff` without exception (manifest.py:1550-1553). If a leg has not succeeded for longer than 90 days (the default, config.py:627-634; R1 makes this realistic), its G2/G4 baseline (`guard_baseline`, manifest.py:858-880), its RECOVERED reference and its audit pending reference all disappear. G2 then passes as a "first run" (guards.py:97-99). Purge runs only after a night where every pass of that flow succeeded (flow.py:3462-3468), so a separate successful cloud deployment does purge while LAN is blocked. R4 itself is not settled. With both legs blocked from 2091-04-04 and no switch in 2092, the code should not have purged rows from 2091-01..03. To settle it, list the lane A DB's clean nights after 2091-04-04 (cloud-only or LAN-only flow runs with all passes OK) and the harness's lost-row method.
10. **P4 (design, low risk): the switch completes while the new FY is blocked, and the INFO mail says "Nothing to do."** G6 looks only at the old FY (fy_overlap.py:467-503). Data-wise this is acceptable: the new FY stays blocked and mailed either way. But the announcement (flow.py:2857-2873) states something false. Fix the wording, not the gate.
11. **Audit LAN has no SMB readiness when WoL is off (flow.py:3162-3171).** The box precedent was a boot-time audit with the share not ready and DNS failing, which ended VERIFICATION_FAILED with 703 mismatches. I could not prove from code which branch produced that, since transport errors should give AUDIT_INCOMPLETE (integrity.py:303-308). To settle it, read that run's rclone check log on the box. Either way, the audit should use the same smb_ready wait as the backup.
12. **R5: FY identity is carried by the run_id suffix.** `_fy_clause` (manifest.py:197-206) treats unsuffixed ids as the configured FY. After the switch, the previous unsuffixed rows belong to the old FY but count as "previous" for the new configured FY (RECOVERED, `previous_run_status` 834-842). That needs the `row_fy_or_default` heuristics (fy_overlap.py:573-585). Rows that fail before the guards carry no FY at all once the overlap ends (`_pass_metrics` returns {} when fy_pass is None, flow.py:457-467).

Not wrong: P6 (the wake per pass costs one TCP probe plus a stat when the NAS is up; wol.py:147-154). The audit shutdown gated on `shutdown_after_backup` (flow.py:3175) is consistent with lan_shutdown_task (1452). The docstring's "shuts it down after the last pass" means "when shutdown is configured". Keep the code.

---

## D) Truth-in-reporting gaps

1. **BLOCKED hides that nothing new is protected.** The ACTION mail and card say "Nothing was copied or deleted" (flow.py:1247-1250). The mail never says "new and changed files since <date> are NOT backed up" or how many days it has been. Add both, using the last success time per leg/FY.
2. **Subject severity follows the fault code instead of the event kind (P8)**, and **the subject drops the FY (R2)** (email_compose.py:232-235).
3. **Escalation can never be mailed** (P1): the kind change is not a new incident (notify.py:645-648), and the 24 h cooldown is jitter-sensitive (702-709).
4. **No-internet reads as "renew the key"** (P2), so the office is sent to the wrong fix.
5. **New-FY audit false red** (F-E1) also flows into the weekly report on 1 Jan.
6. **Announcement mail after a switch with a blocked new FY says "Nothing to do"** (flow.py:2864-2865).
7. **The "unmeasurable" BLOCK asks for an approval that cannot work** (approvals.py:136-137), so the mail asks for an action that is impossible.
8. **Mail volume**: every blocked leg/FY mails daily, because the cooldown is 12 h (config.py:558-563) against a 24 h schedule. On rollover mornings the 06:00 final runs add a block request and an ACTION mail of their own. Lane A had 1906 ACTION mails out of 2034, which teaches the office to ignore them.
9. **render_alert runs before the suppress decision** (notify.py:661 vs 669). A suppressed repeat still builds the support bundle and log zip. If that build fails, the fallback path uses its own in-process cooldown (511-553), so a suppressed alert can turn into a sent short notice. Low risk, but the order is wrong.
10. Correct and worth keeping: one alert per failed run (`alert_sent`), the record-then-alert order on cancel, the evidence gate, never-COMPLETED-on-failure (`PartialRun`), MISSED as a single warning, and RECOVERED scoped per FY.

---

## E) Remove / simplify list (and the risk of each)

| What | Saves | Risk of removing |
|---|---|---|
| Cloud preflight probe A (cloud_preflight.py:70-97) | code only | none; it is an exact duplicate of health.py:47-56 |
| G2 on cloud (`rclone size` source, flow.py:757-758, 772-773) | 1 full source walk per cloud pass | a shrink that plans no deletes is harmless to a mirror; G3 (10%) trips before G2 (20%). Keep G2 on LAN where the count is free, or drop it everywhere for one less concept |
| Cloud write probe (flow.py:821-838) | 1 API create/delete per pass, a failure mode | rclone deletes nothing on IO errors; a 403 still fails the run with a clear cause |
| Post-sync LAN walk + file_entries + both cross-checks (flow.py:1712-1722, 1861-1885, 2173-2212; `_cloud_diff_count` 292-322) | 1 SMB walk per LAN pass, the cloud `lsjson -R`, about 150 lines | no consumer reads them (git grep); the tool's own counts stay the record |
| Cloud verify: one `check --combined --size-only` + one `rclone size` instead of check+size+lsjson+check | 2 source walks + 2 bucket listings per pass | none: the same comparison and the same gate inputs |
| Prefect retry on verify (1707-1710); cloud `max_attempts` 3 → 1-2 | repeated walk sets | rclone `--retries` and the next night cover transients |
| Duplicate SMB probe (lan_preflight.py:111-124 when WoL is on) | up to 2 min of probing on a dead NAS | none if smb_ready ran once just before |
| Rollover: re-running final backups every morning (2760, 2786-2819) | daily daytime full backups, NAS uptime, mails | none if the gate counts the verified nightly runs after the closing moment and re-runs only a leg with no copy that is not guard-blocked |
| Per-pass mails for one environment cause (no internet, NAS down) | half the mails during overlap | none if pass 2 is skipped with the same status and the mail lists both FYs |

Do NOT remove: G1, G3 plus the backstop, G4, G5, the slot and lock, the evidence gate, the robocopy /L listing, the clock proof
before any one-way FY step, the watchdog stale check, the run budget and MISSED.

---

## F) Minimal reliable core, then the improvement order

### F.1 The smallest set that gives reliable backups and honest reporting
Per leg, per night (run all FY passes inside one slot hold; environment checks once per night, data checks per FY):
1. Config valid (incl. the schedule check, with the rollover's final-backup time counted).
2. Slot and lock, RUNNING row, budget/MISSED.
3. Environment, once per leg per night: source listable; binary; (cloud) key file plus clock plus `lsjson --max-depth 0`, classified NETWORK/DNS/TLS/AUTH correctly; (LAN) wake once, then smb_ready once. If this fails, **every FY pass gets the same FAILED status and one mail lists the FYs**.
4. Data guards, per FY pass, each measured once: G1 first file; dry-run (rclone `--dry-run` / robocopy `/L`) plus the destination count gives G3; G4; (LAN) G5 canary; (LAN) NAS free space. A metric that cannot be measured means FAILED, retried next night, never BLOCKED.
5. **If only G3 blocks: run a copy-only pass** (rclone `copy` / robocopy `/E` without `/PURGE`, the same filters). New and changed files are protected; the deletions wait for approval. The status stays `*_BLOCKED` with "copied N, deletions held" in the row and the mail. G1/G4/G5 blocks still stop everything (wrong source or destination).
6. Sync with the backstop. Success needs the tool verdict plus (cloud) one `check --combined` plus a size.
7. One row, one mail per leg per night (FY in the subject), then RECOVERED when it clears. The NAS is shut down after the last pass whenever this run woke it, unless the run was interrupted (owner question G1).
8. Weekly audit with smb_ready. An FY with no successful backup of that leg gets "not backed up yet" (no mail), never VERIFICATION_FAILED.
9. The rollover gate counts verified runs after the closing moment and re-runs only what is missing and not guard-blocked.

### F.2 Ordered improvements (each small; test first)
1. **Copy-only fallback on a G3-only block** (C1, R1). One flag on each sync builder (no `/PURGE` / `copy`), plus the status text. This removes "blocked forever = no backup".
2. **G3 small-tree floor** (P3): `allowance = min(max_delete_files, max(guards.min_delete_floor_files, floor(dest*pct)))`. Add the new config key (e.g. 20) with bounds.
3. **P2 classification**: test NETWORK (`dial tcp|i/o timeout|connection refused|...`) before AUTH, and drop the wrapper text `oauth2: cannot fetch token` from AUTH (keep 401/403/invalid_grant). One regex reorder plus fixture tests built from real rclone text.
4. **Cooldown and escalation** (P1): treat "same alert within cooldown" as `elapsed < cooldown - alerts.cooldown_slack_minutes` (new key, e.g. 60), and make a kind/severity change a new incident (notify.py:645-648). Then a fixed reminder ladder (day 1, 2, 4, 7, then weekly) for blocked/action mails, which addresses R3.
5. **Subject from kind plus FY** (P8, R2): severity = the lower of the kind and the fault severity; label includes `title_fy` (email_compose.py:232-236).
6. **One environment failure → one result for all FY passes** (P7, R2, P6): run the env checks once per leg per night, skip the later passes with the same status, send one mail.
7. **Rollover: no daily re-run of final backups while blocked** (P5, "220 retries"). Use `final_copy_state` with nightly runs. Release the NAS in `finally` when the rollover woke it.
8. **Audit new-FY and readiness** (F-E1, C11): no success for that FY means record "not backed up yet" without a mail; call smb_ready before a LAN audit pass.
9. **Retention keeps the last success per leg and FY** (C9). One `AND id NOT IN (latest success per mode/FY)` clause; set the default to 730 as the owner asked.
10. **Trim the walks** (E): drop the cloud probe A, the G2 source size, the write probe, the post-sync LAN walk with file_entries and cross-checks, and merge the cloud checks. Then measure `stage_seconds` on the box before and after.

---

## G) Questions only the owner can answer

1. **G3 block behaviour**: may the backup copy new and changed files while deletions wait for your approval (recommended)? Or must a blocked night copy nothing, as today?
2. **Small-tree floor**: how many deletions may a backup make without asking when a folder is small (proposal: 20 files, or 10%, whichever is larger, never above 500)?
3. **NAS after a failed or blocked run**: shut it down anyway (it is woken again the next night), or leave it on so someone can look? Today it stays on until the next complete run, which can mean weeks.
4. **Reminder rhythm for a waiting decision**: daily (today), or day 1, 2, 4, 7, then weekly?
5. **Rollover touching the NAS (F-OWN-1)**: should the 1 April switch wake the NAS at all? Or should the next 21:01 LAN run create the new NAS folder (that code already exists: `_complete_pending_rollover_nas_folder`)?
6. **Cloud dry-run**: GCS keeps about 2 versions. Would you accept relying on rclone's own `--max-delete` stop and dry-running only after it trips? That saves 1 full walk of both sides per night; worst case, up to the allowance is deleted (and recoverable as versions) before the block. Default answer: keep the dry-run.
7. **Retention**: confirm 730 days, and confirm that the last success of each leg/FY is never purged.
8. **One mail per leg per night during the overlap** (both FYs listed in one mail): yes or no?
