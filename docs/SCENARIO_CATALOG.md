# AAM Backup Automation V1 — Complete Real-World Scenario Catalog

> Purpose: Every realistic scenario this server will face in production, split by feature branch.
> For internal QA — experimental proof required (real NAS + real GCS bucket, zero mocks).
> Status codes: `COMPLETE | PARTIAL | FAILED | NO_CHANGES | VERIFY_FAILED | SKIPPED | BLOCKED`
> Branch = feature area. ID is stable for traceability → test spec + implementation.

**FY Convention:** India FY = Apr 1 → Mar 31. Folder `FYxx-xx` (e.g. `FY25-26`). Rollover detects via `get_fy_prefix()` (`core/time_utils.py`). Config paths **must** end with same FY or guard refuses to start (`models/config.py:447`).

---

## How to read

| ID | Scenario | Real Precondition | Trigger | Expected Operation (Contract) | Failure if violated |
|---|---|---|---|---|---|

**Data-protection invariant:** Any `FAILED/BLOCKED` must alert via mail (`core/report.py:108`) within minutes, never silently succeed. `PARTIAL` (4-7 or 8-15) keeps NAS ON for retry (`flow.py:627`).

---

## BRANCH A — LAN Backup (`core/lan_sync.py`, `core/lan_manifest.py`, `core/lan_preflight.py`)

| ID | Scenario | Precondition | Trigger | Expected Operation | Code / Log |
|---|---|---|---|---|---|
| LAN-01 | Golden mirror — 3 files + nested dir | NAS `\\share\FY25-26` exists, canary `.AAM_TARGET_MOUNTED` present, source has 1KB+2MB+nested | Nightly `lan` flow 21:00 IST | All files land on NAS, sizes equal, exit 1 → `LAN_COMPLETE` | `robocopy` 1, `sync exit 1 → LAN_COMPLETE` |
| LAN-02 | Mirror delete — orphan on NAS removed | LAN-01 synced 3 files, 1 deleted on source | Next `lan` sync | Deleted file disappears on NAS (`/MIR` = `/E /PURGE`), 2 remain | `LAN_COMPLETE` |
| LAN-03 | Canary missing → hard abort before transfer | Delete `.AAM_TARGET_MOUNTED` from NAS, create file on source | `run_lan_dry_run` / preflight | `HealthError` with **full UNC path** to canary, zero bytes copied | `Canary file … not found` |
| LAN-04 | NAS share offline / UNC not reachable | NAS powered off or `\\IP\BAD_SHARE\` | `preflight` or `sync` | Fail fast, no deletion on source, message contains bad UNC | `LAN_SKIPPED` or `LAN_FAILED`, not crash |
| LAN-05 | SMB reachable but share permission denied | Service Log On = LocalSystem (no UNC auth) | `robocopy` attempt | Exit 16+ → `LAN_FAILED`, log tail in `error`, `files_failed` counted | `robocopy log unreadable` fallback if log missing |
| LAN-06 | OS-level locked file held by app | `msvcrt.locking(LK_NBLCK)` on `locked_doc.txt` during sync | `run_lan_sync` with lock held | No Python crash → `LAN_PARTIAL` (bit 3) or `LAN_FAILED` (bit 4), `error` has `** FAILED:` lines, `files_failed` =1 | `exit 8` + `count_failed_lines` |
| LAN-07 | Long path >260 + unicode filename | File `…\अकाउंट\FY25-26\very_long_…_260+` | Sync | Robocopy handles (Win10 longPath) or single-file fail → `PARTIAL` (bit 3) not whole-run FAILED | `files_failed` 1, rest COMPLETE |
| LAN-08 | Large file 50MB+ integrity | `os.urandom(50MB)` + SHA256 before | Sync + SHA256 after on NAS | `LAN_COMPLETE`, SHA equal — no truncation (tests `/ZB` + `/Z`) | — |
| LAN-09 | Empty source folder (operator error) | Source contains 0 files after `iterdir` | `check_source_drive` | `HealthError: Source drive appears empty` → `LAN_SKIPPED`, no NAS delete | — |
| LAN-10 | Source path does not exist | `D:\FY25-26` missing (drive unplugged) | `health_check_task` | `HealthError: Source drive not accessible` → `LAN_SKIPPED` | — |
| LAN-11 | HDD walk scale — 1M files, dry-run timeout | `dry_run_timeout_seconds` old 300s, actual walk 8min | `run_lan_dry_run` | Must use config `900s` (tunable to 7200), not hard-coded — proves F8 | No premature `TimeoutExpired` |
| LAN-12 | Post-sync walk fails (SMB drop mid-enumeration) | `walk_lan_destination` raises `OSError` after sync | `lan_snapshot_after_task` | Returns `None`, logs CRITICAL, sync outcome **unaffected**, DB record skipped (`G14`) → next run re-derives | `LAN_COMPLETE` kept, not demoted |
| LAN-13 | Snapshot diff correctness | Before 10 files, add 1 new, modify 1 (size/mtime), delete 0 | `diff_snapshots` | `added 1, modified 1, removed 0, unchanged 9` → `files_copied/bytes_copied` correct for dashboard | Metrics drive extended_metrics JSON |
| LAN-14 | Anomaly only — extra files on dest | Extra file placed directly on NAS, source unchanged | Sync exit 2-3 or 4-7 | `LAN_PARTIAL` with `anomaly_details` tail, **no** `error`, no mail alert, but warning logged | `check anomaly_details` not `error` |
| LAN-15 | Copy errors — bit 3 set | 2 locked files + 1 good file | Sync exit 8-15 | `LAN_PARTIAL` with `error` tail (100KB), `files_failed=2`, alert sent | `error` populated → mail |
| LAN-16 | Fatal usage — bit 4 set | Wrong params or `robocopy.exe` not found | Sync | `LAN_FAILED` exit 16+ or -1, `error` contains reason | — |
| LAN-17 | Orphaned robocopy log cleanup | Kill `robocopy` mid-run, leaves `robocopy_sync_*.log` in `%TEMP%` | Next `backup(mode=lan)` start `cleanup_orphaned_robocopy_logs(24h)` | Deletes only >24h old logs, count logged, live log untouched (`G8`) | `F15` seek tail 100KB |
| LAN-18 | Shutdown contract | `shutdown_after_backup=true` + `wol.enabled=true` | After `LAN_COMPLETE` | `shutdown_server(NAS_IP)` issued; after `PARTIAL` **skipped** + alert, NAS stays on for retry | `lan_shutdown_task` |
| LAN-19 | Shutdown disabled | `shutdown_after_backup=false` | After success | No shutdown, log `disabled, skipping` | — |

---

## BRANCH B — Cloud Backup (`core/cloud_sync.py`, `core/cloud_verify.py`, `core/cloud_reporter.py`)

| ID | Scenario | Precondition | Trigger | Expected Operation | Code |
|---|---|---|---|---|---|
| CLOUD-01 | Golden rclone sync — new files | GCS bucket `gs://bkt/FY25-26` empty, 3 files on source | `cloud` flow 22:00 | `rclone sync --check-first --fast-list` → exit 0 → `CLOUD_COMPLETE`, manifest shows 3 | `CLOUD_COMPLETE` |
| CLOUD-02 | No changes — idempotent rerun | Source unchanged since last sync, `--error-on-no-transfer` on | Rerun cloud | Exit 9 → `CLOUD_NO_CHANGES_COMPLETE` (treated as success, not failure) | `9 → NO_CHANGES_COMPLETE` |
| CLOUD-03 | Preflight fast probe — 2 probes | Source has files, bucket auth valid | `run_cloud_dry_run` (<3s) | Probe A: source readable; Probe B: `rclone lsjson --max-depth 0` validates key+bucket+net; fails fast before HDD scan | — |
| CLOUD-04 | Verify post-sync — `rclone check` must pass | After sync | `verify_cloud_integrity` `rclone check --combined` | If `verified=false` with diff `missing/added/modified` counts → `CLOUD_VERIFY_FAILED` not COMPLETE, mail alert, `RuntimeError` (`F1`) | `VERIFY_FAILED` |
| CLOUD-05 | Report pipeline — size/manifest/diff | After verify | `get_cloud_size` / `get_cloud_manifest lsjson` / `get_cloud_diff --combined` | Timeouts `300/900/1800s` (`F7`) sized for 1M objects; counts persisted to `extended_metrics` JSON | — |
| CLOUD-06 | Bucket/key auth failure | `gcs_key_path` missing/empty or bad JSON, or bucket name typo | Preflight or sync | Exit 7 → `CLOUD_FAILED`, error contains path/key reason, not generic | `GCS key file not found` |
| CLOUD-07 | Network transient — retries | Simulate flaky GCS (or real retry loop) | Sync with `--retries 3 --retries-sleep 30s` | Transient 4-5 → `CLOUD_PARTIAL` flag for retry; flow-level `max_attempts 3` retries whole sync | `4,5 → PARTIAL` |
| CLOUD-08 | Subprocess timeout >6h | Set `subprocess_timeout_seconds=10` and hold rclone | `TimeoutExpired` | Return `CLOUD_FAILED` -1 with message `rclone sync is resumable; progress preserved` (`G7`) | No false success |
| CLOUD-09 | Bandwidth cap respected | `bandwidth_limit 10M` on 50MB file, measure time | Sync | Wall time ≥ file/bwlimit, no error | — |
| CLOUD-10 | Storage class mismatch | Bucket is `STANDARD` but config says `NEARLINE` | `temp_rclone_config` | Flag `--gcs-storage-class` propagates; new objects get configured class | — |
| CLOUD-11 | Differential metrics — DB vs manifest | DB has 10 entries, manifest has +1 new size-changed | Pipeline delta calc | `files_copied` = new+size-changed (>0.01 byte guard) + mtime >1.1s (`flow.py:498`) | — |
| CLOUD-12 | Empty source → verify skipped? | Source empty (should have been health-blocked) | Cloud pipeline | Health `SKIPPED` before sync, not `VERIFY_FAILED` | — |
| CLOUD-13 | Rclone not found | Remove `deploy\bin\rclone.exe` and PATH | `health` or `sync` | `HealthError: rclone not found` or `FileNotFoundError → CLOUD_FAILED -1` | — |

---

## BRANCH C — Health Gate (`core/health.py`)

| ID | Scenario | Trigger | Expected |
|---|---|---|---|
| HL-01 | Source missing | `Path(D:\FY25-26).exists()==False` | `False, "not accessible"` → `HealthError` → `SKIPPED` |
| HL-02 | Source empty | `any(source.iterdir())==False` | `appears empty` |
| HL-03 | Permission denied | `iterdir` raises `PermissionError` | `permission denied` |
| HL-04 | Low disk free < min | `shutil.disk_usage free 0.2GB <1GB` | `critically low on space: 0.2 GB free (minimum: 1 GB)` |
| HL-05 | rclone missing | `resolve_binary rclone==None` + mode cloud/all | `rclone not found in PATH` |
| HL-06 | robocopy missing | `resolve_binary robocopy==None` + mode lan/all | `robocopy not found` |
| HL-07 | GCS key missing/empty | Key path no file / size 0 + mode cloud | `GCS key file not found/empty` |
| HL-08 | Clock skew >600s | Local UTC vs `www.googleapis.com Date` >600s | `Clock skew exceeds limit … Run w32tm /resync` (GCS JWT would be rejected) |
| HL-09 | Clock unreachable | `HTTPSConnection` `OSError` | Gracefully `True, ""` with warning — skip, don't block backup |
| HL-10 | Invalid mode string | `mode="banana"` | `HealthError Invalid mode` |

---

## BRANCH D — Wake-on-LAN (`core/wol.py`)

| ID | Scenario | Trigger | Expected |
|---|---|---|---|
| WOL-01 | WoL disabled | `wol.enabled=false` | No packets, `ensure_server_online` returns True immediately |
| WOL-02 | Already online | SMB 445 `connect_ex==0` at start | `already online` — no magic packet sent |
| WOL-03 | Offline wake success | SMB 445 closed, send 3× dual broadcast | `send_magic_packet` to `255.255.255.255:9` + subnet `x.x.x.255:9` 3 rounds 5s (`G3`), then `wait_for_server` polls 300s/15s + stability 30s → SUCCESS |
| WOL-04 | Wake timeout NAS never wakes | SMB stays closed 300s | `WolTimeout: SMB not accessible within 300s` → `LAN_SKIPPED/FAILED` |
| WOL-05 | Subnet broadcast blocked | Global broadcast dropped by managed switch | Subnet-directed fallback must still deliver (prove with explicit `broadcast_address`) |
| WOL-06 | Invalid MAC format | `AA:BB:CC` | `ValueError` at config load (`config.py:124`) — refused before runtime |
| WOL-07 | Invalid broadcast IP | `999.999.0.1` | `ValueError Invalid broadcast_address` |

---

## BRANCH E — FY Rollover & Config & Folder Creation (`core/fy_rollover.py`, `core/time_utils.py`, `models/config.py`)

> Rollover runs **twice**: at boot `launch.py:201` + daily `rollover_check_flow` `serve.py:61` `06:00` (`G10`). Idempotent — crash-retry safe.

| ID | Scenario | Precondition | Trigger | Expected Operation |
|---|---|---|---|---|
| FY-01 | No rollover — same FY | `source FY25-26`, computed `FY25-26` | `detect_rollover` | `False` → `NO_ROLLOVER_NEEDED`, no file change |
| FY-02 | Rollover due — Apr 1 boundary | `source FY25-26`, computed `FY26-27` | `detect_rollover` | `True` → enters `rollover()` |
| FY-03 | No FY folders — opt-out permanent disabled | `source D:\DATA` (no `FYxx-xx`) + `lan \\share\DATA` | `detect_rollover` | Logs warning, returns `False` — valid choice, data accumulates same folder (`fy_rollover.py:108`) |
| FY-04 | 4-digit trap — `FY2026-27` in config | Path contains `FY2026-27` | `load_config` | `ValueError: 4-digit FY name … Rename … disables rollover forever` (`config.py:434`) — refuse at load, not at boundary |
| FY-05 | FY mismatch guard — source `FY25-26` vs lan `FY24-25` | Mismatch | `load_config` cross-field | `ValueError: CRITICAL DATA LOSS PREVENTION: source FY vs lan FY do not match!` (`config.py:447`) |
| FY-06 | Rollover happy — final backups succeed | Apr 1, both cloud+lan enabled, NAS online, GCS up | `run_final_backup` old FY `FY25-26` | `cloud_ok=True` (rclone 0/9) + `lan_ok=True` (robocopy 0-7) → proceed |
| FY-07 | Final cloud fails → rollover BLOCKED | GCS down on old FY | `run_final_backup` | `RolloverError: final backup failed for cloud` → config **unchanged**, retry next day, mail alert `flow.py:798` |
| FY-08 | Final LAN fails → BLOCKED (if lan enabled) | NAS offline at Apr 1 00:01 | `run_final_backup` | `RolloverError: final backup failed for LAN` |
| FY-09 | Only one dest enabled fails → BLOCKED | `cloud disabled, lan fails` | Same | BLOCKED (required list respects `enabled`) |
| FY-10 | Archive transition success | After final backups, `gcloud` on PATH, key valid | `run_archive_transition` `gcloud storage objects update --storage-class=ARCHIVE --recursive gs://bkt/FY25-26/` | Server-side metadata rewrite, returns `True`, `archive_ok=True` logged |
| FY-11 | Archive transition non-blocking fail | `gcloud` missing / auth fail / timeout 600s | Same | Logs WARNING, returns `False`, **rollover still proceeds** — operator retries manually (`fy_rollover.py:373`) |
| FY-12 | Create new FY folders — source OK, LAN offline | `source_root D:\` + `lan_root \\share` + `new FY26-27` | `create_new_fy_folders` | `D:\FY26-27` mkdir succeeds, `.AAM_TARGET_MOUNTED` on LAN fails → logs `ACTION REQUIRED: Manually create …` but does **not** block rollover (`fy_rollover.py:219`) |
| FY-13 | Config YAML atomic update preserves comments | `config.yaml` has comments | `update_config_yaml` ruamel round-trip + `os.replace` temp | `source_drive D:\FY25-26 → D:\FY26-27`, `lan_destination … → …FY26-27`, comments kept |
| FY-14 | Rollover idempotency — crash after folder create | Kill after folder creation before YAML update | Rerun `rollover` | Re-entrant: folders `exist_ok=True`, re-runs final backup? Actually detection still true until YAML updated — second run repeats final backup (safe, transactional via DB `run_id`) |
| FY-15 | Scheduled rollover daily 06:00 | 24×7 server never rebooted | `rollover_check_flow` daily cron | Same logic as boot, daily retries until Apr 1 succeeds |
| FY-16 | Gcloud auth stateless | `GOOGLE_APPLICATION_CREDENTIALS` injected, per-file key | `run_archive_transition` | `gcloud auth activate-service-account --key-file=` then `objects update` |
| FY-17 | Gcloud deploy/bin discovery priority | 1) `deploy\bin\gcloud.cmd` 2) `Program Files\…` 3) `%LOCALAPPDATA%` 4) `which` | `_resolve_gcloud` | Correct exe chosen (`fy_rollover.py:29`) |

**Folder creation contract:** Operator must create initial `FY26-27` + canary manually (`DEPLOYMENT_GUIDE.md:252`). Future years auto-created. Old FYs go to `D:\_OLD_FY_DATA\` outside automation.

---

## BRANCH F — Scheduler & Orchestration (`serve.py`, `flow.py`, `launch.py`)

| ID | Scenario | Trigger | Expected |
|---|---|---|---|
| SCH-01 | 4 deployments register with correct crons | `load_config` has `cloud 22:00 lan 21:00 weekly MON 08:00 monthly 1st 08:00 rollover 06:00 IST` | `backup.to_deployment` tags `cloud/lan`, `CronSchedule` validates (`config.py:387`) |
| SCH-02 | Invalid cron or timezone rejected at load | `cloud_cron="99 99 * * *"` or `timezone="Fake/Zone"` | `ValueError … rejected by Prefect's scheduler` — crash-loops avoided at install, not at 21:00 |
| SCH-03 | Missed run no catch-up | Agent down at scheduled time, up next day | Run is **skipped**, not caught-up, gap is expected (`DEPLOYMENT_GUIDE.md:400`) |
| SCH-04 | Orphan PENDING cancelled at boot | 2 PENDING flows left from crash, lock not held | `_cancel_orphaned_runs` cancels both → `Cancelled` |
| SCH-05 | Orphan RUNNING preserved if lock alive | RUNNING flow + `backup.lock PID alive` + live transfer | RUNNING **not** cancelled, PENDING still cancelled (`launch.py:108`) |
| SCH-06 | Stale lock cleanup | `backup.lock` PID dead/reused | Delete lock at boot, then cancel RUNNING too |
| SCH-07 | Concurrency 1 serializes cloud vs lan | `mode=all` or overlapping manual triggers | `concurrency("aam-backup",1,3600)` — one holds, other waits up to 1h |
| SCH-08 | Lock written inside concurrency block | Two flows race for slot | Winner writes `PID:create_time`, loser overwrites? Fixed `F13` — only holder writes inside block (`flow.py:878`) |
| SCH-09 | Stable run_id across Prefect retries | Task retry with same `FlowRunContext.id` | `f"{flow_run.id}-{mode}"` reused, DB `ON CONFLICT(run_id) DO UPDATE` |
| SCH-10 | Monotonic duration | NTP jump mid-run | `time.monotonic()` diff, not `pendulum.parse` wall-clock (`flow.py:705`) |
| SCH-11 | `mode` validation | `backup(mode="banana")` | `ValueError Invalid mode` |
| SCH-12 | All disabled → refuse | `lan.enabled=false && cloud.enabled=false` | `ValueError At least one destination must be enabled` (`config.py:418`) |

---

## BRANCH G — Manifest DB (`core/manifest.py`, `core/backup_repository.py`)

| ID | Scenario | Trigger | Expected |
|---|---|---|---|
| DB-01 | First open creates DDL + WAL | No `manifest.db` | `PRAGMA journal_mode=WAL`, tables `file_entries + run_history + db_meta` created |
| DB-02 | Bulk upsert 10K files fast | 10K entries via `bulk_upsert_synced` | Single `executemany` in 100-row chunks (700 params <999), 10-100× faster than loop |
| DB-03 | Collate NOCASE dedup | `Foo\Bar.txt` vs `foo/bar.txt` | `UNIQUE COLLATE NOCASE` — second upserts first |
| DB-04 | `get_cloud_synced_entries` for delta | 1000 cloud-synced rows | Returns `{path: (size,mtime)}` for pipeline diff, not private internals |
| DB-05 | `prune_stale_synced` self-healing | GCS manifest lost 2 files, DB still has them `synced` | Nulls `cloud_status`, deletes rows where both statuses null, returns pruned count |
| DB-06 | `delete_entries` chunking | 1200 removed paths | Chunk 500 to avoid `SQLITE_MAX_VARIABLE_NUMBER` |
| DB-07 | `insert_run` dedup on `run_id` | Retry same `run_id` | `ON CONFLICT DO UPDATE` merges, not duplicate row |
| DB-08 | `last_successful_run` | Status `LAN_COMPLETE` vs `LAN_PARTIAL` | Only `%_COMPLETE` counts, `PARTIAL` not success |
| DB-09 | Purge retention + VACUUM threshold | 100 days data, retention 90 | Deletes `< cutoff_iso(90)`, then `freelist >10000` triggers `VACUUM` (`G14`), else commit |
| DB-10 | Busy timeout under concurrency | Two threads hit dashboard + flow | `PRAGMA busy_timeout=30000`, `threading.Lock` per connection |
| DB-11 | Thread safety dashboard vs updater | `flow.py` commit while `ui.py:get_db` reads | `ManifestDB` reuse via global + TTL, but `Vacuum` vs read must not deadlock — proven via iteration |

---

## BRANCH H — Dashboard UI (`ui.py`)

| ID | Scenario | Trigger | Expected |
|---|---|---|---|
| UI-01 | Unauthenticated `/_health` | `GET /health` | 200 `healthy` + `source_accessible` bool, no auth |
| UI-02 | Auth enabled blocks `/` | `GET /` without session/`X-API-Key` | 303→`/login` for browsers, 401 JSON for API (`_require_auth`) |
| UI-03 | Login success | `POST /login` correct `api_key` (hmac compare) | `secrets.token_hex(32)` session, cookie `httponly samesite=lax 24h` |
| UI-04 | Rate limit triggers | 6 `POST /trigger/cloud` per 5min from same IP | 429 `Rate limit exceeded` (`_RATE_MAX_TRIGGER=5`) |
| UI-05 | Trigger while running | `POST /trigger/cloud` when Prefect RUNNING/PENDING exists | 400 `already_running` |
| UI-06 | Trigger success awaits deployment | `arun_deployment backup-cloud` resolves | 200 `triggered` + `flow_run.id`; failed deployment → 500 `trigger_failed` (`G15` inline await) |
| UI-07 | Config TTL refresh + DB path switch | `config.yaml` edited FY, wait 300s, `GET /status` | `_cfg()` reloads YAML, evicts `_DB_INSTANCE` if `database_path` changed, reconnects (`F13` RLock) |
| UI-08 | Report download no DB | `GET /report/weekly` before any backup | 503 `No database found. Run a backup first.` |
| UI-09 | Manual email report via UI | `POST /trigger/report/weekly/email` | `generate_report_html(7)` → `send_weekly_report` → 200 `sent` or 404 `no_data` or 500 `SMTP error` |
| UI-10 | Concurrent requests thread-safety | 5 parallel `GET /status` during config refresh | `_CFG_LOCK RLock` serializes — no `close()` mid-query 503s |
| UI-11 | `/status` contract | Valid session | JSON `firm, fy_prefix, schedule human, cloud.running/last_run/last_success, lan…, manifest lan_files/cloud_files, health source_free_gb, recent_runs[25]` |

---

## BRANCH I — Email Reporting (`core/report.py`)

| ID | Scenario | Trigger | Expected |
|---|---|---|---|
| REP-01 | Failure alert sent | `CLOUD_FAILED` with `send_on_failure=true` | `send_failure_alert` HTML table + error (truncated 1000) + attachment if >1000, subject `Backup Failure Alert — {firm} ({mode})` |
| REP-02 | Failure suppressed | `send_on_failure=false` | Return False, `skipping alert` |
| REP-03 | SMTP auth permanent fail | `SMTPAuthenticationError` | Log `permanent SMTP error`, return `False`, **no retry** |
| REP-04 | SMTP transient retries with jitter | `OSError` first 2 attempts, 3rd succeeds | Retry `10s * 2^(n-1)` ±20% jitter (`_SMTP_MAX_ATTEMPTS 3`), succeed on 3rd |
| REP-05 | SMTP all retries fail | 3× timeout | Log `failed after 3 attempts`, return False |
| REP-06 | CSV injection neutralized | `error_message="=cmd|'/C calc'!A0"` in run_history | `_csv_safe` prepends `'` so Excel renders literal, not formula (`G5`) |
| REP-07 | Weekly report HTML weekly 7d | 12 runs last 7d (mix C/P/F) | `generate_report_html` shows `Total 12, Success 8, NoChanges 1, Partial 1, Failed 2`, success rate 75%, 10-row table, `total_files/bytes` with `humanize` |
| REP-08 | Monthly report 30d attachment | Monthly trigger | `send_summary_report` attaches `CSV` of all runs un-truncated + HTML body, notice `CSV attached` |
| REP-09 | No runs in period | `get_runs_since 7` empty | `""` → caller skips mail + UI shows 404 `No runs found in last 7 days` |
| REP-10 | Gmail path (prod) | `smtp.gmail.com:587` TLS + app password | `STARTTLS → login → sendmail` path; port 465 uses `SMTP_SSL` |

---

## BRANCH J — Watchdog (`watchdog.py`)

| ID | Scenario | Trigger | Expected |
|---|---|---|---|
| WD-01 | Healthy — pass | `GET 127.0.0.1:4200/api/health 200` | Failures reset 0, check if `AamBackupAgent` RUNNING; breaker reset |
| WD-02 | 5 consecutive fails threshold | Fail 5× 60s | Enters restart branch; `<5` just sleep 60s |
| WD-03 | Transfer deferral 8h cap | API down + `rclone/robocopy` alive, defer 239× | Log `Deferring restart … transfer in progress`, sleep 120s. On 240th → error `Possible zombie`, delete lock, fall through to `sc stop` |
| WD-04 | Stale lock deferral 30m cap | API down + lock alive but **no** transfer proc, defer 14× | Warn `lock held but no transfer … possibly between rclone calls`. On 15th → delete lock + restart |
| WD-05 | No backup → prompt restart | API down + no lock + no transfer, service RUNNING | `sc stop AamPrefectServer` → NSSM auto-restart cycle |
| WD-06 | START_PENDING — wait | `STATE START_PENDING` | Log `transitioning — resetting failure counter`, sleep 60s, don't kick |
| WD-07 | STOPPED — auto-start with breaker | `STATE STOPPED` | `sc start AamPrefectServer` if `<3 starts/hour`, else `CRITICAL Manual intervention required` |
| WD-08 | Circuit breaker resets on RUNNING | Service recovers | `service_start_log.pop` so window reopens |
| WD-09 | Agent down while Prefect healthy | Prefect 200 but `AamBackupAgent STOPPED` | Auto-start Agent (`G4` — scheduler lives there!). Pre-fix this was silent total stoppage |
| WD-10 | Corrupted lock file | Lock contains `not_a_pid` or `-999` | Caught as generic `Exception`, treated as stale not crash-loop (`G6`) |
| WD-11 | `psutil` failure during transfer check | `process_iter` raises | Warn, return `False` — fail-safe to restart logic |

---

## BRANCH K — System / Deployment / Runtime

| ID | Scenario | Trigger | Expected |
|---|---|---|---|
| SYS-01 | First-time FY folders manual | Fresh install, no `FY26-27` | Operator creates `D:\FY26-27` + `\\NAS\FY26-27` + canary `.AAM_TARGET_MOUNTED` manually (`DEPLOYMENT_GUIDE.md:252`) |
| SYS-02 | Old FYs migrated outside automation | Legacy `FY23-24` still under `D:\` | Move to `D:\_OLD_FY_DATA\` and `rclone copy → :gcs:Archive/STANDARD` (`DEPLOYMENT_GUIDE.md:260`) |
| SYS-03 | Service Log On wrong → Access Denied | Install left `Local System` | Robocopy 16 `LAN_FAILED`, WD healthy — must document `services.msc → Log On → domain user` (`DEPLOYMENT_GUIDE.md:340`) |
| SYS-04 | Firewall blocks 8080/4200 | Windows Firewall default | Must run `netsh advfirewall … allow 8080/4200` or `bind_address 127.0.0.1` localhost only |
| SYS-05 | Active Hours protect headless reboot | Windows Update window `12-6` (18h max) | `04_check_readiness.ps1` fails if Active Hours wrong, reboot only 06:00-12:00 outside both backups |
| SYS-06 | Runtime_dir on HDD vs SSD | `C:\BackupAgent` DB on HDD | `sqlite_vacuum_freelist_threshold 10000 (~40MB)` reduces HDD I/O; busy_timeout 30000 |
| SYS-07 | GCS lifecycle rules idempotency | `gcs_lifecycle.json` applied | `gcloud storage buckets describe --format=lifecycle.rule` shows versioning ON + soft-delete cleared + lifecycle set |
| SYS-08 | Restore drill quarterly | Disaster simulation | `09_restore_from_gcs.bat` or `rclone copy :gcs:bkt/FY25-26 D:\RESTORED --transfers 8` + SHA compare |
| SYS-09 | Duplicate 05 test — readiness probe | Before install | `05_test_config.bat` runs Pydantic load → `✅ SUCCESS` |
| SYS-10 | Log retention rotated | `runtime_dir/logs/*.log` grow | Loguru rotation 10MB/24h, retention 5 files/30d, watchdog separate `watchdog_svc.log` |
| SYS-11 | Python/pinned toolchain | Upgrade attempt | `prefect==3.7.2 rclone 1.74.2 py3.12 gcloud pinned` — suite 1415 pass must stay green before deploy |

---

## Coverage summary

* LAN: 19 • Cloud: 13 • Health: 10 • WoL: 7 • FY: 17 • Scheduler: 12 • DB: 11 • UI: 11 • Report: 10 • Watchdog: 11 • System: 11 = **122 scenarios** (all realizable on your server+GCS; none mocked).
* `F1..F16 + G1..G15` historically-fixed regressions are embedded as distinct scenarios so they never regress.

**Next step:** Reply `go` or flag any scenario to add/trim, then we start **Step A function inventory → Step B real probes** (`health → lan dry-run → walk → cloud preflight → rclone size/manifest/diff → WoL → lock PID reuse → Gmail report`) and fix constraint mismatches one pass up to midpoint.
