# Runbook: financial-year rollover days (owner)

**Covers:** 30 Dec 2026 to 5 Apr 2027. **Times are IST.** **Draft 2026-11-23 (updated the same day).**
**Sources:** `docs/ARCHITECTURE.md` (sections 5.4, 5.5, 5.9, 5.9b), `docs/RELEASE_BREAK_SESSION_2026-11-22_PART6.md`
(FIX 1, FIX 2, manual-only mode, button and watchdog fixes), `config.example.yaml`.
Anything not in those documents is marked **TO CONFIRM**.

**Times used below** are the `config.example.yaml` defaults: cloud backup 02:00, NAS (LAN) backup 21:00,
rollover check 06:00 daily, weekly report Monday 08:00, monthly report 1st at 08:30, audit Sunday 10:00.
TO CONFIRM your exact `schedule.*` values in `config.yaml`.

**Folder names used:** `FY26-27` = current year (old), `FY27-28` = next year (new).

---

## 1. Five rules for these weeks

1. **Do nothing unless the dashboard or an email asks you to.** The system is designed to keep going.
2. **Never change the Windows clock.** Activation and the switch refuse an unproven clock on purpose.
3. **Never rename, move or delete** `FY26-27` or `FY27-28` folders, or the NAS marker file `.AAM_TARGET_MOUNTED`.
   The only time you create a marker by hand is in section 8.3.
4. **Approve only what you understand.** Read the reason on the card first. Some blocks cannot be approved at all (section 8.3).
5. **Silence is not proof.** Use the checklist in section 5 every Monday and on the key days.

---

## 2. Weeks in between

**December (from 1 Apr to 31 Dec):** one card, one folder, one backup per night. Staff may already save into
`FY27-28`. Those files are **not backed up yet**: the daily folder check sends one action-needed email. It is a
reminder, not a fault. Do nothing; the files are backed up from 1 January. TO CONFIRM it clears by itself.

**January to March (overlap):**
1. Two cards each day, new year first. Each card has its own runs, approvals and alerts.
2. A problem on one year turns only that card red. The other card stays green. Do not treat that as "all fine".
3. Sunday 10:00: the weekly integrity check covers both years (one NAS wake for both).
4. Monday 08:00: the weekly email has one section per year.
5. 1 Feb and 1 Mar at 08:30: monthly email, one section per year.

---

## 3. Day by day

### Wed 30 Dec 2026
- **Automatic:** 21:00 NAS backup of `FY26-27` only (overlap is not active yet).
- **Expect:** nothing new. One card on the dashboard. No red banner.
- **Check:** the dashboard shows the last NAS run as success.
- **If not:** section 4 (table).
- **Do not:** create `FY27-28` under any other name (for example `FY 27-28`).

### Thu 31 Dec 2026
- **Automatic:** 02:00 cloud backup of `FY26-27`. 06:00 rollover check: daily folder check.
  21:00 NAS backup of `FY26-27` only.
- **Expect:** possibly one action-needed email "files in FY27-28 are not backed up yet" (section 2).
- **Check:** the email names `FY27-28`. The dashboard still shows one card.
- **If not:** no email is normal too.
- **Do not:** move the files into `FY26-27`. The app never moves files.

### Fri 1 Jan 2027 (activation day)
- **Automatic:** the first backup run on or after 1 January (02:00 cloud) **activates** `FY27-28`:
  checks the clock, creates or adopts `D:\FY27-28` (or your source drive's next-year folder), records activation,
  then backs up `FY27-28` and then `FY26-27`. 06:00 check retries activation if it is still missing.
  21:00 NAS backup: wakes the NAS, creates `\\nas\...\FY27-28` with a marker, backs up both years, shuts the NAS down once.
- **Expect:** an INFO email that `FY27-28` is now backed up as well (exact subject TO CONFIRM). Two cards by the evening.
- **Check (morning, 09:00):** dashboard has two cards; new year first. No red banner. The INFO email arrived.
  If the mail server was down that day, look for an "alerts could not be delivered" banner. The digest
  email (up to 72 h later) should bring the lost mail (TO CONFIRM).
- **Check (next morning):** the NAS was on and is off again. `FY27-28` shows a success on the NAS card. Both cards show success on 2 Jan.
- **If not:** section 4. Activation not done after 24 hours = action-needed email (`rollover.activation_alert_after_hours`).
- **Do not:** restart the server or stop services between 02:00 and 08:00 or 21:00 and 01:00 (backup window), or press **Back up now** repeatedly (8.4).

### Sun 3 Jan and Mon 4 Jan 2027
- **Automatic:** 10:00 integrity audit of both years (one NAS wake, one shutdown); Monday 08:00 weekly email, one section per year.
- **Expect:** no alert if everything matches. An audit alert names the year in its subject. The weekly email says no action needed.
- **If not:** section 4 (audit alerts).

### Jan to Mar 2027 (every week, including 28-29 Mar)
- **Automatic:** two cards nightly; Monday weekly email; Sunday 10:00 audit; 1 Feb and 1 Mar monthly email.
- **Check each Monday:** both sections in the weekly email. Dashboard: no red card, no approval waiting.
- **If not:** section 4. Approve only on the card of the year that is blocked.

### Wed 31 Mar 2027 (last overlap night)
- **Automatic:** 21:00 NAS backup of both years.
- **Check (morning 1 Apr):** the last night shows success for both cards.

### Thu 1 Apr 2027 (switch day)
- **Automatic:**
  1. 02:00 cloud backup still backs up **both** years (the switch is not done yet).
  2. 06:00 rollover check: final backup of `FY26-27` on the cloud and the NAS (the NAS is woken for it).
     A **manual-only** NAS that cannot be reached or woken is an expected warning, not a block. A **scheduled** NAS
     that cannot be reached still blocks the switch (approval card) (TO CONFIRM on the day).
  3. Creates or confirms the `FY27-28` folders (already exist since 1 January).
  4. Switches the configuration so `FY27-28` is the current year (automatic).
  5. Moves the old year's cloud copy `gs://<bucket>/FY26-27/` to the cheaper ARCHIVE storage class.
     Files stay in the cloud; only the storage class changes. TO CONFIRM restore time from ARCHIVE. TO CONFIRM
     whether early-deletion fees apply if `FY26-27` is not yet fully copied (owner decision open).
  6. NAS shut down if configured. One INFO announcement email (wording TO CONFIRM; open item W1).
  7. 21:00 NAS backup: `FY27-28`, and `FY26-27` too while it is not yet fully copied to the NAS.
- **Expect:** rollover card in Settings says the switch is done. Current folder `FY27-28`.
  **`FY26-27` closes (its card goes) only when every enabled leg has fully copied it.** If it has not, the card stays as
  "Last year - still being copied" with an amber item. That is not a fault: see section 8.1.
- **Check (09:00):** Settings > FY rollover card shows the switch done and the current folder `FY27-28`.
  Dashboard: no red banner. Archive step done (TO CONFIRM where it is shown).
- **If not:** section 4. A final-backup block needs your approval; the switch waits until then. Approve the leg
  first, then the "roll over anyway" request, only if the reason is understood (and only if the card allows it).
- **Do not:** edit `config.yaml` by hand to switch the year. Do not save new work in `FY26-27` after the switch
  (it stops being backed up once it is closed).

### Fri 2 Apr 2027 and after
- **Automatic:** 02:00 cloud and 21:00 NAS: `FY27-28` only, plus `FY26-27` until it is fully copied. Archive retried each morning if it failed.
- **Check:** one card (or two while `FY26-27` is still being copied), success each night. Archive step done within 3 days
  (after that an action-needed email).

### Sun 4 Apr and Mon 5 Apr 2027
- **Automatic:** audit (Sunday) and weekly email (Monday): `FY27-28`; `FY26-27` only if it is still being copied.
- **Check:** the weekly email has one section (`FY27-28`) and no "action needed".

---

## 4. Abnormal signs: what to do

Codes are shown as `AAM-ROL-xx` / `AAM-NAS-xx` in the emails. Some docs drop the `AAM-` prefix, and the
clock codes are CLK-03 (clock behind) and ROL-03 (not proven). TO CONFIRM each code against the email.

| You see | Meaning (from the docs) | What to do |
|---|---|---|
| Red banner on the dashboard | A card needs action (overdue, blocked, failed, or mail not delivered) | Open that card and read the reason. Do the matching row below |
| Card "overdue" (red) | No success for that year and leg for about 26 h on a daily schedule | Press **Back up now** for that leg once. Check the NAS is on and the network works. If it fails again, call (section 6) |
| Amber "FY26-27 is not yet fully copied to LAN / Cloud" | Old year not yet copied by every enabled leg (not a fault) | Section 8.1 |
| Amber "LAN: manual only, last backup ... N days ago" or "no leg runs automatically" | Manual-only leg: nothing runs by itself (not a fault) | Section 8.2 |
| "Alerts could not be delivered" banner | Mail server failed | Check the mail provider and password. The digest re-sends when mail works (TO CONFIRM) |
| Approval waiting on a card | A run was blocked by a safety guard | Read the reason (this table and 8.3). Approve **only** if the reason is understood and the card offers approval |
| Card says **"Do NOT approve"** | The block cannot be approved (NAS unreachable, target is this server, or a folder with files and no marker) | Do not approve (the button is gone or refused). Fix the cause, then press **Back up now**. See 8.3 |
| ACTION NEEDED email, subject names a year | Read the email body: it says what, whether data is safe, and the next step | Open the dashboard link in the email. Do not reply to the email |
| AAM-NAS-01 | NAS or share unreachable (not approvable) | Check NAS power, network and share name. Press **Back up now (NAS)** after the NAS is back |
| AAM-NAS-09 | NAS folder is **new and empty**, no marker yet | Approve only if it is the correct year folder. Approving creates the marker and the first copy then runs |
| AAM-NAS-10 | NAS folder **has files but no marker** (not approvable) | Section 8.3 (create the marker by hand, only if the folder is right) |
| AAM-NAS-07 | Backup target is this server's own disk (not approvable) | Do not approve. Call (section 6) |
| AAM-SRC-01 | Source folder missing or unreadable | Check the drive is connected. Do not recreate the folder by hand with a new name |
| AAM-ROL-02 | Final backup of the old year blocked (switch waits) | Approve the leg first, then "roll over anyway", only if the reason is understood |
| AAM-ROL-03 (ROL-03) / CLK-03 | Clock not proven (waits) / clock behind (switch refused) | Check the server clock and time sync. Do not change the clock by hand. Call if it does not clear |
| AAM-ROL-04 | Switch could not be finished (retried daily) | Read the reason. It usually self-heals the next morning. Call if it repeats 2 days |
| AAM-ROL-05 | Archive step failed (retried daily) | Nothing to approve. Action-needed email after 3 days. Call if it repeats |
| AAM-ROL-07 | A year-like folder is misnamed or an inactive newer folder holds files | Do not rename by hand without calling. Tell the developer the folder name |
| Guard G1 (empty source) | Source has no real files | Check the source drive. Do not approve until you know why it is empty |
| Guard G2 or G3 (shrink or mass delete) | Many files would be removed on the backup | Check whether files were really deleted or moved. Approve only if you agree |
| Guard G6 "roll over anyway" | Switch waits for a decision | See AAM-ROL-02 |
| Watchdog email (CRITICAL, or RECOVERED after one) | The backup service stopped or was restarted | Check the dashboard. Do not restart services during a run. A "restarted by the watchdog" mail: section 8.5 |
| Power cut | Run was cut off | Start the server. The next run continues; a reboot email may say the switch could not be finished (open item W2) |
| `/health` shows 503 | A red item exists | Read the dashboard reason (same as banner) |

---

## 5. Is everything fine? (one screen)

Tick each line. If any line fails, go to section 4.

- [ ] Dashboard: **no red banner**.
- [ ] Cards: **one** (Apr to Dec, from 1 Apr) or **two** (1 Jan to 31 Mar, and after 1 Apr while the old year is still being copied). Newest year first in January.
- [ ] Each card: last cloud and last NAS success **last night** (or within the overdue rule), for each leg that runs automatically.
- [ ] No approval waiting on any card. Monday weekly email: no "action needed" in any section.
- [ ] 1 Jan: the activation email arrived (or no "alerts could not be delivered" banner).
- [ ] 1 Apr, 09:00: Settings rollover card says the switch is done; config shows `FY27-28`; the old year is gone or "still being copied" (8.1).
- [ ] NAS is off after each NAS run (unless you asked it to stay on). `http://127.0.0.1:8080/health` shows healthy (TO CONFIRM the page content).

---

## 6. Who to call

- **IT contact (NAS, network, server):** TO CONFIRM name and phone. **Developer:** TO CONFIRM name and contact.
- **Mail provider:** TO CONFIRM (password or account only). **Google Cloud:** TO CONFIRM (bucket or key only).

Have ready: the dashboard reason (screenshot), the fault code (AAM-...), the date and time, and whether the NAS was on.

---

## 7. What NOT to do (summary)

1. Do not change the Windows clock or the time-sync setting.
2. Do not rename, move or delete the year folders, or the NAS marker `.AAM_TARGET_MOUNTED` (except section 8.3).
3. Do not edit `config.yaml` by hand for the year switch, the overlap, or the folder paths.
4. Do not stop or restart services during a backup window (02:00 to 08:00, 21:00 to 01:00) or during the 06:00 rollover check.
5. Do not approve a request you do not understand, or a request for the other year.
6. Do not approve a "Do NOT approve" card, an unreachable NAS, or the server's own disk.
7. Do not delete the old year's source folder to make the "not yet fully copied" note go away (section 8.1).
8. Do not switch the overlap off between January and March (the early year would stop being backed up; call first).
9. Do not rename or delete `manifest.db` (or its `-wal` and `-shm` files). A missing database is recreated empty and an alarm follows.
10. Do not press **Back up now** repeatedly. Wait for the result first.

---

## 8. Special cases (added 2026-11-23)

### 8.1 Old year not fully copied on 1 or 2 April

**What you see:** the old-year card shows "Last year - still being copied" with the note "FY26-27 is not yet fully copied
to LAN" (or "to Cloud"). An amber item is on the dashboard. The mail is **one WARNING** for that year (subject TO CONFIRM).
The dashboard, weekly email and monthly email all say the same thing.

**Why:** the switch to `FY27-28` still happens on 1 April. But `FY26-27` is only **closed** (tracking stops) when
**every enabled leg** has a full, successful copy of it made after 31 March. Until then both years are backed up
(scheduled legs nightly, manual legs by button). If one leg (usually the NAS) was off on 1 April, the year stays open.

**What to do:**
1. Check the cloud card first. If the cloud already has the year, only the NAS copy is missing.
2. **For a manual-only NAS:** turn the NAS on, then press **Back up now (NAS)** once. The run copies the open year(s)
   and shuts the NAS down afterwards (TO CONFIRM whether one press covers both folders, section 8.2).
3. **For a scheduled NAS:** the next 21:00 run tries again by itself. Press the NAS button only if you do not want to wait.
4. **It is done** when the amber item and the "still being copied" note are gone, the old card is closed, and a
   **RECOVERED** mail says the year is fully copied. That RECOVERED mail is sent once, when the year closes.

**What NOT to do:**
- Do not delete, rename or move `FY26-27` to make the note disappear. There is no automatic give-up: runs for a
  deleted source folder fail and the note stays. There is no "close year anyway" button yet (owner decision pending).
- Do not approve a request for the old year unless you understand it (it is shown on that year's card).

### 8.2 Manual-only days (a leg is set to manual: no automatic runs)

**What it means:** the office has set `lan.scheduled` (or `cloud.scheduled`) to `false`. Nothing runs for that leg at night,
and there are **no overdue or stale alarms** for it. The dashboard button is the only way to start it.

**How to start a backup by hand:**
1. Open the dashboard, the card for the year, and press **Back up now (NAS)** (or **(cloud)**).
2. **NAS button:** the system wakes the NAS, runs the backup, verifies it, then **shuts the NAS down**. This is the
   same every time, even if the NAS was already on before you pressed (TO CONFIRM the button text and the
   confirmation message on screen).
3. **Cloud button:** runs the backup and verify. No NAS involved.
4. The page waits up to about 3 minutes, then says the run continues. Watch the card. Do not press again (8.4).

**What the dashboard text means:**
- **"LAN: manual only, last backup <date>, N days ago"**: no automatic NAS run. The date is the last successful NAS backup.
  Text turns **amber** after the warn days (`lan.on_demand_warn_after_days`, default 30, range 1 to 365). Amber is a soft
  reminder: no red, no email. It means "press the button when you want a copy".
- **"Cloud: manual only ..."**: the same for the cloud leg (`cloud.on_demand_warn_after_days`).
- **"LAN: off"**: the NAS leg is switched off in the config. Not a fault.
- **Amber "no leg runs automatically"** (TO CONFIRM exact text): every enabled leg is manual. Nothing will back up tonight.
- No "Next scheduled backup" line appears for a manual leg. That is correct.

**Points to know:**
- A manual NAS run makes the NAS copy match the source, so files deleted from the source are removed from the NAS copy too.
  Read the confirmation text first (TO CONFIRM it says this; a review noted it may not). A run after weeks away can be large.
- If a manual NAS run **fails** and the NAS stays on, an amber item **"NAS was woken and left on"** shows until the next run.
  Turn the NAS off (TO CONFIRM the method) or call (section 6).
- The 1 April rollover and the Sunday audit still try the NAS. If it is off, that is expected. The audit then says
  "not run: the NAS is not reachable (LAN is manual only)" with no alert mail.

### 8.3 NAS folder has files but no marker (block NAS-10), and what the approval means

**Background:** before a NAS folder is used, the system checks for an empty marker file named `.AAM_TARGET_MOUNTED`.
This stops a backup from writing into the wrong disk. A **new, empty** folder gets its marker from the approval. A folder
**with files and no marker** cannot be approved from the dashboard any more. The card says **"Do NOT approve"** and
offers only **Dismiss**.

**Which blocks can be approved:**

| Card says | Approvable from the dashboard? | Why |
|---|---|---|
| NAS-09: new **empty** folder, no marker | **Yes** | Approving creates the marker and the first copy then runs. Check it is the right NAS folder first |
| NAS-08: backup destination changed | Yes (as before) | Read the reason |
| NAS-10: folder **with files**, no marker | **No** | The copy would make the folder match the source and **delete** files there that are not on the source |
| NAS-01: share unreachable or marker cannot be checked | **No** | Target is not known to be mounted |
| NAS-07: target is this server's own disk | **No** | Would copy onto the server itself |

**When the right folder has files but no marker** (for example a new year folder copied in by hand):
1. Be sure the folder on the NAS is the correct year folder for this client. If not, do not continue.
2. On the NAS, in that folder, create an **empty** file named exactly `.AAM_TARGET_MOUNTED` (leading dot, no extension,
   zero bytes). TO CONFIRM the easiest method on your NAS (for example Notepad "Save as", then clear the text).
3. Press **Back up now** for that leg. The next backup checks the marker, and the block should clear on that run
   (TO CONFIRM on a real NAS; the docs do not say which run removes the block).
4. If the card still says "Do NOT approve", stop and call (section 6).

**Never:**
- Never approve an unmounted target (NAS-01) or the server's own disk (NAS-07). The button is gone or refused for those.
- Never create the marker in a folder you are not sure about, and never delete or move a marker that exists.

### 8.4 Double clicks and "the backup service is not running"

- **Second click:** a second press while a backup is starting or running is refused with **"already running"**.
  Nothing is started twice. Wait for the first result. The dashboard buttons also ignore a second click while the first
  request is still in flight.
- **"The backup service is not running, nothing was started"**: this means the **agent service** (the part that
  runs backups) has not answered for about 2 minutes. No run was created and the NAS was **not** woken.
  1. Open Windows **Services** and check **AamBackupAgent** (and **AamPrefectServer**) says Running.
     Normally the watchdog starts a stopped service again within a few minutes (TO CONFIRM the time).
  2. If a service is stopped and does not come back, call (section 6). Do not restart services during a backup window.
  3. When the service is Running, press the button once.
- **A manual run that starts too late** (more than 30 minutes after the press, `dashboard.manual_start_max_wait_minutes`)
  is recorded as **Missed** and the NAS is not woken for it. Press again if you still need the copy.

### 8.5 Mail: "the backup service was restarted by the watchdog"

- **What you get:** one **WARNING** mail (fault AAM-SVC-05) saying the watchdog restarted a backup service (name given)
  after about N minutes down, then one **RECOVERED** mail when it runs again. It says **no action needed unless it repeats**.
- **What to do:** nothing, if it happens rarely. Check the dashboard shows no red card after the RECOVERED mail.
- **If it repeats** (the same service again and again): call the developer (section 6). Mails for the same service
  are limited to one pair per repeat window (`alerts.repeat_cooldown_hours`).
- While the agent is down the dashboard is down too, so mail is the only channel. Nothing alerts you if the
  watchdog itself is down, and a server that is switched off sends nothing (accepted risk; check the server if you
  hear nothing for a long time).

### 8.6 Start next year's folder early (Settings button)

Settings, FY card: **Create next year's folder now (force FY activation)**. Use it only when next year's files
must be saved before 1 January (a client sends next year's papers early). Switch the NAS on first: the button never wakes it.
It creates next year's folder on the NAS (with its marker) and in the source folder, and from then on both years are
backed up until the switch on 1 April. It does not switch the year and deletes nothing. A folder that already exists with the
right name (FY27-28) is used as it is; a wrongly spelled one (FY 27-28, FY2027-28) is refused: rename it and press again.
A NAS folder that has files but no marker is left untouched (block NAS-10, see 8.3). Pressing twice changes nothing
and says so. It is refused while a backup is running, when the NAS is off, or when overlap is switched off in config.yaml.
