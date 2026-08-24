# AAM Backup Automation V1 — Complete Real-World Scenario Catalog V2

> **Purpose:** Every realistic scenario this server will face, split by feature branch. Experimental proof only (real NAS + real GCS, zero mocks).
> **Added in V2:** Branches L/M/N so **every exported function in every file** has ≥1 scenario + explicit **Expected Operation** contract.
> **Status vocabulary:** `COMPLETE | PARTIAL | FAILED | NO_CHANGES | VERIFY_FAILED | SKIPPED | BLOCKED`
> **ID is stable** → test spec + code + `core/*` `file:line`.

**FY rule:** India FY Apr 1→Mar 31 = folder `FYxx-xx` via `core/time_utils.py:61 get_fy_prefix()`. Config must end with same FY on both paths or `models/config.py:447` refuses to start. `FY2026-27` 4-digit is rejected at load `models/config.py:423`.

## Reading contract — Expected Operation columns

| Column | Means |
|---|---|
| **Trigger** | What you do to provoke it |
| **Expected Op** | System MUST do this (observable) — includes `status` written to `run_history`, `mail?` (`core/report.py:108`), `NAS shutdown?` (`flow.py:627`), `DB?`, `Dashboard?`, `Log` substring, `Operator action` |

**Invariant:** Any `FAILED/BLOCKED` → mail within `~1min` via Gmail `smtp.gmail.com:587` + `run_history.error_message` persists, never silent `COMPLETE`. `PARTIAL` keeps NAS ON (`flow.py:629`).

---

## BRANCH A — LAN Backup (`core/lan_sync.py`, `core/lan_manifest.py`, `core/lan_preflight.py`)

| ID | Scenario | Precondition | Trigger | Expected Operation (must be observed) |
|---|---|---|---|---|
| LAN-01 | Golden mirror 3 files + nested | NAS `\\share\FY25-26` exists + canary `.AAM_TARGET_MOUNTED` present, source 1KB+2MB+nested | `lan` flow 21:00 IST | `status=LAN_COMPLETE` exit 1, all 3 land on NAS sizes equal, `files_copied=3` `bytes_copied` >0, no mail, `lan_sync exit 1 → LAN_COMPLETE` log, dashboard shows Recent run |
| LAN-02 | Mirror delete orphan | LAN-01 had 3, delete 1 on source | next `lan` sync | deleted disappears on NAS (`/MIR=/E /PURGE`), 2 remain, `LAN_COMPLETE` |
| LAN-03 | Canary missing → abort | delete canary on NAS, create file on source | `run_lan_dry_run` | `HealthError` with full UNC to canary, zero bytes copied, `SKIPPED`, mail if flow-level, log `Canary file` + path |
| LAN-04 | UNC unreachable | `\\IP\BAD_SHARE\` | preflight/sync | fail fast message contains bad UNC, no source delete, `SKIPPED` or `LAN_FAILED` |
| LAN-05 | Share permission denied | service Log On = LocalSystem | robocopy | exit 16+ `LAN_FAILED`, `error` = 100KB log tail, `files_failed` counted |
| LAN-06 | Locked file `msvcrt.LK_NBLCK` | hold lock on `locked_doc.txt` | sync with lock | no crash → `LAN_PARTIAL` bit3 or `LAN_FAILED` bit4, `error` has `** FAILED:` lines `files_failed=1` |
| LAN-07 | Long path >260 + unicode | `अकाउंट\…very_long_260+` | sync | single file fails → `PARTIAL` not whole FAILED, rest COMPLETE |
| LAN-08 | Large 50MB integrity | `os.urandom(50MB)` + SHA256 | sync + SHA on NAS | `LAN_COMPLETE` SHA equal, `/Z /ZB` no truncation |
| LAN-09 | Empty source | `iterdir` empty | `check_source_drive` | `Source drive appears empty` → `SKIPPED`, no NAS delete |
| LAN-10 | Source missing | `D:\FY25-26` missing | health | `Source drive not accessible` → `SKIPPED` |
| LAN-11 | HDD scale dry-run timeout | 1M files walk ~8min | `run_lan_dry_run` with old 300s | must use `dry_run_timeout_seconds=900` (tunable 7200) no `TimeoutExpired` |
| LAN-12 | Post-walk SMB drop after sync | `walk_lan_destination` raises `OSError` after sync | `lan_snapshot_after_task` | return `None`, CRITICAL log, sync outcome **unaffected** `LAN_COMPLETE` kept, DB skip `G14` |
| LAN-13 | Diff correctness | before 10, +1 new +1 modified | `diff_snapshots` | `added 1 modified 1 removed 0 unchanged 9` → `files_copied/bytes` correct in `extended_metrics` |
| LAN-14 | Extra file anomaly on NAS | extra file on NAS only | sync exit 2-3/4-7 | `LAN_PARTIAL` with `anomaly_details` tail, no `error`, no mail, warning only |
| LAN-15 | Copy errors bit3 8-15 | 2 locked +1 good | sync | `LAN_PARTIAL` `error` 100KB tail `files_failed=2` → mail |
| LAN-16 | Fatal bit4 16+ | wrong params / robocopy missing | sync | `LAN_FAILED` -1/16+ `error` reason |
| LAN-17 | Orphan log cleanup `G8` | kill robocopy → `robocopy_sync_*.log` in `%TEMP%` | next lan start `cleanup_orphaned_robocopy_logs(24h)` | deletes only >24h, count logged, live log untouched; tail seek via `F15` |
| LAN-18 | Shutdown on COMPLETE | `shutdown_after_backup=true` + `wol.enabled` | after `LAN_COMPLETE` | `shutdown_server(NAS_IP)` issued |
| LAN-19 | Shutdown skipped on PARTIAL | same config | after `LAN_PARTIAL` | **no shutdown** + alert `NAS was NOT shut down; next run will re-sync` |
| LAN-20 | `/NC` flag forbidden | build flags contain `/NC` | `build_robocopy_command` | `ValueError /NC flag suppresses file class labels` `core/lan_sync.py:34` |

---

## BRANCH B — Cloud Backup (`core/cloud_sync.py`, `core/cloud_verify.py`, `core/cloud_reporter.py`, `core/cloud_preflight.py`)

| ID | Scenario | Precondition | Trigger | Expected Operation |
|---|---|---|---|---|
| CLOUD-01 | Golden sync new files | bucket `gs://bkt/FY25-26` empty, 3 files source | cloud 22:00 | `rclone sync --check-first --fast-list --error-on-no-transfer` exit0 `CLOUD_COMPLETE`, manifest 3 |
| CLOUD-02 | No changes idempotent | source unchanged | rerun cloud | exit9 `CLOUD_NO_CHANGES_COMPLETE` (success) |
| CLOUD-03 | Preflight 2-probe <3s | source readable + bucket auth valid | `run_cloud_dry_run` | probe A source + probe B `rclone lsjson --max-depth 0` — fails fast before HDD scan |
| CLOUD-04 | Verify must pass | after sync | `verify_cloud_integrity rclone check --combined` | `verified=false` with missing/added/modified → `CLOUD_VERIFY_FAILED` mail + `RuntimeError` `F1`, not COMPLETE |
| CLOUD-05 | Size/manifest/diff timeouts `F7` | 1M objects | `get_cloud_size/size 300s manifest 900s diff 1800s` | counts → `extended_metrics` JSON, not `0 files` timeout |
| CLOUD-06 | Key/bucket auth fail | key missing/empty/bad JSON, bucket typo | preflight/sync | exit7 `CLOUD_FAILED` error contains path/key reason |
| CLOUD-07 | Transient 4-5 retry | flaky net | sync `--retries 3 --retries-sleep 30s` flow `max_attempts 3` | `CLOUD_PARTIAL` then retry whole sync |
| CLOUD-08 | Timeout >6h resumable `G7` | `subprocess_timeout_seconds=10` hold rclone | `TimeoutExpired` | `CLOUD_FAILED` -1 `rclone sync is resumable; progress preserved` |
| CLOUD-09 | Bandwidth cap `10M` | 50MB file | sync measured | wall ≥ file/bw, no error |
| CLOUD-10 | Storage class propagation | bucket STANDARD config NEARLINE | `temp_rclone_config` | `--gcs-storage-class` on new objects |
| CLOUD-11 | Delta metrics DB vs manifest | DB 10, manifest +1 size-changed | pipeline delta | `files_copied` >0.01 byte guard + mtime >1.1s `flow.py:498` |
| CLOUD-12 | Empty source blocked upstream | source empty (health would SKIPPED) | cloud pipeline | `SKIPPED` before sync, not `VERIFY_FAILED` |
| CLOUD-13 | rclone missing | delete `deploy\bin\rclone.exe` + PATH | health/sync | `rclone not found` / `FileNotFoundError → CLOUD_FAILED -1` |

---

## BRANCH C — Health Gate (`core/health.py:18`)

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| HL-01 | Source missing | `Path.exists==False` | `not accessible` → `HealthError` → `SKIPPED`, no mail if pre-check? actually mail via flow catch |
| HL-02 | Source empty | `any(iterdir)==False` | `appears empty` |
| HL-03 | Permission denied | `PermissionError` | `permission denied` |
| HL-04 | Low free <min | free 0.2GB <1GB | `critically low on space: 0.2 GB free (minimum: 1 GB)` |
| HL-05 | rclone missing (cloud/all) | `resolve_binary rclone==None` | `rclone not found in PATH` |
| HL-06 | robocopy missing (lan/all) | `resolve_binary robocopy==None` | `robocopy not found` |
| HL-07 | GCS key missing/empty (cloud) | no file / size0 | `GCS key file not found/empty` |
| HL-08 | Clock skew >600s | vs `www.googleapis.com Date` >600s | `Clock skew exceeds limit … w32tm /resync` |
| HL-09 | Clock unreachable | `OSError` on HTTPS | `True ""` warning — don't block backup |
| HL-10 | Invalid mode | `banana` | `Invalid mode` |
| HL-11 | `parsedate_to_datetime` ValueError | bad Date header | `True ""` warning — skip |

---

## BRANCH D — WoL (`core/wol.py`)

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| WOL-01 | Disabled | `wol.enabled=false` | no packet, return True |
| WOL-02 | Already online | SMB 445 `connect_ex==0` | `already online` no packet |
| WOL-03 | Wake success dual broadcast | SMB closed | `255.255.255.255:9` + subnet `x.x.x.255:9` 3×5s `G3` then poll 300s/15s + stability 30s → SUCCESS |
| WOL-04 | Timeout never wakes | SMB stays closed 300s | `WolTimeout: SMB not accessible within 300s` → `SKIPPED/FAILED` |
| WOL-05 | Global blocked fallback | managed switch drops global | subnet-directed `broadcast_address` explicit still delivers |
| WOL-06 | Invalid MAC | `AA:BB:CC` | `ValueError` at config load `config.py:124` |
| WOL-07 | Invalid broadcast | `999.999.0.1` | `ValueError Invalid broadcast_address` |
| WOL-08 | `_smb_port_open` OSError | socket error | `False` safely |
| WOL-09 | `_send_magic_packet` per-round OSError | one round fails | warning per round, continue other rounds |

---

## BRANCH E — FY Rollover / Folder / Config (`core/fy_rollover.py`, `core/time_utils.py`)

> Runs twice: boot `launch.py:201` + daily 06:00 `rollover_check_flow` `serve.py:61` `G10`. Idempotent.

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| FY-01 | Same FY no-op | `FY25-26` vs computed `FY25-26` | `detect_rollover False` → `NO_ROLLOVER_NEEDED` |
| FY-02 | Apr 1 due | `FY25-26` vs `FY26-27` | `True` → `rollover()` |
| FY-03 | No FY folders opt-out | `D:\DATA` `\\share\DATA` | warning `rollover is disabled by configuration` `fy_rollover.py:108`, return False |
| FY-04 | 4-digit `FY2026-27` trap | path contains it | `ValueError 4-digit FY … disables rollover forever` `config.py:434` at load |
| FY-05 | FY mismatch guard | `FY25-26` vs `FY24-25` | `CRITICAL DATA LOSS PREVENTION… do not match!` `config.py:447` |
| FY-06 | Happy final backups | Apr 1 both enabled NAS+GCS up | `cloud_ok True` (0/9) + `lan_ok True` (0-7) → proceed |
| FY-07 | Final cloud fail → BLOCKED | GCS down old FY | `RolloverError final backup failed for cloud` config unchanged retry next day mail `flow.py:798` |
| FY-08 | Final LAN fail → BLOCKED | NAS offline | `RolloverError for LAN` |
| FY-09 | One dest enabled only | cloud disabled lan fail | BLOCKED respects `enabled` list |
| FY-10 | Archive success `gcloud` | after final | `gcloud storage objects update --storage-class=ARCHIVE --recursive gs://bkt/FY25-26/` server-side only `archive_ok True` |
| FY-11 | Archive non-blocking fail | gcloud missing/auth/timeout 600s | WARNING returns False rollover still proceeds `fy_rollover.py:373` |
| FY-12 | New folders source OK LAN offline | `create_new_fy_folders` | `D:\FY26-27` mkdir ok, LAN mkdir fail logs `ACTION REQUIRED: Manually create` no block `fy_rollover.py:219` |
| FY-13 | YAML atomic + comments | comments in file | `ruamel.yaml` round-trip `os.replace` tmp `source FY25-26→FY26-27` comments kept |
| FY-14 | Idempotency crash after mkdir | kill before YAML | re-entrant `exist_ok=True` repeats final backup safely via stable `run_id` |
| FY-15 | Daily 06:00 scheduler | never rebooted | `rollover_check_flow` daily retries till Apr 1 |
| FY-16 | Stateless gcloud auth | `GOOGLE_APPLICATION_CREDENTIALS` | `gcloud auth activate-service-account --key-file` then update |
| FY-17 | gcloud discovery priority | 1) `deploy\bin\` 2) `Program Files` 3) `LOCALAPPDATA` 4) `which` | correct exe `fy_rollover.py:29` |
| FY-18 | `_fy_name` parsing | `E:\SOURCE\FY26-27` + `\\srv\share\FY26-27` | returns upper `FY26-27`, else None |
| FY-19 | `_parent_path` / `_child_path` separator | `E:\A\FY25-26` vs `/mnt/a/FY25-26` vs `\\srv\share\FY25-26` | preserves `\` vs `/` style |
| FY-20 | `get_fy_prefix` boundary Mar 31 vs Apr 1 | `2026-03-31` → `FY25-26`, `2026-04-01` → `FY26-27` | `time_utils.py:61` |

---

## BRANCH F — Scheduler & Orchestration (`serve.py`, `flow.py`, `launch.py`)

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| SCH-01 | 4 deployments register | crons `lan 21:00 cloud 22:00 weekly MON 08:00 monthly 1st 08:00 rollover 06:00 IST` | `backup.to_deployment` tags `cloud/lan`, `CronSchedule` validates |
| SCH-02 | Invalid cron/tz rejected | `99 99 * * *` or `Fake/Zone` | `ValueError … rejected by Prefect's scheduler` `config.py:387` at load not at 21:00 |
| SCH-03 | Missed no catch-up | agent down at scheduled time | skipped not caught-up gap expected `DEPLOYMENT_GUIDE.md:400` |
| SCH-04 | Orphan PENDING cancelled | 2 PENDING left lock not held | `_cancel_orphaned_runs` → `Cancelled` |
| SCH-05 | RUNNING preserved if lock alive | RUNNING + `backup.lock PID alive` | RUNNING not cancelled PENDING still cancelled `launch.py:108` |
| SCH-06 | Stale lock deleted at boot | PID dead/reused | delete lock then cancel RUNNING |
| SCH-07 | Concurrency 1 | `mode=all` overlap | `concurrency("aam-backup",1,3600)` one waits up to 1h |
| SCH-08 | Lock inside concurrency | two race | only holder writes `PID:create_time` inside block `flow.py:878` `F13` |
| SCH-09 | Stable run_id | retry same `FlowRunContext.id` | `f"{flow_run.id}-{mode}"` DB `ON CONFLICT DO UPDATE` |
| SCH-10 | Monotonic duration | NTP jump | `time.monotonic()` diff not wall-clock `flow.py:705` |
| SCH-11 | Mode validation | `banana` | `ValueError Invalid mode` |
| SCH-12 | All disabled refuse | `lan=false && cloud=false` | `At least one destination must be enabled` `config.py:418` |
| SCH-13 | `pause_on_shutdown=False` | Ctrl+C `serve` | deployments stay active across restarts `serve.py:80` |
| SCH-14 | Prefect API 300s retry at boot | Prefect not ready 45s after watchdog restart | wait loop `300s/10s` `launch.py:173` not sc failure |
| SCH-15 | `_ensure_concurrency_limit` upsert | first + second boot | global `aam-backup limit 1` + tag limit created, second `pass` if exists |

---

## BRANCH G — Manifest DB (`core/manifest.py`, `core/backup_repository.py`)

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| DB-01 | First open DDL WAL | no `manifest.db` | `journal_mode=WAL` tables `file_entries + run_history + db_meta schema_version 1` |
| DB-02 | Bulk 10K `bulk_upsert_synced` | 10K entries | `executemany` 100-row chunks 700 params <999 |
| DB-03 | NOCASE dedup | `Foo\Bar.txt` vs `foo/bar.txt` | `UNIQUE COLLATE NOCASE` upsert same row |
| DB-04 | `get_cloud_synced_entries` | 1000 rows | `{path: (size,mtime)}` for delta |
| DB-05 | `prune_stale_synced` | GCS lost 2 | nulls `cloud_status` deletes both-null rows returns pruned count |
| DB-06 | `delete_entries` chunk 500 | 1200 removed | chunk 500 avoids `SQLITE_MAX_VARIABLE_NUMBER` |
| DB-07 | `insert_run` dedup | same `run_id` retry | `ON CONFLICT DO UPDATE` |
| DB-08 | `last_successful_run` | `COMPLETE` vs `PARTIAL` | only `%_COMPLETE` counts |
| DB-09 | Purge + VACUUM `G14` | 100d data retention 90 | delete `<cutoff` then `freelist>10000` → `VACUUM` |
| DB-10 | Busy timeout concurrency | dashboard + flow | `PRAGMA busy_timeout=30000` + `threading.Lock` |
| DB-11 | `record_sync_results` used by cloud/lan | after manifest | marks `synced` via `bulk_upsert` + `delete` removed |
| DB-12 | `record_run_history` true status | exception after init | records `CLOUD_FAILED` not `SKIPPED` `F2` `flow.py:529` |
| DB-13 | `upsert_file_entry` COALESCE | existing `md5` vs new null | `COALESCE` keeps old `md5` |
| DB-14 | `file_count` invalid field | `status_field="bad"` | `ValueError must be one of {lan_status, cloud_status}` |
| DB-15 | `wal_checkpoint TRUNCATE` | after run | truncates WAL to prevent bloat |
| DB-16 | `get_synced_paths` mode guard | `mode=bad` | `ValueError mode must be cloud or lan` |
| DB-17 | `update_checksums` bulk | 100 paths | `executemany` `md5_checksum` + `updated_at` |

---

## BRANCH H — Dashboard UI (`ui.py`)

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| UI-01 | `GET /health` unauth | `health()` | 200 `healthy` + `source_accessible` bool via `Path.exists` thread |
| UI-02 | Auth block `/` | no session/`X-API-Key` | 303→`/login` browsers else 401 JSON `_require_auth` |
| UI-03 | Login success | `POST /login` correct `api_key` hmac | `token_hex(32)` session cookie `httponly samesite=lax 24h` |
| UI-04 | Rate limit | 6 `POST /trigger/cloud` /5min same IP | 429 `Rate limit exceeded` `_RATE_MAX_TRIGGER=5` |
| UI-05 | Trigger while running | RUNNING/PENDING exists | 400 `already_running` |
| UI-06 | Trigger success await `G15` | `arun_deployment backup-cloud` | 200 `triggered` + `flow_run.id`; fail → 500 `trigger_failed` |
| UI-07 | Config TTL 300s + DB switch | edit FY wait 300s `GET /status` | `_cfg()` reload YAML evicts `_DB_INSTANCE` if `database_path` changed `F13` RLock |
| UI-08 | `GET /report/weekly` no DB | before backup | 503 `No database found. Run a backup first.` |
| UI-09 | Manual email via UI | `POST /trigger/report/weekly/email` | `generate_report_html(7)` → `send_weekly_report` → 200 `sent`/404 `no_data`/500 `SMTP error` |
| UI-10 | Concurrent RLock | 5× `GET /status` during refresh | `_CFG_LOCK RLock` serializes — no `close()` mid-query 503 |
| UI-11 | `GET /status` contract | valid session | JSON `firm fy_prefix schedule human cloud.running/last_run/last_success lan… manifest lan_files/cloud_files health source_free_gb recent_runs[25]` |
| UI-12 | Login rate limit 10/5min | 11 `POST /login` | 429 login limit `_RATE_MAX_LOGIN` |
| UI-13 | Report download 429 | 11 `GET /report/weekly` | 429 `_RATE_MAX_REPORT` |
| UI-14 | Session TTL 24h expiry | token age 25h | `_validate_session` deletes + requires re-login |
| UI-15 | `cron_to_human` display | `0 22 * * *` `Asia/Kolkata` | `Daily at 22:00 Kolkata` etc. |

---

## BRANCH I — Email Reporting (`core/report.py`)

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| REP-01 | Failure alert | `CLOUD_FAILED` `send_on_failure=true` | HTML table + error truncated 1000 + attachment if >1000 subject `Backup Failure Alert — {firm} ({mode})` |
| REP-02 | Suppressed | `send_on_failure=false` | False `skipping alert` |
| REP-03 | Permanent SMTP | `SMTPAuthenticationError` | `permanent SMTP error` no retry |
| REP-04 | Transient retry jitter | `OSError` 2× then success | retry `10s*2^(n-1)` ±20% jitter `3 attempts` success on 3rd |
| REP-05 | All retries fail | 3× timeout | `failed after 3 attempts` False |
| REP-06 | CSV injection `G5` | `=cmd|'/C calc'!A0` | `_csv_safe` prepends `'` |
| REP-07 | Weekly HTML 7d 12 runs mix | 12 runs | `Total 12 Success 8 NoChanges1 Partial1 Failed2 success 75%` 10-row table `humanize` bytes |
| REP-08 | Monthly CSV attach | 30d | `send_summary_report` HTML + CSV all rows un-truncated notice `CSV attached` |
| REP-09 | No runs in period | `get_runs_since 7` empty | `""` → caller 404 `No runs found in last 7 days` |
| REP-10 | Gmail 587 TLS vs 465 SSL | `smtp.gmail.com:587` | `STARTTLS → login → sendmail`; 465 → `SMTP_SSL` |
| REP-11 | `is_email=True` CSV notice | email path | `<p>CSV attached` notice in HTML |
| REP-12 | Status display | `CLOUD_NO_CHANGES_COMPLETE` | maps to `No Changes`, `*_PARTIAL`→`Partial`, `*_FAILED`→`Failed` |

---

## BRANCH J — Watchdog (`watchdog.py`)

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| WD-01 | Healthy pass | `GET 127.0.0.1:4200/api/health 200` | failures 0 breaker reset, check `AamBackupAgent RUNNING` |
| WD-02 | 5 fails threshold | fail 5×60s | enters restart branch `<5` sleep 60s |
| WD-03 | Transfer defer 8h cap | API down + `rclone/robocopy` alive 239× | `Deferring restart … transfer in progress` sleep 120s; on 240th `Possible zombie` delete lock → `sc stop` |
| WD-04 | Stale lock 30m cap | API down + lock alive no transfer 14× | warn `lock held but no transfer … possibly between rclone calls`; on 15th delete + restart |
| WD-05 | No backup prompt | API down no lock no transfer RUNNING | `sc stop AamPrefectServer` → NSSM cycle |
| WD-06 | `START_PENDING` wait | `STATE START_PENDING` | `transitioning — resetting failure counter` sleep 60s |
| WD-07 | `STOPPED` with breaker | `STATE STOPPED` | `sc start` if `<3/hour` else `CRITICAL Manual intervention required` |
| WD-08 | Breaker reset | recovers RUNNING | `service_start_log.pop` window reopens |
| WD-09 | Agent STOPPED while Prefect healthy | Prefect 200 Agent STOPPED | auto-start Agent `G4` — before was silent total stoppage |
| WD-10 | Corrupt lock | `not_a_pid` / `-999` | generic `Exception` → stale not crash-loop `G6` |
| WD-11 | `psutil` failure | `process_iter` raises | warn `False` fail-safe |
| WD-12 | `_resolve_paths` fallback | `config.yaml` missing/invalid | keeps hardcoded `C:\BackupAgent\backup.lock` `C:\BackupAgent\logs` |
| WD-13 | `REQUEST_TIMEOUT 30s` per check | slow API | single check returns False but not crash |

---

## BRANCH K — System / Deploy / Runtime

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| SYS-01 | First FY folders manual | fresh install | operator `D:\FY26-27` + `\\NAS\FY26-27` + canary manual `DEPLOYMENT_GUIDE.md:252` |
| SYS-02 | Old FYs outside automation | legacy under `D:\` | move to `D:\_OLD_FY_DATA\` + `rclone copy → Archive` |
| SYS-03 | Log On wrong | `Local System` | robocopy 16 `LAN_FAILED` WD healthy — fix `services.msc → Log On → domain user` |
| SYS-04 | Firewall 8080/4200 | default | `netsh advfirewall … allow 8080/4200` or `127.0.0.1` only |
| SYS-05 | Active Hours `12-6` 18h | wrong values | `04_check_readiness.ps1` fails reboot only 06-12 outside backups |
| SYS-06 | `runtime_dir` HDD vs SSD | `C:\BackupAgent` DB on HDD | `vacuum threshold 10000 (~40MB)` + `busy 30000` |
| SYS-07 | GCS lifecycle idempotency | `gcs_lifecycle.json` | versioning ON soft-delete cleared lifecycle set |
| SYS-08 | Restore drill quarterly | disaster | `09_restore_from_gcs.bat` or `rclone copy :gcs:bkt/FY25-26 D:\RESTORED --transfers 8` + SHA |
| SYS-09 | `05_test_config.bat` readiness | before install | `✅ SUCCESS` |
| SYS-10 | Log rotation | logs grow | 10MB/24h retain 5/30d watchdog separate `watchdog_svc.log` |
| SYS-11 | Pinned toolchain | upgrade try | `prefect==3.7.2 rclone 1.74.2 py3.12` suite 1415 pass green before deploy |

---

## BRANCH L — Core Utilities (every function in `core/process.py`, `core/time_utils.py`, `core/rclone_config.py`, `core/hashing.py`, `core/shutdown.py`, `core/logging.py`, `core/fy_router.py`, `core/backup_repository.py`)

| ID | File:function `line` | Scenario | Expected Operation |
|---|---|---|---|
| L-01 | `process.py:22 _get_create_time` negative PID | lock `-5:123` | `None` (ValueError mapped) no crash `G6` |
| L-02 | `process.py:22 _get_create_time` huge PID | `999999999999` | `None` OverflowError mapped |
| L-03 | `process.py:40 write_lock` atomic | concurrent `write_lock` | `mkstemp + os.replace` never partial read, `content PID:ct.6f` |
| L-04 | `process.py:71 read_lock_alive` new format live | `12345:1717000000.123456` same PID same ct ±0.1s | `(True, pid)` |
| L-05 | `process.py:71` PID reused | same PID diff ct >0.1s | `(False, pid)` — stale |
| L-06 | `process.py:71` legacy bare PID alive | `12345` PID exists | `(True, pid)` via `pid_exists` |
| L-07 | `process.py:71` corrupted `not_a_pid` | `not_a_pid` | `(False, None)` |
| L-08 | `process.py:71` AV exclusive lock | `PermissionError` on read | `(True, -1)` fail-safe assume alive |
| L-09 | `process.py:145 resolve_binary` `deploy\bin` priority | `deploy\bin\rclone.exe` exists | returns `deploy\bin\rclone.exe` not `which` |
| L-10 | `process.py:145` `.exe` suffix | name `rclone` | checks `rclone.exe` fallback |
| L-11 | `time_utils.py:28 now_iso` | call | `2026-…+05:30` IST iso with offset |
| L-12 | `time_utils.py:49 cutoff_iso` | `cutoff_iso(7)` | `now-7d` IST iso |
| L-13 | `time_utils.py:61 get_fy_prefix` Mar vs Apr | `2026-03-31` / `2026-04-01` | `FY25-26` / `FY26-27` |
| L-14 | `time_utils.py:85 cron_to_human` `MON` | `0 22 * * MON Asia/Kolkata` | `Every Monday at 22:00 Kolkata` |
| L-15 | `time_utils.py:85` dom `1st` | `0 8 1 * *` | `1st of month at 08:00 Kolkata` 11th→`th` fix `F14` |
| L-16 | `rclone_config.py:temp_rclone_config` | context manager | temp file with `type=gcs service_account_file location project_number storage_class` auto-deleted |
| L-17 | `hashing.py:compute_md5` | 1KB file | hex digest correct |
| L-18 | `shutdown.py:shutdown_server` | `LAN_COMPLETE` + `wol.enabled` | `shutdown /s /m \\NAS_IP /t 00` via subprocess |
| L-19 | `logging.py:configure` | `runtime_dir/logs` | Loguru rotate 10MB retain 30d + bridge to Prefect |
| L-20 | `fy_router.py:get_fy_prefix` wrapper | delegates `time_utils.get_fy_prefix` | same as L-13 |
| L-21 | `backup_repository.py:record_run_history` phase sync fail | `phase=sync` exception | inserts `CLOUD_FAILED`/`LAN_FAILED` not `SKIPPED` `F2` |
| L-22 | `backup_repository.py:record_sync_results` | cloud manifest | calls `bulk_upsert_synced` + `delete_entries` removed |

---

## BRANCH M — Flow / Launch / Serve entry (`flow.py`, `launch.py`, `serve.py`)

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| M-01 | `flow.py:62 health_check_task` cloud mode missing key | `pre_backup_health` | `HealthError` raises `SKIPPED` recorded |
| M-02 | `flow.py:106 cloud_sync_task` partial mapping | rclone 4 | `CLOUD_PARTIAL` not `FAILED` |
| M-03 | `flow.py:131 cloud_verify_and_report_task` gather | after sync | `verify + size + manifest + diff` via temp config |
| M-04 | `flow.py:323 cloud_publish_artifact_task` | post verify | `create_markdown_artifact key=cloud-backup-summary` with status/files/GB/verified |
| M-05 | `flow.py:266 lan_sync_task` `files_failed` | robocopy 8 with 2 `** FAILED:` | `files_failed=2` persisted |
| M-06 | `flow.py:682 _record_run` monotonic | monotonic_start passed | duration = `monotonic - start` not wall |
| M-07 | `flow.py:830 backup mode=all` cloud→lan seq | `mode all` both enabled | cloud pipeline then lan pipeline, `ExceptionGroup` if either fail, mail summary, purge `db_retention_days` after |
| M-08 | `launch.py:39 _check_prefect_api` | `GET /api/health 200` | `True`, else `False` |
| M-09 | `launch.py:53 _ensure_concurrency_limit` | first boot | upsert global `aam-backup 1` + tag limit 1 |
| M-10 | `launch.py:88 _cancel_orphaned_runs` respects lock | RUNNING lock alive | cancel PENDING only skip RUNNING |
| M-11 | `launch.py:195 rollover` boot | Apr 1 | `rollover()` before scheduler, `RolloverError` non-fatal |
| M-12 | `serve.py:16 _deployments` | `load_config` | returns 5 deployments `backup-cloud lan report-monthly rollover-check` with `Cron(tz)` `pause_on_shutdown=False` |

---

## BRANCH N — Config Validation matrix (`models/config.py`)

| ID | Scenario | Trigger | Expected Operation |
|---|---|---|---|
| N-01 | `source_drive` empty | `""` | `ValueError source_drive must not be empty` |
| N-02 | `lan_destination` not UNC | `D:\share` | `LAN destination must be a UNC path` |
| N-03 | `runtime_dir` derive | empty `database_path` | derives `C:\BackupAgent\manifest.db` + `logs` ends `.db` else ValueError |
| N-04 | `wol.mac_address` empty while enabled | `enabled=true mac=""` | `wol.mac_address is required when wol.enabled is true` `config.py:130` |
| N-05 | `wol.broadcast_address` invalid | `999.0.0.1` | `Invalid broadcast_address IPv4` |
| N-06 | `cloud.bucket` empty while enabled | `enabled=true bucket=""` | `cloud.bucket is required when cloud.enabled is true` `config.py:212` |
| N-07 | `storage_class` lower | `standard` | uppercased `STANDARD` |
| N-08 | `bandwidth_limit` bad | `10` | `Invalid bandwidth_limit Format: 10M` |
| N-09 | `dashboard.api_key` empty with `auth_enabled` | `auth true key ""` | `api_key must be set when auth_enabled is True` |
| N-10 | Schedule invalid cron `99 99 * * *` | load | `rejected by Prefect's scheduler` `config.py:387` |
| N-11 | Invalid tz `Fake/Zone` | load | `ValueError … timezone` |
| N-12 | All disabled | `lan false cloud false` | `At least one destination must be enabled` |
| N-13 | Unknown key `send_on_success` still present | old YAML has it | loads fine `ignored safely` `config.py:245` |
| N-14 | `get_broadcast_address` derive | `server 192.168.10.10` empty broadcast | returns `192.168.10.255` |

---

## Coverage V2 summary

* A 20 + B 13 + C 11 + D 9 + E 20 + F 15 + G 17 + H 15 + I 12 + J 13 + K 11 + L 22 + M 12 + N 14 = **204 scenarios** — every exported function now has ≥1 row, each with **Trigger → Expected Op (status/mail/shutdown/DB/log/operator)** for data-protection proof.
