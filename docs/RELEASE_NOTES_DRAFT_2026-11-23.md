# Release notes (DRAFT) - FY overlap release

**For:** the owner (plain language). **Date of draft:** 2026-11-23 (updated the same day). **Branch:** `fy-overlap`.
**Status:** draft for your review. Not yet deployed to a real server. Release date: TO CONFIRM.
**Release gate:** Opus verdict **GO WITH CONDITIONS** (`docs/OPUS_FINAL_VERIFICATION_2026-11-23.md`). It was written
before FIX 1 and FIX 2 below; their tests are in the status section.

Sources (every fact here comes from these): `docs/PLAN_FY_OVERLAP_2026-11-20.md`, `docs/ARCHITECTURE.md`,
`docs/RELEASE_BREAK_SESSION_2026-11-22_PART6.md`, `docs/OPUS_FINAL_VERIFICATION_2026-11-23.md`, `docs/AUDIT_MAIL_CONTENTS_2026-11-23.md`,
`docs/RESEARCH_PREFECT_AND_PACKAGING_2026-11-23.md`, `docs/REVIEW_WINDOWS_COMPAT_2026-11-23.md`, `config.example.yaml`.

---

## 1. In one paragraph

From 1 January the US clients work in the next financial-year folder (for example `FY27-28`), while Indian
clients keep working in the current one (`FY26-27`) until 31 March. This release backs up **both folders**
during those three months, in the cloud and in the office copy (NAS). The dashboard shows one card per folder.
For a single year the nightly backup works as before. A leg can now be **manual only** (started by the dashboard
button, no nightly run). The release also fixes problems found by deliberately trying to break the product, and it
makes sure a problem you should know about reaches you by email and on the dashboard.

---

## 2. What is new

### 2.1 Two year folders for three months (the overlap)

| Period | Folders backed up | Dashboard |
|---|---|---|
| 1 Apr to 31 Dec | the current year only | one card |
| 1 Jan to 31 Mar | next year AND current year | two cards (next year first) |
| 1 Apr (switch) onwards | next year; the old year too until it is fully copied (2.8) | one card, or two while the old year is still being copied |

- **Activation on 1 January:** the first backup run on or after 1 January creates the next-year folder
  (or adopts one that the office already made with the correct name). An email says the folder is now backed up.
  The app never moves files between folders.
- **Each folder has its own** backup history, alerts, approvals and "overdue" clock.
- **Dashboard:** each card has its own **Back up now** (cloud or NAS) and **Verify now** buttons.
- **Reports:** the weekly and monthly email has one section per year.
- **Weekly integrity check:** checks every active year in one run.
- **Daily folder check:** warns if a year-like folder is misnamed (for example `FY 27-28`), or if next year's
  folder already holds files before it is active (typical in December).

### 2.2 Problems found by trying to break the product (fixed, one line each)

1. A financial-year name with a one-digit end year (`FY27-8`) was not flagged as wrong. Now it is.
2. Some settings pages crashed on a malformed request. Now they answer with a clear error.
3. A non-text confirmation phrase on the dashboard crashed the page. Now it is refused cleanly.
4. Cross-site form submissions to the dashboard are refused.
5. Alert emails ignored the length limit when they had no fault code. Now they respect it.
6. Retention cleanup matched only plain ASCII dates. Now it handles any file name.
7. Restore tool: valid folder names with characters such as `:` were refused, junctions were checked too late, and the year name was upper-cased. Fixed.
10. Cancelling a run while it waited for the backup slot left a run marked as still running. Now it is closed.
11. A lock file that was not UTF-8 crashed the lock check. Fixed.
12. Approving a request for a year that is not active is refused (409).
13. Log cleanup could delete loose files in the log root. Now it removes only run-log day folders, and refuses
    to clean up when the retention value is zero or negative.
14. A broken `config.yaml` stopped the backup service silently. Now one email says it cannot start.
15. Early-year activation that failed was reported only once. Now it stays on the dashboard, is repeated,
    and sends one "resolved" email when it works.
16. A vanished early-year NAS folder was silently re-created. Now an approval is asked first (and see 2.7 for which blocks can be approved).
17. Closing a year now also closes its audit alerts (once the year is closed, see 2.8).
18. A second folder that ran after midnight was recorded with the wrong date. Fixed.
19. The weekly and monthly report said "all OK" when nights were missing. Now it says how many nights are
    missing ("action needed").
20. A year that left the active list without a switch is reported as "no longer backed up".
21. Raising `early_start_month` after activation no longer drops the early year (it stays active until the switch).
22. A final backup that only had to wait for the backup slot now retries for two mornings before it asks you
    for an approval.
23. On 1 April, a switch that ran with a stale plan could sync the closed year into the archive. Now it re-reads
    the plan first.
24. A late April 1 switch could leave old approvals open after a failed close. The close is retried.
25. Service status on non-English Windows was read wrongly (now the numeric state code). The readiness script skipped the
    service-account check on PowerShell 5.1 (regex bug). The restore script used `wmic`, removed on newer Windows 11. All fixed.
28. An old "waiting for approval" request kept the dashboard red after a later successful run. Now it is superseded.
29. The service stop settings now kill the whole process tree explicitly.
30. The discovery script used a `Test-Connection` option that does not exist on Windows PowerShell 5.1. Replaced.
31. The three services set `PYTHONUTF8=1`, so log text with non-English characters does not crash them.

### 2.3 Silence fixes (you are told when something is wrong)

- **Undelivered emails:** if the mail server was down, a "resolved" email was suppressed. Now the
  "resolved" email goes out and says the earlier alert was lost.
- **Digest of lost alerts:** after a mail outage, one digest email lists the alerts that could not be
  delivered (settings in section 3).
- **Database replaced silently:** if `manifest.db` is lost or renamed, a fresh database was created with no
  warning. Now an action-needed email and a red dashboard item say so. The backup is still not blocked.
  A new install gives no alarm.
- **Unreachable NAS:** was labelled as an unexpected program error (AAM-SYS-06). It is now labelled as the
  NAS being unreachable (AAM-NAS-01).
- **Invalid config.yaml:** when the file is valid again, one "resolved" email is sent.
- **Watchdog restarts a service:** used to be silent. Now one **WARNING** mail (AAM-SVC-05) names the service and
  how long it was down, then one **RECOVERED** mail. Owner action: none unless it repeats.

### 2.4 Rollover rule (the "D15" behaviour)

On 1 April the switch first needs the **normal approval for the folder's backup** (if that backup was blocked),
and only then the "roll over anyway" approval. Caveat: any approved request for that folder and year counts, so a later different block goes straight to "roll over anyway".

### 2.5 Database history kept for 730 days

`maintenance.db_retention_days` now defaults to 730 days (about 2 years), up from 90. A size-based limit was
tried and reverted (not in this release).

### 2.6 Manual-only mode (a leg is started by hand): DONE

- **Keys:** `lan.scheduled` and `cloud.scheduled` (default true). `false` = no nightly run for that leg, and no
  schedule-based alarms (no overdue, stale, "N of M nights missed" or watchdog overdue mail for it).
- **The button still works:** the NAS button wakes the NAS, backs up, verifies and shuts the NAS down; the cloud
  button backs up and verifies. A manual NAS run covers both active folders under one start and always shuts the NAS down
  after success (TO CONFIRM whether one press covers both folders or each card covers its own; the sources differ).
- **Dashboard and reports:** "LAN: manual only, last backup <date>, N days ago" (amber after the warn days);
  "LAN: off" for a disabled leg; no "next scheduled backup" line for a manual leg; a "mode notes" block in
  the reports; never "all backups OK" when every leg is manual.
- **Amber, no email:** a manual leg with no success, or older than `on_demand_warn_after_days`; and "no leg runs
  automatically" when every enabled leg is manual.
- **1 April and the Sunday audit** still try a manual-only NAS. If it is unreachable, that is an expected warning
  (not a block, not red). The audit says "not run: the NAS is not reachable (LAN is manual only)" with no alert mail.
- **Proven on the test stack:** the schedules are removed while the leg is manual, and restored when switched back.
- **Config note:** `lan.enabled: false` still needs a UNC `paths.lan_destination` (a placeholder) and `wol.enabled: false`.
  Not relaxed (open, section 5).

### 2.7 Buttons and approvals (fixed)

- **NAS blocks that cannot be approved:** NAS-01 (share unreachable), NAS-07 (target is this server's own disk) and
  NAS-10 (folder with files, no marker) show "Do NOT approve, fix the cause" with only Dismiss. The approve endpoint
  refuses them (409). A **new empty** folder (NAS-09) stays approvable, with text saying what approval does.
  `guards.forbid_local_destination` keeps its meaning. Mixed requests cannot be approved as a whole.
- **Double click:** a second start is refused with "already running" (a process-wide lock). Before, two quick presses
  started two back-to-back runs (no data harm seen).
- **Agent down:** a start is refused with "the backup service is not running, nothing was started" when the agent has
  not answered for `dashboard.agent_max_silence_seconds` (default 120). No run is created and the NAS is not woken.
- **Late start:** a manual run that begins more than `dashboard.manual_start_max_wait_minutes` (default 30) after the
  press is recorded as Missed and the NAS is not woken for it.
- **Guard blocks no longer switch the NAS off** (a block is not a backup that ran). The "stuck" Back up button after a fast
  run is fixed. Confirmation texts name the NAS switch-off and deletions. A blocked or partial result shows a clear message.
- **Stale approval:** a successful run for the same leg and year supersedes an older waiting approval, so the dashboard
  no longer stays red after a fix.

### 2.8 A year closes only when every enabled leg has fully copied it (FV-1)

- The switch on 1 April still happens. But the **old year** is closed (tracking stops, its alerts and approvals close)
  only when **every enabled leg** has a successful full copy of it made after 31 March. A leg that is off is not
  required; a manual-only leg is.
- **Until then** both years are backed up: scheduled legs nightly, manual legs by button. The dashboard card reads
  "Last year - still being copied", an amber item reads "FY26-27 is not yet fully copied to LAN" (or Cloud),
  and the reports show it. **One WARNING mail** per old year; **one RECOVERED mail** when it closes.
- The state is worked out from the run history each time, so a restart or crash loses nothing. A year that was closed
  stays closed (a leg switched on later does not re-open it).
- **No automatic give-up:** if the old year's source folder is deleted, its runs fail and the year stays tracked. There is
  no "close year anyway" control yet (see section 4).

- **Dependency:** `cronsim` is now a direct dependency (pinned `2.8`). It was used by the app but only arrived through Prefect.

---

## 3. New or changed settings (defaults)

Settings are in `config.yaml`. Each has a default and an allowed range. The change takes effect at the next run.

| Setting | Default | Allowed | What it does |
|---|---|---|---|
| `rollover.overlap_enabled` | true | on / off | Back up the next year from `early_start_month` until the switch |
| `rollover.early_start_month` | 1 (January) | 1 to 3 | First month of the overlap |
| `rollover.activation_alert_after_hours` | 24 | 1 to 168 | Action-needed email if the next-year folder cannot be set up after this many hours |
| `rollover.archive_alert_after_days` | 3 | 1 to 365 | Action-needed email when a failed archive step has not been fixed after this many days |
| `rollover.sibling_ignore` | empty list | plain names | Folder names to ignore in the daily folder check |
| `rollover.final_backup_wait_retries` | 2 | 0 to 7 | Mornings a final backup may wait for the backup slot before you are asked for approval |
| `guards.small_folder_files` | 100 | 0 to 10000000 (0 = off) | Below this many files, the small-folder delete limit applies |
| `guards.small_folder_max_deletes` | 10 | 0 to 100000 | Deletes allowed in a small folder before a block |
| `guards.forbid_local_destination` | true | true / false | Refuse a NAS destination that points at this server (false = allowed) |
| `lan.scheduled` | true | true / false | false = manual only: no nightly NAS run; the button still works |
| `cloud.scheduled` | true | true / false | false = manual only: no nightly cloud run; the button still works |
| `lan.on_demand_warn_after_days` | 30 | 1 to 365 | Amber reminder when a manual-only NAS backup is older than this |
| `cloud.on_demand_warn_after_days` | 30 | 1 to 365 | Amber reminder when a manual-only cloud backup is older than this |
| `dashboard.agent_max_silence_seconds` | 120 | 30 to 3600 | A start is refused when the backup service has not answered for this long |
| `dashboard.manual_start_max_wait_minutes` | 30 | 5 to 1440 | A manual start that begins later than this is Missed and does not wake the NAS |
| `alerts.undelivered_digest_max_age_hours` | 72 | 0 to 336 | Lost alerts newer than this are re-sent in one digest (0 = off) |
| `alerts.undelivered_digest_retry_minutes` | 30 | 5 to 1440 | Wait between digest retries while mail still fails |
| `maintenance.db_retention_days` | 730 | 35 to 3650 | Age of run history kept (was 90) |

---

## 4. Known limitations and accepted risks

- **Ransomware and the NAS mirror:** the NAS is a mirror of the source. Files encrypted on the source are
  copied to the NAS too. Accepted risk (owner, 2026-10-19).
- **Cloud versions:** the cloud keeps about 2 versions of each file. Accepted.
- **Crash between the two folders:** if the server crashes after the first folder and before the second, the
  second folder is skipped that night. The overdue check catches it.
- **Files edited in a year after it is closed are not backed up.** A year is closed once every enabled leg has its full copy (2.8).
- **Fresh database:** the new version starts with an empty history. Nothing in production depends on the old one.
- **Small-folder rule:** at exactly 100 files, the allowed drop rises from 10 to 20 files. Accepted and documented.
- **Jump of more than one year:** a jump rollover does not back up the years it skipped.
- **Old-schema database at start:** the service stays up with a warning in the log and no mail. Accepted: the real
  deploy is a fresh install (finding B).
- **Server down or watchdog down:** nothing outside the box alerts, and a missed night writes no row. Accepted (2026-10-19).
- **Restore tool default:** the restore engine's default `forbidden_paths` is empty. The only caller passes the list.
  Accepted and documented.

---

## 5. Known issues and owner decisions

**Open decisions (owner):**
- **"Close year anyway" control (decision pending):** none exists. Options: a dashboard button with an audit line, or
  an age limit in the config. Recommendation in the docs: the button.
- **Cloud ARCHIVE early-deletion fees:** the 1 April archive step still runs even if the old year is not yet fully
  copied to the cloud. Syncing into ARCHIVE-class objects may cost early-deletion fees until the copy completes (not changed).
- **Early year's first big upload (D4):** can use the shared night budget and leave the current year late for a few
  nights. Recommendation in the docs: process the current year first until the early year has its first success.
- **GCS lifecycle rule (D13):** `age 365 -> ARCHIVE` has no per-year scope. Apply `deploy/gcs_lifecycle.fy_overlap_proposed.json`
  after review, **before January 2028**.
- **Wording (W1, W2, W3):** the 1 April "New financial year FY27-28 started" email, the power-cut "switch could not be
  finished" email, and a late LAN run dated by its actual start (plan says the scheduled night).
- **Alert-controls plan steps D to G (follow-up limits):** not in the code. Build or drop (A1).
- **Old password in public git history (S1):** the repo is public. The owner said not to chase credentials in
  this round (Part 6); TO CONFIRM whether it is rotated before release.
- **Uninstall script:** `deploy/08_uninstall_services.bat` prints "COMPLETE - fully removed" even if removing a
  service failed. Check with `sc query` after using it. Fix before it is used at a customer.

**Mail wording (deferred to a new session, owner decision 2026-11-23):**
- The four CRITICAL items of the mail audit (`docs/AUDIT_MAIL_CONTENTS_2026-11-23.md`) are not fixed:
  1. The NAS and G5 texts said "Do NOT approve" in one place and "Approve" in another. FIX 1 now shows
     "Do NOT approve" on the blocks that cannot be approved (2.7), but the mail wording was not re-audited.
  2. The 1 April "blocked" and "switched by hand" mails show the wrong steps (the ROL-02 steps).
  3. The "final backup still waiting" warning shows as "Unexpected program error (AAM-SYS-06)".
  4. The invalid-config mail prints raw validator text (it can echo a wrong value) and does not say backups stop.
- The HIGH and MEDIUM mail items are in the same audit document. The manual-only mails need a re-audit.
- `core/email_samples.py` (the preview tool) does not show every mail type yet.

**Known issues that can ship (listed for the record):**
- **L2 Unicode twins (cloud):** if a source folder holds two files whose names differ only in Unicode form (for example
  `café.txt` in two spellings), the cloud copy keeps one of them and still reports complete. The NAS keeps both. Low
  likelihood, but Malayalam names can have composed and decomposed forms. Needs a test against real GCS before a fix.
- **Overlapping cloud runs (investigated):** not a double run. The second run waited about 73 s for the slot. A queued
  run shows a start time that includes the queue wait (recorded only).
- **Queued LAN run can time out** after a long manual cloud run (LAN_SLOT_TIMEOUT).
- **A late LAN run** counts as missed and is dated by its actual start (W3).
- **Windows labels:** robocopy summary labels and `icacls` names are matched in English only (non-English Windows).
- **Install check items:** section 6 below.
- **Audit alert with WoL off:** an unreachable NAS in manual mode can still alert if the audit itself reports it incomplete.

**Not run live (evidence is unit tests or the simulations):**
- LS6 (the live 1 January overlap on a patched date), LS4 (revoked GCS key), LS5 (NAS volume full), LS2b (agent dies,
  robocopy survives). Run LS6 on the test stack before it is cleaned up (Opus condition).

**Verdicts on alternatives (research, `docs/RESEARCH_PREFECT_AND_PACKAGING_2026-11-23.md`):**
- Replace Prefect: **later** (after the first season, if the problems recur; first an adapter, about 2 days).
- Packaging into an executable (source protection): **later**. The contract and licence route is the one to take now.
- Port to Go, Rust or .NET: **never** for now (about 4 to 6 person-months).

---

## 6. What could NOT be tested on the build PC

The build PC is Windows 11 Home with loopback NAS and a real Google Cloud test bucket. It cannot prove:

1. **Windows Server 2016 and 2019** real runs (2016 is the target; 2019 is also in scope).
2. **Install-time checks on the real host** (`docs/REVIEW_WINDOWS_COMPAT_2026-11-23.md` section 7):
   - **Domain controller:** group policy may deny "log on as a service" or the backup privilege.
   - **NAS credentials:** the code has no NAS credential handling. The trial needed a manual `net use`, which does not
     carry into the service session. Owner action at install.
   - **rclone 1.76 against GCS on Server 2016** (the trial used 1.74.3).
   - **Dashboard browser:** Internet Explorer 11 is not supported (the dashboard script does not run there, and it is
     the default on Server 2016). Use Edge or Chrome from another PC.
   - **Folder-creation rights** of the service account (`deploy/04_check_readiness.ps1` on the real server).
3. **Real NAS power-off and wake**, real mail provider delivery, and a real clock on 1 January and 1 April (the
   simulations patched the date; the system clock was never changed).
4. **NAS volume full** (LS5 skipped; covered only by fake-tool tests).

---

## 7. Smoke checklist after a fresh install on the real machines

Run in this order, on the real server, before 30 December:

1. Run `deploy/04_check_readiness.ps1 -ServiceAccount .\<account>` and check every line passes.
2. Run `deploy/05_test_config.bat`. Expect no errors.
3. Run `deploy/06_install_services.ps1 -ServiceAccount .\<account>`. Three services are Running.
4. Open the dashboard in **Edge or Chrome** and sign in. One card for the current year. No red banner and no pending approval.
5. Press **Back up now (cloud)**, then **(NAS)**: each completes; the NAS wakes and shuts down afterwards. Press **Verify now** on both legs: no differences.
6. Send one test email (`tools/preview_emails.py --to <your address>`); it arrives with the `[PREVIEW]` mark.
7. Settings shows the rollover card with the section 3 values. Restart the server: agent and watchdog come back and the next run completes.
8. Write the date and result of each step in your install log (TO CONFIRM where it is kept).

---

## 8. Sources and status

- **Full unit suite:** 4265 passed, 21 skipped (environment), 1 xfail (finding B, moot) on `66d653a`, the basis of the
  Opus verdict. FIX 2's own run: 1245 passed, 0 failed (a subset). **A full run after FIX 1 and FIX 2: TO CONFIRM.**
- **Audit lab:** the full run is from 22 November. Opus asks for one full run on the release candidate (only w16, and the
  classified w26b-L2 and w26e-L7, may be red). Today's targeted w26/w27 re-run: w26a, w26c, w27b pass; w26b-L2 and w26e-L7 still red (classified).
- **Live test stack (real cloud bucket, loopback NAS):** passed LS1, LS2, LS3, LS7b, LS9 to LS13 and the live buttons
  (wave 3), including manual-only mode. The Prefect stop time did not reproduce on an idle stack (`C:\AAMTest\live\LIVE_RESULTS.md`, not in git).
- **Opus conditions:** before the release branch: the full lab, owner acceptance of L2 and the not-run items, branch cut from
  `origin/main`, button smoke on the final code. Before go-live: LS6, mail CRITICAL items. Before 1 April: rollover mails, April-1 rehearsal.
