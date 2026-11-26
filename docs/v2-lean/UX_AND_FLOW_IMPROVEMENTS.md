# AAM Backup Automation (`v2-lean`) — Real-World Pain Points & UX/Flow Improvements

> Research-backed UX, feature, email, and reporting blueprint for `v2-lean`, grounded in real-world user & sysadmin pain points (Reddit `r/sysadmin`, `r/msp`, `r/DataHoarder`, `r/backup`, and industry forums) and tailored to [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) and [FINAL_STACK_AND_DEPS.md](FINAL_STACK_AND_DEPS.md).

---

## 1. Real-World Backup Software Pain Points (Reddit & Forum Research)

Across sysadmin, MSP, and SMB communities, users consistently abandon or resent backup tools (even major ones like Veeam, Acronis, Backblaze, and Duplicati) for seven specific reasons:

| Pain Point | What Real Users Complain About | How `v2-lean` Eliminates It |
|---|---|---|
| **1. "Silent Failures" & the Green Dashboard Trap** | Jobs stop running (expired creds, missed schedule, killed service) and no alert fires because *no failure event was logged*. Admins only discover months of missing backups during a disaster. | **RPO Data-Age Clock** + **Watchdog `overdue` check** + **Boot catch-up** (`cronsim` vs `run_history`) + inactive **Dead-Man's Switch** provision (`healthcheck_url`). |
| **2. Initial Bulk Upload & Bandwidth Nightmare** | First full backup chokes office internet for days, drops mid-way, or restarts from 0%, triggering false timeout/overdue alarms. | **Single-Time First Bulk Upload (Initial Seed)** flow with resumable `rclone`, daytime `--bwlimit`, and `overdue` suppression while seeding. |
| **3. "Restore Nightmares" (Untested & Clunky)** | Green checkmarks for months, then panic during an incident: broken/mangled restores, slow single-threaded downloads, or hunting through raw version folders. | **Weekly Safety Audit** (full MD5 `rclone check`) + **3-Step Guided Restore Flow** (NAS `_versions/` first for LAN speed, Cloud fallback, safe non-destructive target). |
| **4. Alert Fatigue ("Boy Who Cried Wolf")** | Daily floods of routine "Success" emails and false-positive transient network alerts train operators to ignore or filter all backup emails. | **Tiered alerts** (no routine success spam), **transient retry suppression**, **cooldown deduplication**, and **`[AAM RESOLVED]`** recovery emails. |
| **5. Cryptic Error Codes & Log Soup** | Messages like `Error 0x80070002` or `Exit code 1` force users to dig through nested log files just to know *what* broke and *what to do*. | **Plain-English 4-part alert template** (What happened, Is data at risk?, 1–2 action steps, direct deep-link to run). |
| **6. Local DB Desync / Corruption (Duplicati Syndrome)** | Proprietary chunked formats + fragile local SQLite state break after power cuts, leaving backups unreadable without rebuilding the DB. | **1:1 readable mirrors** on both GCS and NAS (`rclone` native files, not opaque chunks) + isolated `huey.db` / `manifest.db` in WAL mode. |
| **7. Bloat & Management Silos (Acronis / Veeam)** | Security bloatware, popups, or jumping between multiple consoles/servers to check one backup. | **Single-purpose appliance**, clean web UI over local HTTPS, zero bloat. |

---

## 2. Layer-by-Layer UX & Flow Improvements

### 2.1 Layer 1: Onboarding & First-Run Flow (Zero-Surprise Setup)

1. **Pre-Flight "Test Everything Now" Readiness Gate:**
   * Immediately after `AAMBackupSetup.exe` finishes (and accessible anytime from the dashboard), run a one-click live check:
     * Source folder readable & non-empty
     * Office NAS reachable (or WoL wakes it + SMB write + `.AAM_TARGET_MOUNTED` canary OK)
     * Cloud (GCS) auth + bucket reachable
     * Test email actually delivered to both `alerts` and `reports` recipients
   * **Goal:** Eliminate the classic *"installed at 4 PM, failed at 1 AM"* surprise.

2. **Single-Time First Bulk Upload (Initial Seed):**
   * Offer a post-install choice: **"Run Initial Bulk Upload Now"** or **"Start Tonight at 1:00 AM"**.
   * During the initial seed:
     * Apply an optional daytime bandwidth cap (`--bwlimit`) so office internet stays usable.
     * Suppress `overdue` / false-timeout alerts while `backup.lock` is held.
     * Show clear progress (bytes/files transferred) and send a dedicated **"Initial Backup Complete — Nightly Protection Active"** notification once `guard_baseline` is established.

3. **Plain-English Schedule & Mode Labels:**
   * Never show raw cron strings (`0 1 * * *`) or internal mode keys (`fy` / `whole_drive`) to end users.
   * Use `cron-descriptor` + countdown: *"Runs every night at 1:00 AM IST (next run in 6h 20m)"* and *"Protecting Accounting Years (FY25-26, FY26-27)"* vs *"Protecting Entire Drive (E:\)"*.

---

### 2.2 Layer 2: Dashboard & Operational Flows (Self-Explanatory)

1. **Single "Am I Protected?" Hero Status + RPO Clock:**
   * Answer one question immediately at the top of the dashboard:
     * **Protected (Green):** *"All backups current. Cloud: 4h ago. Office NAS: 4h ago."*
     * **Attention Needed (Amber):** *"New files are safe (Copy-Only Mode), but 312 deleted files are waiting for your cleanup approval."*
     * **Action Required (Red):** *"Cloud backup failed 18 hours ago — data changed in the last 18h is not backed up off-site."*
   * Display **Actual Data Age (RPO)** vs **Target (24h)** instead of forcing users to do mental math on timestamps.

2. **3-Step Guided Restore Flow (The Missing Pillar):**
   * Having `_versions/<YYYY-MM-DD_HHmm>/` on NAS and Object Versioning on GCS is only half the story—panicked users should never have to browse raw `_versions/` trees manually.
   * **Flow:**
     1. **Pick Source & Point-in-Time:** Default to *Office NAS* (fast LAN) with *Cloud (GCS)* as fallback; pick a date/snapshot.
     2. **Browse & Select:** Search/browse folders and files (`rclone lsjson`).
     3. **Safe Restore Target:** Default to **"Restore to a new folder (`C:\Restored\<date>\...`)"** (never overwrite live files by default), or offer a direct browser ZIP download for individual files/small folders.

3. **G3 `Copy-Only` Guided Resolution:**
   * When G3 triggers, explain **why** in plain English (*"312 files (4.8 GB) were deleted or moved on E:\ since yesterday — above your 10% safety limit"*), preview the **top 5–10 affected folders/files** from the diff, and offer two clear actions:
     * **"Yes, I deleted/moved these — clean up backup on next run"** (sets `allow_deletes_<leg>` with expected count + 24h TTL)
     * **"No, keep protecting them in Copy-Only mode"** (acknowledges without deleting)

4. **Storage Capacity Runway:**
   * Track NAS free space (`shutil.disk_usage`) and GCS size growth over time to warn **before** storage fills up: *"Office NAS has 180 GB free (~45 days remaining at current growth)."*

5. **Maintenance / Pause Window:**
   * Allow an admin to click **"Pause Backups for 24h / Until Date"** during planned server maintenance or accounting year-end restructuring, so `AamWatchdog` and `overdue` checks don't fire false `CRITICAL` alerts.

---

### 2.3 Layer 3: Emails & Alerts (Actionable, Zero Fatigue)

Separate **Operational Alerts** (`alerts` list) from **Executive Reports** (`reports` list):

1. **Scannable Subject Lines:**
   * `[AAM CRITICAL] <ClientName> — Cloud Backup Failed (Data age: 28h)`
   * `[AAM ACTION] <ClientName> — Safety Guard Paused Deletions (312 files, 4.8 GB)`
   * `[AAM RESOLVED] <ClientName> — Cloud Backup Back to Normal` *(auto-sent when a previously failing leg succeeds)*
   * `[AAM REPORT] <ClientName> — Weekly Backup Health: 100% Protected`

2. **4-Part Plain-English Alert Body (No Raw Tracebacks):**
   1. **What happened:** *"Office NAS did not respond to Wake-on-LAN after 3 attempts."*
   2. **Is my data at risk?** *"Your Cloud Backup succeeded and is current (4h old). Only your local Office NAS copy is behind."*
   3. **What to do next (1–2 steps):** *"Check NAS power/network cable, then click 'Run Office NAS Backup' in the dashboard."*
   4. **Direct Deep-Link + Attachment:** Link directly to the failing run (`https://<server>/runs/<id>`), with compressed `run_log` attached only on failure.

3. **Quiet Hours / Morning Digest for Non-Critical Warnings:**
   * Avoid waking users at 1:00 AM for non-urgent warnings (`COPY_ONLY` reminders, NAS >80% full); queue them for morning business hours (e.g., 09:00 AM IST).

---

### 2.4 Layer 4: Weekly & Monthly PDF Reports (`fpdf2`) — Audit-Ready

1. **Page 1 — Executive Summary ("30-Second Read"):**
   * **Overall Badge:** `PROTECTED` / `ATTENTION NEEDED` / `AT RISK`
   * **3 Key Numbers:**
     * **Reliability:** `7 / 7 nights succeeded (100%)`
     * **Data Protected:** `412 GB across 84,210 files` (+ weekly/monthly delta)
     * **Integrity Proof:** *"Weekly Safety Audit passed — 100% of Cloud & NAS files match source checksums."*
   * **Storage Runway:** *"Office NAS: 1.4 TB / 2.0 TB used (70% — ~5 months remaining)"*

2. **Page 2 — Daily Timeline & Exceptions:**
   * Clean 7-day / 30-day table covering **Cloud Backup**, **Office NAS Backup**, **Daily Check**, and **Weekly Safety Audit** with plain-English statuses (`OK`, `Copy-Only`, `Paused`, `Failed`), duration, and transferred size.
   * **Exceptions & Actions Taken:** Plain-English summary of any G3 pauses, auto-retries, or resolved failures during the period.

3. **Audit Footer:**
   * Client/server name, last verified integrity audit timestamp, and AAM version for compliance record-keeping.
