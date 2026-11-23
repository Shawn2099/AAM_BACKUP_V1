# Release notes (DRAFT) - FY overlap release

**For:** the owner (plain language). **Date of draft:** 2026-11-23. **Branch:** `fy-overlap`.
**Status:** draft for your review. Not yet deployed to a real server. Release date: TO CONFIRM.

Sources (every fact here comes from these): `docs/PLAN_FY_OVERLAP_2026-11-20.md`, `docs/ARCHITECTURE.md`,
`docs/RELEASE_BREAK_SESSION_2026-11-22*.md` (Parts 1 to 6), `docs/OPUS_XFAIL_DECISIONS_2026-11-23.md`,
`docs/PLAN_E2E_SIMULATION_2026-11-23.md`, `docs/INVESTIGATION_CLOUD_ONLY_MANUAL_LAN_2026-11-23.md`, `config.example.yaml`.

---

## 1. In one paragraph

From 1 January the US clients work in the next financial-year folder (for example `FY27-28`), while Indian
clients keep working in the current one (`FY26-27`) until 31 March. This release backs up **both folders**
during those three months, in the cloud and in the office copy (NAS). The dashboard shows one card per folder.
For a single year the nightly backup works as before. The release also fixes the problems found by deliberately
trying to break the product, and it makes sure that a problem you should know about reaches you by email
and on the dashboard.

---

## 2. What is new

### 2.1 Two year folders for three months (the overlap)

| Period | Folders backed up | Dashboard |
|---|---|---|
| 1 Apr to 31 Dec | the current year only | one card |
| 1 Jan to 31 Mar | next year AND current year | two cards (next year first) |
| 1 Apr (switch) onwards | next year only (current year is closed) | one card |

- **Activation on 1 January:** the first backup run on or after 1 January creates the next-year folder
  (or adopts one that the office already made with the correct name). An email says the folder is now backed up.
  The app never moves files between folders.
- **Each folder has its own** backup history, alerts, approvals and "overdue" clock.
- **Dashboard:** each card has its own **Back up now** (cloud or NAS) and **Verify now** buttons.
- **Reports:** the weekly and monthly email has one section per year.
- **Weekly integrity check:** checks every active year in one run.
- **Daily folder check:** warns if a folder with a year-like name is misnamed (for example `FY 27-28`), or if
  next year's folder already holds files before it is active (typical in December).
- **Closing a year (1 April):** after the switch, the old year's pending approvals are closed, its open
  alerts end quietly, and its history stays visible.

### 2.2 Problems found by trying to break the product (fixed, one line each)

1. A financial-year name with a one-digit end year (`FY27-8`) was not flagged as wrong. Now it is.
2. Some settings pages crashed on a malformed request. Now they answer with a clear error.
3. A non-text confirmation phrase on the dashboard crashed the page. Now it is refused cleanly.
4. Cross-site form submissions to the dashboard are refused.
5. Alert emails ignored the length limit when they had no fault code. Now they respect it.
6. Retention cleanup matched only plain ASCII dates. Now it handles any file name.
7. The restore tool refused valid folder names that contain characters such as `:`. Fixed.
8. The restore tool checked for junctions after resolving the path. Now it checks before.
9. The restore tool upper-cased the financial-year name. Fixed.
10. Cancelling a run while it waited for the backup slot left a run marked as still running. Now it is closed.
11. A lock file that was not UTF-8 crashed the lock check. Fixed.
12. Approving a request for a year that is not active is refused (409).
13. Log cleanup could delete loose files in the log root. Now it removes only run-log day folders, and refuses
    to clean up when the retention value is zero or negative.
14. A broken `config.yaml` stopped the backup service silently. Now one email says it cannot start.
15. Early-year activation that failed was reported only once. Now it stays on the dashboard, is repeated,
    and sends one "resolved" email when it works.
16. A vanished early-year NAS folder was silently re-created. Now an approval is asked first.
17. Closing a year now also closes its audit alerts.
18. A second folder that ran after midnight was recorded with the wrong date. Fixed.
19. The weekly and monthly report said "all OK" when nights were missing. Now it says how many nights are
    missing ("action needed").
20. A year that left the active list without a switch is reported as "no longer backed up".
21. Raising `early_start_month` after activation no longer drops the early year (owner choice: it stays
    active until the switch).
22. A final backup that only had to wait for the backup slot now retries for two mornings before it asks you
    for an approval.
23. On 1 April, a switch that ran with a stale plan could sync the closed year into the archive. Now it re-reads
    the plan first.
24. A late April 1 switch could leave old approvals open after a failed close. The close is retried.
25. Service status on non-English Windows was read wrongly. Now it reads the numeric state code.
26. The readiness script skipped the service-account check on Windows PowerShell 5.1 (a regular-expression
    bug). Fixed.
27. The restore script used `wmic`, which is removed on newer Windows 11. Replaced.
28. An old "waiting for approval" request kept the dashboard red after a later successful run. Now it is
    superseded.
29. The service stop settings now kill the whole process tree explicitly.

### 2.3 Silence fixes (you are told when something is wrong)

- **Undelivered emails:** if the mail server was down, a "resolved" email was suppressed. Now the
  "resolved" email goes out and says the earlier alert was lost.
- **Digest of lost alerts:** after a mail outage, one digest email lists the alerts that could not be
  delivered (see the new settings in section 3).
- **Database replaced silently:** if `manifest.db` is lost or renamed, a fresh database was created with no
  warning. Now an action-needed email and a red dashboard item say so. The backup is still not blocked.
  A new install gives no alarm.
- **Unreachable NAS:** was labelled as an unexpected program error (AAM-SYS-06). It must be labelled as the
  NAS being unreachable (AAM-NAS-01). Fixed on `fy-overlap` (Part 6).
- **Invalid config.yaml:** when the file is valid again, one "resolved" email is sent.

### 2.4 Rollover rule (the "D15" behaviour)

On 1 April the switch first needs the **normal approval for the folder's backup** (if that backup was
blocked), and only then the "roll over anyway" approval. Before this fix the "roll over anyway" request
appeared first. Caveat: any approved request for the same folder and year counts as "approved".

### 2.5 Database history kept for 730 days

`maintenance.db_retention_days` now defaults to 730 days (about 2 years), up from 90. The 15-month
overlap cycle keeps its history.

### 2.6 Manual-only LAN mode (cloud on a schedule, NAS by hand): in progress, see Part 6

The owner asked for a mode where the NAS backup is not scheduled and is started by hand from the dashboard
(waking and shutting down the NAS). This is **in progress, see Part 6**
(`docs/RELEASE_BREAK_SESSION_2026-11-22_PART6.md`). Today the only switch `lan.enabled` turns off the
manual button too, so this mode is **not available in this release** unless Part 6 confirms otherwise. TO CONFIRM.

---

## 3. New or changed settings (defaults)

Settings are in `config.yaml`. Each has a default and a allowed range. The change takes effect at the next run.

| Setting | Default | Allowed | What it does |
|---|---|---|---|
| `rollover.overlap_enabled` | true | on / off | Back up the next year from `early_start_month` until the switch |
| `rollover.early_start_month` | 1 (January) | 1 to 3 | First month of the overlap |
| `rollover.activation_alert_after_hours` | 24 | 1 to 168 | Action-needed email if the next-year folder cannot be set up after this many hours |
| `rollover.sibling_ignore` | empty list | plain names | Folder names to ignore in the daily folder check |
| `rollover.final_backup_wait_retries` | 2 | 0 to 7 | Mornings a final backup may wait for the backup slot before you are asked for approval |
| `guards.small_folder_files` | 100 | (see config) | Below this many files, the small-folder delete limit applies |
| `guards.small_folder_max_deletes` | 10 | (see config) | Deletes allowed in a small folder before a block |
| `alerts.undelivered_digest_max_age_hours` | 72 | 0 to 336 | Lost alerts newer than this are re-sent in one digest (0 = off) |
| `alerts.undelivered_digest_retry_minutes` | 30 | 5 to 1440 | Wait between digest retries while mail still fails |
| `maintenance.db_retention_days` | 730 | 35 to 3650 | Age of run history kept (was 90) |

Also in `config.example.yaml`: `rollover.archive_alert_after_days` (3, 1 to 365) controls when a failed
archive step becomes action-needed. Exact allowed ranges for the guard keys: TO CONFIRM in `models/config.py`.

---

## 4. Known limitations and accepted risks

- **Ransomware and the NAS mirror:** the NAS is a mirror of the source. Files encrypted on the source are
  copied to the NAS too. Accepted risk (owner, 2026-10-19).
- **Cloud versions:** the cloud keeps about 2 versions of each file. Accepted.
- **Crash between the two folders:** if the server crashes after the first folder and before the second, the
  second folder is skipped that night. The overdue check catches it. The NAS stays on until the next LAN run.
- **Files edited in a closed year after the switch are not backed up** (for example FY26-27 after 1 April).
- **Fresh database:** the new version starts with an empty history. Nothing in production depends on the old one.
- **Small-folder rule:** at exactly 100 files, the allowed drop rises from 10 to 20 files. Accepted and documented.
- **Jump of more than one year:** a jump rollover does not back up the years it skipped.
- **Early year's first big upload** can use the shared night budget and leave the configured year late for a few
  nights (owner decision D4, still open; see section 7).
- **Restore tool default:** the restore engine's default `forbidden_paths` is empty. The only caller passes the
  list. Accepted and documented. TO CONFIRM whether you want it changed.
- **Old-schema database at start:** the service stays up silently. Accepted: the real deploy is a fresh install.
- **Uninstall script:** may print "COMPLETE - fully removed" even if removing a service failed. Not fixed.
- **Prefect stop time:** a stop of the Prefect service can take longer than expected (the agent has a 180 s
  stop window by design). Not fully explained; experiment planned. Not a data risk.
- **Two overlapping cloud runs (about 80 s)** were seen on the test stack although the slot limit is one. No data
  loss (0 files). Open: code check not yet done (Part 6).
- **Watchdog after a long Prefect outage:** on the test stack the agent was not restarted within a 3-minute
  window. Open: TO CONFIRM the restart time in a real run (Part 6).

---

## 5. What could NOT be tested on the build PC

The build PC is Windows 11 Home with loopback NAS and a real Google Cloud test bucket. It cannot prove:

1. **Windows Server 2016 and 2019** real runs (the target is Windows Server 2016; 2019 is also in scope).
2. **Real NAS power-off and wake** on the office hardware.
3. **Real mail provider** delivery and its limits.
4. **A real clock** on 1 January and 1 April (the simulations patched the date; the system clock was never changed).
5. **NAS volume full** (LS5 skipped; covered only by fake-tool tests).
6. **Real folder-creation rights** of the production service account (test in `deploy/04_check_readiness.ps1`
   on the real server).

---

## 6. Smoke checklist after a fresh install on the real machines

Run in this order, on the real server, before 30 December:

1. Run `deploy/04_check_readiness.ps1 -ServiceAccount .\<account>` and check every line passes. Expect the
   folder-creation test in `parent(source)` and on the NAS share root to pass.
2. Run `deploy/05_test_config.bat`. Expect no errors.
3. Run `deploy/06_install_services.ps1 -ServiceAccount .\<account>`. Three services are Running.
4. Open the dashboard and sign in. One card for the current year. No red banner and no pending approval.
5. Press **Back up now (cloud)**. Expect a completed cloud run with files verified.
6. Press **Back up now (NAS)**. The NAS wakes, the backup completes, and the NAS shuts down afterwards.
7. Press **Verify now** on both legs. Expect no differences.
8. Send one test email (`tools/preview_emails.py --to <your address>`). Check it arrives with the `[PREVIEW]` mark.
9. Check the Settings page shows the rollover card with the overlap values from section 3.
10. Restart the server. Check the agent and watchdog come back and the next run completes.
11. Write the date and result of each step in your install log. TO CONFIRM where the install log is kept.

---

## 7. Owner decisions still open

| Ref | Decision | Recommendation (from the docs) |
|---|---|---|
| D4 | Early year's first big upload may starve the current year for a few nights | Process the current year first until the early year has its first success, or reserve budget |
| D13 | GCS lifecycle rule `age 365 -> ARCHIVE` has no per-year scope | Apply `deploy/gcs_lifecycle.fy_overlap_proposed.json` after review, **before January 2028** |
| W1 | Wording of the 1 April rollover email "New financial year FY27-28 started" (it has been backed up since January) | Owner decides the wording (no recommendation recorded) |
| W2 | Wording of the action-needed email after a power cut ("switch could not be finished"); the next morning usually self-heals | Owner decides the wording |
| W3 | A late LAN run that counts as missed is dated by its actual start, not the scheduled night | Plan rev 4 section 3 says the scheduled night counts; the row should follow it |
| W4 | Settings and fault text for manual-only LAN (section 2.6) | Decide after Part 6 |
| A1 | Alert-controls plan steps D to G (follow-up limits) are not in the code | Owner decides: build or drop (no recommendation recorded) |
| S1 | Rotate the old password that is still in the public git history | Open (Parts 5 and 6); repo is public |
| R1 | Restore tool default `forbidden_paths` | Accepted; make it required when a second caller appears |

---

## 8. Sources and status

- Full unit suite last recorded on `a87243e`: 4089 passed, 21 skipped, 2 xfailed. A run after the last
  merges: TO CONFIRM.
- Audit lab (real robocopy and rclone): 249 passed, 0 failed earlier in the session. The 15 newest lab tests
  have not been run yet (Part 6).
- Live test stack (real cloud bucket, loopback NAS): passed LS1, LS2, LS3, LS9, LS10, LS11, LS12, LS13;
  LS7b passed with findings; LS4 and LS5 skipped; LS7a, LS8b (moot) and the overlapping cloud runs are open.
  Details: `C:\AAMTest\live\LIVE_RESULTS.md` (not in git) and Part 6.
