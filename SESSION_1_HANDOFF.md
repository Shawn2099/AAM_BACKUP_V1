# SESSION 1 HANDOFF — AAM Backup Reliability (2026-08-20)

> Senior test/debug/reliability session. Goal: make the backup app stable and
> reliable — no missed files, no silent failures, no redesign.
> Everything below is verified by execution against the REAL environment
> (real GCS bucket, real SMB share, real Gmail SMTP, live Prefect server).

---

## 1. Current state (as of 2026-08-20 ~12:55 IST — DEPLOY COMPLETE)

| Item | State |
|---|---|
| Production code | `C:\AAMBackup` @ branch `reliability-2026-08-20` commit **2bb8024** (was ff779d3 — 8 commits behind) |
| Workspace repo | `C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1`, branch `reliability-2026-08-20` @ **2b3db32** (docs-only commit ahead of deployed **2bb8024**; main untouched @ 0b3b417) |
| Services | All 3 **Running** on new code: AamPrefectServer (fresh, API healthy, port 4200), AamBackupAgent (agent proc 12:50:46, dashboard :8080 → 200), AamWatchdog (fresh) — no orphan processes, 3 nssm hosts |
| Deployments | **All 5 served** (verified via API): backup-cloud, backup-lan, weekly-report, monthly-report, **rollover-check** (fix L1 proven live) |
| Prod data | `E:\FY26-27` = 572 files / 56,982,882 B (matches 08-18 prod manifest); GCS `aam-backup-demo-innovizta/FY26-27` = same 572 (`rclone check`: 0 diffs); DB 572 cloud entries |
| Prod config | `C:\AAMBackup\config.yaml` SHA256 `2F75FBC9AFD30EE5577274E6201B9888DBF4C53E7DC24276F2FB854BCA28D750` — **never touched by deploy** (untracked in git). Backups: `C:\AAMBackup\deploy\config.yaml.pre-reliability-2026-08-20.bak` and `restore\PROD_config.yaml.2026-08-20` (workspace) |
| Test suite | **1439 passed, 53 skipped, 0 failed, exit 0** (was 1415 + exit-1 noise) |
| Prod schedules | cloud `0 22 * * *` IST, lan `0 21 * * *` IST, weekly Mon, monthly 1st, rollover-check (NOW actually scheduled — see fix L1) |

### Data restore (DONE, user-approved)
- Purged cloud FY26-27 data restored from GCS **versions** (572/572 recoverable,
  0 unrecoverable — `restore/phase1_crosscheck.py`, manifest `restore/restore_manifest.json`).
- `E:\FY26-27` recreated locally (`restore/phase2_download.py`, parallel generation-pinned download + mtime restore + size verify).
- 26 extra versions identified (test junk + pre-purged files) — NOT restored (correct).

---

## 2. Bugs found & FIXED this session (all committed on `reliability-2026-08-20`)

| # | Where | Bug | Fix | Proof |
|---|---|---|---|---|
| F12 | `core/lan_sync.py` | Robocopy failure counter **always 0**: regex looked for `*** FAILED:` lines which real failure output never contains | Count FAILED from the job-summary line `Files : ...` (5th number); removed `/NJS` so the summary reaches the log; `count_failed_files_from_summary()` + fallback | T3 live: sharing-violation run → `files_failed 0 → 1` in DB |
| F1 | `flow.py` | Cloud verify error message had diff labels **swapped** — told operator the opposite of the truth | `missing-from-cloud=len(added)`, `unexpected-in-cloud=len(removed)` (rclone `check --combined` semantics, proven live in T6) | T6 live sabotage + unit tests |
| A1 | `flow.py` + `core/manifest.py` | **Failed failure-alerts were swallowed** (`except: pass` / ignored `False`) — the 2026-07-25→08-13 blackout (backup + SMTP both network-blocked) left 19 nights with no notification AND no trace of the notification gap | CRITICAL log at all 4 alert sites (summary, PARTIAL, verify-failed, rollover-blocked); new `ManifestDB.mark_alert_not_delivered(run_id=|since_iso=)` annotates the run record(s) `[ALERT_NOT_DELIVERED]` (idempotent) | T9 live double-failure: CRITICAL log + annotated DB row |
| P1 | `core/fy_rollover.py` | Rollover final LAN backup accepted `LAN_PARTIAL` with robocopy **bit 3 (copy errors, exit 8-15)** as OK — at permanent FY cutover, failed files would be stranded forever | `exit_code & 8` → block cutover (RolloverError, daily retry); anomaly-only PARTIAL (4-7) still allowed; -1 (timeout) blocked | unit tests (exit 10 blocked, 5 allowed, -1 blocked) |
| P2a | `core/lan_manifest.py` | `os.walk` without `onerror` — unreadable subdirs silently skipped → incomplete snapshot → diff reports intact files "removed" → DB rows pruned (silent under-reporting) | `WalkIncompleteError`; pre-sync snapshot → empty dict (no false "removed" possible); post-sync → None (G14) → record skipped, self-heals next run; `normcase` base instead of `resolve()` (no UNC network round-trip) | unit tests (walk error raises; clean walk fine) |
| P2b | `core/logging.py` | `configure()`'s `logger.remove()` killed the Prefect bridge sink; sticky flag prevented re-add — from the 2nd flow run in the always-on agent, loguru logs **silently stopped reaching the Prefect console** for the process lifetime | Bridge callable tracked in module global, restored across reconfigure; idempotency checks sink presence | live probe: 3 consecutive configures, bridge alive, no duplicate sinks |
| P2c | `tests/conftest.py` + `pyproject.toml` | Test runs could configure loguru into the **production** log dir (tests call real flow paths with prod-derived configs) | Session autouse guard redirects `configure`/`flow.configure_logging` to a no-op (loguru stderr sink remains); `--capture=sys` (fd-capture vs loguru file-sink race = the "all passed but exit 1" noise) | full suite exit 0 |
| P2d | `flow.py` report flows | Weekly/monthly report send result ignored — a report silently not arriving left no Prefect trace | Flow fails visibly when runs exist but email not delivered; no-runs skip stays a normal no-op | unit tests (raises / no-raise) |
| P2e | `ui.py` `/status` | Sync SQLite reads on the event loop (async endpoint) — a slow/locked DB froze the whole dashboard incl. `/health` | DB section in `_status_db_data()` run via `asyncio.to_thread` (same pattern as `/health`) | UI tests pass |
| P2f | `ui.py` auth | `auth_enabled` + **empty api_key silently disabled auth** (any key/header accepted) | Fail-closed: deny + one-time ERROR log + clear login message; auth-disabled behavior unchanged | unit tests (both files updated) |
| **L1** | `launch.py` | **`rollover_deployment` was created but never passed to `serve()`** — the scheduled FY-rollover check (G10) silently never ran in production (only 4 of 5 deployments served) | Added to `serve()` call | post-deploy: 5 deployments incl. `rollover-check` |
| G1 | `deploy/install_gcloud.ps1` | Pre-existing deploy script untracked | Committed | — |
| G2 | `.gitignore` | Test-session scratch would have polluted the repo | Patterns added (check_t*, run_flow, restore/, config.*_test*.yaml, probes, saboteur, raw-robo, etc.) | `git status` clean |

---

## 3. E2E test matrix (all live, 2026-08-20)

| Test | Scenario | Result |
|---|---|---|
| T1 | LAN robocopy /MIR mirror: 10 files (2.2 MB, incl. 2 MB image + unicode filename) → local SMB share `\\INNOVIZTA-SERVE\aam_test` | **PASS** — 10/10 byte-identical, DB exact, 9 Prefect tasks COMPLETED, lock released |
| T2 | Cloud incremental on `E:\FY26-27` | **PASS** — +{1} add, *{1} modify, −{1} purge, DB prune correct |
| T3 | LAN PARTIAL (file locked mid-copy, `max_attempts:1`) | **PASS** — exit 10 → LAN_PARTIAL, **files_failed=1 (post-F12 fix)**, NAS shutdown skipped, **real Gmail alert delivered (attempt 1/3)** |
| T3b | Recovery re-run | **PASS** — exactly the 1 missing file re-synced |
| T4/T4b | Cloud no-change runs | **PASS** — exit 9 (`--error-on-no-transfer`), verified, correct baseline diff |
| T5 ×3 | Cloud add / modify / purge | **PASS** — all verified live in bucket |
| T6 | Cloud verify failure (saboteur deleted 1 cloud file during run) | **PASS** — sync exit 0 → verify caught it → CLOUD_VERIFY_FAILED in DB, 2 alerts emailed, Prefect FAILED; next run self-healed (re-upload) |
| T7 | Cloud preflight failure (bogus GCS key) + real SMTP | **PASS** — preflight [B] failed → CLOUD_SKIPPED + error message, Prefect FAILED, **real alert email delivered** |
| T9 | **Double failure**: backup fails AND SMTP unreachable | Gap confirmed (old code: silent) → **fixed**: CRITICAL log + `[ALERT_NOT_DELIVERED]` DB annotation (3 attempts w/ backoff visible in log) |

Environment notes:
- All test runs used isolated `runtime_dir` (`_t1_runtime`/`_t2_runtime`) — `C:\BackupAgent` (prod DB/logs/lock) was **never** touched by tests.
- All test configs: `wol.enabled: false` (never wakes MAC 6C-4B-90-25-70-5F; NAS 10.10.186.231 intentionally skipped per user — SMB permissions not enabled there).
- Prefect server accumulated ~16 additive test flow runs on `aam-backup` (harmless, visible in Console).
- Test configs/runners (all gitignored, workspace root): `config.lan_test.yaml`, `config.lan_test_t3.yaml`, `config.cloud_test.yaml`, `config.cloud_test_t7.yaml` (bad key), `config.cloud_test_t9.yaml` (bad key + bad SMTP), `run_flow.py`, `run_t1.py`, `check_t1*.py/t2/t3/t4.py`, `lock_holder.py`, `saboteur_t6.py`, `make_t7_t9_configs.py`, `probe_*.py`, `raw_robocopy_lock*.py`, `restore/` (phase1/phase2 + manifest + logs).

---

## 4. OPEN items (known, deliberately not changed — documented for session 2)

| # | Item | Severity | Notes |
|---|---|---|---|
| O1 | Bucket `aam-backup-demo-innovizta` holds test junk outside `FY26-27/` prefix: `AUDIT-20260819` (~1070 versions), `rclone_exp*`, `listing_bench*`, `E2E_TEST_FY` (20 versions) | cosmetic/cost | **Do not delete without user approval.** Versioning ON + lifecycle (numNewerVersions:2, 90d noncurrent) protects real data |
| O2 | `cloud_reporter.py`: `capture_output` output unbounded (P2); `_partial`/error markers set but never consumed by callers | P2 | Consider size cap + consuming markers in session 2 |
| O3 | `ui.py` `/health` reports healthy even when DB unreadable; VACUUM can 503 dashboard briefly | P3 | cosmetic |
| O4 | `fy_rollover.py` config rewrite uses `mkstemp` without explicit ACL (inherited ACL may deny non-admin service account) | P3 | untested path |
| O5 | `core/cloud_sync.py` runs `gcloud auth activate-service-account` as side effect | P3 | works, but rclone JSON key suffices — candidate for removal |
| O6 | `enqueue=True` log sink: hard-killed process loses un-flushed queued log tail | P3 | clean exits flush via atexit; watchdog uses graceful stop |
| O7 | At 12:30 the agent logged `FlowRunCancellingObserver ... Service exceeded error threshold` — caused by the **Prefect server downtime during our restart** (12:26→12:33), self-resolved | P3 | monitor at tonight's run |
| O8 | NSSM stop hangs on this host: `nssm stop`/SCM stop can leave the service STOP_PENDING after the app exited (control dispatcher blocked; `sc.exe stop` → 1061). Workaround proven this session: kill the nssm host process → service STOPPED → `sc.exe start` (SCM recovery also auto-restarted the services after host kills) | P2 ops | Consider NSSM upgrade / AppStopMethod tuning in session 2 |
| O9 | Cosmetic: "Exception ignored in atexit" (loguru file-sink close) still prints at suite shutdown — **exit code is 0**, results unaffected | P3 | identify leftover sink owner if it bothers |
| O10 | NAS `10.10.186.231` not tested (user: SMB permissions not enabled yet) — local SMB share used instead. Re-test LAN path against the real NAS once permissions are on | user action | same code path proven on `\\INNOVIZTA-SERVE\aam_test` |
| O11 | `C:\AAMBackup\scratch/` untracked dir exists (pre-existing) | P3 | inspect before deleting anything |

---

## 5. Exact next commands (session 2 / verification)

```powershell
# 0) Where things live
$ws  = "C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1"   # workspace repo (branch reliability-2026-08-20 @ 2bb8024)
$prod = "C:\AAMBackup"                                          # deployed (same commit), prod config UNTRACKED
$py  = "C:\AAMBackup\.venv\Scripts\python.exe"
$nssm = "$prod\deploy\bin\nssm.exe"

# 1) Post-deploy verification (run after tonight's 21:00/22:00 IST runs)
& $py - <<'EOF'
import asyncio, os
os.environ['PREFECT_API_URL'] = 'http://127.0.0.1:4200/api'
from prefect.client.orchestration import get_client
from prefect.client.schemas import filters
async def main():
    async with get_client() as c:
        deps = await c.read_deployments(limit=20)
        print('deployments:', sorted(d.name for d in deps))   # expect 5 incl. rollover-check
        runs = await c.read_flow_runs(limit=10)
        for r in runs:
            print(r.name, r.state.name, r.tags)
asyncio.run(main())
EOF

# 2) Prod DB health (read-only)
& $py -c "import sqlite3; con=sqlite3.connect(r'C:\BackupAgent\manifest.db'); print(con.execute('SELECT mode,status,files_copied,files_failed,started_at FROM run_history ORDER BY started_at DESC LIMIT 8').fetchall()); print('entries:', con.execute('SELECT COUNT(*) FROM file_entries WHERE cloud_status=\"synced\"').fetchone())"

# 3) Bucket integrity (read-only, prod key)
#    rclone conf with project_number aam-demo-gcs, or reuse C:\Users\Administrator\Desktop\testing\rclone_probe_prod.conf
rclone check --one-way --size-only --modify-window 2s "prodgcs:INNOVIZTA:..." ...   # see restore/probe notes; simplest: repeat T4-style check with the workspace demo key conf

# 4) Re-run the full suite any time:
& $py -m pytest "$ws\tests" -q --tb=line -p no:cacheprovider     # expect 1439 passed / 53 skipped / exit 0

# 5) Trigger a manual deployment run from the dashboard (http://127.0.0.1:8080)
#    or:  prefect deployment run backup-cloud/backup-lan
```

### Checklist for session 2
- [ ] Tonight's 21:00 LAN + 22:00 cloud runs: DB statuses COMPLETE, Prefect states Succeeded, dashboard green, emails only on failure
- [ ] Confirm `rollover-check` deployment shows next scheduled run (fix L1)
- [ ] Watch O7 (cancellation observer) — should be quiet now server is stable
- [ ] If user enables NAS permissions → re-run T1/T3 against `\\10.10.186.231\...`
- [ ] Decide on O1 bucket junk cleanup (needs approval)
- [ ] Optional: O2 cloud_reporter hardening, O8 NSSM stop tuning, O5 gcloud side-effect removal
- [ ] `main` branch: `reliability-2026-08-20` is 2 commits ahead — merge when user is happy with tonight's runs

---

## 6. Repository / paths reference

- **Workspace**: `C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1` (git, branch `reliability-2026-08-20`)
- **Deployed**: `C:\AAMBackup` (git, same branch/commit; `config.yaml` untracked = production values)
- **Prod runtime**: `C:\BackupAgent\` — `manifest.db` (SQLite, WAL), `logs\backup_YYYY-MM-DD.log`, `logs\agent_svc.log`, `logs\prefect_svc.log`, `logs\watchdog_svc.log`, `.prefect\` (server DB + deployments)
- **Prefect server**: `127.0.0.1:4200` (Console UI), service `AamPrefectServer`
- **Agent**: service `AamBackupAgent` (launch.py: dashboard :8080 + serve() 5 deployments), **never run launch.py manually** — it cancels active prod runs
- **Watchdog**: service `AamWatchdog` (watchdog.py)
- **Python**: `C:\AAMBackup\.venv` (3.12.3; prefect 3.7.2, loguru 0.7.3, pydantic 2.13.4)
- **GCS**: bucket `aam-backup-demo-innovizta`, prefix `FY26-27`, versioning ON, lifecycle numNewerVersions:2 + 90d noncurrent
- **Demo GCS key**: workspace root `aam-demo-gcs-d9427ae2cacc.json` (works on prod bucket via bucket IAM; rclone needs `project_number = aam-demo-gcs`)
- **Prod GCS key**: `C:\AAMBackup\deploy\keys\aam-gcs-key.json` (read-only probes)
- **Test SMB share**: `\\INNOVIZTA-SERVE\aam_test` → `C:\lan_dest_test` (canary file pre-created)
- **Test source trees**: `E:\aam_t1_src` (LAN, 10 files), `E:\FY26-27` (cloud, 572 files = prod dataset)
- **rclone probe confs**: `C:\Users\Administrator\Desktop\testing\rclone_probe_demo.conf` / `rclone_probe_prod.conf`

### GCS ops recipe (used by restore/probes)
JWT RS256 via `cryptography`: `load_pem_private_key` → `key.sign(si.encode(), padding.PKCS1v15(), hashes.SHA256())`; scope `devstorage.read_only`/`read_write`; token via `oauth2.googleapis.com`. Media download: `https://storage.googleapis.com/download/storage/v1/b/{bucket}/o/{urlenc-name}?generation=<gen>&alt=media` — name must be URL-encoded (`/`→`%2F`) **and** include the `FY26-27/` prefix. Delete: DELETE on meta URL → 204. Versions: objects/list with `versions=true`.

### Robocopy gotchas (this build)
- Colon forms only: `/MT:8`, `/R:1` — space form `/MT 8` mis-parses.
- Real failure output has NO `*** FAILED:` lines; count FAILED from `Files :` summary (5th number) — needs `/NJS` absent.
- Exit codes: 0-3 complete, 4-7 anomaly PARTIAL (bit 2), 8-15 copy-error PARTIAL (bit 3), 16+ fatal. Callers MUST check `& 8`.
- Windows locking for fault injection: `kernel32.CreateFileW(path, 0xC0000000, 0, None, 3, 0, None)` holds exclusive lock.
