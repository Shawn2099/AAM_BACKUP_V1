# Email content specification (owner decisions 2026-10-18; updated 2026-10-19)

Status: **BUILT** (owner decisions D1-D6 answered 2026-10-18, see §9; attachment ladder, calendar
report periods and the preview tool added 2026-10-19). Source of truth for every email the system
sends. The file-list CSV (§5, item 3) is not built (post-MVP).

Goal (owner, 2026-10-18): every email is easy for a non-technical person, says what to
do, and carries enough evidence (fault code + logs) that, when it is forwarded to
support, support can find the reason without logging in to the server.

---

## 1. What the research says (and what we take from it)

| Source | Finding | What we do |
|---|---|---|
| Google "Email sender guidelines" (all senders, not only bulk) | Every message must follow RFC 5322, **include a valid Message-ID**, single-instance headers (Date, From, To, Subject) only once, TLS. HTML must be standard HTML; no hidden content. | Add `Message-ID` and `Date` ourselves (today we send neither - we rely on the mail server to add them). Keep TLS (already). |
| Gmail attachment rules | 25 MB total per message; executables (.exe .bat .ps1 .js ...) blocked even inside .zip; **password-protected archives blocked**. .txt/.csv/.zip with logs are fine. | Attach only `.txt`, `.csv`, `.zip` of `.log/.txt/.json`. Never encrypt the zip. Keep the total far below 25 MB (see §5: 5 MB, because company mail servers often stop at 10 MB after encoding). |
| Microsoft error-message guidelines | Say what happened, why, the result for the user, and how to fix it; **one specific message per known cause**, not one generic message. | One plain-language text per fault code (§4), not per status. |
| Support-oriented error guidance (content-strategy / design-system guides) | Main sentence in plain words; a **unique code at the end** that the customer can quote to support. Codes in a secondary place so they do not scare. | Fault code in the subject end + a "For support" block at the bottom. |
| Commvault alert guidance | Alerts must state their purpose and the next steps; send them to someone who can act; **test the whole delivery path**; define response steps for frequent alerts. | "What to do" in every email; "Who fixes it" per code; a "Send test email" button (§8). |
| Veeam job notifications | Subject carries the job, result and counts; a detailed report can be attached; failures notified on the last retry only. | Subject = what + result + code; one email per run after our own retries (already so); detailed support report attached. |
| Atlassian / Statuspage incident communication | Communicate early, say what is affected and when the next update comes. | Every problem email ends with "What happens next" (next automatic try, next reminder). |

Sources: [Google sender guidelines](https://support.google.com/mail/answer/81126?hl=en),
[Gmail blocked file types](https://support.google.com/mail/answer/6590?hl=en),
[Microsoft error message guidelines](https://learn.microsoft.com/en-us/windows/win32/debug/error-message-guidelines),
[Error messages for supportable products](https://contentstrategyalliance.com/2014/12/29/error-messages-best-practices-for-supportable-product-development/),
[Writing for errors (design system)](https://developers.os.wpp.com/docs/design-system/writing-guidelines/writing-for-errors),
[Commvault alerts](https://documentation.commvault.com/11.42/software/alerts.html),
[Veeam job result notifications](https://helpcenter.veeam.com/docs/backup/em/em_notifications_on_job_results.html),
[Atlassian incident communication templates](https://www.atlassian.com/incident-management/incident-communication/templates).

## 2. Re-check of the fixes proposed on 2026-10-18 (are they the best?)

| # | Proposed | Verdict after research | Best fix |
|---|---|---|---|
| 1 | One switch silences every alert | Keep, **strengthen** | Emails that need a decision or warn of lost protection (blocked, overdue, audit differences, services down) are **not switchable at all**. `notifications.send_on_failure` only silences routine failure emails, moves to Advanced, needs a typed confirmation, and the dashboard shows amber while it is off. |
| 2 | Email link = 127.0.0.1 | Keep | Office-network only (owner 2026-10-18): dashboard listens on the office LAN, Windows Firewall rule for port 8080 **from the local subnet only**, `dashboard.public_url` required (refuse to start when it is empty or points at 127.0.0.1/0.0.0.0). Port 4200 (Prefect, no login) stays on 127.0.0.1 - remove the "open 4200" advice from `01_server_discovery.ps1`. |
| 3 | Dashboard shows old green when the server is gone | Keep | Red "Cannot reach the backup server since HH:MM" banner, cards turn grey, and a "last updated HH:MM:SS" line. |
| 4 | Mail settings never tested | Keep, **extend** | "Send test email" (through the real alert path, recorded). After mail settings change, the dashboard shows amber "Email settings changed - not tested yet" until a test is delivered. |
| 5 | Reports mix both legs | Keep | Per-leg section first, in plain words (§7.13). |
| 6 | Cooldown hides a new failure after a recovery | Keep, **better rule** | Suppress a repeat only while the problem has *not changed*: a RECOVERED email (or any different fault code on that leg) starts a new incident, so its first email always goes out. Unchanged problems keep the reminder per `alerts.repeat_cooldown_hours`. |
| 7 | Shared rate limit | Keep | Separate limits per action type (trigger / approve / settings / login / report), all config keys. |
| new | Missing Message-ID / Date | **Raised to Medium** (Google requires Message-ID for all senders) | Add `Message-ID` (`<run-ref@aam-backup>`), `Date`, and a plain-text part next to the HTML. |
| new | No fault codes | **New** | Fault code catalog (§4), stored on the run row, shown on dashboard, report and email. |
| new | No support evidence | **New** | Support report `.txt` + logs `.zip` attached (§5). |
| new | Follow-ups scattered | **New** | Reminder and "resolved" emails reply in the same thread as the first email of the incident (`In-Reply-To` / `References`), so mail programs group them. |

## 3. Rules for every email

**Structure (always this order; the top part fits on a phone screen, about 120 words):**

1. **Headline** - one sentence, plain words: what happened, to which backup.
2. **Is my data safe?** - three lines: the server's files, the office NAS copy, the online copy,
   each with its last good copy time.
3. **What to do** - numbered steps, at most 4, starting with verbs. Says who should do it
   ("You", "Your IT person", "Support"). If nothing is needed: "Nothing - this is for your information."
4. **What happens next** - the next automatic try / reminder / report, with its time.
5. Dashboard button: `Open the backup dashboard` (link = `dashboard.public_url`, works on office network).
6. Divider, then **For support** (small grey text): fault code, reference, technical status,
   exit code + meaning, server, software version, time, and "the attached support report explains the cause".

**Words.** Never in the top part: robocopy, rclone, exit code, SMB, Prefect, guard, G3, NSSM, UNC,
stack trace. Use these names everywhere:

| Technical | Email word |
|---|---|
| LAN leg / NAS | **office backup (NAS)** - "the backup box in the office" |
| cloud leg / GCS | **online backup (Google Cloud)** |
| source `E:\FY26-27` | **your shared folder (E:\FY26-27)** |
| guard BLOCK | "we stopped the backup to protect your files" |
| approval phrase | "type the confirmation words shown on the dashboard" |
| SUSPECT | "did not finish cleanly - we cannot prove every file was copied" |
| PARTIAL | "only part of the changes were copied" |
| integrity audit | "weekly safety check" |
| FY rollover | "switch to the new financial year folder" |
| watchdog / services | "the backup program on the server" |

**Tone.** Calm, no blame, no capitals except the severity word. "We" = the backup program, "you" = the reader.
Never "error occurred"; always the specific cause.

**Times.** Indian time, 12-hour, the dashboard format: `18 Oct 2026, 9:00 PM IST`. Relative words
only next to a date ("tonight, 18 Oct, 9:00 PM IST").

**Severity word (subject + coloured bar at the top):**

| Word | Colour | When |
|---|---|---|
| ACTION NEEDED | red | a person must do something (blocked, overdue, safety check found differences, program stopped) |
| PROBLEM | red | a backup failed; the steps say whether you must act now or only if it repeats |
| WARNING | amber | protection reduced but still working (partial, NAS not switched off, clock not verified, safety check could not run) |
| RESOLVED | green | an earlier problem is fixed |
| INFO | blue | reports, financial-year switch done, test email |

**Subject:** `[<firm> Backup] <SEVERITY>: <plain headline> (<fault code>)`, at most ~90 characters, e.g.
`[AAM Backup] PROBLEM: Office NAS backup failed - NAS not reachable (AAM-NAS-01)`.
Reports and INFO have no code. The subject of one incident stays the same for its reminders;
the resolved email is `[AAM Backup] RESOLVED: Office NAS backup is working again (was AAM-NAS-01)`.

**Format.** `multipart/alternative` with a plain-text part and an HTML part; inline CSS only; no images
(blocked by default in many mail programs), no web fonts; max width 600 px; colours readable in dark mode
(dark text on light boxes, never colour alone - every status also has a word).

## 4. Fault codes

Format `AAM-<AREA>-<NN>`. Codes never change meaning once released; new causes get new numbers.
Each code has: plain title, what happened, who fixes it, steps. One code per run (the first cause
that stopped it). Stored in `run_history.extended_metrics.fault_code`, shown on the dashboard row,
in the report CSV and in the email.

**Who fixes it:** **You** = office staff can do it; **IT** = whoever manages the office network/PCs;
**Support** = the backup vendor.

### 4.1 Your shared folder (source)

| Code | Title | What happened (email text) | Who | What to do |
|---|---|---|---|---|
| AAM-SRC-01 | Shared folder not found | The backup could not open your shared folder {source}. The drive may be disconnected, renamed, or the server was started without it. Nothing was copied or deleted. | You → Support | 1. Check that drive {drive} is visible in "This PC" on the server. 2. If the folder was renamed or moved, put it back or tell support. 3. Then press "Run backup" on the dashboard. |
| AAM-SRC-02 | No permission to read the shared folder | The backup program is not allowed to read {source}. Nothing was copied or deleted. | Support | Forward this email to support. Someone probably changed the folder's permissions. |
| AAM-SRC-03 | Shared folder is empty | Your shared folder {source} has no files. We stopped the backup so an empty folder cannot wipe your backup copy. | You | 1. Check the folder on the server - are your files there? 2. If files are missing, **do not approve**; call support. 3. Only if the folder is empty on purpose, approve on the dashboard. |
| AAM-SRC-04 | Many files disappeared from the shared folder | Your shared folder has {now} files; at the last good backup it had {before} ({pct}% fewer, limit {limit}%). We stopped the backup so the missing files stay safe in the backup. | You | 1. Ask your team whether a large folder was moved or deleted on purpose. 2. If not: **do not approve**; the files are still in the backup - call support to restore. 3. If it was on purpose, approve on the dashboard. |
| AAM-SRC-05 | Could not count the shared folder's files | We could not count the files in your shared folder, so we cannot prove it is safe to continue. Nothing was copied or deleted. | Support | Forward this email to support. |

### 4.2 Office backup box (NAS)

| Code | Title | What happened | Who | What to do |
|---|---|---|---|---|
| AAM-NAS-01 | NAS not reachable | The office backup box (NAS, {nas_ip}) did not answer on the network for {waited}. Nothing was copied. | You | 1. Check the NAS is switched on (lights on). 2. Check its network cable and the office switch/router. 3. Press "Run LAN Backup" on the dashboard. 4. If it fails again, call support. |
| AAM-NAS-02 | NAS did not wake up | We sent the wake-up signal to the NAS but it did not start within {waited}. Nothing was copied. | You → Support | 1. Switch the NAS on by hand. 2. Press "Run LAN Backup". 3. Tell support - automatic wake-up needs checking (it may be switched off in the NAS settings). |
| AAM-NAS-03 | Not allowed to write on the NAS | The NAS refused to let the backup save files in {dest}. Usually the NAS user's password was changed or its permissions were removed. Nothing was copied. | IT / Support | Forward this email to support or your IT person. (Support: the service account `{account}` needs modify rights on the share.) |
| AAM-NAS-04 | NAS is full | The NAS has {free} free, the backup needs about {needed}. Nothing was copied. | You / IT | 1. Free space on the NAS or add a disk. 2. Do **not** delete the FY folders of the backup. 3. Call support if unsure. |
| AAM-NAS-05 | Could not check NAS free space | We could not measure the NAS free space, so we did not start. | Support | Forward to support. |
| AAM-NAS-06 | NAS was not confirmed switched off | The backup finished, but the NAS did not confirm it switched off ({detail}). Your backup is fine. | You | Check the NAS and switch it off by hand if it is still on. Tell support if this repeats. |
| AAM-NAS-07 | Backup target is on this server | The backup target {dest} is a disk of the server itself, not the NAS. A copy on the same machine does not protect you, so we stopped. | Support | Forward to support - the backup target setting is wrong. Do not approve. |
| AAM-NAS-08 | Backup target changed | The backup target changed from {old} to {new} since the last good backup. We stopped to make sure your files go to the right place. | You | If you or support changed it on purpose, approve on the dashboard. If not, do not approve - call support. |
| AAM-NAS-09 | New, empty backup target | The backup target {dest} is new and empty. Before the first full copy we need your OK. | You | If this is the new NAS/folder you expected, approve on the dashboard (type the words shown). |
| AAM-NAS-10 | Wrong disk or folder on the NAS | {dest} has files but not the backup's marker file, so it may be a different disk or folder. We stopped so we do not overwrite it. | Support | Do not approve. Forward to support. |
| AAM-NAS-11 | NAS is not storing files reliably | We saved a small test file on the NAS but could not read the same content back. Nothing was copied. | IT / Support | Check the NAS for disk warnings; forward to support. |

### 4.3 Office backup copy (LAN)

| Code | Title | What happened | Who | What to do |
|---|---|---|---|---|
| AAM-LAN-01 | Copy to the NAS failed | The copy to the NAS stopped with an error ({meaning}). Your server's files are untouched; the NAS copy is from {last_good}. | Support | Forward to support (the attached report has the tool log). The next backup runs automatically {next_run}. |
| AAM-LAN-02 | Some files could not be copied | {failed} file(s) could not be copied - usually files that were open (Tally, Excel) during the backup. All other files were copied. | You | Close programs at the end of the day. The list of files is in the attachment. Tonight's backup tries again. |
| AAM-LAN-03 | Backup ran out of time | The backup reached its time limit ({hours} h) and stopped; {copied} files were copied, the rest follow in the next backup. | Nothing / Support | Nothing if this happens once (for example after many changes). If it repeats, tell support - the time limit may be too short. |
| AAM-LAN-04 | Backup did not finish cleanly | The copy ended in an unexpected way, so we cannot prove every file was copied. | Support | Forward to support. The next backup re-checks everything. |
| AAM-LAN-05 | Backup user lacks backup rights | The backup user on the server is missing the Windows "Back up files and directories" right. | IT / Support | Forward to support. |

### 4.4 Online backup (Google Cloud)

| Code | Title | What happened | Who | What to do |
|---|---|---|---|---|
| AAM-CLD-01 | Cannot reach Google Cloud | The server could not connect to Google Cloud ({category}: no internet, DNS or firewall). Nothing was uploaded. | You / IT | 1. Check the server has internet (open google.com on it). 2. Press "Run Cloud Backup". 3. Call support if it fails again. |
| AAM-CLD-02 | Secure connection blocked by the firewall/antivirus | Your firewall or antivirus (for example Sophos web protection) is inspecting the secure connection to Google, so Google's certificate is rejected. Nothing was uploaded. | IT | Your IT person must exclude `storage.googleapis.com` and `oauth2.googleapis.com` from HTTPS inspection for the server. |
| AAM-CLD-03 | Google Cloud refused the login | Google refused the backup's key (expired, deleted or permissions removed). Nothing was uploaded. | Support | Forward to support - the cloud key must be renewed. |
| AAM-CLD-04 | Cloud key file missing | The file with the cloud key ({key_path}) is missing on the server. | Support | Forward to support. |
| AAM-CLD-05 | Not allowed to save in the cloud | Google accepted the login but did not allow saving files in the backup bucket. | Support | Forward to support. |
| AAM-CLD-06 | Upload failed | The upload to Google Cloud stopped with an error ({meaning}). | Support | Forward to support. The next backup runs automatically {next_run}. |
| AAM-CLD-07 | Online backup ran out of time | The online backup reached its time limit ({hours} h); {copied} files were uploaded, the rest follow next time. | Nothing / Support | Nothing for the first full upload (it takes several nights). Tell support if it continues after that. |
| AAM-CLD-08 | Online backup did not finish cleanly | The upload ended in an unexpected way; we cannot prove every file was uploaded. | Support | Forward to support. |
| AAM-CLD-09 | Size check after upload failed | After the upload, the online copy did not match the server's files in number or size ({detail}). | Support | Forward to support. The next backup re-checks. |
| AAM-CLD-10 | Could not check the online backup before starting | The check of the online backup before the upload failed (cause not recognised), so nothing was uploaded. | Support | Forward to support. The next backup tries again. |

### 4.5 Deleting files (protection against mass deletion)

| Code | Title | What happened | Who | What to do |
|---|---|---|---|---|
| AAM-DEL-01 | Backup would delete many files | To match your shared folder, this backup would delete {n} of {total} files from the {leg} ({pct}%; limit {limit}). We stopped so nothing is deleted without your OK. | You | 1. Ask your team whether a large folder was deleted or moved on purpose. 2. If not: **do not approve** - the files are still in the backup; call support. 3. If on purpose: approve on the dashboard. |
| AAM-DEL-02 | Could not count planned deletions | We could not work out how many files this backup would delete, so we did not start. | Support | Forward to support. |
| AAM-DEL-03 | Stopped at the delete limit | During the backup the number of deletions reached the limit ({deleted}), so we stopped it. | You | Same as AAM-DEL-01. |

### 4.6 Server clock and financial year

| Code | Title | What happened | Who | What to do |
|---|---|---|---|---|
| AAM-CLK-01 | Server clock is wrong | The server's clock is off by {skew} compared with internet time. A wrong clock can put files in the wrong financial-year folder, so we did not start. | IT | Correct the server's date/time (Windows: Settings > Time > Sync now). Then press "Run backup". |
| AAM-CLK-02 | Server clock could not be checked | No time server answered, so we could not prove the clock is right. The backup ran (setting: {policy}). | IT | Nothing urgent. If this repeats, check the server's internet / time settings. |
| AAM-CLK-03 | Clock is behind - FY switch refused | The server's clock says it is before the financial year the backups already use. We refuse to switch back. | IT / Support | Correct the server's date/time; call support. |
| AAM-CLK-04 | Server clock could not be checked - backup stopped | No time server answered and `health.clock_unverified_policy` is `block`, so nothing was uploaded. | IT | Check the server's internet access / time.google.com; then press "Run Cloud Backup". |
| AAM-ROL-01 | Financial year jumped more than one year | The date says FY {to}, but the backups use {from}. Moving more than one year at once is unusual. | You | Check the server date. Approve only if this is really intended. |
| AAM-ROL-02 | Last backup of the old year incomplete | The final backup of FY {from} did not complete for {legs}. Switching now would leave the last changes of {from} unprotected. | You / Support | Fix the backup problem first (see its own email); the switch then runs automatically. |
| AAM-ROL-06 | Files in the new year are not backed up yet | N files were saved in the FY {to} folder, but the backups still use FY {from} until the switch is done, so they exist on the server only. Do not move files out of FY {from} meanwhile. | You / Support | Open the dashboard and approve the switch, or fix the problem it shows. Sent by the daily 06:00 check, repeated at most every `alerts.stale_repeat_hours`; a RESOLVED mail follows once the switch is done. |
| AAM-ROL-03 | Clock not proven - financial-year switch waits | The switch to the new FY needs a proven clock but no time server answered; backups keep using the current FY folder, nothing changed. | IT | Check internet access and the date/time; the switch is retried every morning. |
| AAM-ROL-04 | Financial-year switch could not be finished | The switch to FY {to} stopped part-way. Your data is safe: nothing was deleted; the settings file is exactly as before. | Support | Forward this email to support; the switch is retried every morning and continues where it stopped. |
| AAM-ROL-05 | Old year not moved to cheaper storage yet | The switch is done, but the online copy of FY {from} could not be moved to ARCHIVE storage. Data is safe; it only costs more until this works. | Support | Nothing needed - retried every morning. Becomes ACTION NEEDED after `rollover.archive_alert_after_days`. |

### 4.7 Weekly safety check (integrity audit)

| Code | Title | What happened | Who | What to do |
|---|---|---|---|---|
| AAM-AUD-01 | Safety check found differences | The weekly safety check compared every file and found {n} difference(s) between your shared folder and the {leg} (missing {m}, changed {c}, extra {e}). | Support | Forward to support. Do not delete anything. The list is attached. |
| AAM-AUD-02 | Safety check could not run | The weekly safety check could not finish ({reason}). This is not a difference in your files. | Nothing / Support | Nothing if it runs next week. Tell support if it repeats. |

### 4.8 Backup program on the server

| Code | Title | What happened | Who | What to do |
|---|---|---|---|---|
| AAM-SYS-01 | Backup could not start | The backup could not start because another job was still running after {wait}. | Support | Forward to support if it repeats. |
| AAM-SYS-02 | Backup was interrupted | The backup was interrupted - the server restarted or the program was stopped while it ran ({when}). We cannot prove it finished. | You | If the server restarted for Windows Update, nothing; the next backup re-checks everything. Tell support if it repeats. |
| AAM-SYS-03 | Backup overdue | The {leg} has had no successful backup for {hours} hours (limit {limit} h). Either each try fails (see the other emails) or the backup is not starting at all. | You → Support | 1. Open the dashboard and look at the last runs. 2. Forward this email to support. |
| AAM-SYS-04 | Backup tool missing | A program the backup needs ({tool}) is missing on the server. | Support | Forward to support. |
| AAM-SYS-05 | Server disk almost full | The server's disk {disk} has only {free} free (needs {min}). | IT | Free space on the server disk; call support if unsure. |
| AAM-SYS-07 | Backup night missed | The server was switched off or busy at the scheduled backup time and the backup started too late to run without reaching into office hours. Nothing was changed. | Nobody → Support | Nothing needed; the next backup copies everything. Tell support if it repeats. |
| AAM-SYS-08 | Backup slot stuck | The backup could not start. The program believes another backup is still running, but none is: an earlier backup was cut off (e.g. the backup service restarted while it ran) and its place was never given back. Nothing was changed. | Support | Forward to support; no backup starts until support clears the slot. |
| AAM-SYS-06 | Unexpected program error | Something unexpected went wrong in the backup program ({error_type}). | Support | Forward to support - the attached report has the details. |
| AAM-SVC-01 | Backup program stopped | The backup program on the server is stopped and could not be restarted automatically. **No backups will run until it is fixed.** | Support | Forward to support now. |
| AAM-SVC-02 | Backup program stuck | The backup program did not stop or restart properly. | Support | Forward to support now. |
| AAM-SVC-03 | Automatic restarts paused | The backup program restarted {n} times in one hour; automatic restarts are paused to avoid a loop. No backups run until fixed. | Support | Forward to support now. |
| AAM-SVC-04 | Scheduler not responding | The part that starts backups on time did not answer and could not be repaired automatically. | Support | Forward to support now. |
| AAM-RPT-01 | *(retired, plan C P1)* | No longer sent: a period with no backup is reported by the weekly / monthly report itself ("action needed: ... no backup ran"). Kept in the catalogue only so old references resolve. | - | - |

Dashboard-only (cannot be emailed, by definition): **AAM-MAIL-01 Alert emails are not being delivered**
(shown as a red banner with the mail server's reason).

Mapping (built in code, never by parsing message text): the raising code attaches the fault code -
`HealthError` / `PermissionCheckError` / `WolTimeout` / `BudgetExceeded` get a `code` field at each raise site;
guards map by name (G1→SRC-03, G2→SRC-04/05, G3→DEL-01/02, backstop→DEL-03, G4→NAS-07/08,
G5→NAS-09/10, G6→ROL-01/02); `rclone_errors` category → CLD-01/02/03; sync verdicts → LAN-01..04 /
CLD-06..08; statuses SLOT_TIMEOUT/ABANDONED → SYS-01/02; anything without a code → **AAM-SYS-06**
(and a unit test fails if any raise site lacks a code).

## 5. What is attached (so support can find the reason)

Every PROBLEM / ACTION NEEDED / WARNING email carries:

1. **`AAM-support_<yyyymmdd-hhmm>_<code>.txt`** - always, plain text, UTF-8, readable in any mail program:
   ```
   AAM BACKUP - SUPPORT REPORT
   Fault code   : AAM-NAS-01  NAS not reachable
   Reference    : LAN-20261018-2100-3f9c   (run id 3f9c1e...)
   Firm / server: AAM / WIN-SRV2016 (10.10.151.20), Windows Server 2016
   Software     : aam-backup mvp-hardening d78c41d, rclone v1.74.3, robocopy 10.0.14393
   Status       : LAN_FAILED  (phase: pre, exit -1 = tool not started)
   When         : 18 Oct 2026, 9:00:01 PM IST -> 9:08:07 PM IST (8 min 6 s)

   CAUSE
   Cannot reach LAN host '10.10.151.53' on SMB port 445 after 8 attempt(s) (15s each, 15s apart).

   STEPS (time spent)
   pre   health 0.4 s OK | clock OK (+0.9 s vs time.google.com) | lan-access FAILED 486 s

   CHECKS (guards)       G1 PASS 580 files | G2 PASS ... | G3 not reached ...
   SETTINGS IN EFFECT    source E:\FY26-27 | target \\10.10.151.53\aam_nas\FY26-27 |
                         wol off | shutdown off | budget 4 h | max_attempts 1   (no passwords/keys)
   LAST 5 RUNS (LAN)     17 Oct 9:00 PM Completed ... | 16 Oct 9:00 PM Completed ...
   DATA SAFETY           Cloud last success 18 Oct 2:00 AM | NAS last success 17 Oct 9:00 PM
   SERVER                C: 1347 GB free | E: 200 GB free | services: server/agent/watchdog Running
   TOOL LOG (last 200 lines, error lines first)  ...
   FULL LOGS on the server: C:\BackupAgent\run_logs\2026-10-18\<run id>\  (kept 90 days)
   ```
2. **`AAM-logs_<yyyymmdd-hhmm>_<code>.zip`** - the run's full log folder (`run_logs/<day>/<run_id>/`:
   robocopy log converted to UTF-8, rclone JSON log, our agent log lines for this run, the run row as JSON).
   Not password-protected (Gmail rejects that). Only `.txt/.log/.json` inside.
3. For AAM-LAN-02 and AAM-AUD-01: **`..._files.csv`** - the list of affected files.
   **Not built yet (2026-10-18):** the failed files are in the full robocopy log inside the
   logs zip; the audit's file list stays in its detail. Follow-up.

**Size:** all attachments together at most `alerts.attachments_max_total_kb` (default **5120 KB**; base64 makes
it ~6.8 MB on the wire, under the common 10 MB company limit and Gmail's 25 MB). If the zip would exceed it,
it is left out and the support report says so and gives the folder path on the server.

**Attachment ladder (built, plan B):** the mail server is asked what it accepts (RFC 1870 `SIZE` in the
EHLO reply). The message is measured (base64 about 4/3) against min(server SIZE,
`alerts.attachments_max_total_kb`) and sent with the richest rung that fits: **full** (support report +
logs zip) -> **report only** -> **none** (the email says "Logs could not be attached; they are on the
server at <path>"). A 5xx rejection of a message with attachments (552 too large, or a filter refusing
the zip with 550/554) or a hang-up during DATA steps ONE rung down and resends at once, so an
attachment problem can never stop an alert. The rung delivered is stored in
`notifications.attachments_rung` and named in the "For support" block. Reports with a CSV use the
same ladder.

**Never attached / always removed:** the SMTP password, the dashboard password hashes, the Google key file and
anything that looks like a private key or token (redaction pass on every attachment, with a test that plants each
secret and checks it never appears).

**Privacy (owner decision D1, §9):** the logs contain **client file and folder names**. The zip and the file
list are attached only when `alerts.attach_file_names` is true.

## 6. Delivery behaviour

- **Two recipient lists (owner 2026-10-22).** `notifications.recipients` = the ALERTS list (problems, action
  needed, warnings, resolved, overdue, password-changed); `notifications.report_recipients` = the REPORTS list
  (weekly / monthly report, new-financial-year notice). An address may be in both lists, never twice in one. An
  empty list uses the other one (noted in the log; the dashboard shows an amber note when the alerts list is
  empty and a red one when both are). The test email and the password-changed notice go to both lists, each
  address once. `core.notify.resolve_recipients` is the only place that decides; every email states its audience.
  A recipient the mail server refuses makes the delivery FAILED and names the address.

- One email per run outcome (after our own retries), never per attempt (already so).
- **Incident threading:** the first email of an incident gets `Message-ID: <{reference}@aam-backup>`; its
  reminders and the RESOLVED email carry `In-Reply-To` / `References` to it and keep the same subject, so Gmail
  and Outlook show the story as one conversation. Built 2026-10-18: schema v3 adds
  `notifications.fault_code / message_id / thread_id`. Note: Gmail groups by subject too, so the
  reminders join the first email's conversation in Gmail, while the RESOLVED email (its own,
  clearer subject) is threaded by Outlook/Apple Mail but shown separately by Gmail.
- **Repeat rule:** the same unresolved problem (same leg + same code) is re-sent at most once per
  `alerts.repeat_cooldown_hours` (12 h) as a reminder ("Reminder 2 - still not fixed since ...");
  a new code or a RESOLVED starts a new incident and is always sent.
- Not switchable: ACTION NEEDED emails, AAM-SYS-03, AAM-AUD-01, AAM-SVC-*.
  Switchable (Advanced, typed confirmation): routine PROBLEM/WARNING emails.
- Headers: `Message-ID`, `Date`, `From` (one address), `To`, `Subject`, `Auto-Submitted: auto-generated`
  (RFC 3834 - stops out-of-office auto-replies), `X-AAM-Fault-Code`, `X-AAM-Reference` (support can search on them).

## 7. The emails, one by one

Placeholders in `{braces}`. Text in the top part is exactly what the reader sees.

### 7.1 Backup failed (PROBLEM) - one template, the fault code fills it

```
Subject: [AAM Backup] PROBLEM: Office NAS backup failed - NAS not reachable (AAM-NAS-01)

[red bar] PROBLEM - Office NAS backup did not run

Tonight's backup to the office NAS (18 Oct 2026, 9:00 PM IST) did not run:
the office backup box (NAS) did not answer on the network for 8 minutes.
Nothing was copied or deleted.

Is my data safe?
  Your files on the server ...... untouched
  Office backup (NAS) ........... last good copy 17 Oct 2026, 9:00 PM IST  (1 day old)
  Online backup (Google Cloud) .. last good copy 18 Oct 2026, 2:00 AM IST  (up to date)

What to do (you)
  1. Check that the NAS is switched on (its lights are on).
  2. Check the NAS network cable and the office switch/router.
  3. Open the dashboard and press "Run LAN Backup".
  4. If it fails again, forward this email to support.

What happens next
  The next backup runs automatically tonight, 19 Oct 2026, 9:00 PM IST.
  You get one email when it works again.

[ Open the backup dashboard ]  http://backup-server:8080

------------------------------------------------------------
For support: AAM-NAS-01 · ref LAN-20261018-2100-3f9c · status LAN_FAILED ·
exit -1 (tool not started) · server WIN-SRV2016 · version d78c41d ·
18 Oct 2026 9:00:01 PM IST. Cause and logs: see the attached support report.
```

The "What happened", "Who" and "What to do" lines come from the code's row in §4. "Is my data safe?" is
computed from `last_successful_run` for both legs plus the source check. "What happens next" uses the
schedule (cronsim) in IST.

### 7.2 Backup stopped to protect your files (ACTION NEEDED - approval)

```
Subject: [AAM Backup] ACTION NEEDED: Office NAS backup stopped to protect your files (AAM-DEL-01)

[red bar] ACTION NEEDED - your decision is needed

We stopped tonight's office NAS backup (18 Oct 2026, 9:00 PM IST) before it changed anything.
To match your shared folder, it would delete 1,240 of 5,800 files from the NAS (21%; limit 10%).
Usually this means a large folder was deleted or moved on the server.

Is my data safe?
  Yes. Nothing was copied or deleted. The 1,240 files are still in the office backup.

What to do (you)
  1. Ask your team: was a large folder deleted or moved on purpose today?
  2. NOT on purpose  -> do NOT approve. Call support; the files can be restored from the backup.
  3. On purpose      -> open the dashboard, read the details, type the confirmation words
                        shown there and press "Approve this backup once".

What happens next
  Until you decide, every office NAS backup stops at this point (nothing is deleted).
  This request stays open on the dashboard; we remind you every 12 hours.

[ Open the backup dashboard to decide ]

For support: AAM-DEL-01 · ref ... · status LAN_BLOCKED · approval 0b14b56b... · planned deletions 1,240 /
destination 5,800 / limits 500 files or 10% · ...  (support report + list of the files to be deleted attached)
```

Variants (same template, text from §4): AAM-SRC-03, SRC-04, DEL-02, DEL-03, NAS-07, NAS-08, NAS-09, NAS-10,
ROL-01, ROL-02. For **AAM-NAS-09 (new empty target)** the tone is not alarming: "This is expected after setting
up a new NAS or folder."

### 7.3 Only part of the changes were copied (WARNING - partial)

```
Subject: [AAM Backup] WARNING: Online backup only partly done - ran out of time (AAM-CLD-07)

Last night's online backup reached its time limit (6 hours) and stopped.
4,210 of 9,800 changed files were uploaded; the rest follow in the next backup.

Is my data safe? ... (three lines)
What to do: Nothing if this is the first full upload or after a day with many changes.
            If you get this email 3 nights in a row, tell support.
What happens next: The next online backup continues tonight, 19 Oct 2026, 2:00 AM IST.
```
Variants: AAM-LAN-02 (open files, with file list), AAM-LAN-03.

### 7.4 Backup did not finish cleanly (PROBLEM - suspect): AAM-LAN-04 / AAM-CLD-08
Headline: "Last night's {leg} ended in an unexpected way, so we cannot prove every file was copied."
What to do: "Forward this email to support. You do not need to do anything else; tonight's backup re-checks every file."

### 7.5 Size check after upload failed (PROBLEM): AAM-CLD-09
Headline: "After last night's upload, the online copy did not match your files ({detail in words, e.g. 580 files on
the server, 579 online})." What to do: forward to support. Next: tonight's backup re-checks.

### 7.6 Backup could not start / was interrupted (PROBLEM): AAM-SYS-01 / AAM-SYS-02
SYS-02 headline: "Last night's {leg} was interrupted at {time} - the server restarted or the backup program was
stopped while it ran." What to do: "If the server restarted for Windows Update, nothing. If this happens again,
tell support." Next: "The next backup re-checks everything, {next_run}."

### 7.7 Backup overdue (ACTION NEEDED): AAM-SYS-03
```
Subject: [AAM Backup] ACTION NEEDED: No successful online backup for 50 hours (AAM-SYS-03)

Your online backup has not completed successfully since 16 Oct 2026, 2:00 AM IST (50 hours). The next scheduled run
was due 17 Oct 2026, 2:00 AM IST and was not finished by 17 Oct 2026, 10:00 AM IST (next scheduled run + 6 h run
budget + 2 h grace). "Overdue" follows the chosen schedule: a weekly plan is not overdue after a day.
Either each try fails (you will have separate emails for those) or backups are not starting at all.

Is my data safe? Your server's files are untouched, but changes since 16 Oct are NOT in the online backup yet.
What to do: 1. Open the dashboard and look at "Recent Backups". 2. Forward this email to support.
What happens next: We remind you every 24 hours until a backup succeeds.
```
Not sent while a failure email about the latest missed run was already sent (it says the same); the dashboard
banner stays red. Repeats per `alerts.stale_repeat_hours` (24 h).

### 7.8 Weekly safety check (ACTION NEEDED / WARNING): AAM-AUD-01 / AAM-AUD-02
AUD-01 headline: "This week's safety check compared every file and found 3 differences between your shared folder
and the office NAS copy (2 missing, 1 changed)." Is my data safe: "Your server's files are untouched. The NAS copy
is not an exact copy right now." What to do: "Forward to support. Do not delete anything." Attached: file list.
AUD-02 headline: "This week's safety check could not finish ({reason in words: no internet / NAS not reachable}).
This does not mean your files differ." What to do: "Nothing; it runs again next Sunday, 10:00 AM IST."

### 7.9 NAS not switched off (WARNING): AAM-NAS-06
"The backup finished fine, but the NAS did not confirm it switched off. Please check it and switch it off by hand if
it is still on."

### 7.10 Financial year (ACTION NEEDED / INFO)
- AAM-CLK-03 / ROL-01 / ROL-02 as in §4.6, template 7.2 when an approval is needed.
- **New INFO email "New financial year started"** (today nothing is sent on success):
  "From 1 Apr 2027 your backups use the new folder FY27-28. The FY26-27 backups are kept unchanged.
  Nothing to do." (Owner decision D5.)

### 7.11 Backup program on the server (ACTION NEEDED): AAM-SVC-01..04
```
Subject: [AAM Backup] ACTION NEEDED: Backup program on the server is stopped (AAM-SVC-01)

The backup program on the server stopped at 18 Oct 2026, 3:12 PM IST and could not be restarted automatically.
No backups will run until it is fixed.

Is my data safe? Your files are untouched. The last good copies: NAS 17 Oct 9:00 PM, online 18 Oct 2:00 AM.
What to do: Forward this email to support now. (If you are comfortable doing it: restart the server.)
What happens next: We email you again when it is running.
```

### 7.12 RESOLVED
```
Subject: [AAM Backup] RESOLVED: Office NAS backup is working again (was AAM-NAS-01)

The office NAS backup completed successfully on 19 Oct 2026, 9:04 PM IST (582 files copied).
The problem reported on 18 Oct 2026, 9:08 PM IST (NAS not reachable) is fixed.
Nothing to do.
```
Sent in the same thread as the problem email. Also for the program on the server ("running again").

### 7.13 Weekly / monthly report (INFO)

Top of the email, plain words first; the table and CSV follow.
```
Subject: [AAM Backup] Weekly backup report 12-18 Oct 2026 - all backups OK
         (or: "- action needed: office NAS backup failed 3 times")

This week in one line: Both backups worked every night. The safety check on Sunday found no differences.

Online backup (Google Cloud)      7 of 7 nights OK   last good 18 Oct 2:00 AM   1,204 files uploaded
Office backup (NAS)               6 of 7 nights OK   last good 18 Oct 9:00 PM   1 problem: 15 Oct NAS not reachable (resolved)
Weekly safety check               OK - every file matched (18 Oct 10:00 AM)
Open items                        none   (or: "1 backup waiting for your decision on the dashboard")

What to do: Nothing.   (or the steps of the open item)
```
Then: per-leg table of every run of the period (not "last 10"), the CSV attachment, and the support line
(server, version). Periods (plan C, `core/report_period.py`):
- **Scheduled emails** = calendar periods, computed from the flow run's scheduled time: weekly = the
  previous Monday-Sunday, monthly = the previous calendar month (IST).
- **Dashboard downloads and the dashboard "send email" buttons** = the last 7 / 30 days up to now.
- A problem counts as resolved when a later good run exists in the FULL history, not only inside the
  period; rollover rows are not counted as backups; files are shown per leg, never summed across
  legs; an integrity row is shown only for an enabled leg.
- Every scheduled report is recorded in `notifications` (mode `report`); a failed delivery shows on
  the dashboard.

Previewing: `tools/preview_emails.py --to <address>` sends one sample of every email kind, fault
code, reminder / RESOLVED, report, password notice and attachment rung through the real composer and
transport with `[PREVIEW]` in the subject (no notification row, no cooldown); `--out <dir>` writes
`.eml` files. Samples live in `core/email_samples.py` (a test fails when a fault code has none).

### 7.14 No backups recorded (retired)
Replaced (plan C, P1): the weekly / monthly report is sent even when nothing ran; its subject and top banner say "action needed: <leg> - no backup ran" and the table row says "No backup ran in this period."

### 7.16 Password changed (INFO, plan E6)
"The Dashboard (or Advanced) password of the backup dashboard was changed by {who} at {time}. If this was not you, contact support." Sent to both lists (each address once); never contains a password.

### 7.15 Test email (INFO, new)
"This is a test from your backup system, sent by {who} from the dashboard on {time}. If you can read this,
backup emails reach you. Nothing to do." Recorded like every other email; shown on the Settings page.

## 8. Build plan (after owner approval)

1. `core/faults.py`: the catalog (code → title, severity, who, texts, steps) + `fault_for(...)`; raise sites
   attach codes; unit test: every code renders, every raise site has a code, unknown → SYS-06.
2. `core/email_compose.py`: the template of §7 (HTML + plain text) from the catalog + data-safety block +
   next run (cronsim, IST). Golden-file tests per code.
3. `core/support_bundle.py`: support report + logs zip + file list, redaction, size cap. Tests: planted
   secrets never appear, cap respected, UTF-16 robocopy log converted.
4. `core/notify.py`: headers (Message-ID, Date, Auto-Submitted, X-AAM-*), plain-text part, threading
   (`notifications.message_id` column), incident-based repeat rule, non-switchable classes.
5. Dashboard: fault code + plain title on each run row; "Send test email"; amber "not tested"; server-gone banner.
6. Reports: per-leg summary, all rows, calendar month.
7. Real-SMTP tests (`test_support/smtp_server`) for every email type; one real Gmail send per type on the box.

New config keys (default, bounds in the model):

| Key | Default | Notes |
|---|---|---|
| `alerts.attach_support_report` | true | the `.txt` |
| `alerts.attach_file_names` | true (owner D1) | logs zip + file lists |
| `alerts.attachments_max_total_kb` | 5120 (100-20480) | replaces `alerts.attachment_max_kb` (deprecated key) |
| `alerts.support_log_tail_lines` | 200 (20-5000) | tool log lines in the report |
| `dashboard.public_url` | required | §2 item 2 |

## 9. Owner decisions (answered 2026-10-18)

| # | Question | Decision |
|---|---|---|
| D1 | Attach logs that contain client file/folder names (zip + file lists)? | **Yes** (`alerts.attach_file_names: true`). |
| D2 | Support contact (name, phone, email) in every email? | **Not for now** - no contact keys, no contact line. |
| D3 | Also send support-only faults directly to support? | **Not for now** - no `support_recipients`; the reader forwards. |
| D4 | Subject prefix | **`[<firm_name> Backup]`**, e.g. `[AAM Backup] PROBLEM: ...` |
| D5 | INFO email when the financial year switches | **Yes** (§7.10). |
| D6 | Language | **English only** for the MVP. |
