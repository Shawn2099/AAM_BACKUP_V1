# Runbook: financial-year rollover days (owner)

**Covers:** 30 Dec 2026 to 5 Apr 2027. **Times are IST.** **Draft 2026-11-23**, from the design and test docs
(`docs/PLAN_FY_OVERLAP_2026-11-20.md`, `docs/ARCHITECTURE.md`, Parts 1 to 6 of the release break session).

**Times used below** are the `config.example.yaml` defaults: cloud backup 02:00, NAS (LAN) backup 21:00,
rollover check 06:00 daily, weekly report Monday 08:00, monthly report 1st at 08:30, audit Sunday 10:00.
The trial box used 02:01 and 21:01 (plan). TO CONFIRM your exact `schedule.*` values in `config.yaml`.

**Folder names used:** `FY26-27` = current year (old), `FY27-28` = next year (new).

---

## 1. Five rules for these weeks

1. **Do nothing unless the dashboard or an email asks you to.** The system is designed to keep going.
2. **Never change the Windows clock.** Activation and the switch refuse an unproven clock on purpose.
3. **Never rename, move or delete** `FY26-27` or `FY27-28` folders, or the NAS marker `.AAM_TARGET_MOUNTED`.
4. **Approve only what you understand.** Read the reason on the card first. Approvals are typed on the dashboard.
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
  The 08:30 monthly email (December) has no `FY27-28` section yet.
- **Check (morning, 09:00):** dashboard has two cards; new year first. No red banner. The INFO email arrived.
  If the mail server was down that day, look for an "alerts could not be delivered" banner. The digest
  email (up to 72 h later) should bring the lost mail; TO CONFIRM it is in the build.
- **Check (next morning):** the NAS was on and is off again. `FY27-28` shows a success on the NAS card.
- **If not:** section 4. Activation not done after 24 hours = action-needed email (`rollover.activation_alert_after_hours`).
- **Do not:** restart the server or stop services between 02:00 and 08:00 or 21:00 and 01:00 (backup window).

### Sat 2 Jan 2027
- **Automatic:** 02:00 cloud and 21:00 NAS, both years.
- **Check:** both cards show success for last night.
- **Do not:** press **Back up now** repeatedly. A manual run is refused while another backup is running.

### Sun 3 Jan 2027
- **Automatic:** 10:00 integrity audit of both years (one NAS wake, one shutdown).
- **Expect:** no alert if everything matches. An audit alert names the year in its subject.
- **If not:** section 4 (audit alerts).

### Mon 4 Jan 2027
- **Automatic:** 08:00 weekly email, one section per year (`FY27-28` counted from 1 January).
- **Check:** the email says no action needed for either section.

### Jan to Mar 2027 (every week)
- **Automatic:** two cards nightly; Monday weekly email; Sunday audit; 1 Feb and 1 Mar monthly email.
- **Check each Monday:** both sections in the weekly email. Dashboard: no red card, no approval waiting.
- **If not:** section 4. Approve only on the card of the year that is blocked.

### Sun 28 Mar and Mon 29 Mar 2027
- **Automatic:** 10:00 audit on Sunday (both years). Monday 08:00 weekly email (both years).
- **Check:** same as above.

### Wed 31 Mar 2027 (last overlap night)
- **Automatic:** 21:00 NAS backup of both years.
- **Check (morning 1 Apr):** the last night shows success for both cards.

### Thu 1 Apr 2027 (switch day)
- **Automatic:**
  1. 02:00 cloud backup still backs up **both** years (the switch is not done yet).
  2. 06:00 rollover check: final backup of `FY26-27` on the cloud and the NAS (the NAS is woken for it).
  3. Creates or confirms the `FY27-28` folders (already exist since 1 January).
  4. Switches the configuration so `FY27-28` is the current year (automatic).
  5. Moves the old year's online copy `gs://<bucket>/FY26-27/` to the cheaper ARCHIVE storage class.
     The files stay in the cloud; only the storage class changes. TO CONFIRM restore time from ARCHIVE.
  6. NAS shut down if configured. One INFO announcement email (wording TO CONFIRM; open item W1).
  7. 21:00 NAS backup: `FY27-28` only. 08:30 monthly email (March) covers both years.
- **Expect:** rollover card in Settings says the switch is done. One card (`FY27-28`). `FY26-27` card gone.
- **Check (09:00):** Settings > FY rollover card shows the switch done and the current folder `FY27-28`.
  Dashboard: one card. No red banner. Archive step done (TO CONFIRM where it is shown).
- **If not:** section 4 (rollover codes AAM-ROL-02, -04, -05). A final-backup block needs your approval;
  the switch waits until then. Approve the leg first, then the "roll over anyway" request, only if the reason is understood.
- **Do not:** edit `config.yaml` by hand to switch the year. Do not save new work in `FY26-27` after the switch
  (it is no longer backed up, accepted rule).

### Fri 2 Apr 2027 and after
- **Automatic:** 02:00 cloud and 21:00 NAS: `FY27-28` only. Archive retried each morning if it failed.
- **Check:** one card, success each night. Archive step done within 3 days (after that an action-needed email).

### Sun 4 Apr and Mon 5 Apr 2027
- **Automatic:** audit (Sunday) and weekly email (Monday): `FY27-28` only; `FY26-27` is closed.
- **Check:** the weekly email has one section (`FY27-28`) and no "action needed".

---

## 4. Abnormal signs: what to do

Codes are shown as `AAM-ROL-xx` / `AAM-NAS-xx` in the emails. Some docs drop the `AAM-` prefix, and the
clock codes are CLK-03 (clock behind) and ROL-03 (not proven). TO CONFIRM each code against the email.

| You see | Meaning (from the docs) | What to do |
|---|---|---|
| Red banner on the dashboard | A card needs action (overdue, blocked, failed, or mail not delivered) | Open that card and read the reason. Do the matching row below |
| Card "overdue" (red) | No success for that year and leg for about 26 h on a daily schedule | Press **Back up now** for that leg once. Check the NAS is on and the network works. If it fails again, call (section 6) |
| "Alerts could not be delivered" banner | Mail server failed | Check the mail provider and password. The digest re-sends when mail works (TO CONFIRM) |
| Approval waiting on a card | A run was blocked by a safety guard | Read the reason (table below). Approve **only** if the reason is understood. Approvals are typed on the card (phrase TO CONFIRM) |
| ACTION NEEDED email, subject names a year | Read the email body: it says what, whether data is safe, and the next step | Open the dashboard link in the email. Do not reply to the email |
| AAM-NAS-01 | NAS or share unreachable | Check NAS power, network and share name. Press **Back up now (NAS)** after the NAS is back |
| AAM-NAS-09 or -10 | NAS folder has no mount marker (or is non-empty without one) | Approve only if the folder is the correct year folder. The approval recreates the marker |
| AAM-SRC-01 | Source folder missing or unreadable | Check the drive is connected. Do not recreate the folder by hand with a new name |
| AAM-ROL-02 | Final backup of the old year blocked (switch waits) | Approve the leg first, then "roll over anyway", only if the reason is understood |
| AAM-ROL-03 (or ROL-03) | Clock not proven (activation or switch waits) | Check the server clock and time sync. Do not change the clock by hand |
| CLK-03 | Clock behind (switch refused) | Same as above; call if it does not clear |
| AAM-ROL-04 | Switch could not be finished (retried daily) | Read the reason. It usually self-heals the next morning. Call if it repeats 2 days |
| AAM-ROL-05 | Archive step failed (retried daily) | Nothing to approve. Action-needed email after 3 days. Call if it repeats |
| AAM-ROL-07 | A year-like folder is misnamed or an inactive newer folder holds files | Do not rename by hand without calling. Tell the developer the folder name |
| Guard G1 (empty source) | Source has no real files | Check the source drive. Do not approve until you know why it is empty |
| Guard G2 or G3 (shrink or mass delete) | Many files would be removed on the backup | Check whether files were really deleted or moved. Approve only if you agree |
| Guard G4 (destination changed, or NAS is this server) | Backup target is not the expected one | Do not approve. Call |
| Guard G5 (mount marker missing) | NAS share does not look like the expected drive | Check the NAS share is the right one. Approve only then |
| Guard G6 "roll over anyway" | Switch waits for a decision | See AAM-ROL-02 |
| Watchdog email (CRITICAL, or RECOVERED after one) | The backup service stopped or was restarted | Check the dashboard. Do not restart services during a run; wait for the dashboard to show no run |
| `/health` shows 503 | A red item exists | Read the dashboard reason (same as banner) |
| Power cut | Run was cut off | Start the server. The next run continues; a reboot email may say the switch could not be finished (open item W2) |

---

## 5. Is everything fine? (one screen)

Tick each line. If any line fails, go to section 4.

- [ ] Dashboard: **no red banner**.
- [ ] Cards: **one** (Apr to Dec, from 1 Apr) or **two** (1 Jan to 31 Mar). Newest year first in January.
- [ ] Each card: last cloud and last NAS success **last night** (or within the overdue rule in section 4).
- [ ] No approval waiting on any card.
- [ ] Monday weekly email: no "action needed" in any section.
- [ ] 1 Jan: the activation email arrived (or no "alerts could not be delivered" banner).
- [ ] 1 Apr, 09:00: Settings rollover card says the switch is done; one card; config shows `FY27-28`.
- [ ] NAS is off after each NAS run (unless you asked it to stay on).
- [ ] `http://127.0.0.1:8080/health` on the server shows healthy (TO CONFIRM the page content).

---

## 6. Who to call

- **Your IT contact (NAS, network, server):** TO CONFIRM name and phone.
- **Developer (backup software):** TO CONFIRM name and contact.
- **Mail provider:** TO CONFIRM (only for password or account problems).
- **Google Cloud:** TO CONFIRM (only for bucket or key problems).

Have ready: the dashboard reason (screenshot), the fault code (AAM-...), the date and time, and whether the NAS was on.

---

## 7. What NOT to do (summary)

1. Do not change the Windows clock or the time-sync setting.
2. Do not rename, move or delete the year folders or the NAS marker `.AAM_TARGET_MOUNTED`.
3. Do not edit `config.yaml` by hand for the year switch, the overlap, or the folder paths.
4. Do not stop or restart services during a backup window (02:00 to 08:00, 21:00 to 01:00) or during the 06:00 rollover check.
5. Do not approve a request you do not understand, or a request for the other year.
6. Do not switch the overlap off between January and March (the early year would stop being backed up; call first).
7. Do not rename or delete `manifest.db` (or its `-wal` and `-shm` files). A missing database is recreated empty
   and an alarm follows.
8. Do not save new work in `FY26-27` after the 1 April switch. It is not backed up.
9. Do not use the restore tool on a year that is active; the upload and restore tools refuse active years.
10. Do not press **Back up now** repeatedly. Wait for the result first.
