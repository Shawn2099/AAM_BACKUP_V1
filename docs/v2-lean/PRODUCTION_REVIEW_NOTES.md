# AAM Backup Automation (`v2-lean`) — Production Readiness Review & Action Items

> Fresh-eyes review of [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) and [FINAL_STACK_AND_DEPS.md](FINAL_STACK_AND_DEPS.md) ahead of production release on Windows Server 2016+.

---

## 1. Critical Stack & Concurrency Issues (Fix Before Implementation)

### 1.1 Huey `@huey.on_startup()` Runs Once Per Worker Thread (`2x` Race on `-w 2`)
* **Where:** `DESIGN_DECISIONS.md` §1.1, `FINAL_STACK_AND_DEPS.md` §5.3
* **Issue:** In Huey (`-k thread -w 2`), `@huey.on_startup()` executes **inside each worker thread**, not once per consumer process. Running `cronsim` boot catch-up and `sweep_abandoned_runs()` inside `@huey.on_startup()` will execute both concurrently on startup.
* **Fix:**
  * Run `sweep_abandoned_runs()` and boot catch-up **once** in `aam.exe worker` right before calling `huey.create_consumer(...).run()` (or guard `@huey.on_startup()` with a `threading.Lock` / `once` flag).
  * Standardize on `huey.create_consumer(...)` across all docs (`FINAL_STACK_AND_DEPS.md` §1/§5 still reference `huey_consumer` CLI flags, while §6 correctly forbids CLI string loading for Nuitka).

### 1.2 `worker.heartbeat` Starvation Under `-w 2`
* **Where:** `DESIGN_DECISIONS.md` §1.7, `FINAL_STACK_AND_DEPS.md` §5.4
* **Issue:** With `-w 2`, if Thread 1 runs a multi-hour backup (`backup.lock` held) and Thread 2 runs `weekly_audit` (full MD5 `rclone check`, also multi-hour) or a slow report, **both worker threads are blocked**. The 5-minute `worker.heartbeat` task will be queued and starved for hours. If `weekly_audit` does not hold `backup.lock`, `AamWatchdog` will observe `heartbeat > 15 min` + no `backup.lock` and **kill `AamWorker` mid-audit**.
* **Fix:** Touch `worker.heartbeat` from a lightweight dedicated daemon thread inside `AamWorker` (that verifies the Huey consumer/scheduler threads are alive), rather than scheduling heartbeat as a queued task competing for the `-w 2` thread pool.

### 1.3 `pyrate-limiter >= 4.0.0` Removed `MemoryListBucket`
* **Where:** `FINAL_STACK_AND_DEPS.md` §2 (`pyrate-limiter`)
* **Issue:** `FINAL_STACK_AND_DEPS.md` references `MemoryListBucket`, which was removed in `pyrate-limiter` v4.0.
* **Fix:** Use `InMemoryBucket` (or pass `Rate(...)` directly to `Limiter(...)` in v4.x) and update the dependency note.

### 1.4 Caddy `tls internal` on LAN & Service Count
* **Where:** `DESIGN_DECISIONS.md` §1.6, `FINAL_STACK_AND_DEPS.md` §1 & §4
* **Issue:**
  1. Caddy's `tls internal` only auto-trusts its local CA on the **Windows Server itself**, not on other LAN client PCs. Remote browsers hitting `https://<server-ip>` will show `NET::ERR_CERT_AUTHORITY_INVALID` unless Caddy's `root.crt` is exported and installed into `Trusted Root Certification Authorities` on client PCs (or pushed via AD GPO), or a real domain + internal DNS is used.
  2. `AamCaddy` under Servy makes **4** Windows services (`AamUI`, `AamWorker`, `AamWatchdog`, `AamCaddy`), whereas §1 describes "2 independent peer services + watchdog".
* **Fix:** Document the 4-service topology explicitly and add a `root.crt` export/trust step (or installer guidance) for LAN admin workstations.

---

## 2. Flow & Feature Edge Cases

### 2.1 `whole_drive` Root (`E:\`) & `_versions/` Exclusions
* **Where:** `DESIGN_DECISIONS.md` §1.2, §1.3, §1.5
* **Issue:**
  * Pointing `paths.mode = "whole_drive"` to a drive root like `E:\` will hit `System Volume Information` and `$RECYCLE.BIN`, causing `rclone` `Access Denied` errors (even with `SeBackupPrivilege`).
  * Storing NAS `--backup-dir` (`_versions/<YYYY-MM-DD_HHmm>/...`) inside the NAS mirror root requires excluding `_versions/**` from both `rclone sync` and `rclone check` (otherwise `rclone` tries to delete or audit `_versions/`).
* **Fix:**
  * Add default Windows exclusions for `whole_drive` (`$RECYCLE.BIN/**`, `System Volume Information/**`, `pagefile.sys`, `hiberfil.sys`, `DumpStack.log.tmp`).
  * Either exclude `_versions/**` in all NAS `rclone` commands or place `_versions/` as a sibling directory outside the mirror root.

### 2.2 G3 `copy-only` Interaction with `weekly_audit` & `allow_deletes_<leg>`
* **Where:** `DESIGN_DECISIONS.md` §1.4 & §1.5
* **Issue:**
  * While a leg is in `CLOUD_COPY_ONLY` / `LAN_COPY_ONLY`, deleted-on-source files remain on the destination. Daily Check uses `--one-way` (safe), but if `weekly_audit` runs a bidirectional `rclone check` without `--one-way`, it will flag extra destination files as `VERIFICATION_FAILED`.
  * A bare 1-shot boolean `allow_deletes_<leg> = true` has a race/retry gap: if cleared *before* the run and the run fails on a transient network error, the admin must re-click; if cleared *after* success, a second unrelated mass-delete before the next run could be auto-approved.
* **Fix:**
  * Ensure `weekly_audit` accounts for `COPY_ONLY` state (e.g. `--one-way` or expected-extra-files handling).
  * Store `{expected_deletes: int, expires_at: iso}` (e.g., 24h TTL) in `allow_deletes_<leg>` instead of a bare boolean.

### 2.3 FY `old_year_until` Off-by-One Ambiguity
* **Where:** `DESIGN_DECISIONS.md` §1.3
* **Issue:** `old_year_until` (e.g. `03-31`) "until its last pass after the cut-off succeeds" is ambiguous on April 1 (`04-01`), when `03-31` is already in the past.
* **Fix:** Define the exact inclusive predicate in code/spec (e.g., `prev_fy()` stays active until at least one successful pass exists with `started_at >= (old_year_until + 1 day) 00:00 IST`).

### 2.4 Locked Files (No VSS) & Non-Windows NAS Shutdown
* **Where:** `DESIGN_DECISIONS.md` §1.2, §1.5, §1.6
* **Issue:**
  * **Open/locked files:** `SeBackupPrivilege` does **not** bypass exclusive Windows file locks (e.g., Tally, SQL/DB files, `.pst`). AF-12 handles files *modified* during a run, not *exclusively locked* files.
  * **`shutdown /m \\nas`:** Only works on Windows targets (and in workgroups requires matching admin credentials or `LocalAccountTokenFilterPolicy=1`). It fails on Linux/Synology/QNAP/TrueNAS NAS devices.
* **Fix:** Document locked-file behavior (warn/skip cleanly vs future VSS support) and gate the `shutdown /m \\nas` installer probe so it does not block installs targeting non-Windows NAS devices.

---

## 3. Security, Packaging & OS Lifecycle Notes

### 3.1 Machine-Scope DPAPI & Least-Privilege Services
* **Where:** `DESIGN_DECISIONS.md` §1.6
* **Issue:**
  * Machine-scope DPAPI (`CRYPTPROTECT_LOCAL_MACHINE`) allows *any* local user or process on the server to decrypt `smtp_password` and `gcs_key.json.dpapi`.
  * `AamUI` (web-facing) does not need `SeBackupPrivilege`/`SeRestorePrivilege`, and `AamWorker` does not need `SeRestorePrivilege` unless performing in-place restores.
  * `SessionMiddleware` requires a persistent `session_secret`; if regenerated on boot, restarts still invalidate all sessions.
* **Fix:**
  * Enforce strict NTFS ACLs on `config.toml` and `gcs_key.json.dpapi` (`SYSTEM`, `Administrators`, and `AamBackupSvc` only).
  * Generate and store `session_secret` DPAPI-encrypted on install.

### 3.2 Nuitka `--standalone` Includes & Windows Server 2016 EOL
* **Where:** `FINAL_STACK_AND_DEPS.md` §1 & §6
* **Issue:**
  * `FINAL_STACK_AND_DEPS.md` §6 omits `fpdf` (`fpdf2` + `PIL`/Pillow + `fpdf/font` data dir if needed), `cffi` (used by `argon2-cffi` / `pwdlib`), `jinja2`, `pendulum`, and `psutil` from the explicit Nuitka `--include-package` list.
  * **OS Lifecycle:** Python 3.14, Servy v11.0, Peewee 4.5.3, Huey 3.5.4, and `httpx2` are all current and compatible, but **Windows Server 2016 extended support ends January 12, 2027** (and Python 3.15+ is expected to drop Server 2016).
* **Fix:** Add the missing packages to the Nuitka build command and validate a full `--standalone` build (including PDF generation and Argon2 login) on a clean Windows Server 2016 VM early.
