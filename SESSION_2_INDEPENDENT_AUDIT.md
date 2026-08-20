# SESSION 2 — Independent Fresh-Eyes Audit (Findings Register)

- **Auditor session:** 2 (independent; no prior audit material consulted while forming these findings)
- **Date:** 2026-08-20 (IST)
- **System under test:** AAM Backup Automation V1 — Robocopy→NAS + rclone→GCS, Prefect 3.7.2 orchestration, Windows services
- **Code under audit:** dev checkout `C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1`, branch `reliability-2026-08-20`, HEAD `d27198c`
  - Deploy checkout `C:\AAMBackup` is one docs commit behind; all tracked source files are content-identical (verified via Compare-Object). Dev repo audited as the system; deploy used as the authorized real-hardware test environment.
- **Environment facts:** Windows Server 2016 Standard (10.0.14393, 8 cores, 31 GB), production Python 3.12.3 (`C:\Program Files\Python312`), dev venv CPython 3.14.5 (uv-managed, broken — see S2-16), rclone v1.74.3 (`deploy\bin`) **and** v1.74.2 (`C:\Windows\system32`), NSSM 2.24, gcloud per-user install (no `deploy\bin\google-cloud-sdk`).

## Evidence labels used below

- **FACT** — directly observed (live system, real test run, file read).
- **INFERENCE** — logical conclusion from FACTs; high confidence unless noted.
- **HYPOTHESIS** — plausible, not yet proven.
- **NOT VERIFIED** — could not be tested; reason given.

## Test evidence summary (independently executed by this session)

| Run | Command / setup | Result |
|---|---|---|
| Full unit+integration suite, pristine env | fresh `.venv_audit` (uv, Python 3.12.3 = production parity), `pytest tests/ -q` | **1439 passed, 53 skipped, 0 failed** (190 s), reproduced twice |
| Skip analysis | same run + `-rs` | **all 53 skips = `AAM_RUN_REAL_HARDWARE`-gated tests** (rt_01/02/03/05/06/07/08, e2e_real_hardware). Zero real-infrastructure tests in the default run |
| Real-hardware acceptance suite | from `C:\AAMBackup`, production venv 3.12.3, `AAM_RUN_REAL_HARDWARE=1`, 7 rt_* files, `-k "not test_fy_07"` (excluded: destructive, see S2-20) | **47 passed, 1 failed, 1 deselected** (303 s). The 1 failure (`test_wd_05`) was caused by my *concurrent* E1 experiment's rclone process being detected by `_transfer_process_running()`; re-run in isolation: **passed** (15.6 s) |
| E1 `--modify-window 2s` | real GCS test bucket, isolated prefix `AUDIT_S2_E1`, project's real `build_rclone_sync_command` | **CONFIRMED** (details S2-30) |
| E5 service-stop orphan behavior | disposable NSSM service `AamAuditScratch` with production stop methods, production process chain (`uv → python → rclone`), 300 MB transfer to isolated prefix `AUDIT_S2_E5` at 5M bwlimit, stop mid-transfer | **NEGATIVE result for orphan hypothesis** — entire tree dead ≤5 s after stop; no partial GCS object (S2-31) |
| E8 SMTP partial recipient refusal | local STARTTLS SMTP server (self-signed cert, test-scaffolding monkeypatch of `ssl._create_stdlib_context` in my own process only), real `_send_email_with_attachments`, recipients `[bad@nowhere.invalid (550), good@example.com (250)]` | **CONFIRMED false success** (S2-11) |
| Live Prefect API | `PREFECT_API_URL=http://127.0.0.1:4200/api` | 5 active deployments: backup-lan `0 21 * * *`, backup-cloud `0 22 * * *`, weekly `0 8 * * MON`, monthly `0 8 1 * *`, rollover-check `0 6 * * *` (all Asia/Kolkata) |
| Live manifest.db | read-only probe of `C:\BackupAgent\manifest.db` | 111 runs 2026-07-01→08-19; cloud 31 NO_CHANGES / 19 SKIPPED / 4 COMPLETE; lan 42 SKIPPED / 15 COMPLETE |
| Live GCS bucket | SA-JWT read-only | versioning ON; lifecycle deletes noncurrent versions after **90 days** (repo file says 92); FY26-27/ = 572 objects / 57 MB |
| Live dashboard | `/status` with prod key | LAN last success 2026-07-10; cloud last success 2026-08-18; 08-19 both SKIPPED "Source drive not accessible: E:\FY26-27" |

---

# FINDINGS

Severity scale: **Critical** (data loss / silent corruption), **High** (backup silently not happening), **Medium** (alerting/consistency defect with real operational impact), **Low**, **Info**.

## M1 — LAN anomaly-only exits (robocopy 4–7) are alerted as "files were not copied", violating the core layer's own severity contract
- **Severity:** Medium
- **Files/Location:** `flow.py:659–691` (`_run_lan_pipeline`), vs `core/lan_sync.py:105–146` (`classify_exit_code`), `:248–251` (severity contract), `:305–315`
- **Evidence: FACT (code) + NOT VERIFIED at runtime** (forcing a real robocopy exit 4–7 requires attribute-mismatch state; not reproduced — the code path is unambiguous)
- **Problem:** `run_lan_sync()` documents a two-tier severity contract: `error` populated → "alert system MUST notify"; `anomaly_details` set (exit 4–7) → "log a warning, investigate later. Backup is complete", and it deliberately leaves `error=None` "so alert systems are not triggered". But the flow's `elif status == "LAN_PARTIAL":` branch does **not** consult `returncode & 8` or `error` — it sends a failure alert claiming "some files were not copied" and skips NAS shutdown for **all** partials, including anomaly-only.
- **Why it matters:** exit 4 = "Mismatched files detected — no copy errors" per the same file's own MS bit documentation. An operator gets a nightly failure email for a state that is, by the system's own definition, a *complete* backup; and the NAS stays powered on after a complete run.
- **Reproduction (static):** exit code 4 → `classify_exit_code` → `LAN_PARTIAL` with `error=None` → flow L661 branch → `send_failure_alert` L669.
- **Impact:** alert fatigue / training the operator to ignore PARTIAL emails; real copy-error PARTIALs (exit 8–15) become harder to distinguish.
- **Remediation:** in the LAN_PARTIAL branch, check `sync_result["exit_code"] & 8`: alert only when set; for 4–7 log a warning and shut down the NAS (backup complete).
- **Confidence:** High.
- **Test coverage gap:** `tests/test_flow_status_semantics.py` F3 tests exit 9 (alert) only; anomaly-only 4–7 is not covered anywhere (FACT — grep of tests).

## M2 — Cloud verify-failure produces a double failure alert
- **Severity:** Medium
- **Files/Location:** `flow.py:469–505` (pipeline-level alert + `raise RuntimeError`) and `flow.py:1004–1030` (flow-level summary alert on `excs`)
- **Evidence: FACT (code path, deterministic)**
- **Problem:** When `verify_data["verified"]` is False, the pipeline sends a failure alert (L489) and then re-raises; the flow's `backup()` catches it into `excs` and sends a second summary alert (L1010) with the same message.
- **Impact:** every verify failure = 2 emails to all recipients (the `[ALERT_NOT_DELIVERED]` annotation logic doubles as well).
- **Remediation:** don't alert inside the pipeline when the flow-level summary will alert (or mark the exception "already alerted").
- **Confidence:** High.

## M3 — Concurrency-slot timeout (1 h) fails the flow with no email and no run_history row
- **Severity:** Medium
- **Files/Location:** `flow.py:969` (`with concurrency("aam-backup", occupy=1, timeout_seconds=3600)`), `:1061` (`except Exception: raise`); Prefect 3.7.2 `prefect/concurrency/sync.py` (`concurrency()` raises builtin `TimeoutError` on timeout; slots are 300 s leases)
- **Evidence: FACT (Prefect source + flow code). Runtime demo: NOT EXECUTED (holding a slot for >1 h is impractical; source-level proof is exact)**
- **Problem:** If a slot can't be acquired within 3600 s (e.g., a previous run held the slot for hours — possible per S2-07's 3×6 h retry budget — and a new scheduled/manual run starts), `concurrency()` raises `TimeoutError` **inside** the `try`, the `except` re-raises, the flow run FAILS in Prefect, but: no alert email (the only alert sites are inside the slot), no `run_history` row (only written by `_record_run` in the pipeline `finally`, which never executes), no dashboard trace.
- **Impact:** the one failure mode that is *invisible everywhere except the Prefect console* — exactly the opposite of this system's design goal.
- **Remediation:** wrap slot acquisition: on `TimeoutError`, record a `*_FAILED` run row and send an alert ("backup skipped — another run held the lock >1h").
- **Confidence:** High.
- **Related note (FACT):** the concurrency slot is the *only* mutual exclusion between backup flows. The watchdog lock file (`write_lock`, flow.py:972) is written but never checked by the flow — it exists solely to defer watchdog restarts. The slot uses 300 s leases; if the Prefect API is unreachable >5 min mid-backup, the lease is lost and another run *could* acquire the slot (NOT VERIFIED — requires an induced Prefect outage during backup).

## M4 — (E8) SMTP partial-recipient refusal is reported as success
- **Severity:** Medium
- **Files/Location:** `core/report.py` `_send_email_with_attachments` (`server.sendmail()` return value unchecked; logs "Email sent" and returns True)
- **Evidence: FACT (experiment E8, real code, controlled local SMTP server: RCPT 550 for 1 of 2 recipients; function returned `True`, logged `Email sent`)**
- **Problem:** `smtplib.sendmail()` raises only when *all* recipients are refused; with partial refusal it returns a `{recipient: error}` dict. The code ignores it, so a typo'd / stale / full-mbox recipient silently stops receiving alerts while the system believes all recipients got them — including the `[ALERT_NOT_DELIVERED]` mechanism, which only fires on returned `False`.
- **Impact:** the alerting channel — the system's primary failure-visibility path — degrades silently.
- **Remediation:** inspect the `sendmail()` return dict; if non-empty, log per-recipient rejections, treat as partial failure, and surface in the `[ALERT_NOT_DELIVERED]` annotation path.
- **Confidence:** High (reproduced).

## M5 — Unguarded `Path.exists()` on the canary can raise raw WinError 5, bypassing the self-recovery message
- **Severity:** Medium
- **Files/Location:** `core/lan_preflight.py:36` (`canary_file.exists()`, no try/except), `core/cloud_preflight.py:64` (`source_path.exists()`, same exposure)
- **Evidence: FACT (three-way proof):**
  1. **Live logs** `C:\BackupAgent\logs\agent_svc-20260820T065624.518.log` lines 133–143 (2026-07-10 21:01 and 07-11 21:00 runs): `PermissionError: [WinError 5] Access is denied: '\\\\10.10.186.231\\lan_backup\\FY26-27\\.AAM_TARGET_MOUNTED'` raised inside task `lan-preflight`, surfacing in the flow summary as a raw OS error.
  2. **CPython 3.12.3 source** (`C:\Program Files\Python312\Lib\pathlib.py:852–864, 45–54`): `exists()` re-raises OSError unless errno ∈ {ENOENT, ENOTDIR, EBADF, ELOOP} or winerror ∈ {21, 123, 1177}. **WinError 5 (Access Denied) is not in the set** → it propagates.
  3. **Probe:** `exists()` on missing local/UNC paths returns False (the common case is fine); the raise requires the OS to return ACCESS_DENIED instead of NOT_FOUND — exactly what the NAS returned on those nights (INFERENCE on the NAS-side cause; the code-side behavior is proven).
- **Problem:** the G11 self-recovery message ("Canary file missing — recovery: `cmd /c type nul > …` / run `10_recreate_canary.bat`") is bypassed; the operator gets a raw `PermissionError` with no recovery hint, and the run is recorded via the exception path instead of the HealthError path.
- **Remediation:** wrap `exists()` in try/except OSError → treat as "not proven mounted" and raise the HealthError with the recovery message.
- **Confidence:** High (mechanism fully pinned; exact NAS-side trigger state is inference).

## M6 — Two rclone versions on the production machine; the sync command uses the bare-PATH one
- **Severity:** Medium
- **Files/Location:** `core/cloud_sync.py` `build_rclone_sync_command` (bare `"rclone"` → PATH resolution); vs `core/cloud_preflight.py:86`, `core/cloud_verify.py`, `core/cloud_reporter.py` (`resolve_binary("rclone")` → `deploy\bin` first)
- **Evidence: FACT (live machine state):**
  - `C:\Windows\system32\rclone.exe` = **v1.74.2** (dated 2026-05-22; machine PATH → visible to LocalSystem services).
  - `C:\AAMBackup\deploy\bin\rclone.exe` = **v1.74.3**.
  - Therefore in production the actual `rclone sync` runs **1.74.2** while preflight/verify/report run **1.74.3** — two different binaries per pipeline.
- **Why it matters:** the inconsistency is not style — version drift *already happened* (initial install copied 1.74.2 into system32; a later update only refreshed `deploy\bin`). Any behavioral delta between patch versions (e.g., modify-window handling, checksum logic) would apply to sync but not to verify — and DEPLOYMENT_GUIDE.md:408 still claims "rclone 1.74.2" is the pinned version.
- **Remediation:** make `build_rclone_sync_command` use `resolve_binary("rclone")`; delete/ignore the system32 copy or keep them in lockstep.
- **Confidence:** High (both binaries and their resolution order verified).

## M7 — `tests/test_rt_05_fy_rollover.py::test_fy_07` would delete the live production source folder
- **Severity:** Medium
- **Files/Location:** `tests/test_rt_05_fy_rollover.py:150–202`
- **Evidence: FACT (code reading; test excluded from my real-hardware run for this reason)**
- **Problem:** the "full rollover end-to-end" test's `finally:` block computes the *new* paths from the rewritten temp config and does `shutil.rmtree` on them. On this machine those resolve to **`E:\FY26-27`** (the live source folder, holding the restored 572-file dataset) and `\\10.10.186.231\lan_backup\FY26-27` (the production LAN destination). The "safe" assumption (temp folders under a scratch parent) breaks because `source_test_dir().parent` is `E:\` and the rollover's new FY equals the *current* FY.
- **Impact:** running the documented acceptance command (`AAM_RUN_REAL_HARDWARE=1 pytest tests/`) on a production-like checkout destroys the production source folder (recoverable from GCS, but destructive nonetheless) and the NAS FY folder.
- **Remediation:** assert the rollover target differs from the live config paths and skip/abort otherwise; or build the old-FY scenario under a dedicated scratch root.
- **Confidence:** High.

## M8 — DEPLOYMENT_GUIDE.md prescribes config key names that don't exist in the model (and unknown keys are silently ignored)
- **Severity:** Medium
- **Files/Location:** `DEPLOYMENT_GUIDE.md:296–303` (`notification.smtp_host`, `notification.email_from`, `notification.email_to`); `models/config.py` (actual model: `notifications` section with `sender`/`recipients`; no `extra="forbid"` → unknown top-level keys ignored)
- **Evidence: FACT (file reads). The "silent ignore" behavior itself: FACT (dev `config.yaml` carries unknown keys `temp_directory`, `send_on_success`, `weekly_summary_*` and loads without error)**
- **Problem:** an operator following the guide writes `notification:` (singular). `load_config` succeeds (the real `notifications:` falls back to defaults or is absent), `05_test_config.bat` prints SUCCESS, and the system's alerting is silently misconfigured. The guide also states schedules 01:00 LAN / 18:00 cloud (live is 21:00/22:00) and "rclone 1.74.2 pinned" (bin has 1.74.3).
- **Remediation:** fix the guide's key names; consider `model_config = ConfigDict(extra="forbid")` at the root (would also catch future typos) — at minimum document the silent-ignore behavior.
- **Confidence:** High.

## M9 — Restore-by-generation will 404 once the pinned generation ages past the 90-day noncurrent-delete lifecycle
- **Severity:** Medium
- **Files/Location:** `restore/phase2_download.py` (restores the exact `generation` recorded in `restore_manifest.json`); live bucket lifecycle (FACT: noncurrent versions deleted after 90 days; current versions never deleted)
- **Evidence: FACT (lifecycle metadata) + INFERENCE (restore 404 after expiry)**
- **Problem:** `restore_manifest.json` pins generations as of the 2026-08-20 cross-check. Any file that is re-synced (new generation created) makes the pinned generation noncurrent; 90 days later the lifecycle deletes it. A restore run after that date 404s on those objects. Current versions always survive, so a *fallback* restore (latest generation) is always possible — but the manifest-driven, point-in-time restore degrades silently.
- **Impact:** DR point-in-time guarantee has an implicit ~90-day-after-next-sync horizon per file, undocumented in the DR procedure.
- **Remediation:** make phase2 fall back to latest generation when the pinned one is gone, and log the substitution; document the horizon.
- **Confidence:** Medium-High (lifecycle facts verified; the 404 itself NOT VERIFIED — would require waiting 90 days).

## S2-30 (Medium) — `--modify-window 2s` silently and permanently skips same-size resaves (cloud data integrity)
- **Severity:** Medium (narrow trigger, permanent effect)
- **Files/Location:** `core/cloud_sync.py` `build_rclone_sync_command` (`--modify-window 2s`)
- **Evidence: FACT (experiment E1, real GCS, isolated prefix `AUDIT_S2_E1`, project's real command builder):**
  - Source file re-saved with **same size, different content**, mtime = object mtime + 1 s → `rclone sync` exit **9 (no transfer)** → GCS object content unchanged (byte-verified with `rclone cat`: source all-B vs cloud all-A).
  - Control 1: size change → re-uploaded (exit 0). Control 2: same size, mtime + 3 s (outside window) → re-uploaded (exit 0).
  - **Persistence:** two further syncs (no source change) still exit 9; cloud still stale. Because source mtime − object mtime stays 1 s < 2 s window with equal size, **every subsequent sync skips forever** until size or mtime changes by ≥ 2 s.
- **Why it matters:** the window is intended to suppress false-positive re-uploads of unchanged files; the failure direction is inverted — it suppresses *real* changes. Nightly-only backups mean a same-size edit made within a 2-second window of the previous mtime never reaches GCS.
- **Remediation:** drop `--modify-window` (or set it very small) and rely on rclone's size+hash compare; or add `--size-only` off with proper change detection. Note the same flag is not present on the LAN side (robocopy compares without a window) — cloud-only exposure.
- **Confidence:** High (reproduced + persisted).

## Low-severity findings

### S2-04 — `files_failed=0` hardcoded for cloud run records
`flow.py:577` (finally block of `_run_cloud_pipeline`). Cloud failures (CLOUD_FAILED / CLOUD_VERIFY_FAILED) always record `files_failed=0` even when verify diff lists missing files (`diff["added"]`). Reports/dashboard understate cloud damage. **FACT (code).** Low.

### S2-05 — Cloud artifact labels a size-only verify as "Cryptographic Checks: Passed"
`flow.py:363` area (`cloud_publish_artifact_task`). `verify_cloud_integrity()` is `rclone check --one-way --size-only` (by design — no checksums). Labeling it "Cryptographic Checks: Passed" overstates the guarantee. **FACT (code).** Low.

### S2-06 — `rollover_check_flow` takes no concurrency slot and writes no backup lock
`flow.py:863–911`; only `backup()` (L969) acquires the slot. When the configured FY is stale, the daily 06:00 check runs a *final backup* (robocopy /MIR + rclone sync to the old-FY destinations) which can overlap a manually triggered backup with no mutual exclusion (scheduled backups at 21:00/22:00 don't collide with 06:00; manual triggers can). **FACT (code);** window is narrow. Low-Medium.

### S2-07 — Cloud sync retry = up to 2 extra full 6-hour re-runs, slot held throughout
`flow.py` cloud sync task `retries=config.cloud.max_attempts - 1` (= 2 for max_attempts=3), `retry_delay_seconds=300`, each re-run with its own 21600 s timeout. Worst case one flow holds the "aam-backup" slot ~18+ h → any other run waiting for the slot hits S2-03's 1 h timeout. **FACT (code);** worst case NOT VERIFIED at runtime. Low.

### S2-08 — Dashboard "active run" detection is blind to mode="all"
`ui.py` `_prefect_has_active_run` matches only `parameters.mode in {"cloud", "lan"}`. A manual cloud/LAN trigger while a `backup-all` run is in progress is not blocked by the UI (it queues on the concurrency slot — bounded, see S2-03). **FACT (code).** Low (visual + queued-timeout interaction).

### S2-09 — `/health` endpoint exposes the source path unauthenticated
`ui.py`. Production dashboard binds 127.0.0.1 (FACT — live config), so exposure is local-only. Low.

### S2-10 — Dashboard session cookie lacks `Secure` flag
`ui.py` (httpOnly, 24 h expiry, token_hex(32), hmac.compare_digest on API key — all good). Loopback bind limits impact. Low.

### S2-14 — GCS lifecycle drift: repo file 92 days vs live bucket 90 days
`deploy/gcs_lifecycle.json` says `daysSinceNoncurrentTime: 92`; live bucket has 90 (FACT). Rules are noncurrent-only (current objects safe — FACT), but re-applying the repo file would change live behavior; the drift itself signals the repo file is not the source of truth. Low.

### S2-16 — Dev venv is broken; interpreter drift dev 3.14 vs production 3.12
FACT: dev `.venv` (CPython 3.14.5) fails `import prefect` with `ModuleNotFoundError: No module named 'annotated_types'` → **no test can run in the checked-out venv as-is**; `uv sync` fails (locked files, access denied). `.python-version` = 3.12; production runs 3.12.3. A pristine `uv` env on 3.12.3 imports and runs the suite green. "Suite green" on the deployed checkout is only reproducible with a fresh env. Low (dev hygiene) but audit-relevant.

### S2-17 — `CloudConfig.project_number` hardcoded default `"920173882190"`
`models/config.py`. A fresh config omitting project_number silently inherits this tenant's project number. (Mitigated: the SA key file carries its own project; rclone uses the key.) Low.

### S2-18 — Code cron defaults (18:00 cloud / 01:00 LAN) differ from config.example (22:00 / 21:00)
`models/config.py`. A config missing the `schedule:` section silently moves backups 3 hours. Live deployments use 21:00/22:00 (FACT). Low.

### S2-22 — Startup orphan-run cancellation always cancels PENDING runs
`launch.py` `_cancel_orphaned_runs`: PENDING runs are cancelled unconditionally; RUNNING runs are spared only when the backup lock is alive. A restart cascade (watchdog `sc stop` → service restart) that lands while a backup is active can cancel a queued scheduled run; the next run is 24 h later (documented "missed runs are not made up"). **FACT (code);** NOT VERIFIED at runtime (would need killing the agent mid-backup with a queued run). Low-Medium.

### S2-23 — Dead tag-based concurrency limit "aam-backup"
`launch.py` `_ensure_concurrency_limit` creates both a global and a tag-based limit; no Prefect run carries the tag (FACT — deployments/runs inspected), so only the global limit enforces. Harmless dead code. Info-Low.

### S2-24 — Watchdog `_transfer_process_running()` matches ANY rclone/robocopy on the machine
`watchdog.py`. Not AAM-scoped: a user's unrelated rclone/robocopy defers agent restarts up to the 8 h cap. **My own wd_05 test failure is live proof of the false-positive direction** (my concurrent E1 rclone made the "no transfer running" assertion fail). Low.

### S2-27 — Plaintext production SMTP app password on disk (not in VCS)
`restore/PROD_config.yaml.2026-08-20` (untracked) contains the live Gmail app password for `client.test.innovizta@gmail.com`; dev `config.yaml` (untracked) likewise. `git grep` finds no tracked file containing the password or private-key material (FACT). Secrets hygiene: on-disk plaintext in the source tree, anyone with read access to the box gets the sender account. Low-Medium.

### S2-28 — `deploy/09_restore_from_gcs.bat` uses `wmic`
`wmic` was removed on newer Windows (22H2+/2022+). Works on this 2016 box (FACT — OS version) but breaks the documented DR path on the post-2027 OS upgrade the guide itself recommends. Low.

### S2-32 — Health-check failures are recorded as `*_SKIPPED` in run_history
`flow.py:444/566` (cloud), `:602/704` (lan): exception in the "pre" phase → status stays `*_SKIPPED` in the DB row while the operator simultaneously gets a failure alert. Reports cannot distinguish "pipeline disabled" from "pipeline failed health". **FACT (code).** Low.

### S2-34 — `run_archive_transition` mutates ambient gcloud state despite "stateless" docstring
`core/fy_rollover.py`: runs `gcloud auth activate-service-account` (persists the default credentials for all future gcloud use on the box); missing-key fallback uses whatever ambient gcloud auth exists. Low.

### S2-29 — Uninstall script kills all robocopy/rclone processes machine-wide
`deploy/08_uninstall_services.bat` `taskkill /F /IM robocopy.exe /T` (and rclone) — any user's transfer on the box is killed during uninstall. Acceptable for a decommissioning script; noted. Low.

## Informational / negative results

### S2-31 — (E5, NEGATIVE) Service stop does NOT orphan the transfer process
Disposable NSSM service (production stop methods: console/window/threads 15000 ms each, no `AppKillProcessTree`), production chain `NSSM → uv.exe → python → rclone`, 300 MB GCS upload at 5M bwlimit, `nssm stop` issued ~40 s into the transfer: **entire tree dead within 5 s** of the stop (polled to T+276 s; zero rclone processes). No partial object in GCS (resumable upload not finalized).
- **Conclusion:** the "orphaned robocopy/rclone after agent restart" hypothesis is **not confirmed** on the normal stop path — the console CTRL_CLOSE_EVENT reaches all tree members. Residual risk remains only if the 15 s console phase times out (NSSM then falls back to thread-suspend + terminate-parent-only); NOT VERIFIED (hard to force deterministically).
- **Side FACT:** NSSM default AppExit restarts the app on *any* exit — observed 5 auto-restarts of my scratch app after natural exits; this is the intended auto-recovery behavior of the production services.

### S2-25 — `archive/ARCHITECTURE.md` is stale
Task-Scheduler boot story (reality: NSSM services), cloud preflight described as `rclone check --one-way` (reality: `lsjson --max-depth 0` two-probe), robocopy exit table maps 4–7 → COMPLETE (reality: LAN_PARTIAL), 356-test claim (reality: 1492 collected), schedules 18:00/01:00 (reality: 22:00/21:00), `templates/dashboard.py` + time_utils helpers that no longer exist. Archived, so low stakes — but it would mislead a new maintainer. Info.

### S2-35 — Test pollution observed in live production destinations (documented, not cleaned)
- GCS bucket: prefixes `NONEXISTENT_BUCKET/`, `docs/`, `E2E_TEST_FY/` (plus my own `AUDIT_S2_E1/`, `AUDIT_S2_E5/` from this session).
- NAS share root: `E2E_TEST_DEST/`, `E2E_TEST_FY/`, stray files.
- Source `E:\FY26-27`: `E2E_TEST_FY/`, `test_data/group_A..F`, `new_folder/`.
Root cause: `tests/test_e2e_real_hardware.py` cleanup only runs under `__main__` (`run_all()`), never under pytest; `rt_*` suites use isolated sibling dirs (cleaner design). Info-Low.

### Positive confirmations (system works as designed — evidence)
- **Real-hardware behavior (47 passing rt tests):** LAN golden path, mirror-delete, canary-missing abort, **locked file → robocopy exit 8 → LAN_PARTIAL with real `files_failed` count parsed from the job summary (F12 verified against live robocopy)**, 50 MB hash integrity, unreachable-destination handling, snapshot diff; cloud sync/verify/record against real GCS; watchdog lock semantics (stale lock, PID reuse, AV-locked file fail-safe, atomic writes); rollover detect/create/atomic-config-rewrite (crash-injection); full pipelines through real code; log quality; health checks.
- **GCS safety posture:** versioning ON; lifecycle rules are noncurrent-version-only (current objects can never be auto-deleted) — FACT from bucket metadata.
- **Atomicity:** config rewrite (ruamel + os.replace, crash test passes), lock file write (tempfile + os.replace), manifest DB single-lock ops.
- **Dashboard XSS:** all dynamic fields escaped (`escapeHtml`) in `static/js/dashboard.js`/`templates/dashboard.html`.
- **Scheduling:** 5 active deployments with correct crons/timezone, live-verified via Prefect API.
- **Suite:** green and reproducible on production-parity Python 3.12.3 (1439 passed / 53 skipped — all skips are the real-hardware gate).

---

## Open verification items (carried to reconciliation)
1. Concurrency-slot lease loss during a >5 min Prefect API outage mid-backup — NOT VERIFIED (needs induced outage).
2. Robocopy exit 4–7 real-world frequency on this NAS — NOT VERIFIED (no anomaly runs in 111-run history; live history shows only SKIPPED/COMPLETE/NO_CHANGES outcomes).
3. NSSM stop fallback path (console timeout → orphan possible) — NOT VERIFIED.
4. M9 404 after 90-day version expiry — NOT VERIFIED (time).

*Register complete as of Phase 5. Phase 6 (reading prior audit material) has NOT influenced any finding above.*
