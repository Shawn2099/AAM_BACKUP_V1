# AAM Backup Automation (`v2-lean`) — Design Decisions & Removal Log

> Living architecture decision record for the `v2-lean` clean-slate rewrite.  
> Companion to [FINAL_STACK_AND_DEPS.md](FINAL_STACK_AND_DEPS.md).

---

## 1. Core Decisions Locked So Far

### 1.1 Architecture & Deployment
* **Language:** Keep **Python 3.14** (`>=3.14,<3.15` — newest release with full Windows Server 2016 support and official Nuitka 4.2+ support; brings native `compression.zstd` for `run_logs`/support bundles and deferred annotations). Do not rewrite in Go/Rust; `rclone.exe` already handles all heavy I/O in Go.
* **Packaging & Distribution:** Compile a single unified CLI (`aam.exe ui` / `aam.exe worker` / `aam.exe watchdog`) using **Nuitka (`--standalone`)**, bundle with `rclone.exe` + **Servy v11.0** inside **Inno Setup (`AAMBackupSetup.exe`)**, and code-sign both `.exe`s. Source code is not visible to clients, and no Python/`.venv` install is needed on client servers.
* **Service Model:** **2 independent peer Windows services** (`AamUI` + `AamWorker`) + `AamWatchdog`, managed by **Servy v11.0** (self-contained build for Windows Server 2016+).
* **Orchestration:** **Huey (`SqliteHuey`)** in `-k thread -w 2` mode using a dedicated `huey.db` (separate from `manifest.db`), `@huey.lock_task` + `filelock`, and `@huey.on_startup` boot catch-up via `cronsim` + `run_history`.
* **Fresh Install / Clean Slate:** No legacy DB migration or backwards-compatibility shims; fresh `config.toml` + fresh `manifest.db` (`Peewee` models).
* **Dead-Man's Switch:** Skipped for now, with an inactive provision (`healthcheck_url: str | None = None` + Huey `@huey.signal` hook using `httpx2` when enabled).

### 1.2 Production Additions
1. **Whole-Drive Mode (`paths.mode = "whole_drive" | "fy"`):**
   * `whole_drive` points directly to a drive or folder (e.g. `E:\`, `D:\ClientData`, `\\nas\backup\ClientData`) with **no `FYxx-yy` suffix**; all syncs, `--backup-dir` versions, daily verification, and weekly hash audits cover the entire drive/folder, with FY logic bypassed.
2. **No Plain-Text SMTP Password:**
   * `smtp_password` must never sit in plain text in `config.toml` in production (auto-encrypted via Windows DPAPI on install/save and rejected if plain at runtime, or stored in a DPAPI-protected secret store).
3. **Install-Time Service Account Permissions & Verification:**
   * Automated and verified during `AAMBackupSetup.exe` installation so WoL, remote shutdown, SMB, and backup never fail at 01:00 AM:
     * `SeServiceLogonRight` + `SeBackupPrivilege`
     * NTFS ACLs on `runtime_dir`, `config.toml`, `gcs_key.json`
     * Windows Firewall rules (Dashboard HTTPS + WoL UDP broadcast)
     * Live install-time probes for NAS SMB write, GCS auth, and remote shutdown rights (`shutdown /m \\nas`).
4. **Single-Time First Bulk Upload (Initial Seed):**
   * Dedicated one-time initial upload flow (post-install or triggered via UI / CLI) for seeding large existing datasets to Cloud/NAS without waiting for the 01:00 AM window:
     * Runs resumable `rclone` transfer with optional daytime bandwidth limit (`--bwlimit`) so office internet isn't saturated.
     * Suppresses `overdue` / false timeout alerts while `backup.lock` is held during the initial seed, establishes the first `guard_baseline` on completion, and sends a clear completion notification when the baseline is ready.

### 1.3 Section 1 Decision: Backup Modes & Date-Derived FY Model
* `paths.mode = "whole_drive"` → syncs/verifies/audits the configured path directly.
* `paths.mode = "fy"` → `config.toml` stores the **parent folder** (`E:\Data`, `\\nas\backup\Data`), never a leaf `FY26-27` path.
* Active FY passes are **derived from today's IST date**:
  * Current FY (`get_fy_prefix()`): always active.
  * Next FY (`next_fy()`): active from `overlap_activate_on` (e.g. `12-31`) through `03-31` (auto-creates source folder + placeholder and NAS folder + canary).
  * Previous FY (`prev_fy()`): optional `old_year_until` cut-off (e.g. `03-31` or `10-31`), guarded by one DB check so a year is never dropped until its last pass after the cut-off succeeds.

### 1.4 Section 2 Decision: Simplified Data-Safety Guards (G1, G3, G4, G5)
* **Hard Blocks (never write to an empty/wrong/unmounted target):**
  * **G1 (`G1_SOURCE_HAS_FILES`):** Blocks if source has zero real files.
  * **G4 (`G4_DESTINATION_IDENTITY`):** Blocks if destination changed since last mirror baseline or resolves to localhost (`127.0.0.1`).
  * **G5 (`G5_CANARY`, LAN only):** Blocks if `.AAM_TARGET_MOUNTED` is missing on a populated NAS share.
* **Simplified `G3_MASS_DELETE` (Automatic `copy-only` + 1-Click Override):**
  * **Unmeasurable G3 (`planned_deletes is None` or `destination_files is None`):** Fails closed as `*_FAILED` (never `BLOCKED` asking a human to approve blind).
  * **Over-limit G3:** Automatically runs **`rclone copy`** (`copy-only`, zero deletions—new/modified files are always protected), records `CLOUD_COPY_ONLY` / `LAN_COPY_ONLY` (does **not** overwrite `guard_baseline`), and alerts with exact **file count + GB** (`humanize.naturalsize`) using normal `alerts.repeat_cooldown_hours`.
  * **One-click override:** Dashboard shows **`Allow cleanup on next run`** (sets a 1-shot `allow_deletes_<leg> = true` flag in `db_meta`), running `rclone sync` once and updating `guard_baseline`. If ignored, `rclone copy` continues protecting new files every night without breaking.
* **Keep `pendulum>=3.3.0` + `tzdata`:** Keeps existing working datetime formatting/parsing intact with zero rewrite churn.

### 1.5 Section 3 Decision: Sync, Versions, Verification & Audits
* **Cloud (GCS) Versioning:** Live bucket is a clean 1:1 mirror (no `_versions/` folder in GCS). Use **native GCS Object Versioning + Lifecycle** (`deploy/gcs_lifecycle.json`) to retain non-current versions in the GCS Console.
* **LAN (NAS) Versioning:** Live NAS share is a 1:1 mirror, using **`--backup-dir` (`_versions/<YYYY-MM-DD_HHmm>/...`)** + 90-day retention purge (`core/lan_versions.py`) for Windows Explorer browsing.
* **`rclone` Rename Flags (AF-2 & AF-3):** Keep `--track-renames` **disabled on GCS** (GCS does Copy+Delete, which trips `--max-delete`) and **never** pass `--track-renames` to `rclone copy`.
* **Cloud Daily Size Check:** Consolidate into **1 command** (`rclone check --one-way --size-only --combined <diff>`), keeping F-08 completion-summary verification, and apply **AF-12** (files modified on source *after* `sync_started_at` are treated as changed-during-run rather than false `CLOUD_VERIFY_FAILED`).
* **LAN Nightly Arrival Hash Check:** Keep delta MD5 check (`rclone check --one-way --files-from-raw`) on tonight's copied files. *(SMB cannot force a remote NAS to bypass its own RAM cache; physical disk-surface verification is covered by the weekly cold-boot audit after `lan_shutdown`.)*
* **Weekly Deep Integrity Audit (`core/integrity.py` + `core/audit_pending.py`):** Keep intact—full MD5 `rclone check` (no `--size-only`) with 4 truthful verdicts: `VERIFIED`, `PENDING_BACKUP` (source modified after last good backup), `AUDIT_INCOMPLETE` (network/TLS/auth/I/O error), and `VERIFICATION_FAILED`.

### 1.6 Section 4 Decision: Auth, Secrets, Caddy HTTPS & Install-Time Permissions
* **Secrets at Rest (Zero Plain Text):**
  * **SMTP password:** Machine-scope Windows DPAPI (`dpapi:<base64>` in `config.toml`), readable by both `AamWorker` and `LocalSystem` `AamWatchdog` (unlike per-user Windows Credential Manager). Prompted masked at install; editable anytime via UI (`Settings > Notifications`), `aam.exe set-smtp-password`, or auto-encrypted on startup if pasted plain.
  * **GCS key (`gcs_key.json.dpapi`):** Service Account JSON encrypted with machine-scope DPAPI and passed to `rclone` strictly in memory via `RCLONE_CONFIG_AAM_GCS_SERVICE_ACCOUNT_CREDENTIALS` (no plain JSON on disk). Replaceable anytime via UI upload (`Settings > Advanced`, validated + `rclone lsjson` tested before swap) or `aam.exe set-gcs-key`.
* **Dashboard Auth & Local HTTPS:**
  * Two-tier passwords (`login_password_hash`, `admin_password_hash`) hashed with **`pwdlib[argon2]`** (`Argon2id`); resettable via UI or `aam.exe reset-passwords`.
  * Starlette **`SessionMiddleware`** (`itsdangerous`, `https_only=True`, `same_site="lax"`) with password-hash fingerprint invalidation, plus **`pyrate-limiter`** (`Depends`).
  * **`Caddy` (`tls internal`)** managed under **Servy** as a peer service reverse-proxying `127.0.0.1:8080` over HTTPS with auto-renewing local certs.
* **Install-Time Account & Permission Provisioning (`AAMBackupSetup.exe`):**
  * Option to **auto-create** dedicated local service account (`.\AamBackupSvc`, custom masked password or random 32-char, `PasswordNeverExpires=true`) **or** use an existing Domain/custom account.
  * Grants `SeServiceLogonRight`, `SeBackupPrivilege`, `SeRestorePrivilege`, NTFS ACLs, and Windows Firewall rules (Caddy HTTPS + WoL UDP 9).
  * Runs live install-time probes *as* the service account (source read, `whoami /priv`, NAS SMB write + canary, GCS `lsjson`, WoL broadcast + remote shutdown permission check on `\\nas`).
  * Everything remains editable afterwards via UI Settings or `aam.exe` CLI (`set-smtp-password`, `set-gcs-key`, `reset-passwords`, `doctor`).

### 1.7 Section 5 Decision: Notifications, PDF Reports, Watchdog, Lean Config & Naming Consistency
* **Notifications & Reports:**
  * Consolidate `attention` + `overdue` + `alert_class` + `faults` + `messages` → **1 `alerts` module**.
  * Consolidate `report` + `email_compose` + `support_bundle` → **1 `reports` module** using **`Jinja2`** (HTML) + **`fpdf2`** (**PDF primary** for client weekly/monthly email attachments & UI downloads; Nuitka-safe with zero external DLLs) + stdlib **`csv`** (**secondary** data export in UI; no `.xlsx` dependency needed) + Python 3.14 **`compression.zstd`** / `.zip` for support bundles.
  * Keep: single alert path, SMTP `4xx` vs `5xx` classifier (`tenacity`), two recipient lists (`alerts` vs `reports`), cooldown deduplication, attachment ladder, and `notifications` table.
* **Watchdog (`watchdog.py` — ~70% smaller):**
  * Huey touches `worker.heartbeat` every 5 min; watchdog restarts `AamWorker` if stale > 15 min and no `backup.lock` is held.
  * Snapshots SMTP/recipient settings to `last_good_alerts.json` on every valid load so corrupt `config.toml` or unreadable DB still emails `CRITICAL`.
  * Self-healing overdue check (restarts `AamWorker` if overdue & unlocked) + proactive disk-space and DPAPI/GCS-key checks.
* **Lean `config.toml` (~40 essential keys instead of 235):**
  * Keep only operator/installer keys (`paths`, `cloud`, `lan`, `wol`, `schedule`, `guards`, `notifications`, `alerts`, `dashboard`, `healthcheck_url`); move internal poll intervals, lock wait timeouts, buffer sizes, and retry backoffs into code defaults.
* **Consistent, User-Friendly Naming Contract (DB, Code, UI & Emails):**
  * Eliminate mixed terminology (`lan` vs `nas` vs `office backup`, `verify` vs `integrity_audit`) in favor of **one consistent vocabulary** everywhere:
    * Legs: `cloud` (**Cloud Backup**) and `nas` (**Office NAS Backup**)
    * Checks: `daily_check` (**Daily Check**) and `weekly_audit` (**Weekly Safety Audit**)
    * Clear, plain-English DB function names, status labels, UI strings, and email copy aligned to the same terms.

---

## 2. Removed Items — What Each One Was Doing & Why It Is Removed

### 2.1 Orchestration & Service Plumbing Removed

| Removed Item | What It Was Doing | Why It Is Removed / Replaced By |
|---|---|---|
| **Prefect 3 (`prefect==3.7.2`)** & **`AamPrefectServer`** | Ran a local FastAPI/SQLite orchestration server on `:4200` storing flow/task states, schedules, and logs. | Replaced by **Huey (`SqliteHuey`)**; eliminates the separate server, unbounded `.prefect/` DB growth, and HTTP round-trips on every task state change. |
| **`serve.py`** | Registered up to 6 Prefect deployments (`backup-cloud`, `backup-lan`, `weekly-report`, `monthly-report`, `rollover-check`, `integrity-audit`) with crons from config. | Replaced by Huey `@periodic_task(crontab(...))` + `@task` decorators. |
| **`launch.py` (`_ensure_concurrency_limit`, `_reconcile_disabled_legs`, `_cancel_orphaned_runs`)** | Bootstrapped Prefect global/tag concurrency limits (`aam-backup` limit=1), paused disabled legs server-side so Prefect wouldn't fire stale deployments, and cancelled orphaned Prefect flow runs on boot. | Replaced by `@huey.lock_task` + `filelock`, `sweep_abandoned_runs()`, and a minimal `aam.py` CLI (`ui` / `worker` / `watchdog`). |
| **Global slot `aam-backup` & `SLOT_TIMEOUT` (`AAM-SYS-08`)** | Acquired a Prefect concurrency lease (300s renewal) on top of `backup.lock`; could wedge for 3600s if a lease wasn't released. | Redundant with `backup.lock` (`filelock`) + Huey `@huey.lock_task`. |
| **`core/logging.py` (`configure_prefect_bridge`)** | Forwarded every `loguru` log line into Prefect's API/DB (inflating Prefect DB size). | Removed; logs stay in `loguru` rotating files + `run_logs\`. |
| **`nssm.exe` (2.24)** | Wrapped Python scripts as Windows services using Windows Registry `Parameters`. | Replaced by **Servy v11.0** (active, recursive process-tree kill, built-in log rotation, no plaintext Registry env storage). |
| **Prefect & `sc.exe` babysitting in `watchdog.py`** | Polled `127.0.0.1:4200/api/health`, ran ordered `sc stop/start` to work around `AamBackupAgent` → `AamPrefectServer` error `1051` dependency, and killed `STOP_PENDING` processes. | Replaced by **Servy v11.0** (independent peer services + crash recovery) + lean `watchdog.py` (heartbeat stale check + `overdue` + `last_good_alerts.json`). |

### 2.2 Config, DB & UI Plumbing Removed

| Removed Item | What It Was Doing | Why It Is Removed / Replaced By |
|---|---|---|
| **`pyyaml`, `ruamel.yaml`, `config.yaml` round-trip** | Loaded YAML (`pyyaml`) and preserved comments/quoting on UI/rollover writes (`ruamel.yaml`). | Replaced by `config.toml` via stdlib `tomllib` + `pydantic-settings` + `tomlkit` (no YAML backslash/boolean footguns). |
| **Legacy `manifest.db` migrations (`SCHEMA_VERSION = 5`, `_migrate_legacy_schema`)** | Hand-written SQLite `ALTER TABLE` / table-rebuild scripts upgrading `v1`–`v5` schemas in `core/manifest.py`. | Fresh install uses **Peewee** `db.create_tables([...])` + `pwmigrate` for any future changes. |
| **Custom PBKDF2-SHA256 (`core/admin_auth.py`)** | Hand-rolled `hashlib.pbkdf2_hmac` (600k iterations) + `$`-delimited hash format. | Replaced by **`pwdlib[argon2]`** (`Argon2id`, FastAPI official standard). |
| **In-memory `_RATE_LIMITS` & `_sessions` (`ui.py`)** | Custom dict + `threading.Lock` rate limiter and in-memory session dict (wiped on every UI restart). | Replaced by **`pyrate-limiter`** (`Depends`) and Starlette **`SessionMiddleware`** (`itsdangerous` signed cookies). |
| **Manual HTML email builder (`core/email_compose.py`)** | Concatenated HTML email tags/styles in Python strings. | Replaced by **`Jinja2`** templates (already installed via FastAPI). |
| **`pendulum` (`core/time_utils.py`)** | Wrapped `datetime` for IST formatting and `cutoff_iso()`. | **Kept** (`pendulum>=3.3.0` + `tzdata`) to avoid rewriting working formatting/parsing code; `3.3.0+` fixes Windows/Python 3.12+ `zoneinfo` overhead. |

### 2.3 Section 1 (FY Rollover & Overlap) Items Removed

| Removed Item | What It Was Doing | Why It Is Removed / Replaced By |
|---|---|---|
| **`core/rollover_journal.py` & `rollover_journal(_steps)` DB tables** | Persisted a 7-step saga (`final_backups` → `source_folder` → `nas_folder` → `config_switch` → `archive` → `nas_shutdown` → `announce`) so a crashed/interrupted April 1 rollover could resume step-by-step. | Date-derived FY passes need **no** multi-step switch, **no** config rewrite, and **no** saga journal. |
| **`config_switch` (`update_config_yaml`), compare-and-set, hand-switch & re-entered detection** | Rewrote `paths.source_drive` & `paths.lan_destination` from `FY26-27` to `FY27-28` on April 1, guarded against manual edits during rollover, and prevented re-running rollover if an old config was restored. | `config.toml` points to the **parent folder** (`E:\Data`) and never changes on April 1. |
| **G6 Approval (`G6_FY_ROLLOVER`) & `final_copy_state` / `ended_at` tracking** | Blocked the April 1 switch until a verified `>= Apr 1 00:00 IST` final copy of the closing FY existed (or human typed `APPROVE`), and stamped `ended_at` on `db_meta['overlap:<fy>']`. | Date-derived `old_year_until` cut-off (with one DB check that the last pass after cut-off succeeded) replaces `G6`, `final_copy_state`, and `ended_at`. |
| **`_resolve_gcloud()` & `run_archive_transition()` (`AAM-ROL-05`)** | Located Google Cloud SDK (`gcloud.cmd`) and ran `gcloud storage objects update` to move old FY objects to `ARCHIVE` storage class immediately on rollover (failing daily if `gcloud` was missing or key was DPAPI-protected). | `deploy/gcs_lifecycle.json` already transitions old objects to `ARCHIVE` automatically in GCS—zero `gcloud` SDK dependency on client servers. |
| **Dead rollover helpers (`create_new_fy_folders`, `detect_rollover`)** | Legacy pre-saga helper functions in `core/fy_rollover.py` with no active callers / log-only behavior. | Removed as dead code. |

### 2.4 Section 2 & 3 Items Removed

| Removed Item | What It Was Doing | Why It Is Removed / Replaced By |
|---|---|---|
| **`G2_SOURCE_SHRINK`** (`core/guards.py`) | Blocked the run if total source file count dropped > `max_source_shrink_percent` vs `guard_baseline`. | Redundant with **G1** (empty source) + **G3** (exact planned deletions); caused false alarms during intentional folder cleanups and while in `copy-only`. |
| **`core/approvals.py` & `approvals` DB table** | Managed `PENDING`/`APPROVED`/`DISMISSED`/`USED`/`SUPERSEDED` lifecycle, `"APPROVE"` typed phrase, `expires_at`, `tolerance_percent` re-checks, and `due_reminder_rung`. | Replaced by automatic `copy-only` fallback on G3 + a single `allow_deletes_<leg>` flag in `db_meta` (deletes are still preserved in `--backup-dir` / GCS versions). |
| **`cloud.write_probe_enabled` (`_aam_preflight` probe)** | Created and deleted a dummy `_aam_preflight/probe_<run_id>` object in GCS before every cloud sync. | Redundant with `rclone lsjson` preflight and `rclone sync` itself. |
| **Redundant `cloud_verify` commands** | Ran `rclone check --one-way --size-only`, `rclone check --combined`, and `rclone lsjson -R` as separate calls after every cloud sync. | Consolidated into a single `rclone check --one-way --size-only --combined` (+ `rclone size` if needed). |
| **`--track-renames` on GCS & `rclone copy`** | Attempted rename tracking on GCS (where it runs Copy+Delete and trips `--max-delete`) and on `rclone copy` (where rclone logs an `ERROR`). | Disabled on GCS (`track_renames=False` default) and never passed to `rclone copy`. |
