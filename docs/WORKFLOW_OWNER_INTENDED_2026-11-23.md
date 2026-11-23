# Owner's intended workflow (dictated 2026-11-23)

Status: documentation only. No code was read or changed to write this file. Use it later to write tests and to check the code against the owner's intent.

Terms used in this file:
- FY = financial year.
- `.AAM_TARGET_MOUNTED` = the NAS marker file (dictated as ".am_mount").
- NAS = the backup server. Air-gapped, normally powered off.
- "TCS" = unclear, see Open points.

---

## 1. Verbatim notes

The owner's dictated text, copied unchanged (speech-to-text, so some words are garbled):

> I will tell a few things. Use a Haiku agent to just document this into an empty file. This is the actual workflow that I am expecting to use. There are two servers. One is the main server, one is the backup server, and also there is TCS. Basically, in the main server is where we deploy this program. So the NAS is powered off, and it's an air-gapped server, and it is only manually turned. The power is manually turned on only when required, and mostly what we do is we are going to go to the panel, or what is it, the UI, and from the UI we will click manually run sync, or like the button to manually sync the documents. For the 12 months, it will only sync one folder. Then for the 3 months which is left, that is from January 1st to April 1st, we will sync both the folders, and also the old FI, which has been running for 15 months, will only retire once the final backup has been copied, and if it is marked only, we will retire that file. The same goes for Cloud as well. And also we are planning for automating the power requirements such that at a specified time the switch will be turned on, and after which that will be coordinated with the automatic schedule of this. It will be, the power will be turned on 10 minutes earlier. That is done in a separate section that we are not part of this program. This is the intended flow, and then we will schedule it into our program. So the manual flow will turn on the computer or the NAS, which is the backup NAS, and the data will be transferred and it is turned off. At the same time, or after that, sorry, not at the same time, after that, we will send these files to Cloud also. And for about 3 months or until the files are marked as the final transfer is completed and verified, we will keep retrying as much times as needed in both Cloud and LAN. This is the intended workflow. And rest of the things like weekly and monthly reports are generated for there are 2 types of people to whom the mail is being sent. That is, one is the IT admin, or the people responsible, or the workers, and one is the partners, where only the weekly and monthly mails are being sent to the partners, and they are not spammed with anything else. That is the main feature that should be checked thoroughly. And also there is a email queue, right? The email queue must be such a way that the ones which are already fixed shall not be resent if some failure has happened. If it is recovered normally, just keep it in an audit log. We are the company who is deploying this, so we will do, we have an AMC, and we will usually read the logs once in 6 to 1 year. So keep the logs for at least 2 years, the retention of everything for at least 2 years. And then this is the workflow. So make sure this workflow is also tested once this production release is being pushed. This is my workflow. One is the complete manual workflow. One is a mixture between Cloud automated and manual LAN, then is fully automated. These are the workflows that we are going to face, and in every scenario this shall work. And also, if anything happens, the UI should have the override for that. If there is an issue with copying over the files, the reason should be displayed on the the UI as well. So that should be there. And also, we need to discuss if the manual override shall be there, but no need of manual override. I think it is worth retrying till it is done, rather than risk of losing data. Since what we only lose is some retries, which is anyways happening on idle period of the system. Then the reason for that shall be shown in the UI after 2 or 3 times, such that we can fix if it is a hardware issue or something. And also there are instances where the server itself may be turned off on January 1st or April 1st, or the NAS being turned off. So if something is turned off, the next cycle or the next time it is being turned on, or the program is being run, anything which is pending shall be done, like making the folder or anything else. So that is a priority. And also, if the FI folder is already created manually, then we shall take that if that is according to our terminology or the format. The same goes for everything. So it shall retry or pick up the client created one. In the NAS server, we are not expecting them to copy anything over to that. That is the real workflow. They are only copying from that. Either the backup would be not copied. They can restore from the NAS only manually. That is the only internet thing. Also, the newly created folder must have the .am_mount. Everything shall be set. And also, the FI rollover can manually create the new folder in our system also. So the force FI rollover or activation shall also be there. So make sure these are also documented for now, and we will later make tests for it and do a thorough understanding of this and check if in our flow some issues are there. Basically, this is an automatic program. The cloud section is automated and always running. This particular client is preferring manual local backups. So that is the only thing. The cloud backup shall be automatic, and the LAN backup shall be going to be automatic in the future. So that shall also be tested, and currently is manual. So manual flow also shall be tested.

---

## 2. The workflow in plain language

Notes on reading the dictation: "FI" is read as FY. "internet thing" is read as "intended thing" (probable speech-to-text error; marked here, not silently fixed). "am_mount" is read as `.AAM_TARGET_MOUNTED`.

### 2.1 Environment

1. There are two main systems, plus one unclear third system:
   - **Main server**: where this program is deployed and runs.
   - **Backup server = NAS**: an air-gapped machine. It is powered off by default. It is switched on only when needed.
   - **TCS**: mentioned once; its role is unclear (see Open points).
2. The NAS is powered on by hand (a power switch/panel), or later by an external power timer. That timer is outside this program.
3. The main server runs the program and the UI (the "panel").
4. Clients only copy FROM the NAS. Nothing is expected to be copied INTO the NAS by clients.

### 2.2 The three operating modes

5. **Mode A - fully manual**: the cloud copy and the LAN copy are both started by hand from the dashboard.
6. **Mode B - mixed (THE CLIENT'S CURRENT MODE)**: the cloud copy runs automatically; the LAN copy is started by hand from the dashboard.
7. **Mode C - fully automatic (future)**: the LAN copy is also scheduled. An external power timer switches the NAS on about 10 minutes before the scheduled run. That timer is outside this program. This program must be coordinated with that schedule.
8. The owner's summary: the cloud section is automated and always running. This client prefers manual local (LAN) backups. The LAN backup will become automatic in the future.

### 2.3 Manual LAN flow (the current client flow)

9. The user clicks the UI button "run sync" (manual sync).
10. If the NAS is off, the program wakes it / asks for power-on.
11. The program copies the data to the NAS.
12. The program verifies the copy.
13. The NAS is powered off.
14. Afterwards (NOT at the same time), the same files are also sent to the cloud.

### 2.4 Financial-year (FY) rules

15. During the first 12 months of a FY, only one FY folder is synced.
16. In the last 3 months of the FY (1 January to 1 April), BOTH the current FY folder and the next-year FY folder are synced.
17. The old FY (the one that has been running for 15 months) is retired ONLY after its final backup has been copied AND verified. Only then is it marked closed.
18. The same retirement rule applies to cloud and to LAN.
19. Both FY folders stay tracked until retirement.
20. Retrying: in both cloud and LAN, keep retrying as many times as needed, for about 3 months, or until the final transfer is marked complete and verified.

### 2.5 Retry and visibility

21. Retrying until the copy is done is preferred over any manual override. Only retries are lost, and they happen in idle time.
22. After 2 or 3 failed attempts, the UI must show the reason, so a hardware problem or other problem can be fixed.
23. If copying fails, the reason must be visible in the UI.
24. Manual override: the owner said no manual override is needed, but also said "we need to discuss it". This is an OPEN POINT, not a decision.

### 2.6 Catch-up after outages

25. The main server or the NAS may be off on 1 January or 1 April (the FY boundaries).
26. When the server is next started, or the NAS is next powered on, or the program is next run, anything pending must be done. Examples: creating the new FY folder, activation, rollover.
27. This catch-up has priority.

### 2.7 Folders

28. If the client has already created the FY folder by hand and it matches our naming and format, the program adopts it (it does not create a duplicate).
29. The same adoption rule applies to everything else (any folder the program would create).
30. Every newly created NAS folder must get the `.AAM_TARGET_MOUNTED` marker file.
31. Restore from the NAS is MANUAL ONLY. The program does not restore automatically.
32. The UI must offer "force FY rollover / activation". This creates the new FY folder in our system manually.

### 2.8 Mail and recipients

33. There are two recipient groups:
    - **Group 1 - internal**: IT admin, the people responsible, the workers, and the support company. They get alerts (and the reports).
    - **Group 2 - partners**: they get ONLY the weekly and monthly reports. They must never be sent anything else (no alerts, no failure mails, no spam). The owner calls this "the main feature that should be checked thoroughly".
34. Email queue rule: if a failure to send happens, alerts that are already fixed or recovered must NOT be resent.
35. If an alert recovered normally, it is kept only in an audit log (not mailed).

### 2.9 Retention

36. The deploying company has an AMC (maintenance contract). They read the logs about once every 6 to 12 months.
37. Keep logs and the retention of everything for at least 2 years.

### 2.10 Testing expectations

38. After the production release is pushed, test these workflows:
    - Mode A - fully manual.
    - Mode B - cloud automatic + LAN manual.
    - Mode C - fully automatic.
39. In every scenario the workflow must work.
40. The UI must offer overrides and reasons where needed (see 22-24 for the one override question still open).
41. The manual LAN flow must be tested now. The future automatic LAN flow must be tested when it exists.

---

## 3. Requirements checklist

Each row is one testable requirement. "Source" quotes the dictated sentence (the verbatim notes in section 1) it comes from. "Status in current code" is TO CHECK for every row; the code has not been read for this file.

| ID | Requirement (short, testable) | Source (dictated sentence) | Status in current code |
|----|-------------------------------|----------------------------|------------------------|
| W1 | Program is deployed on the main server; the NAS is a separate air-gapped backup server. | "in the main server is where we deploy this program" / "the NAS is powered off, and it's an air-gapped server" | TO CHECK |
| W2 | NAS is powered off by default and only powered on when required. | "The power is manually turned on only when required" | TO CHECK |
| W3 | System supports three modes: A fully manual, B cloud automatic + LAN manual, C fully automatic. | "One is the complete manual workflow. One is a mixture between Cloud automated and manual LAN, then is fully automated." | TO CHECK |
| W4 | Mode B (cloud automatic, LAN manual) is the client's current configuration. | "This particular client is preferring manual local backups." | TO CHECK |
| W5 | Cloud copy runs automatically and always (in modes B and C). | "The cloud section is automated and always running." | TO CHECK |
| W6 | In mode A, the cloud copy is started by hand from the dashboard. | "the cloud and LAN both started by hand" (interpreted from "complete manual workflow") | TO CHECK |
| W7 | The UI has a "run sync" button that starts a manual LAN sync. | "from the UI we will click manually run sync" | TO CHECK |
| W8 | A manual LAN run powers on the NAS if it is off. | "the manual flow will turn on the computer or the NAS" | TO CHECK |
| W9 | A manual LAN run copies the data to the NAS, then verifies the copy. | "the data will be transferred" / "verified" (see also W22) | TO CHECK |
| W10 | After a manual LAN run, the NAS is powered off again. | "and it is turned off" | TO CHECK |
| W11 | The cloud copy of the same files is sent AFTER the LAN run, never at the same time. | "At the same time, or after that, sorry, not at the same time, after that, we will send these files to Cloud also." | TO CHECK |
| W12 | Outside the last 3 months of the FY, only one FY folder is synced. | "For the 12 months, it will only sync one folder." | TO CHECK |
| W13 | From 1 January to 1 April, both the current FY folder and the next-year FY folder are synced. | "from January 1st to April 1st, we will sync both the folders" | TO CHECK |
| W14 | The old FY (running 15 months) is retired only after its final backup is copied AND verified. | "will only retire once the final backup has been copied" | TO CHECK |
| W15 | A retired FY is marked closed only after its final backup is copied and verified. | "if it is marked only, we will retire that file" (dictation garbled; meaning taken as "marked [complete and verified]") | TO CHECK |
| W16 | The same FY retirement rule applies to cloud and to LAN. | "The same goes for Cloud as well." | TO CHECK |
| W17 | Both FY folders stay tracked until the old FY is retired. | "both the folders" (section 2.4 item 19) | TO CHECK |
| W18 | Failed copies are retried in cloud and LAN, as many times as needed, for about 3 months or until the final transfer is marked complete and verified. | "for about 3 months or until the files are marked as the final transfer is completed and verified, we will keep retrying as much times as needed in both Cloud and LAN" | TO CHECK |
| W19 | Retrying until done is preferred over a manual override. | "I think it is worth retrying till it is done, rather than risk of losing data." | TO CHECK |
| W20 | After 2 or 3 failed attempts, the UI shows the reason for the failure. | "Then the reason for that shall be shown in the UI after 2 or 3 times" | TO CHECK |
| W21 | If copying fails, the reason is visible in the UI. | "If there is an issue with copying over the files, the reason should be displayed on the the UI as well." | TO CHECK |
| W22 | Copy verification result (pass/fail) is visible and recorded. | "verified" (from "the files are marked as the final transfer is completed and verified") | TO CHECK |
| W23 | The UI offers overrides wherever a workflow needs them. | "if anything happens, the UI should have the override for that." | TO CHECK |
| W24 | Manual override of the retry rule: decision pending (OPEN POINT, see section 4). | "we need to discuss if the manual override shall be there, but no need of manual override." | TO CHECK |
| W25 | If the server or the NAS was off on 1 January or 1 April, the next start or power-on runs pending work first. | "if something is turned off, the next cycle or the next time it is being turned on, or the program is being run, anything which is pending shall be done" | TO CHECK |
| W26 | Pending catch-up includes creating the new FY folder, activation and rollover. | "like making the folder or anything else" / "the FI rollover" (dictated "FI") | TO CHECK |
| W27 | Catch-up work has priority over normal work. | "So that is a priority." | TO CHECK |
| W28 | If the client already created the FY folder by hand and it matches our naming and format, the program adopts it. | "if the FI folder is already created manually, then we shall take that if that is according to our terminology or the format" | TO CHECK |
| W29 | The same adopt-instead-of-create rule applies to all other folders. | "The same goes for everything. So it shall retry or pick up the client created one." | TO CHECK |
| W30 | Clients never copy into the NAS; they only copy from it. | "In the NAS server, we are not expecting them to copy anything over to that. ... They are only copying from that." | TO CHECK |
| W31 | Restore from the NAS is manual only; the program does not restore on its own. | "They can restore from the NAS only manually." | TO CHECK |
| W32 | Every newly created NAS folder gets the `.AAM_TARGET_MOUNTED` marker file. | "the newly created folder must have the .am_mount." | TO CHECK |
| W33 | The UI offers "force FY rollover / activation", which creates the new FY folder in the program manually. | "the FI rollover can manually create the new folder in our system also. So the force FI rollover or activation shall also be there." | TO CHECK |
| W34 | Two recipient groups exist: group 1 (IT admin, responsible people, workers, support company) and group 2 (partners). | "there are 2 types of people to whom the mail is being sent ... one is the IT admin, or the people responsible, or the workers ... and one is the partners" | TO CHECK |
| W35 | Partners receive ONLY the weekly and monthly reports, and nothing else. | "only the weekly and monthly mails are being sent to the partners, and they are not spammed with anything else." | TO CHECK |
| W36 | Partner-only-reports rule is checked thoroughly (main feature to verify). | "That is the main feature that should be checked thoroughly." | TO CHECK |
| W37 | Group 1 receives the alerts (and the reports). | "one is the IT admin, or the people responsible, or the workers" (alerts part inferred from "rest of the things" context) | TO CHECK |
| W38 | Alerts that are already fixed or recovered are NOT resent after a mail-send failure. | "the ones which are already fixed shall not be resent if some failure has happened." | TO CHECK |
| W39 | An alert that recovered normally is kept only in an audit log. | "If it is recovered normally, just keep it in an audit log." | TO CHECK |
| W40 | Logs and the retention of all data are kept for at least 2 years. | "So keep the logs for at least 2 years, the retention of everything for at least 2 years." | TO CHECK |
| W41 | After the production release, test mode A (fully manual). | "make sure this workflow is also tested once this production release is being pushed" / "complete manual workflow" | TO CHECK |
| W42 | After the production release, test mode B (cloud automatic + LAN manual). | "a mixture between Cloud automated and manual LAN" | TO CHECK |
| W43 | After the production release, test mode C (fully automatic). | "then is fully automated" | TO CHECK |
| W44 | In every mode, the workflow works end to end. | "in every scenario this shall work." | TO CHECK |
| W45 | The manual LAN flow (W7-W11) is tested. | "So manual flow also shall be tested." | TO CHECK |
| W46 | The future automatic LAN flow is tested when it exists. | "the LAN backup shall be going to be automatic in the future. So that shall also be tested" | TO CHECK |
| W47 | In mode C, the program's LAN schedule is coordinated with an external power timer that switches the NAS on about 10 minutes before the run (timer is outside this program). | "the power will be turned on 10 minutes earlier. That is done in a separate section that we are not part of this program." | TO CHECK |

---

## 4. Open points / unclear

1. **"TCS"** - a third system, mentioned once, role unknown. Owner to explain. Not guessed.
2. **Manual override** - the owner said "no need" but also "we need to discuss it". Decide yes or no. (W24)
3. **Mode A cloud** - the verbatim notes say the cloud is "automated and always running", but mode A says the cloud is started by hand. Which is right for mode A? (W5, W6)
4. **What "retire" means for cloud** - closing the tracking only, or also archiving/deleting the cloud copy? The LAN meaning is also not defined in the notes. (W14-W16)
5. **Partners on failure** - the owner says partners get NO mail except weekly and monthly reports. Confirm this includes failure and recovery mails. (W35)
6. **Duration of retrying** - "about 3 months" of retrying: from when does the clock start, and what happens after the 3 months (keep trying, stop, alert)? Is "about 3 months" the same as "until marked complete and verified", or a fallback to it? (W18)
7. **Two-year retention scope** - does "everything" include the manifest DB rows, the run logs, the mail audit log and the service logs? Owner to confirm each. (W40)
8. **"Marked complete and verified"** - who or what sets this mark (the program automatically, or the owner on the UI)? (W15, W22)
9. **Force rollover** - what exactly happens if the FY folder already exists when the owner forces it (adopt, refuse, or error)? (W33)
10. **Mode C timing** - if the owner powers the NAS on by hand while the external timer is also set, what happens? (W47)
11. **"internet thing"** - read here as "intended thing". Owner to confirm.
12. **Verification scope** - which verification is meant (file count, size, checksum, or the robocopy/rclone check)? The notes only say "verified". (W9, W22)
