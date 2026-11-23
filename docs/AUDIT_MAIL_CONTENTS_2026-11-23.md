# Mail contents audit (2026-11-23)

Read-only audit of every email the AAM Backup app can send, on branch `fy-overlap`.
No tracked file was edited, no test or preview was run, no service was touched, nothing was committed.
The only file written is this one.

## 0. Scope, method, limits

**Read:** `core/notify.py`, `core/email_compose.py`, `core/email_samples.py`, `core/alert_class.py`,
`core/faults.py`, `core/report.py`, `core/attention.py` (stale wording), `core/fy_overlap.py`, `core/fy_rollover.py`
(messages), `core/guards.py` (approval texts), `core/backup_repository.py`, `core/db_identity.py`, `core/messages.py`,
`watchdog.py`, `flow.py` and `ui.py` (every `AlertEvent(` and `notify(` call, grepped), `docs/ARCHITECTURE.md` §5.7,
`docs/PLAN_ALERT_CONTROLS_2026-10-22.md`, `docs/PLAN_RECIPIENTS_AND_SCHEDULE_ALERTS_2026-10-22.md`,
`docs/INVESTIGATION_CLOUD_ONLY_MANUAL_LAN_2026-11-23.md`.

**Sample renderer:** `core/email_samples.py` renders one sample per fault code, plus a reminder, a RESOLVED, two
password notices and three report variants, all through the real `compose()`. It was read, not run. It does NOT
cover the non-fault mails (rollover/overlap INFO and ACTION, invalid-config, undelivered digest, short notice,
watchdog RESOLVED, test mail, the database alert). See X-3.

**Captured mails:** `C:\AAMTest\mails.jsonl` holds 12 mails from the QA sandbox (22-23 Nov 2026, FY26-27, host
`DESKTOP-N7II2M6`). They are cited as "captured #n" (line n of that file). Only subject, fault header and the plain
text part were read; no credential was printed.

**Not verified here:** live SMTP delivery, the exact text of the report HTML body, the support attachments
(`core/support_bundle.py`), and the contents of the manual-only LAN mode (being built by another agent; see section 5).

**Legend.** Verdict: `OK` = truthful, actionable, plain; `FIX` = change needed (severity in section 3); `N/A` = not
sent. Class = `core/alert_class.py` (switch that governs it). "Re-audit" = must be re-checked when the manual-only
leg mode lands (section 5).

---

## 1. Cross-cutting findings (apply to many rows)

- **X-1 Subject severity comes from the fault code, not from the event kind.** `email_compose.compose` takes
  `severity = fault.severity` whenever a fault code exists (email_compose.py:205). So a WARNING event with a
  PROBLEM or ACTION code is sent as PROBLEM or ACTION, and an escalated ACTION (rollover archive, after N days)
  still says WARNING. The class and switch still follow the event kind (alert_class.classify), so a reader can get
  an ACTION subject that is switched off by the WARNING switch, and the reverse.
- **X-2 "Next run" lines use the configured schedule, not the schedule in effect.** `_next_run` reads
  `config.schedule` (email_compose.py:105-112). The dashboard and watchdog use the schedule the scheduler actually
  registered (`attention._schedule_in_effect`, `db_meta.schedule_in_effect`). After a Settings change that has not
  been applied yet, the mail can name a time the scheduler will not use.
- **X-3 The sample renderer is not a faithful preview.** Every fault sample uses `status="SAMPLE"` (email_samples.py:34),
  so: the "Until you decide ..." line for blocked codes never appears; the "Details" line never appears; and "Is my
  data safe?" always reads "no good copy recorded yet" (db=None). The owner reviewing the samples sees different text
  from what real mails say. Also not covered: section D/E mails, the test email, the digest and the short notice.
- **X-4 "No good copy recorded yet" is also shown when the database cannot be read.** `_data_safety` catches any
  exception and sets `last = None` (email_compose.py:132-140). A read error is then reported as "no good copy yet",
  which is not true.
- **X-5 "You get one email when it works again" is shown on WARNINGS while the backup works.** `_next_text` (email_compose.py:144-159)
  applies this line to every leg/audit mail that is not blocked, including WARNINGs such as "Some files could not be
  copied", "NAS not confirmed off", "clock not checked", "audit could not run". The backup is fine in those cases.
- **X-6 Plain text repeats the dashboard link.** The HTML button link is followed by the URL in the text part, and the
  HTML-to-text step appends the href after the link text. Captured #12 shows the URL twice. Same in the digest.
- **X-7 Privacy: client file names are emailed.** The LAN partial message contains the robocopy listing, with client
  file names and paths (captured #6, for example `Clients\ledger 0.txt`, `Tax\GST\May\bank stmt 13.txt`). It is in the
  "For support" block, but it goes to external mailboxes. The FY start mail prints a full local folder path (`overlap:start`).
- **X-8 Secrets in the invalid-config mail (possible).** The text is the raw pydantic message (watchdog.py, the invalid-config
  path), and pydantic includes `input_value=` for a wrong-type field. A malformed password or key would be echoed
  (captured #8 shows `input_value=1`). The composer and the other templates do not print credentials by design; the
  sample set and the 12 captured mails contain none. Not checked line by line for every path.
- **X-9 Threading and storms: no storm found.** Subjects are stable per incident (reminders keep the subject and say
  "Reminder" in the body; RESOLVED adds "(was CODE)" and threads through In-Reply-To). Cooldowns are per key
  (notify.py:701-705, `stale_repeat_hours` for stale and overlap mails). The digest is capped by a watermark.
  Noise seen: PROBLEM followed by RESOLVED 9-11 min later after a restart (captured #4/#5, #9/#10).
- **X-10 RESOLVED is sent even when no problem mail was sent.** `flow._send_recovery_if_needed` only checks that the
  previous run failed (flow.py:393-415). Plan step E (PLAN_ALERT_CONTROLS) makes RESOLVED depend on a SENT problem
  mail; it is still open. Overdue (stale) mails never get a RESOLVED, because the previous run was a success or absent.

---

## 2. Mail table (one row per mail)

Subjects are shown as they are sent. Prefix `[Firm Backup]` (`[AAM QA Sandbox Backup]` in the sandbox) and FY tag
`[FY26-27]` are omitted in the table unless they matter. Label per family: office NAS = "Office NAS backup", cloud =
"Online backup (Google Cloud)", audit = "Weekly safety check", watchdog = "Backup program on the server", rollover and
overlap = "Financial-year switch", reports = "Backup reports", database = "Backup history database".

### A. Backup mails built from a fault code (`core/faults.py`, one mail per code)

Sent by `flow` failure/blocked/missed paths, `backup_repository` (abandoned), `watchdog` (service), `audit`, `rollover`.
Class: failure (PROBLEM/WARNING) or action (ACTION NEEDED). Audience: alerts. Dashboard link: yes for all.

| ID | Trigger | Kind / class | Subject | Verdict | Issue | Proposed wording / fix | Re-audit |
|---|---|---|---|---|---|---|---|
| AAM-SRC-01 | Shared folder missing (guard G1 / permissions) | PROBLEM / failure | PROBLEM: {label} - Shared folder not found | FIX (LOW) | Step 3 says "press the Run button" but there are two Run buttons | "Then press Run LAN Backup or Run Cloud Backup on the dashboard (whichever failed)." | no |
| AAM-SRC-02 | No read permission on the folder | PROBLEM / failure | ... No permission to read the shared folder | OK | | | no |
| AAM-SRC-03 | Shared folder empty (G1, blocked) | ACTION / decision | ACTION NEEDED: ... Shared folder is empty | OK | Blocked-line text is correct (X-3 only affects the preview) | | no |
| AAM-SRC-04 | Many files gone (G2, blocked) | ACTION / decision | ACTION NEEDED: ... Many files disappeared | OK | | | no |
| AAM-SRC-05 | Could not count folder files | ACTION / action | ACTION NEEDED: ... Could not count the shared folder's files | OK | | | no |
| AAM-NAS-01 | NAS did not answer | PROBLEM / failure | PROBLEM: Office NAS backup - NAS not reachable | OK | Step 4 "press Run LAN Backup" is right only while the NAS leg is scheduled | | YES (NAS off on purpose) |
| AAM-NAS-02 | Wake-up signal sent, NAS did not start | PROBLEM / failure | PROBLEM: ... NAS did not wake up | OK | Assumes the server woke the NAS on purpose; with a manual-only leg this is an on-demand wake | | YES |
| AAM-NAS-03 | NAS refused writes | PROBLEM / failure | PROBLEM: ... Not allowed to write on the NAS | OK | "Usually the NAS user's password was changed" is a guess; acceptable as "usually" | | no |
| AAM-NAS-04 | NAS full | PROBLEM / failure | PROBLEM: ... NAS is full | OK | | | no |
| AAM-NAS-05 | Could not measure NAS free space | PROBLEM / failure | PROBLEM: ... Could not check NAS free space | OK | | | no |
| AAM-NAS-06 | NAS not confirmed switched off (after a good backup) | WARNING / failure | WARNING: ... NAS was not confirmed switched off | FIX (MED) | "Your backup is fine" is vague; "You get one email when it works again" implies a failure (X-5). Captured: none (sample only) | Body: "The backup itself finished normally. The NAS ({nas}) did not confirm that it switched off." Next line: remove the "works again" sentence for WARNINGs | YES (manual-only shutdown wording) |
| AAM-NAS-07 | Backup target is this server (G4) | ACTION / decision | ACTION NEEDED: Office NAS backup - Backup target is on this server | FIX (HIGH) | Steps say "Do NOT approve", but the Details line from guard G5 (guards.py:265) says "Approve to initialise this location as the backup target" (captured #1). A reader can approve the server's own disk | Step 1 stays "Do NOT approve." Suppress guard G5's approve sentence when G4 blocks, or replace it with: "This is the server's own disk. Do not approve. Forward this email to support." (code change in guards.py, owner decision on which text wins) | no |
| AAM-NAS-08 | Target changed since last good backup (G4) | ACTION / decision | ACTION NEEDED: ... Backup target changed | OK | | | no |
| AAM-NAS-09 | New empty target (G5, marker missing) | ACTION / decision | ACTION NEEDED: ... New, empty backup target | OK | Captured #2 is consistent | | no |
| AAM-NAS-10 | Folder has files but no marker (G5) | ACTION / decision | ACTION NEEDED: ... Wrong disk or folder on the NAS | FIX (HIGH) | Steps say "Do NOT approve", but the Details line from guards.py:255-259 says "Approve only if you have confirmed this is the correct backup location" | Step 1: "Do NOT approve unless support has confirmed this folder is the correct backup location." | no |
| AAM-NAS-11 | Test file read-back mismatch | PROBLEM / failure | PROBLEM: ... NAS is not storing files reliably | OK | | | no |
| AAM-LAN-01 | Robocopy stopped with error | PROBLEM / failure | PROBLEM: Office NAS backup - Copy to the NAS failed | OK | "Next backup tries again automatically" (_NEXT_TRY) assumes a schedule | | YES (_NEXT_TRY) |
| AAM-LAN-02 | Some files not copied (robocopy exit bit 8) | WARNING / failure | WARNING: ... Some files could not be copied | FIX (HIGH, privacy X-7) | Support block shows the robocopy listing with client file names (captured #6). Body says "usually files that were open" as a cause, which is an assumption. "Tonight's backup tries them again" assumes a nightly run. X-5 applies | Body: "Some files could not be copied. Often files are open during the backup (for example in Tally or Excel). All other files were copied." Support: "Files not copied: N. The names are in the attached list." (no names in the body) | YES ("tonight") |
| AAM-LAN-03 | Office backup hit its time budget | WARNING / failure | WARNING: ... ran out of time | OK | "the rest follows in the next backup" | | YES (wording) |
| AAM-LAN-04 | Copy ended unexpectedly, cannot prove completeness | PROBLEM / failure | PROBLEM: ... did not finish cleanly | OK | "The next backup re-checks every file" | | YES (wording) |
| AAM-LAN-05 | Backup user lacks "Back up files" right | PROBLEM / failure | PROBLEM: ... Backup user lacks backup rights | OK | | | no |
| AAM-CLD-01 | Cannot reach Google | PROBLEM / failure | PROBLEM: Online backup (Google Cloud) - Cannot reach Google Cloud | OK | | | no |
| AAM-CLD-02 | HTTPS inspection (firewall/antivirus) | PROBLEM / failure | PROBLEM: ... Secure connection blocked by the firewall/antivirus | OK | Names a product as an example; acceptable | | no |
| AAM-CLD-03 | Google refused the key | PROBLEM / failure | PROBLEM: ... Google Cloud refused the login | OK | | | no |
| AAM-CLD-04 | Key file missing | PROBLEM / failure | PROBLEM: ... Cloud key file missing | OK | | | no |
| AAM-CLD-05 | No write right on the bucket | PROBLEM / failure | PROBLEM: ... Not allowed to save in the cloud | OK | | | no |
| AAM-CLD-06 | Upload stopped with error | PROBLEM / failure | PROBLEM: ... Upload failed | OK | _NEXT_TRY | | YES (_NEXT_TRY) |
| AAM-CLD-07 | Upload hit its time budget | WARNING / failure | WARNING: ... ran out of time | OK | "uploaded next time" and "takes several nights" are true only on a nightly run | | YES |
| AAM-CLD-08 | Upload ended unexpectedly | PROBLEM / failure | PROBLEM: ... did not finish cleanly | OK | "The next backup re-checks" | | YES (wording) |
| AAM-CLD-09 | Online copy size/number mismatch after upload | PROBLEM / failure | PROBLEM: ... Size check after upload failed | OK | | | YES (wording) |
| AAM-CLD-10 | Pre-upload check failed | PROBLEM / failure | PROBLEM: ... Could not check the online backup before starting | OK | _NEXT_TRY | | YES (_NEXT_TRY) |
| AAM-DEL-01 | Would delete many files (G3, blocked) | ACTION / decision | ACTION NEEDED: ... Backup would delete many files | OK | Blocked line correct | | no |
| AAM-DEL-02 | Could not count planned deletions | ACTION / action | ACTION NEEDED: ... Could not count planned deletions | OK | | | no |
| AAM-DEL-03 | Delete limit reached during the run | ACTION / action | ACTION NEEDED: ... Stopped at the delete limit | OK | "approve on the dashboard" after a stop mid-run: the approval re-run is the documented path | | no |
| AAM-CLK-01 | Server clock off vs internet time | PROBLEM / failure | PROBLEM: ... Server clock is wrong | FIX (LOW) | "press the Run button" (two buttons) | "Then press Run LAN Backup or Run Cloud Backup on the dashboard (whichever was stopped)." | no |
| AAM-CLK-02 | Clock could not be checked, backup ran | WARNING / failure | WARNING: ... Server clock could not be checked | FIX (MED, X-5) | "You get one email when it works again" although the backup ran | Drop the "works again" line for this code; say "Nothing is needed unless this repeats." | YES (next run) |
| AAM-CLK-03 | Clock behind the FY the backups use (rollover) | ACTION / action | ACTION NEEDED: Financial-year switch - Clock is behind - financial-year switch refused | OK | The support block says "w32tm /resync" (support only, acceptable) | | no |
| AAM-CLK-04 | Clock unproven, backup stopped (cloud) | PROBLEM / failure | PROBLEM: Online backup ... Server clock could not be checked - backup stopped | OK | | | no |
| AAM-ROL-01 | FY jumped more than one year (guard G6 path) | ACTION / action | ACTION NEEDED: Financial-year switch - Financial year jumped more than one year | OK | | | no |
| AAM-ROL-02 | Last backup of the old year incomplete (G6, and two other mails use this code) | ACTION / decision or action | ACTION NEEDED: Financial-year switch - Last backup of the old year incomplete | FIX (HIGH) | Steps say "Fix the backup problem first ... the switch then runs automatically". The two other mails using this code (M-15, M-16) ask for an approval or a support action. The code's steps are wrong for them | Split the code: new codes per situation (see M-15, M-16). Keep ROL-02 for the G6 case only | YES (G6 vs manual-only) |
| AAM-ROL-03 | Clock not proven, switch waits | ACTION / action | ACTION NEEDED: ... Clock not proven - financial-year switch waits | OK | RESOLVED that follows uses this code for any reason (M-27) | | no |
| AAM-ROL-04 | Switch could not be finished (generic step failure; also M-10, M-17, M-22) | ACTION / action | ACTION NEEDED: Financial-year switch - Financial-year switch could not be finished | FIX (HIGH) | Owner-known: after a power cut or restart the switch resumes next morning, yet the mail asks for action ("Forward this email to support"). Three other mails reuse this code for different causes (M-17 reentered, M-22 folder early): wrong steps for them | Severity WARNING for the first time; new wording in M-10. Keep ACTION only after N repeats (N = new config key, owner decision) | YES (rollover on 1 April) |
| AAM-ROL-05 | Archive move of old year failed | WARNING (or ACTION when escalated) / failure or action | WARNING: Financial-year switch - Old year not moved to cheaper storage yet | FIX (MED, X-1) | Escalation to ACTION (archive_alert_after_days) is not visible: the subject still says WARNING | Use the escalated severity in the subject when the event kind is ACTION_REQUIRED. Or a second code for the escalated mail | YES (final backup) |
| AAM-ROL-06 | Files saved in next year's folder that is not backed up yet | ACTION / action | ACTION NEEDED: Financial-year switch - Files in the new year are not backed up yet | OK | Steps match the text in fy_overlap.sibling_text | | no |
| AAM-ROL-07 | Folder next to the year folders has a wrong name | ACTION / action | ACTION NEEDED: Financial-year switch - A year folder has the wrong name and is not backed up | FIX (LOW) | Subject does not name the folder. Two misnamed folders get the same subject and thread together | Subject: `... Financial-year switch - folder "FY 27-28" is not backed up (AAM-ROL-07)` | no |
| AAM-AUD-01 | Weekly safety check found differences | ACTION / action | ACTION NEEDED: Weekly safety check - Safety check found differences | OK | "compared every file": true only when the check scope is "full" (audit results carry a scope field). Check before relying on it | | no |
| AAM-AUD-02 | Weekly safety check could not run | WARNING / failure | WARNING: Weekly safety check - could not run | FIX (MED, X-5) | "You get one email when it works again" for a check that did not run is acceptable, but the next line should name the next check | Keep, but say "The next safety check runs on {next}." (conditional on schedule) | YES (audit schedule) |
| AAM-SYS-01 | Backup could not start: another backup still running | PROBLEM / failure | PROBLEM: ... Backup could not start | FIX (LOW) | A busy slot retries by itself; PROBLEM is loud for that | WARNING: "Another backup was still running, so this one did not start. It is tried again automatically. Nothing is needed." | YES (_NEXT_TRY) |
| AAM-SYS-02 | Run interrupted (restart, power cut, Windows Update) | PROBLEM / failure | PROBLEM: ... Backup was interrupted | FIX (MED) | Restarts produce PROBLEM and then RESOLVED 9-11 min later (captured #4/#5, #9/#10). "Time" is the run's start time, not when it stopped (see M-09). "The next scheduled run" phrase in the support block | WARNING: subject "Backup was interrupted by a restart or power cut"; body "The backup stopped part-way because the server restarted or lost power. Your files are not changed. The next backup checks everything again." | YES ("next scheduled") |
| AAM-SYS-03 | Leg overdue (watchdog `stale:`) | ACTION / overdue | ACTION NEEDED: Office NAS backup - Backup overdue | FIX (MED) | "You get one email when it works again" and "runs automatically on <time>" assume the scheduler is running (it is not, in the case this mail is for). No RESOLVED follows (X-10). Also uses config cron (X-2) | Next line: "Nothing runs from this email. It repeats every {stale_repeat_hours} hours until a backup succeeds." Add RESOLVED when overdue ends (plan step E) | YES (core case) |
| AAM-SYS-04 | Backup tool missing on the server | PROBLEM / failure | PROBLEM: ... Backup tool missing | OK | | | no |
| AAM-SYS-05 | Server disk almost full | PROBLEM / failure | PROBLEM: ... Server disk almost full | OK | | | no |
| AAM-SYS-06 | Unexpected error, cause unknown (catch-all) | PROBLEM / failure | PROBLEM: ... Unexpected program error | FIX (HIGH) | Shown for a rollover WARNING with no fault code (M-20: the real cause is hidden), and for a LAN event that was not classified (captured #12 digest line). Misleading for the reader | Fix the mapping (M-20 gets its own code). Log why the LAN event was unclassified | no |
| AAM-SYS-07 | Night missed (scheduled time passed, server busy/off) | WARNING / failure | WARNING: ... night missed | FIX (LOW) | "started too late to run without reaching into office hours" is jargon-ish. "The next scheduled backup copies everything that changed" | "...started too late to finish before office hours." | YES (next run) |
| AAM-SYS-08 | Backup slot stuck (support) | PROBLEM / failure | PROBLEM: ... Backup slot stuck | OK | Support-only language, acceptable | | no |
| AAM-SVC-01 | Backup program stopped, auto restart failed | ACTION / action | ACTION NEEDED: Backup program on the server - Backup program stopped | OK | | | no |
| AAM-SVC-02 | Backup program stuck | ACTION / action | ACTION NEEDED: ... Backup program stuck | OK | | | no |
| AAM-SVC-03 | Automatic restarts paused (loop guard) | ACTION / action | ACTION NEEDED: ... Automatic restarts paused | OK | | | no |
| AAM-SVC-04 | Scheduler not responding | ACTION / action | ACTION NEEDED: ... Scheduler not responding | OK | "scheduler" is jargon but the steps are clear | | no |
| AAM-RPT-01 | "No backups recorded" | N/A | - | FIX (LOW) | Not emitted by any code path. Still in the catalog and in `email_samples.RETIRED` | Remove from the catalog (new codes never reuse a number; keep the number retired) | no |

### B. Non-fault mails (built directly, or with a fault-less AlertEvent)

| ID | Trigger (file:line) | Kind / class / audience | Subject | Verdict | Issue | Proposed wording / fix | Re-audit |
|---|---|---|---|---|---|---|---|
| M-01 | Watchdog critical (watchdog.py:406-413; stuck stop, live lock, start suppressed, restart failed) | ACTION / action / alerts | ACTION NEEDED: ... (SVC-01..04 texts) | OK | Technical text (sc queryex, PID) is in the support block only, as intended | | no |
| M-02 | Watchdog RESOLVED (watchdog.py:442-453) | RESOLVED / resolved / alerts | RESOLVED: Backup program on the server is working again | FIX (MED) | Body: "The Prefect API is healthy and AamBackupAgent is RUNNING again" - not plain language | Body: "The backup program on the server is running again. Nothing to do." | no |
| M-03 | Invalid config, watchdog direct send (watchdog.py invalid-config path, not an AlertEvent) | action / alerts (no firm prefix, no fault code) | Backup agent cannot start: config.yaml is invalid | FIX (HIGH, X-8) | Raw pydantic text with "input_value=" and an external URL; "fix it and restart AamBackupAgent" cannot be done by the reader; no statement that backups do not run; no firm name, no severity word, no fault code (captured #8) | Subject: "[{firm} Backup] ACTION NEEDED: Backup program cannot start - settings file has an error". Body: "The backup program cannot start, so no backups will run until this is fixed. Your files are not changed. Forward this email to support now." Support line: the settings location only (for example `lan.foo`), never the value | no |
| M-04 | Invalid config RESOLVED (watchdog.py via notify, `recovered:watchdog:invalid_config`) | RESOLVED / resolved / alerts | RESOLVED: Backup program on the server is working again | FIX (LOW) | Body "config.yaml is valid again; the agent can start." is jargon | "The settings file is fixed. The backup program can start again." | no |
| M-05 | Undelivered-alerts digest (notify.py:740-790, watchdog loop) | WARNING / failure / alerts | WARNING: 1 alert could not be delivered (between 2026-11-23T00:44 and 2026-11-23T00:44) | FIX (MED) | Raw ISO timestamps ("2026-11-23T00:44") not IST-formatted (notify.py:766-773); two dashboard links in text (X-6) | Subject: "[{firm} Backup] WARNING: 1 alert could not be delivered (23 Nov 2026, 12:44 AM to 12:44 AM IST)". Use format_ist. One dashboard link | no |
| M-06 | Short notice when a mail cannot be composed (notify.py:533-559) | kind word / as event / alerts | ACTION_REQUIRED: {title} | FIX (MED) | Raw kind names in the subject ("ACTION_REQUIRED:", "FAILURE:"); no dashboard link although `dashboard_urls` needs only the config; "details are in the backup program's logs" is not actionable | Subject uses the same words as the normal mails ("ACTION NEEDED:", "PROBLEM:"). Add the dashboard link line | no |
| M-07 | Reminder of an open incident (any key, compose.py:238-240) | same as the original | same subject as the first mail | OK | Subject stays the same so the thread holds. "Reminder" appears in the body | Limit of reminders (plan step D) not yet visible in the mail | no |
| M-08 | First success after a failed backup (flow.py:393-415, `recovered:{fy}{leg}`) | RESOLVED / resolved / alerts | RESOLVED: Office NAS backup is working again (was AAM-XXX) | FIX (MED, X-10) | Sent even when the failure mail was never sent (send_on_failure off, suppressed). Otherwise OK; body names the fixed problem | Plan step E: send only after a SENT problem mail | YES (stale case) |
| M-09 | Run whose process died (backup_repository.py:146-161, `backup:...:*_ABANDONED`, SYS-02) | PROBLEM / failure / alerts | PROBLEM: ... Backup was interrupted | FIX (MED) | "Time:" shows the run's start time (occurred_at=started_at), not when it stopped. "The next scheduled run copies what is missing" (re-audit) | Label the time "Started:" or use the stop time. Re-audit the schedule sentence | YES |
| M-10 | Rollover process died (backup_repository.py:146-155, `rollover:abandoned`) | PROBLEM / failure / alerts | PROBLEM: Financial-year switch - Financial-year switch could not be finished (AAM-ROL-04) | FIX (HIGH, owner-known) | Restart or power cut shows an alarm, and the title repeats the label. The message itself says the switch continues next morning, but the code's steps say "forward this email to support" | WARNING subject: "Financial-year switch paused - the server restarted or lost power". Body: "The switch to {to_fy} was paused because the server restarted or lost power while it ran. Your data is safe: nothing was deleted. The switch continues by itself each morning from where it stopped. Nothing is needed." Steps: "Nothing is needed." | YES (1 April) |
| M-11 | Stale leg (watchdog `_check_stale_legs`, key `stale:fy:leg`) | ACTION / overdue / alerts | ACTION NEEDED: Office NAS backup - Backup overdue | see AAM-SYS-03 | Same as SYS-03; also the subject does not show how late | | YES |
| M-12 | Blocked leg (flow.py:1187-1197, `blocked:fy:leg`) | ACTION / decision / alerts | ACTION NEEDED: ... approval needed (fault from guard) | OK | Body and steps match the approval flow. Inherits the contradictions of NAS-07 and NAS-10 through the Details line | | no |
| M-13 | NAS not confirmed off (flow.py:1408-1415, `nas_shutdown:ip`) | WARNING / failure / alerts | WARNING: Office NAS backup - NAS not confirmed switched off (AAM-NAS-06) | see AAM-NAS-06 | As NAS-06 | | YES |
| M-14 | Clock behind, switch refused (flow.py:2766-2770) | ACTION / action / alerts | ACTION NEEDED: Financial-year switch - Clock is behind (AAM-CLK-03) | OK | As CLK-03 | | no |
| M-15 | Old-year final backup waits for its own approval (flow.py:2882-2887, `blocked:rollover`) | ACTION / decision / alerts | ACTION NEEDED: Financial-year switch - Financial-year switch blocked - approval needed (AAM-ROL-02) | FIX (HIGH, risky) | Body: "approve or dismiss that backup request". Steps (ROL-02): "Fix the backup problem first ... the switch then runs automatically". Two instructions conflict; the owner may not approve and the switch stalls, or may approve without knowing the cause | Subject: "[{firm} Backup] ACTION NEEDED: Financial-year switch waits for your approval of the last {from_fy} backup". Body: "The switch to {to_fy} waits because the last {from_fy} backup needs your approval first. Nothing was changed and your data is safe." Steps: "1. Open the backup dashboard. 2. Approve or dismiss the waiting {from_fy} backup request. 3. The switch then asks for its own approval." Own fault code (new number) | YES (D-1: 1 April with NAS off) |
| M-16 | Settings changed by hand before the old year was backed up (flow.py:2741-2745, `rollover:switched_by_hand`) | ACTION / action / alerts | ACTION NEEDED: Financial-year switch - Last backup of the old year incomplete (AAM-ROL-02) | FIX (HIGH, risky) | Body tells the reader to ask support to change `source_drive` and `lan_destination` in config.yaml (jargon, and a support task). The ROL-02 steps say "fix the backup problem first ... the switch then runs automatically", which is wrong here. The title "last backup incomplete" is not the cause | Subject: "[{firm} Backup] ACTION NEEDED: Financial-year switch stopped - settings were changed before {from_fy} was fully backed up". Body: "The backup settings were changed to {to_fy} before the last {from_fy} backup finished, so the switch has stopped. Nothing was deleted or archived and your files are safe. Forward this email to support now: they set the folders back to {from_fy}, and the switch then finishes by itself." Steps: "1. Forward this email to support now. 2. Do not change the backup folder settings yourself." New fault code | YES |
| M-17 | Switch already finished, settings point back to the old year (flow.py:2752-2757, `rollover:reentered`) | ACTION / action / alerts | ACTION NEEDED: Financial-year switch - FY rollover not repeated (AAM-ROL-04) | FIX (HIGH) | Code ROL-04 says "tried again every morning and continues where it stopped". Not true here: support must act (the fy_rollover text says so). Body jargon "source_drive" and "lan_destination" | Subject: "[{firm} Backup] ACTION NEEDED: Financial-year switch not repeated - settings point back to {from_fy}". Body: "The switch to {to_fy} had already finished, but the backup settings point back to {from_fy} again. Nothing was backed up, switched or archived, and your files are safe. The switch will not run again until support sets the folders back to {to_fy}." Steps: "1. Forward this email to support now. 2. Do not change the folder settings yourself." New fault code | YES |
| M-19 | Clock not proven, switch waits (flow.py:2793-2797, `rollover:clock_*`) | ACTION / action / alerts | ACTION NEEDED: Financial-year switch - Clock not proven | OK | Daily retry is true (run every morning) | | no |
| M-20 | Old-year final backup waits for a free backup slot (flow.py:2853-2857, `rollover:{from}:final_wait`) | WARNING / failure / alerts, no fault code | PROBLEM: Financial-year switch - Unexpected program error (AAM-SYS-06) (rendered) | FIX (HIGH, misleading) | Code bug: no fault code, status ROLLOVER_FAILED, so the composer looks up the code from the status, finds none, and uses AAM-SYS-06 (email_compose.py:202-208 with effective_code 162-170). The reader gets PROBLEM, "Something unexpected went wrong", "Forward this email to support", and the real message (nothing needed, tried tomorrow) is hidden in the support block | Give it a code of its own (new number, WARNING, who NOBODY): subject "[{firm} Backup] [{from_fy}] WARNING: Financial-year switch - the last {from_fy} backup has to wait its turn"; body "The switch to {to_fy} is waiting because the last {from_fy} backup could not get its turn on the server today (a backup was running or it ran out of time). Nothing was changed and your data is safe. It is tried again tomorrow morning." Steps: "Nothing is needed." | YES |
| M-21 | Switch step failed (flow.py:2968-2972, `rollover:{to_fy}:{step}`) | ACTION / action / alerts | ACTION NEEDED: Financial-year switch - could not be finished (AAM-ROL-04) | see AAM-ROL-04 | | | YES |
| M-22 | FY folder could not be created early (flow.py:2985-2991, WARNING with ROL-04) | WARNING, but subject ACTION (X-1) / alerts | ACTION NEEDED: Financial-year switch - could not be finished (AAM-ROL-04) | FIX (MED) | Body says the switch "stopped part-way", but nothing has started: the new folder could not be created. Severity mismatch (X-1) | Subject: "[{firm} Backup] WARNING: Financial-year switch - the {to_fy} folder could not be created yet". Body: "The folder for {to_fy} could not be created yet: {error_short}. Until it exists, keep saving in {from_fy}. It is tried again every morning and before the switch. Your data is safe." Steps: "Nothing is needed unless this email repeats. If it repeats, forward it to support." New code | YES |
| M-23 | Archive move failed (flow.py:3039-3047) | see AAM-ROL-05 | | see AAM-ROL-05 | | | YES |
| M-24 | FY started (flow.py:2931-2937, `rollover:done`, INFO, reports list) | INFO / info / reports | INFO: New financial year FY27-28 started | FIX (HIGH, owner-known) | FY27-28 has been backed up since January (the activation mail), so "started" is misleading. "From today your backups use the new folder" is right, but the reader already has FY27-28 backups | Subject: "[{firm} Backup] INFO: {to_fy} is now the current backup year". Body: "From today the backups use the {to_fy} folder as the current year. {to_fy} has been backed up since {activated_on}. {from_fy} is kept unchanged. Nothing to do." Use the activation date from the database | YES (1 April with manual LAN) |
| M-25 | Next year's folder starts being backed up (fy_overlap.py:344-357, `overlap:start`, INFO, reports) | INFO / info / reports | INFO: FY27-28 is now backed up as well | FIX (HIGH, owner-known) | "Since <time> the nightly backups copy both folders" is not true for an office copy on demand (investigation: "nightly" false for LAN). Prints the full local folder path (X-7) | Body: "Since {date} the backup also copies {to_fy} (next year's folder) next to {from_fy} (this year's folder). {from_fy} is unchanged and still backed up. You can save next year's files in {to_fy} now. The backup never moves or changes your files. On 1 April the backups switch to {to_fy} by themselves." Folder name only, no full path. "nightly" only for legs that run nightly | YES |
| M-26 | Next year's folder deferred (fy_overlap.py:309-325, `overlap:activation`, ACTION, no code except clock) | ACTION / action / alerts | ACTION NEEDED: FY27-28 is not being backed up yet | FIX (LOW) | Body explains the reason but has no "What to do" steps, and no fault code except for the clock case | Add steps: "1. Read the reason above. 2. Fix it, or forward this email to support. Nothing is needed while {from_fy} is backed up as usual." Use a fault code per reason | no |
| M-27 | Deferred next-year folder now backed up (fy_overlap.py:328-342, `recovered:overlap:activation`) | RESOLVED / resolved / alerts | RESOLVED: Financial-year switch is working again (was AAM-ROL-03) | FIX (HIGH, misleading) | `resolved_code` is hard-coded to AAM-ROL-03 whatever the reason was (junction, no permission, folder not a folder). The subject and body then name the clock problem | Subject: "[{firm} Backup] [{to_fy}] RESOLVED: {to_fy} is now backed up". Body: "The problem that kept {to_fy} from being backed up is fixed. Both year folders are backed up." Code only when the deferral had one | no |
| M-28 | Misnamed folder / unprotected new-year files, daily (flow.py:2596-2601, `overlap:sibling` / `overlap:unprotected`) | ACTION / action / alerts | ACTION NEEDED: Financial-year switch - Files in the new year are not backed up yet (AAM-ROL-06) | OK | The text is plain and tells the reader where to save. Daily repeat via `stale_repeat_hours` (cooldown) | | no |
| M-29 | RESOLVED for a folder alarm (flow.py:2530-2577, `_send_sibling_resolved`, `recovered:overlap:*`) | RESOLVED / resolved / alerts | RESOLVED: Financial-year switch is working again (was AAM-ROL-07) | FIX (MED) | The folder is not the switch. The label says the switch is working again. For a folder fixed by a rename, "is fixed" is right; for "no longer there" it is also right. The subject is the problem | Subject: `[{firm} Backup] RESOLVED: Folder "{name}" is no longer a problem (was AAM-ROL-07)` | no |
| M-30 | Parent of the year folders could not be read (flow.py:3334-3339, `overlap:active_set`, WARNING, no code) | WARNING / failure / alerts | WARNING: Next year's folder was skipped tonight | FIX (MED) | Shows a raw exception text in the body; no fault code, no steps; "tonight only FY26-27 was backed up" may be false under a manual-only leg | Subject: "[{firm} Backup] WARNING: Next year's folder was not backed up tonight". Body: "The backup could not tell which year folders to copy tonight, so only {configured} was copied. Nothing was changed. The next backup tries again. Forward this email to support if it repeats." Exception text to the support block only | YES |
| M-31 | Backup database created new (db_identity.py:65-72, `db:recreated`, ACTION) | ACTION / action / alerts | ACTION NEEDED: Backup history database was created new | FIX (MED, misleading) | "You get one email when this is resolved" is false: acknowledging on the dashboard (ui.py:999-1008) sends no mail and no RESOLVED path exists for `db:` keys. "check for a restore mistake" is jargon | Last line: "After checking, press Acknowledge on the dashboard. No further email follows." Body: "If the database was restored from an old copy by mistake, forward this email to support." | no |
| M-32 | Weekly report, scheduled (flow.py:2392-2446, `build_summary_email`) and dashboard button (ui.py:1631-1648) | report / n/a / reports | [{firm} Backup] Weekly backup report {period} - {headline} | FIX (LOW) | Headline says "all backups OK" when no leg is switched on (report.py:285-287 falls through). No dashboard link in the body (the body says "open the dashboard") | Headline when no leg is switched on: "no backup leg is switched on in this period". Add a dashboard link | YES (shortfall, "N of M nights") |
| M-33 | Monthly report, scheduled and button (flow.py:2445-2458, ui.py:1671-1690) | report / n/a / reports | [{firm} Backup] Monthly backup report {period} - {headline} | FIX (LOW) | As M-32 | As M-32 | YES |
| M-34 | Test email from the dashboard (ui.py:1475-1492, `test:`, INFO) | INFO / test / both | INFO: Test email from your backup system | OK | Clear, says nothing to do | | no |
| M-35 | Dashboard / admin password changed (ui.py:1163-1177, `password:`, INFO) | INFO / security / both | INFO: Dashboard password changed | FIX (LOW) | "changed by 10.10.151.20 (session)" is jargon; "contact support" with no reason | "The Dashboard password was changed at {time} from computer {ip}. If this was not you, call support now." | no |
| M-36 | Weekly/monthly report via the report helpers (core/report.py:44-100, `send_failure_alert`) | PROBLEM (own subject "Backup Failure Alert - ...") | Backup Failure Alert - {firm} ({mode}) | FIX (LOW) | Not called from any production path (only tests may import it). It skips the switches, the cooldown, the recording and the composer. Risk if someone wires it up again | Delete the function | no |

### C. Reports and mails that are not AlertEvents

Report mails are M-32 and M-33 (scheduled: `flow._send_summary_report`, buttons: `ui.py` report endpoints).
Undelivered digest is M-05. Invalid config is M-03 and M-04. Password is M-35. Test is M-34. There is no other mail path.

---

## 3. Ranked FIX list (misleading or risky first)

Severity: **CRITICAL** = misleading and could make the owner act wrongly or miss a problem. **HIGH** = misleading or
wrong instructions. **MEDIUM** = unclear, noisy or inaccurate detail. **LOW** = wording or tidiness.

**CRITICAL**

1. **NAS-07 and NAS-10 tell the reader both "Do NOT approve" and "Approve ...".** The Details line comes from guards.py:255-265.
   Captured #1 shows the server's own disk offered for approval. Owner decision: which text wins; code change in guards.py.
2. **Rollover approval mail M-15 and settings-changed-by-hand mail M-16 use ROL-02 steps that tell the reader to fix the
   backup first, and say the switch then runs automatically.** Wrong instructions for both; a stalled switch or a wrong approval.
3. **M-20 final-backup-wait mail renders as PROBLEM "Unexpected program error (AAM-SYS-06)"** (email_compose effective_code
   gives no code for a rollover event with no fault code). The body says something unexpected went wrong, and the reader
   is asked to forward it. A code bug, not only wording.
4. **M-03 invalid-config mail echoes raw validation text** (possible secret echo via `input_value`), has no firm name, no
   severity word, no fault code, and asks the reader to edit config.yaml and restart a service. No statement that backups stop.
5. **X-2 and SYS-03: "runs automatically on <time>" and "The next scheduled run ..." are printed from the configured
   schedule, not the schedule in effect**, and for an overdue leg they claim a run that is not happening. Misleading
   (re-audit with the manual-only mode).

**HIGH**

6. **M-17 reentered and M-22 folder-early mails reuse ROL-04 ("continues every morning")** for causes where support must act.
7. **AAM-ROL-04 reboot/power-cut alarm (M-10, M-21, ROL-04)** - owner-known. Self-heals next morning, sent as ACTION
   NEEDED with "forward to support". Proposed WARNING "paused" wording.
8. **M-24 "New financial year FY27-28 started"** - owner-known. FY27-28 has been backed up since January; "started" misleads.
9. **M-25 "Since <time> the nightly backups copy both folders"** - owner-known. Also false for an on-demand office leg;
   full local folder path printed.
10. **M-27 RESOLVED for the activation always says AAM-ROL-03** (clock) whatever the reason was.
11. **X-1 subject severity from the fault, not the event** - ROL-05 escalated still reads WARNING; M-22 reads ACTION while the event is WARNING.
12. **M-31 "You get one email when this is resolved" for the database alert** - acknowledging sends no mail.
13. **Preview unfaithful (X-3) and coverage gaps** - the owner reviews the samples, and the samples differ from real
    mails (blocked lines, details, data-safety), and the rollover/overlap/invalid-config/digest/short notice/test mails
    are not previewed at all.
14. **LAN-02 support block carries client file names** (X-7, captured #6). Privacy.

**MEDIUM**

15. **SYS-02 restart PROBLEM followed by RESOLVED 9-11 min later** (captured #4/#5, #9/#10). Noise. Propose WARNING and
    the "Started:" label (M-09).
16. **X-5 "You get one email when it works again" on WARNINGs that did not stop the backup** (NAS-06, CLK-02, AUD-02, LAN-02, LAN-03, CLD-07, SYS-07).
17. **X-4 "no good copy recorded yet" shown when the database read fails.**
18. **SYS-03 overdue: no RESOLVED afterwards (X-10, plan step E still open).**
19. **M-08 RESOLVED sent without a prior problem mail (X-10, plan step E).**
20. **M-05 digest: raw ISO times, two links in text (X-6).**
21. **M-02 watchdog RESOLVED: "Prefect API", "AamBackupAgent" jargon.**
22. **M-06 short notice: raw kind words in the subject; no dashboard link.**
23. **M-29 folder-alarm RESOLVED labelled "Financial-year switch is working again".**
24. **M-30 `overlap:active_set`: raw exception text, no code.**
25. **M-32/M-33 headline "all backups OK" when no leg is switched on.**

**LOW**

26. AAM-SRC-01 and AAM-CLK-01: "press the Run button" (two buttons).
27. AAM-SYS-01: PROBLEM for a busy slot that retries by itself -> WARNING.
28. AAM-SYS-07: "reaching into office hours" -> plainer wording.
29. AAM-ROL-07 and sibling alarms: subject does not name the folder (threads merge).
30. M-09: "Time:" shows run start time, not stop time.
31. M-26: deferred activation has no "What to do" steps.
32. M-35: password notice jargon ("session") and no action.
33. X-6: dashboard URL printed twice in the text part (compose and digest).
34. M-04: invalid-config RESOLVED jargon ("config.yaml").
35. M-36 `send_failure_alert` dead code (delete), AAM-RPT-01 dead catalog entry (remove).
36. Reports have no dashboard link (M-32/M-33).

---

## 4. Proposed exact wording (FIX rows that need text)

Placeholders: `{firm}` firm name, `{from_fy}` / `{to_fy}` FY names, `{leg}`, `{nas}`. Times in IST via `format_ist`. No
technical words in the body; technical facts only in the "For support" block.

- **M-15 (old-year final backup waits for approval)**
  Subject: `[{firm} Backup] ACTION NEEDED: Financial-year switch waits for your approval of the last {from_fy} backup`
  Body: `The switch to {to_fy} waits because the last {from_fy} backup needs your approval first. Nothing was changed and your data is safe.`
  Steps (you): `1. Open the backup dashboard. 2. Approve or dismiss the waiting {from_fy} backup request. 3. The switch then asks for its own approval.`
- **M-16 (settings changed by hand)**
  Subject: `[{firm} Backup] ACTION NEEDED: Financial-year switch stopped - settings were changed before {from_fy} was fully backed up`
  Body: `The backup settings were changed to {to_fy} before the last {from_fy} backup finished, so the switch has stopped. Nothing was deleted or archived and your files are safe. Forward this email to support now: they set the folders back to {from_fy}, and the switch then finishes by itself.`
- **M-17 (settings point back to the old year)**
  Subject: `[{firm} Backup] ACTION NEEDED: Financial-year switch not repeated - settings point back to {from_fy}`
  Body: `The switch to {to_fy} had already finished, but the backup settings point back to {from_fy} again. Nothing was backed up, switched or archived, and your files are safe. The switch will not run again until support sets the folders back to {to_fy}.`
- **M-10 (switch paused after a restart or power cut)**
  Subject: `[{firm} Backup] WARNING: Financial-year switch paused - the server restarted or lost power`
  Body: `The switch to {to_fy} was paused because the server restarted or lost power while it ran. Your data is safe: nothing was deleted. The switch continues by itself each morning from where it stopped. Nothing is needed.`
  (ACTION only after repeated mornings; the number is a new config key, owner decision.)
- **M-20 (old-year final backup waits for a free slot)** - new code, WARNING
  Subject: `[{firm} Backup] [{from_fy}] WARNING: Financial-year switch - the last {from_fy} backup has to wait its turn`
  Body: `The switch to {to_fy} is waiting because the last {from_fy} backup could not get its turn on the server today. Nothing was changed and your data is safe. It is tried again tomorrow morning.`
  Steps: `Nothing is needed.`
- **M-03 (invalid settings file)**
  Subject: `[{firm} Backup] ACTION NEEDED: Backup program cannot start - settings file has an error`
  Body: `The backup program cannot start, so no backups will run until this is fixed. Your files are not changed. Forward this email to support now.`
  Support line: the settings location only (for example `lan.foo`); never the value.
- **M-24 (new year is the current year)**
  Subject: `[{firm} Backup] INFO: {to_fy} is now the current backup year`
  Body: `From today the backups use the {to_fy} folder as the current year. {to_fy} has been backed up since {activated_on}. {from_fy} is kept unchanged. Nothing to do.`
- **M-25 (next year's folder backed up as well)**
  Subject: `[{firm} Backup] [{to_fy}] INFO: {to_fy} is now backed up as well`
  Body: `Since {date} the backup also copies {to_fy} (next year's folder) next to {from_fy} (this year's folder). {from_fy} is unchanged and still backed up. You can save next year's files in {to_fy} now. The backup never moves or changes your files. On 1 April the backups switch to {to_fy} by themselves.`
  (Only say "every night" for legs that run every night; the office copy line depends on the manual-only mode.)
- **M-27 (deferred next-year folder now backed up)**
  Subject: `[{firm} Backup] [{to_fy}] RESOLVED: {to_fy} is now backed up`
  Body: `The problem that kept {to_fy} from being backed up is fixed. Both year folders are backed up.`
- **M-22 (folder for the new year could not be created)**
  Subject: `[{firm} Backup] WARNING: Financial-year switch - the {to_fy} folder could not be created yet`
  Body: `The folder for {to_fy} could not be created yet: {error_short}. Until it exists, keep saving in {from_fy}. It is tried again every morning and before the switch. Your data is safe.`
- **AAM-NAS-06 (NAS not confirmed off)**
  Body: `The backup itself finished normally. The NAS ({nas}) did not confirm that it switched off.` Steps unchanged.
- **AAM-LAN-02 (some files not copied)**
  Body: `Some files could not be copied. Often files are open during the backup (for example in Tally or Excel). All other files were copied.`
  Support: `Files not copied: {n}. The names are in the attached list.`
- **AAM-SYS-02 (interrupted by a restart)** - WARNING
  Subject: `[{firm} Backup] [{fy}] WARNING: {label} - Backup was interrupted by a restart or power cut (AAM-SYS-02)`
  Body: `The backup stopped part-way because the server restarted or lost power. Your files are not changed. The next backup checks everything again.`
  Steps: `Nothing is needed if the restart was planned (for example Windows Update).`
- **AAM-SYS-03 (overdue)** next line: `Nothing runs from this email. It repeats every {stale_repeat_hours} hours until a backup succeeds.` (Re-audit with the manual-only mode before using "runs automatically".)
- **M-31 (database created new)**: replace the last sentence with `After checking, press Acknowledge on the dashboard. No further email follows.`
- **M-05 (digest)**: Subject `[{firm} Backup] WARNING: 1 alert could not be delivered (23 Nov 2026, 12:44 AM to 12:44 AM IST)`; one dashboard link.
- **M-02 (watchdog RESOLVED)**: Body `The backup program on the server is running again. Nothing to do.`
- **M-35 (password)**: Body `The {name} password of the backup dashboard was changed at {time} from computer {ip}. If this was not you, call support now.`
- **AAM-NAS-07 Details / AAM-NAS-10 step**: see the table row. The owner decides which text wins in guards.py:255-265.

---

## 5. Mails that MUST be re-audited when the manual-only LAN leg lands

The new mode (`lan.scheduled: false`, per docs/INVESTIGATION_CLOUD_ONLY_MANUAL_LAN_2026-11-23.md) changes what counts as "due". Re-check each of these after the change:

- **Every lan/cloud/audit mail that prints "The next ... runs automatically on <time>"** (email_compose `_next_text`,
  lines 152-159). Under a manual-only leg there is no next run, so the line must not appear. Rows: all of section A for
  lan and cloud (`mode in lan/cloud`), plus AAM-SYS-02, SYS-07, CLK-02, AUD-02, NAS-06, M-08, M-09, M-13.
- **Sentences that promise a next backup**: "Tonight's backup tries them again" (LAN-02); "The next backup tries again
  automatically" (`_NEXT_TRY`: SYS-01, LAN-01, CLD-06, CLD-10); "The next backup re-checks every file" (LAN-04, CLD-08,
  CLD-09, SYS-02); "the rest is uploaded next time" (CLD-07); "The next scheduled run copies what is missing" (M-09, backup_repository.py:161); "The next scheduled backup copies everything that changed" (SYS-07).
- **Overdue and stale**: AAM-SYS-03 and M-11 (watchdog `_check_stale_legs`, `attention._overdue_item`). A manual-only
  LAN leg must not produce an "overdue" mail at all. Also "Scheduled backups are not completing" in the stale text.
- **Shortfall in reports**: M-32, M-33 (`report._shortfall`, "N of M scheduled nights have no backup record", "action needed: ... no backup ran").
- **Activation and FY mails**: M-25 ("nightly"), M-24 ("from today", activation date), M-26, M-28 (where the office copy is named), M-30 ("tonight").
- **FY rollover on 1 April with the NAS off**: M-15, M-16, M-17, M-20, M-21, M-22, AAM-ROL-02, AAM-ROL-04, M-10, M-27 (D-1 owner decision). The final LAN backup may become a warning instead of a G6 block.
- **NAS wake and shutdown mails**: AAM-NAS-01, AAM-NAS-02, AAM-NAS-06, M-13 (the planned "NAS was woken and left on" mail does not exist yet; re-audit when it lands).
- **Weekly and monthly report headline and body** (M-32, M-33, `report.py:260-287`).

---

## 6. Checks I could not finish (for the next session)

- Whether `core/support_bundle.py` attachments carry the same client file names (X-7). Not read.
- Whether `audit` "every file" is true in every scope setting (AAM-AUD-01).
- Whether acknowledging the database alert ever triggers a mail through another path (M-31). Grep found none.
- Whether the rollover "1 April" INFO mail (M-24) can fire when activation was never recorded (the text would then be wrong in a different way).
- Live rendering with `core/email_samples.py`: not run (per instruction). The findings above come from reading `compose()`.
