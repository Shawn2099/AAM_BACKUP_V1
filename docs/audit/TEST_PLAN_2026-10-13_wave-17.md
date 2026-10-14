# WAVE 17 test plan — coverage-driven: the process/ops layer

**Date:** 2026-10-13 · **Driven by:** coverage.py 7.16.2 (`branch` mode) over the
full lab suite (`audit_lab/raw/cov_v1.json`, report in `cov_v1_report.txt`)

## Method

1. `.venv\Scripts\python.exe -m coverage run --branch --source=core,models,flow,ui,launch,watchdog,collect_config_data -m pytest audit_lab --ignore=audit_lab/test_w7_deployed.py`
2. `python audit_lab\gap_map.py` maps uncovered lines → enclosing functions.
3. Tests written for the biggest uncovered blocks, in risk order.

**Baseline before Wave 17: 71% statements (1027 missed) / 189 partial branches.**

## Gap map (worst first)

| Module | Cov | Biggest uncovered functions | Wave 17 scope |
|---|---|---|---|
| `core/wol.py` | 32% | `_send_magic_packet` (15), `wait_for_server` (10), `ensure_server_online` (6), `_smb_port_open` (4) | full |
| `collect_config_data.py` | 37% | `main` (66), `is_firewall_open` (9), `list_drives` (5) | full |
| `watchdog.py` | 41% | `main` (58), `_service_state` (21), `_start_service` (13), `_is_backup_running` (11), `_transfer_process_running` (10), `_stop_service` (10), `_check_health` (6) | full |
| `launch.py` | 46% | `main` (61), `_watch` (10), `_check_prefect_api` (10), `_create` (9), `_run_dashboard` (8), `_cancel` (7), `_reconcile` (5) | full |
| `core/cloud_preflight.py` | 57% | `run_cloud_dry_run` (21) | full |
| `core/lan_preflight.py` | 62% | `run_lan_dry_run` (20), `_is_smb_reachable`, `_extract_unc_host` | full |
| `core/shutdown.py` | 61% | `shutdown_server` (8) | covered (W16 fakes subprocess; remaining branches are timeout/OSError) |
| `ui.py` | 64% | `_is_verify_running` (28), `trigger_monthly_email` (19), `login_submit` (18), `trigger_verify` (15) | **deferred to Wave 18** |
| `core/report.py` | 77% | `generate_report_html` block 124-168 | **deferred to Wave 18** |
| `flow.py` | 77% | rollover/report flow blocks | **deferred to Wave 18** |

## Tests (W17.1 – W17.5)

**W17.1 core/wol.py (9)** — port clamp (non-int, out-of-range) never raises;
connect_ex 0/non-zero; OSError; magic-packet round/interval contract (global +
subnet, repeat=max(1,repeat), sleep between rounds only); subnet == global
broadcast → single target; per-round failure continues (P1-WOL loud catch);
wait_for_server immediate + stability sleep, stability 0 no sleep, never opens →
WolTimeout; ensure_server_online disabled / already-online / offline wake.

**W17.2 watchdog.py (12)** — pid alive; transfer-process detection (found /
absent / psutil failure); backup-lock states (absent / live / stale / corrupt →
never raise); SCM state parsing (STATE line, one-token fallback, no-state,
timeout retry, exception); `_service_is_running`; start accepted / rc≠0 /
timeout / exception; circuit breaker (allow / block / window reset); stop
never raises; health 200/503/exception; `_alert_wedged_lock` delivery +
contained failure; **main() loop** — healthy + agent RUNNING, agent STOPPED +
allowed → sc start, agent STOPPED + breaker suppressed, unknown state, below
threshold, transfer deferral, transfer cap + live lock (no unlink/restart),
transfer cap + lock released (unlink + sc stop).

**W17.3 launch.py (10)** — `_run_dashboard` uvicorn wiring; supervise alive/dead
thread; API check 200 / ConnectError / other / 500; concurrency limit create +
already-exists + running-loop branch; orphan cancel (no flows / PENDING /
RUNNING + live lock / stale lock cleanup / pagination / set_state failure);
reconcile legs (absent / resume / pause / no-op + running-loop branch);
main() API-never-ready → SystemExit(1); main() happy path (serve called with
deployments, KeyboardInterrupt → clean shutdown prints); main() rollover error
non-fatal.

**W17.4 collect_config_data.py (4)** — firewall open/closed/invalid/exception;
drive listing; network discovery (loopback + APIPA excluded, MAC normalised);
main() end-to-end (all helpers patched, stdout captured, no `input()` when not
a tty, both firewall branches, no-networks warning).

**W17.5 dry-runs (8)** — cloud source missing / empty (warning, still probes) /
read OSError / timeout / rclone missing / OS error / rc≠0 / rc=0; lan SMB
unreachable → HealthError / canary missing → HealthError with recovery text /
canary OSError / happy path; `_extract_unc_host`, `_is_smb_reachable`.

**Expected coverage after Wave 17:** wol ~100%, collect_config_data ~95%,
watchdog ~85%, launch ~90%, both preflights ~90%.

## Results (2026-10-13)

Delivered as `test_w17_ops.py` (29 tests — wol + watchdog) and
`test_w18_ops.py` (30 tests — launch + collector + dry-runs), 59/59 green in 4s.
No production code touched; all subprocess/socket/Prefect-client paths faked.

**Coverage: 71% → 81% overall** (`raw/cov_v2.json`, report `raw/cov_v2_report.txt`):

| Module | Before | After |
|---|---|---|
| `core/wol.py` | 32% | **86%** |
| `collect_config_data.py` | 37% | **95%** |
| `watchdog.py` | 41% | **92%** |
| `launch.py` | 46% | **92%** |
| `core/cloud_preflight.py` | 57% | **100%** |
| `core/lan_preflight.py` | 62% | **100%** |

Findings from writing the tests (all lab-side, no production defects):
the source-loss status is genuinely timing-dependent (already recorded), and
`launch.main()` **silently exits 1** when the Prefect API never answers — the
operator sees one console line on a service that writes no event log.

## Wave 19 targets (next, by gap size)

1. `ui.py` (64%) — `_is_verify_running` (28), `trigger_monthly_email` (19),
   `login_submit` (18), `trigger_verify` (15), `_trigger_result` (10),
   `trigger_cloud`/`trigger_lan` (10/8), auth + session cleanup paths.
2. `core/report.py` (77%) — the `generate_report_html` block 124–168 (a whole
   render path never exercised), plus the email-attachment branches.
3. `flow.py` (77%) — rollover blocks 1052–1067/1222–1240 and the report flows
   832–844/873–884.
4. Small tail: `core/shutdown.py` timeout/OSError branches (45–50),
   `core/rclone_config.py` (66%), `core/process.py` (78%).

## Results 2 (2026-10-13, waves 19–20)

- `test_w19_ui.py` — 23 tests: ui.py 64% → ~90% (session lifecycle, auth
  matrix, all three trigger endpoints + the two Prefect-query branch sets,
  config-refresh path change, status truncation/503, health, both email
  endpoints, `_integrity_summary`).
- `test_w20_report.py` — 20 tests: `send_failure_alert`'s never-executed
  body/attachment path now covered, full SMTP policy, status variants,
  CSV-injection guard, weekly/monthly delegation.
- Lab bugs found while writing them (test-side, fixed in the tests):
  instance attributes must not shadow the methods they record; TestClient
  follows redirects by default (assert on `follow_redirects=False`); the
  dashboard's per-minute rate limiter must be neutralised when a test makes
  many posts from one IP; primed config caches beat function patches.

**Remaining targets (waves 21+):** `flow.py` (77%), `core/manifest.py` (75%),
`core/process.py` (78%), `core/integrity.py` (78%), `core/cloud_sync.py` (78%),
`core/cloud_reporter.py` (68%), `core/rclone_config.py` (66%),
`core/shutdown.py` (61%), `models/config.py` (86%), `core/fy_rollover.py` (81%),
`core/lan_sync.py` (91%), `core/lan_manifest.py` (80%), `core/backup_repository.py` (88%),
`core/health.py` (89%), `core/logging.py` (83%), `core/time_utils.py` (92%),
`core/wol.py` (86%), `core/cloud_verify.py` (91%) — i.e. chase every module
to ~100% with branch-exhaustive tests, wave by wave.



**No production code touched.** All subprocess, sockets and Prefect clients
are faked; every destructive path stays in LAB_ROOT.
