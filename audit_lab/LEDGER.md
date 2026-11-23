# AAM Backup Automation — empirical audit lab (evidence ledger)

Everything below was produced by executing the shipped production code against a fully isolated rig
(mock source, loopback SMB `\\127.0.0.1\C$` as the NAS, an rclone *local* backend as the fake GCS bucket).
No production module or test was edited; only `audit_lab/` was added.

**4424 experiments recorded (158 distinct).** Raw log: `%TEMP%\opencode\evidence.jsonl`.

---

## Index

| ID | Verdict (latest run) |
|---|---|
| `D1.guard` | GUARD PASSED (fail-open) |
| `D1.lan_purge` | MASKED BY /ZB — the purge never happens because robocopy exits 16 first; see C2 for the same scenario without /ZB (12 -> 0 files, LAN_COMPLETE) |
| `D1.cloud_purge` | DATA LOSS CONFIRMED — objects deleted, reported CLOUD_NO_CHANGES_COMPLETE |
| `D2[acl_deny].setup` | ACL deny NOT applied (env limit) |
| `D2[maxpath].setup` | subtree created |
| `D2[maxpath].result` | NO PURGE — subtree survived intact, robocopy reported status=LAN_FAILED |
| `D1.robocopy_exit16` | LAN leg FAILS CLOSED (exit 16 = fatal, nothing copied/deleted) |
| `D2.partial.setup` | ACL DENY WORKS (unprivileged, own dir) |
| `D2.partial.lan` | LAN: B survived (5/5) with status=LAN_FAILED |
| `D2.partial.cloud` | CLOUD: B survived (5/5) and reported status=CLOUD_FAILED |
| `C1./ZB` | CRITICAL: /ZB is a HARD FAILURE (exit 16, privilege absent) — every LAN backup would fail before copying anything |
| `C2.dirs_only_no_zb` | LAN DATA LOSS CONFIRMED (purge with no files in source) |
| `C3.denied_subtree_no_zb` | LAN protected: B survived (5/5), status=LAN_PARTIAL |
| `C4.denied_file` | LAN PARTIAL as designed (old copy retained, exit 8 -> LAN_PARTIAL) |
| `D.kill_exit0.lan` | FAIL-CLOSED OK: status=LAN_SUSPECT (kill detected, nothing falsely COMPLETE) |
| `D.kill_exit0.cloud` | transfer-level contract: status=CLOUD_COMPLETE (the flow-level evidence gate is the real safeguard) |
| `B2.size_only_blind` | BLIND SPOT CONFIRMED — daily gate VERIFIED, weekly audit FAILED |
| `D2.kill_exit0.lan` | FAIL-CLOSED OK — kill detected as LAN_SUSPECT |
| `BUG-1.watchdog_alert` | CONFIRMED DEAD CODE â€” ModuleNotFoundError swallowed by the except; alert is silently dropped (never reaches the operator) |
| `BUG-2.cloud_status` | status correctly reflects the failure |
| `BUG-2.lan_status` | CONFIRMED MISREPORT â€” flow failed but DB row says 'LAN_COMPLETE' (listed as a successful run by last_successful_run / dashboard / reports) |
| `LOCK.roundtrip` | OK |
| `LOCK.corrupt` | OK â€” no exceptions (0 raised) |
| `LOCK.db_concurrency` | OK â€” busy_timeout serialises writers |
| `LOCK.pid_reuse` | OK â€” refuse live owner, collect stale lock |
| `LOCK.db_concurrency.inserts_only` | OK for insert-only contention |
| `F.classify_exit_code` | OK |
| `F.clean_path_idempotence` | MINOR: double application differs (trailing space/separator not stripped on pass 1) |
| `W2.manifest` | extrapolation -> 1M: ~473000 MB RSS, ~11s \| 2.5M: ~1182500 MB RSS, ~28s |
| `W2.lsjson_subprocess` | extrapolation -> 1M ~10s, 2.5M ~24s (config manifest_timeout_seconds=900) |
| `W2b.lan_snapshots` | per file: 0.20 KB RSS, 34 B pickled -> at 1M: ~196 MB RSS and ~34 MB pickled, x2 snapshots per run |
| `W2b.db_before_dict` | per file: 0.38 KB, 34 B pickled -> at 1M ~367 MB RSS, ~3s |
| `W2b.combined` | at 1M objects the cloud leg holds roughly 1021 MB of live Python objects, and writes ~102 MB of task results into the Prefect result store PER NIGHT |
| `W4.2.cloud_kill_gate` | GATE DID NOT REJECT â€” see evidence |
| `W4.3.lan_happy_unc` | see evidence |
| `W4.1.fy` | NOTE: v1.1.3 _effective_fy_prefix aligns the cloud prefix with the configured source leaf |
| `W4.1.cloud_happy` | OK â€” end-to-end cloud leg works against the local backend |
| `LOCK.db_inserts_ok` | OK |
| `W5a.path_variants` | all hostile names mirrored correctly |
| `W5a.path_variants.cloud` | cloud leg handled all names |
| `W5a.deep_paths` | deep paths handled consistently |
| `W5b.junction.lan` | OK — /XJ excluded the junction (no duplication, no recursion) |
| `W5b.junction.cloud` | OK — junction not followed |
| `W5b.rename_mid_run` | MIRROR-RACE CONFIRMED — the destination ends up with 0 copies of the renamed folder in this run; the next run re-copies it (recovers), but for one cycle the backup is mis |
| `W5b.double_trigger` | OK — no interleaving of the two mirror runs (they ran back-to-back or one refused) |
| `W5b.crash_recovery` | OK — stale lock collected, night 2 completed, DB consistent |
| `W5c.missing_canary` | see evidence |
| `W5c.smb_false_positive` | DESIGN NOTE — the WoL/SMB probe is a bare TCP connect, so a hung NAS that accepts connections reads as 'online'; the canary + robocopy access errors are the real backstop |
| `W5c.source_disappears` | ISSUE — status=LAN_COMPLETE on_dest=3000 |
| `W5c.locked_file` | ISSUE — locked file was deleted or replaced at the destination |
| `DEPLOYED.services` | ISSUE - at least one service is not RUNNING |
| `DEPLOYED.deployments` | OK - schedules match config.yaml |
| `DEPLOYED.zb_service_account` | OK - the LAN leg is copying (no 16s) |
| `DEPLOYED.watchdog_recovery` | OK - self-healing works |
| `DEPLOYED.lan_backup` | ISSUE - state=FAILED, missing=[], canary=True |
| `DEPLOYED.cloud_backup` | ISSUE - state=FAILED, missing=[] |
| `DEPLOYED.stale_lock_recovery` | ISSUE - state=FAILED, lock_present=False |
| `LOCK.db_inserts_flaky` | INTERMITTENT: insert-only writers did hit a lock this run |
| `W6.alert_policy` | FINDING — the verify-gate failure PAGES TWICE: the in-pipeline gate alert ('Cloud integrity verification FAILED…') AND backup()'s generic 'Backup completed with errors' a |
| `W6.rollover_flow` | OK — real rollover executed through the flow (config atomically rewritten to the new FY) |
| `W6.integrity_audit_flow` | OK — audit row written through the real flow (divergence -> alert + failed flow) |
| `W8.orphan_cancel` | OK — PENDING always reaped; RUNNING preserved while its lock is live, cancelled once the lock is gone |
| `W8.concurrent_serialise` | OK — the two mirrors ran back-to-back (no overlap), every file copied exactly once, both runs recorded |
| `W8.report_flows` | OK — SMTP retry storm contained inside core.report; both report flows COMPLETED against the real Prefect API |
| `W8.leg_scoped_modes` | OK — one cloud row and one LAN row, no cross-leg writes |
| `W8.dashboard_trigger` | OK — queued run visible to the guard; duplicate click refused with 400 already_running (no second backup queued) |
| `W8.deployments` | OK — all 6 deployments registered, schedules match config.yaml |
| `W8.trigger_fail_closed` | OK — both triggers refused with 503 (fail-closed), /status degrades to 'unknown' with running=False (fail-open display only) |
| `W6.backup_flow_all` | OK — real Prefect flow run with real task runs for both legs |
| `W6.task_retry` | OK — the retry policy executed the failing task a second time and the run recovered |
| `W6.deployments` | OK — registration + pause reconciliation work against the real Prefect server |
| `W9.lan_random_tree` | OK — lossless + honest (I1, I3, I4 hold) |
| `W9.lan_convergence` | OK — second run changed nothing (I2 holds) |
| `W9.fault_lan` | OK — honest: failure status, or COMPLETE with a byte-identical destination (I4 holds) |
| `W9.mutation_repair` | OK — mutation repaired by the next run |
| `W9.junction_excluded` | OK — junction excluded (0 files through it) and the real tree mirrored exactly |
| `W9.db_concurrency_stress` | REAL FINDING — concurrent ManifestDB opens lose run-history rows. The loser raises OperationalError('database is locked') inside _get_conn()'s executescript(DDL) (core/ma |
| `W10.schedules` | OK — all 6 schedules fire at the intended local time; the legs never overlap |
| `W10.line_coverage` | coverage map recorded (see LEDGER for the never-executed lines) |
| `W9.cloud_random_tree` | OK — lossless + honest (I1, I3, I4 hold) |
| `W9.cloud_convergence` | OK — second run changed nothing (I2 holds) |
| `W11.log_corpus_replay` | OK — all replayed logs classify consistently |
| `W11.audit_branches` | OK — same-size corruption DETECTED by the audit; canary filtered |
| `W11.temp_config` | OK — rclone parses the generated config |
| `W11.record_sync_results` | OK — both entry shapes accepted without error |
| `W11.summary_round_trip` | OK — round-trip exact |
| `W11.gate_messages` | OK — each of the three documented categories is attributed distinctly |
| `W11.clock_skew_fail_open` | DOCUMENTED BEHAVIOUR (fail-open): an unreachable network means the clock check passes, so clock skew — which breaks GCS JWT auth — is invisible exactly when the network i |
| `W11.fresh_corpus` | OK — copy/purge/prune/missing-source classified as the run really was, AND a summary-less log is refused even when the copy succeeded |
| `W12.fy_boundary` | OK - the FY boundary is IST-anchored and off-by-5h30m-free |
| `W12.detect_rollover` | OK - boundary, opt-out and disagreement all behave |
| `W12.purge_retention` | OK - retention boundaries are exact and file_entries survive |
| `W12.rollover_flow` | OK - the 1-April event completes and rewrites config.yaml to the new FY |
| `W12.fy_divergence` | OK - M-4 FIXED: both legs wrote the source-leaf FY, no calendar divergence |
| `W12.clock_skew` | OK - fails closed on real skew, fails open on a missing/unreachable network (documented trade-off) - all branches now covered |
| `W12.seven_day_cycle` | OK - the full nightly cycle is lossless, honest and convergent on both legs; the only anomaly is the FY split at the boundary (M-4) |
| `W13.decide_lan_matrix` | OK - all 22 branches correct |
| `W13.lan_sync_parsers` | OK - every parser branch returns the documented value and fails closed |
| `W13.exit_code_bool` | RED - the LAN decider accepts True as a successful copy (exit 1) while the cloud decider rejects it; the two disagree on the same input class |
| `W13.command_contracts` | OK - both command builders match their documented flag contract |
| `W13.rclone_classification` | OK - all rclone classification branches correct |
| `W13.cloud_verify_branches` | OK - every verify branch is correct and never VERIFIED without a normal termination |
| `W13.verify_failure_paths` | OK - every verify failure path degrades to verified=False without raising |
| `W13.audit_lan_branches` | OK - the LAN audit detects every divergence, filters the canary and never VERIFIED on an unreadable source |
| `W13.integrity_helpers` | OK - the integrity helpers behave as documented |
| `W13.health_branches` | OK - every health branch returns the documented reason; the dirs-only source still passes (C-1 remains observable) |
| `W13.fy_path_helpers` | OK - the rollover path helpers are total and separator-preserving |
| `W13.create_fy_folders` | OK - the source folder is created either way and a dead NAS never blocks the rollover |
| `W13.update_config_yaml` | OK - the atomic rewrite is comment-preserving and fails clean |
| `W13.archive_transition` | OK - the archive transition can never block or break a rollover |
| `W13.manifest_run_history` | OK - run history writes and reads are consistent and mode-scoped |
| `W13.record_run_history_failure` | OK - a dropped row is at least loud in the log |
| `W13.legacy_schema_migration` | OK - the legacy migration is complete and idempotent |
| `W13.cloud_reporter` | OK - the reporter's success and failure paths both behave |
| `W13.robocopy_log_cleanup` | OK - the G8 reaper is age-gated and safe |
| `W5c.canary_alert_delegation` | OBSERVATION - the destination-side fault fails closed, but the specific reason reaches the operator only through backup()'s generic alert; a direct pipeline call (manual  |
| `W5c.smb_probe_backstop` | DESIGN NOTE (L-5) - the probe is a bare TCP connect; the canary and robocopy's own access errors are what actually protect the destination |
| `W14.h3_tristate` | OK - the duplicate-run guard's filter and tri-state both behave |
| `W14.integrity_summary` | OK - backup result and integrity status are reported as separate facts, and absence of an audit never reads as verified |
| `W14.http_endpoints` | OK - the dashboard renders and reports without a live Prefect or SMTP |
| `W14.report_no_data` | OK - no-data paths are distinguishable from a real report |
| `W14.auth_rate_limit` | OK - auth and rate limiting are enforced per branch |
| `W14.csv_injection` | OK - CSV injection is neutralised and the header is stable |
| `W14.report_html` | OK - the report renders every run state it was given |
| `W14.report_email` | OK - a report outage is contained (the flow still COMPLETES) |
| `W16.lan_flow_random_tree` | OK — lossless + honest (I1, I3, I4 hold) |
| `W16.lan_flow_convergence` | OK — second run changed nothing (I2 holds) |
| `W16.cloud_flow_random_tree` | OK — lossless + honest (I1, I3, I4 hold) |
| `W16.cloud_flow_convergence` | OK — second run changed nothing (I2 holds) |
| `W16.flow_mutation_repair` | OK — mutation repaired by the next run |
| `W16.flow_fault_injection` | OK — honest: failure status, or COMPLETE with a byte-identical destination (I4 holds) |
| `W16.hashing_md5` | OK — compute_md5 matches rclone; verify_checksum is exact and refuses PENDING_CHECKSUM |
| `W16.logging_rotation` | OK — log file created |
| `W16.backup_repository` | OK — both entry shapes recorded, prune works, run history recorded |
| `W16.watchdog_h2` | OK — H-2 fixed: import from core.report succeeds, no ModuleNotFoundError |
| `W16.collect_config_data` | OK — network discovery and pydantic validation both behave |
| `W16.schedule_collisions` | ISSUE — collisions: [('weekly-report', 'monthly-report', [datetime.datetime(2027, 2, 1, 8, 0), datetime.datetime(2027, 3, 1, 8, 0), datetime.datetime(2027, 11, 1, 8, 0)]) |
| `W16.flow_all_mode` | OK — one cloud row and one LAN row |
| `W16.config_validation_guards` | OK — all 5 guards fire, valid config passes |
| `W16.watchdog_main_loop` | OK — sc stop on hung service, sc start on stopped service, deferral on lock/transfer |
| `W16.watchdog_circuit_breaker` | OK — breaker allows 3 then suppresses, per-service isolation |
| `W16.ui_trigger_verify` | OK — all 4 branches behave |
| `W16.ui_email_report_triggers` | OK — 404 on empty DB, 500 on SMTP failure |
| `W16.ui_config_reload` | OK — TTL refresh + DB eviction work |
| `W16.logging_bridge` | OK — idempotent |
| `W16.shutdown_contract` | OK — documented dict + exact command; both branches safe |
| `W17.smb_port_clamp` | OK - never raises, never connects |
| `W17.magic_packet_contract` | OK - 3 rounds x 2 targets, 2 intervals |
| `W17.backup_lock_states` | OK - the main loop can never be crashed by a bad lock file |
| `W17.main_transfer_deferral` | OK - restart deferred while a real transfer runs |
| `W17.main_transfer_cap_live_lock` | OK - fail-closed under a live backup |
| `W17.main_transfer_cap_dead_lock` | OK - zombie path recovers |
| `W17.main_hung_restart` | OK - the genuine hung state triggers the restart |
| `W17.wedged_lock_alert` | OK - alert delivered, outage contained |
| `W18.launch_main_happy` | OK - the production startup sequence is exercised end to end |
| `W18.network_discovery` | OK - only routable interfaces are offered as config snippets |
| `W18.cloud_dry_run_branches` | OK - all 8 branches behave as documented |
| `W18.lan_dry_run_branches` | OK - all 8 branches behave as documented |
### `D1.guard` — empty-subdir source passes the fail-closed guard

- **Verdict:** GUARD PASSED (fail-open)
- **When:** 2026-10-13 14:25:58

```
check_source_drive('C:\\Users\\malliath\\AppData\\Local\\Temp\\opencode\\aam_lab\\case_wzqytplx\\source\\FY26-26') -> ok=True, reason=''
```

### `D1.lan_purge` — robocopy /MIR against a dirs-only source wipes the LAN destination

- **Verdict:** MASKED BY /ZB — the purge never happens because robocopy exits 16 first; see C2 for the same scenario without /ZB (12 -> 0 files, LAN_COMPLETE)
- **When:** 2026-10-13 14:25:58

```
robocopy exit=16 (SeBackupPrivilege absent -> /ZB aborts the run before any copy/delete, see experiment C1) | dest files before=12 after=12
```

### `D1.cloud_purge` — cloud mirror against an empty (dirs-only) source deletes the bucket objects

- **Verdict:** DATA LOSS CONFIRMED — objects deleted, reported CLOUD_NO_CHANGES_COMPLETE
- **When:** 2026-10-13 14:25:58

```
rclone exit=9 status=CLOUD_NO_CHANGES_COMPLETE | objects before=12 after=0
```

### `D2[acl_deny].setup` — deny read on one source subtree via icacls

- **Verdict:** ACL deny NOT applied (env limit)
- **When:** 2026-10-13 14:25:58

```
icacls rc=87 out= err=Invalid parameter "innovizta_2(RD)"
```

### `D2[maxpath].setup` — source subtree nested past MAX_PATH

- **Verdict:** subtree created
- **When:** 2026-10-13 14:25:58

```
deepest path length=238
```

### `D2[maxpath].result` — unreadable source subtree is purged from the destination

- **Verdict:** NO PURGE — subtree survived intact, robocopy reported status=LAN_FAILED
- **When:** 2026-10-13 14:25:58

```
robocopy exit=16 status=LAN_FAILED files_failed=0 | dest files before=7 after=7 | unreadable-subtree survivors=5 | decision=LAN_FAILED
```

### `D1.robocopy_exit16` — robocopy refuses to mirror a dirs-only source (exit 16?)

- **Verdict:** LAN leg FAILS CLOSED (exit 16 = fatal, nothing copied/deleted)
- **When:** 2026-10-13 14:25:27

```
status=LAN_FAILED exit=16 survivors=1
--- log ---

ERROR : You do not have the Backup and Restore Files user rights.
*****  You need these to perform Backup copies (/B or /ZB).

       Simple Usage :: ROBOCOPY source destination /MIR

             source :: Source Directory (drive:\path or \\server\share\path).
        destination :: Destination Dir  (drive:\path or \\server\share\path).
               /MIR :: Mirror a complete directory tree.

    For more usage information run ROBOCOPY /?

                                                          
****  /MIR can DELETE files as well as copy them !

```

### `D2.partial.setup` — deny-read applied to source subtree B

- **Verdict:** ACL DENY WORKS (unprivileged, own dir)
- **When:** 2026-10-13 14:25:27

```
icacls rc=0 user=DESKTOP-N7II2M6\innovizta_2 out=processed file: C:\Users\malliath\AppData\Local\Temp\opencode\aam_lab\case_pn4qgvrr\source\FY26-26\B
Successfully processed 1 files; Failed processing | iterdir(B) readable=False
```

### `D2.partial.lan` — robocopy /MIR against a partially readable source purges the unreadable subtree

- **Verdict:** LAN: B survived (5/5) with status=LAN_FAILED
- **When:** 2026-10-13 14:25:27

```
status=LAN_FAILED exit=16 files_failed=0 | A survivors=2/2  B survivors=5/5 | counts=None
```

### `D2.partial.cloud` — rclone sync against a partially readable source deletes the unreadable objects

- **Verdict:** CLOUD: B survived (5/5) and reported status=CLOUD_FAILED
- **When:** 2026-10-13 14:25:58

```
status=CLOUD_FAILED exit=1 error={"time":"2026-10-13T14:25:27.9950648+05:30","level":"info","msg":"Starting bandwidth limiter at 1000Mi Byte/s","source":"accounting/token_bucket.go:109"}
{"time":"2026-10-13T14:25:28.0093118+05:30","level":"info","msg":"Running all checks before starting transfers","object":"Local file system at //? | A left=2/2  B left=5/5
```

### `C1./ZB` — robocopy /ZB works for a normal service account

- **Verdict:** CRITICAL: /ZB is a HARD FAILURE (exit 16, privilege absent) — every LAN backup would fail before copying anything
- **When:** 2026-10-13 14:26:04

```
WITH /ZB    : exit=16 err='' | copied file exists=see below
WITHOUT /ZB : exit=1 err=''
```

### `C2.dirs_only_no_zb` — all-empty-dirs source with /ZB removed still wipes the LAN destination

- **Verdict:** LAN DATA LOSS CONFIRMED (purge with no files in source)
- **When:** 2026-10-13 14:26:04

```
robocopy exit=2 | dest files before=12 after=0
```

### `C3.denied_subtree_no_zb` — ACL-denied source subtree is purged from the LAN destination when /ZB is absent

- **Verdict:** LAN protected: B survived (5/5), status=LAN_PARTIAL
- **When:** 2026-10-13 14:26:05

```
robocopy exit=9 decision=LAN_PARTIAL | B survivors=5/5
```

### `C4.denied_file` — a single ACL-denied source file is not mirrored and not deleted (exit 8 path)

- **Verdict:** LAN PARTIAL as designed (old copy retained, exit 8 -> LAN_PARTIAL)
- **When:** 2026-10-13 14:26:05

```
status=LAN_FAILED exit=16 files_failed=0 | dest locked.txt exists=True content=b'OLD COPY\nOLD COPY\nOL'
```

### `D.kill_exit0.lan` — robocopy killed mid-run with exit code 0 is reported as success (F-08 forge)

- **Verdict:** FAIL-CLOSED OK: status=LAN_SUSPECT (kill detected, nothing falsely COMPLETE)
- **When:** 2026-10-13 14:26:00

```
found_robocopy_pid=4148 NtTerminateProcess(0)=True | status=LAN_SUSPECT exit_code=0 termination=abnormal log_complete=False log_retained=C:\Users\malliath\AppData\Local\Temp\robocopy_sync_wi9m4koh.log | dest files copied=1191/4000
```

### `D.kill_exit0.cloud` — rclone killed mid-run with exit code 0 is caught by run_cloud_sync itself

- **Verdict:** transfer-level contract: status=CLOUD_COMPLETE (the flow-level evidence gate is the real safeguard)
- **When:** 2026-10-13 14:26:03

```
found_rclone_pid=11292 NtTerminateProcess(0)=True | status=CLOUD_COMPLETE exit_code=0 suspect_reason= | objects landed=404/3000
```

### `B2.size_only_blind` — a same-size (+ same-mtime) corrupted cloud copy is caught by the daily gate

- **Verdict:** BLIND SPOT CONFIRMED — daily gate VERIFIED, weekly audit FAILED
- **When:** 2026-10-13 14:26:05

```
DAILY  --size-only : verified=True exit=0 termination=normal differences=0 reason=None
WEEKLY  hash       : status=VERIFICATION_FAILED mismatches=1 capability=HASH_GCS_ADVERTISED detail=capability=HASH_GCS_ADVERTISED content mismatch detected (missing-from-dest=0 extra-in-dest=0 content-changed=1 elapsed_s=0 paths=['Clients/ledger.txt'])
```

### `D2.kill_exit0.lan` — robocopy killed mid-run with exit code 0 is reported as success (F-08 forge)

- **Verdict:** FAIL-CLOSED OK — kill detected as LAN_SUSPECT
- **When:** 2026-10-13 14:26:08

```
robocopy_pid=1104 NtTerminateProcess(p,0)=True | status=LAN_SUSPECT exit_code=0 termination=abnormal log_complete=False log_retained=C:\Users\malliath\AppData\Local\Temp\robocopy_sync_36k6nmyx.log | dest files=1634/4000 | reason=robocopy exit 0 WITHOUT a completed job summary - log is truncated (external kill/crash). Exit code is not evidence of success (T04/A1).
--- robocopy log tail (100000 bytes retaine
```

### `BUG-1.watchdog_alert` — the watchdog's wedged-lock CRITICAL alert is actually delivered

- **Verdict:** CONFIRMED DEAD CODE â€” ModuleNotFoundError swallowed by the except; alert is silently dropped (never reaches the operator)
- **When:** 2026-10-13 14:26:09

```
importlib.util.find_spec('core.notifications') = None | _alert_wedged_lock() returned None without raising | captured loguru lines: 'Watchdog alert delivery skipped/failed: lab: this must never be reached'
```

### `BUG-2.cloud_status` — a failed cloud run is never recorded with a success-looking status

- **Verdict:** status correctly reflects the failure
- **When:** 2026-10-13 14:26:09

```
flow raised: RuntimeError: Cloud integrity verification FAILED after sync: rclone check found differences vs source (missing-from-cloud=0, unexpect | run_history.status='CLOUD_VERIFY_FAILED' | last_run(mode)='CLOUD_VERIFY_FAILED' | last_successful_run()=None
```

### `BUG-2.lan_status` — a failed LAN run (post-sync exception) is never recorded as COMPLETE

- **Verdict:** CONFIRMED MISREPORT â€” flow failed but DB row says 'LAN_COMPLETE' (listed as a successful run by last_successful_run / dashboard / reports)
- **When:** 2026-10-13 14:26:09

```
flow raised: OperationalError: database is locked | run_history.status='LAN_COMPLETE' | last_successful_run()='LAN_COMPLETE'
```

### `LOCK.roundtrip` — a live lock is recognised and not stolen

- **Verdict:** OK
- **When:** 2026-10-13 14:26:09

```
write_lock -> read_lock_alive = alive=True pid=19544 (self pid=19544)
```

### `LOCK.corrupt` — corrupt lock payloads never crash the reader

- **Verdict:** OK â€” no exceptions (0 raised)
- **When:** 2026-10-13 14:26:09

```
payload -> (alive, pid):
    '' -> (False, None)
    '   ' -> (False, None)
    'not-a-lock' -> (False, None)
    ':' -> (False, None)
    '-1:1.5' -> (False, -1)
    '0:0' -> (False, 0)
    '1:1:2' -> (False, None)
    'abc:def' -> (False, None)
    '999999999999999999999:1' -> (False, 999999999999999999999)
    '-5' -> (False, -5)
    '999999999999999999999' -> (False, 999999999999999999999)
    '1e9:1e9' -> (False, None)
```

### `LOCK.db_concurrency` — concurrent writers on manifest.db are serialised (DDL phase + inserts)

- **Verdict:** OK â€” busy_timeout serialises writers
- **When:** 2026-10-13 14:26:10

```
2 threads x 25 inserts -> stored rows=50/50 errors=0

```

### `LOCK.pid_reuse` — PID reuse is detected and a live foreign owner is never stolen

- **Verdict:** OK â€” refuse live owner, collect stale lock
- **When:** 2026-10-13 14:26:10

```
live owner (correct create_time)  -> alive=True
reused PID (wrong create_time)    -> alive=False
acquire_lock vs live owner        -> False (must be False)
acquire_lock vs stale/reused lock -> True (must be True); we now own it=True
```

### `LOCK.db_concurrency.inserts_only` — concurrent INSERT-only writers are serialised

- **Verdict:** OK for insert-only contention
- **When:** 2026-10-13 14:26:10

```
2 threads x 25 inserts (schema pre-created) -> stored rows=50/50 errors=[]
```

### `F.classify_exit_code` — robocopy exit-code classification is total & consistent

- **Verdict:** OK
- **When:** 2026-10-13 14:26:10

```
code -> status:
    -5 -> LAN_FAILED
    -4 -> LAN_FAILED
    -3 -> LAN_FAILED
    -2 -> LAN_FAILED
    -1 -> LAN_FAILED
     0 -> LAN_COMPLETE
     1 -> LAN_COMPLETE
     2 -> LAN_COMPLETE
     3 -> LAN_COMPLETE
     4 -> LAN_COMPLETE
     5 -> LAN_COMPLETE
     6 -> LAN_COMPLETE
     7 -> LAN_COMPLETE
     8 -> LAN_PARTIAL
     9 -> LAN_PARTIAL
    10 -> LAN_PARTIAL
    11 -> LAN_PARTIAL
    12 -> LAN_PARTIAL
    13 -> LAN_PARTIAL
    14 -> LAN_PARTIAL
    15 -> LAN_PARTIAL
    16 -> LAN_FAILED
    17 -> LAN_FAILED
    18 -> LAN_FAILED
...
```

### `F.clean_path_idempotence` — _clean_path() is idempotent (normalising twice is a no-op)

- **Verdict:** MINOR: double application differs (trailing space/separator not stripped on pass 1)
- **When:** 2026-10-13 14:26:15

```
input='P8h4\x0b/' -> _clean_path='P8h4\x0b' -> _clean_path again='P8h4'
```

### `W2.manifest` — get_cloud_manifest's cost at this file count (and extrapolation)

- **Verdict:** extrapolation -> 1M: ~473000 MB RSS, ~11s | 2.5M: ~1182500 MB RSS, ~28s
- **When:** 2026-10-13 14:27:46

```
objects=200000 | wall=2.2s | RSS 266MB -> 358MB (+92MB, 0.5 KB/object) | str(list)=34.8 MB (174 B/object) | pickle=24.0 MB (120 B/object)
```

### `W2.lsjson_subprocess` — rclone lsjson -R wall time alone (is manifest_timeout_seconds=900 enough?)

- **Verdict:** extrapolation -> 1M ~10s, 2.5M ~24s (config manifest_timeout_seconds=900)
- **When:** 2026-10-13 14:27:48

```
rclone exit=0 wall=1.9s stdout=32.6 MB stderr=
```

### `W2b.lan_snapshots` — LAN snapshot cost (2 per run, both passed through Prefect task results)

- **Verdict:** per file: 0.20 KB RSS, 34 B pickled -> at 1M: ~196 MB RSS and ~34 MB pickled, x2 snapshots per run
- **When:** 2026-10-13 14:29:57

```
files=100000 | first walk=7.8s (+20MB RSS) | second walk=7.8s (+18MB RSS) | pickle(before)=3.4 MB | pickle(after)=3.4 MB
```

### `W2b.db_before_dict` — get_cloud_synced_entries() (flow before_dict) cost at scale

- **Verdict:** per file: 0.38 KB, 34 B pickled -> at 1M ~367 MB RSS, ~3s
- **When:** 2026-10-13 14:29:58

```
entries=100000 | insert=0.8s | read=0.3s | RSS now 341MB (+37MB) | pickle=3.4 MB
```

### `W2b.combined` — total heap held at once in the cloud pipeline at 1M objects (manifest + before_dict + snapshots)

- **Verdict:** at 1M objects the cloud leg holds roughly 1021 MB of live Python objects, and writes ~102 MB of task results into the Prefect result store PER NIGHT
- **When:** 2026-10-13 14:29:58

```
measured at 100000: manifest ~+59 MB, before_dict ~+367 MB (at 1M), LAN snapshots 2x 34 MB pickled
```

### `W4.2.cloud_kill_gate` — a cloud sync killed mid-transfer with a forged exit 0 is rejected by the evidence gate

- **Verdict:** GATE DID NOT REJECT â€” see evidence
- **When:** 2026-10-13 14:31:46

```
rclone_pid=None killed_exit0=False | objects landed=0/700 | flow raised=RuntimeError: Cloud preflight failed: Exit 3: 2026/10/13 14:31:36 ERROR : error listing: directory not found
2026/10/13 14:31:36 NOTICE: Failed to lsjson with 2 err | run_history=['CLOUD_FAILED'] | last_successful_run=None | alerts_sent=0
```

### `W4.3.lan_happy_unc` — the full LAN pipeline mirrors onto the loopback SMB share

- **Verdict:** see evidence
- **When:** 2026-10-13 14:32:00

```
returned {'status': 'LAN_COMPLETE', 'exit_code': 3} | files on NAS=41 (expected 40 ledger files) | canary present on dest=True | run_history=['LAN_COMPLETE'] | lan_status='synced' entries=41 | alerts=0
```

### `W4.1.fy` — cloud prefix is source-leaf-derived (v1.1.3 _effective_fy_prefix), LAN prefix comes from config

- **Verdict:** NOTE: v1.1.3 _effective_fy_prefix aligns the cloud prefix with the configured source leaf
- **When:** 2026-10-13 14:30:30

```
_effective_fy_prefix()=FY26-26 and lan_destination ends in FY26-26 -> both legs write the same FY folders (M-4 divergence no longer possible on this rig)
```

### `W4.1.cloud_happy` — the full cloud pipeline runs a real transfer and records it

- **Verdict:** OK â€” end-to-end cloud leg works against the local backend
- **When:** 2026-10-13 14:30:32

```
pipeline returned {'status': 'CLOUD_COMPLETE', 'exit_code': 0} | run_history=['CLOUD_COMPLETE'] | cloud_status='synced' entries in manifest=80/80 | alerts sent=0
```

### `LOCK.db_inserts_ok` — insert-only writers never lock

- **Verdict:** OK
- **When:** 2026-10-13 14:26:10

```
0/50 errors
```

### `W5a.path_variants` — filenames with spaces/Unicode/reserved names/trailing dots survive the LAN mirror

- **Verdict:** all hostile names mirrored correctly
- **When:** 2026-10-13 14:32:00

```
created=11/13 | robocopy exit=3 status=LAN_COMPLETE files_failed=0 | landed at destination=11 | NOT COPIED: [] | create failures: {'new\nline.txt': 'SOURCE CREATE FAILED: OSError: [Errno 22] Invalid argument: ', 'tab\tname.txt': 'SOURCE CREATE FAILED: OSError: [Errno 22] Invalid argument: '}
```

### `W5a.path_variants.cloud` — the same hostile filenames sync to the cloud

- **Verdict:** cloud leg handled all names
- **When:** 2026-10-13 14:32:00

```
rclone exit=0 status=CLOUD_COMPLETE error= | landed=11 NOT SYNCED: []
```

### `W5a.deep_paths` — deep (>260 char) paths are copied AND enumerated consistently

- **Verdict:** deep paths handled consistently
- **When:** 2026-10-13 14:32:00

```
deepest source path = 295 chars | source files=5 | robocopy exit=3 status=LAN_COMPLETE files_failed=0 | destination files (ground truth, \\?\ prefix)=6 | walk_lan_destination() saw=6 (deep: 5)
```

### `W5b.junction.lan` — a junction inside the source does not duplicate the tree into the destination

- **Verdict:** OK — /XJ excluded the junction (no duplication, no recursion)
- **When:** 2026-10-13 14:32:00

```
mklink rc=0 | robocopy exit=3 status=LAN_COMPLETE | copied files=20 (expected 20) | files under the junction=0 | e.g. []
```

### `W5b.junction.cloud` — rclone does not follow the junction into the cloud

- **Verdict:** OK — junction not followed
- **When:** 2026-10-13 14:32:00

```
rclone exit=0 status=CLOUD_COMPLETE error= | objects=20 | via junction=0
```

### `W5b.rename_mid_run` — a source rename during the mirror does not lose the destination copy

- **Verdict:** MIRROR-RACE CONFIRMED — the destination ends up with 0 copies of the renamed folder in this run; the next run re-copies it (recovers), but for one cycle the backup is missing those files
- **When:** 2026-10-13 14:32:05

```
robocopy exit=11 status=LAN_PARTIAL files_failed=1 | dest 'TO_BE_RENAMED' (purged by /MIR)=0/50 | dest 'RENAMED_BY_USER'=0/50
```

### `W5b.double_trigger` — two simultaneous LAN runs are serialised by the lock, not interleaved

- **Verdict:** OK — no interleaving of the two mirror runs (they ran back-to-back or one refused)
- **When:** 2026-10-13 14:32:29

```
threads -> [('first', 'LAN_COMPLETE'), ('second', 'LAN_COMPLETE')] | run rows=['LAN_COMPLETE', 'LAN_COMPLETE'] | files on NAS=400/400 | NOTE: PREFECT_TEST_MODE=1 so the Prefect concurrency slot is bypassed here; this exercises only the backup.lock layer
```

### `W5b.crash_recovery` — a hard crash mid-backup does not wedge the next night's run

- **Verdict:** OK — stale lock collected, night 2 completed, DB consistent
- **When:** 2026-10-13 14:32:25

```
killed flow tree rc=0 (robocopy pid=18764) | lock file after crash: exists=False alive=False pid=None | night-2 pipeline={'status': 'LAN_COMPLETE', 'exit_code': 3} | files on NAS=4000/4000 | manifest lan_status rows=4001 | run rows=['LAN_COMPLETE']
```

### `W5c.missing_canary` — a missing canary makes the whole LAN pipeline fail AND sends an alert

- **Verdict:** see evidence
- **When:** 2026-10-13 14:32:59

```
raised=HealthError: Canary file \\127.0.0.1\C$\Users\malliath\AppData\Local\Temp\opencode\aam_lab\case_73j9hthh\nas\FY26-26\.AAM_TARGET_MOUN | run_history=['LAN_FAILED'] | alerts emitted=0 | destination files=0
```

### `W5c.smb_false_positive` — a TCP handshake on 445 is treated as 'NAS online' by the WoL probe

- **Verdict:** DESIGN NOTE — the WoL/SMB probe is a bare TCP connect, so a hung NAS that accepts connections reads as 'online'; the canary + robocopy access errors are the real backstop
- **When:** 2026-10-13 14:33:00

```
_smb_port_open(445)=True | _smb_port_open(decoy port 54067)=True | ensure_server_online()=True | pipeline raised=NoneType: None
```

### `W5c.source_disappears` — the source vanishing mid-run is detected, not silently mirrored as empty

- **Verdict:** ISSUE — status=LAN_COMPLETE on_dest=3000
- **When:** 2026-10-13 14:33:02

```
robocopy exit=3 status=LAN_COMPLETE error= | destination files after=3000/3000 | rename_err=PermissionError(13, 'Access is denied')
```

### `W5c.locked_file` — a file locked by a user is not mirrored but not deleted either

- **Verdict:** ISSUE — locked file was deleted or replaced at the destination
- **When:** 2026-10-13 14:33:05

```
status=LAN_PARTIAL exit=11 files_failed=1 | dest copy present=False content=b'' | dest files=200/200
```

### `DEPLOYED.services` — the three services are installed and running

- **Verdict:** ISSUE - at least one service is not RUNNING
- **When:** 2026-10-09 23:12:42

```
  AamPrefectServer: state=RUNNING logon=LocalSystem
  AamBackupAgent: state=STOPPED logon=.\innovizta_2
  AamWatchdog: state=STOPPED logon=.\innovizta_2
```

### `DEPLOYED.deployments` — the deployments are registered with the config's cron + timezone

- **Verdict:** OK - schedules match config.yaml
- **When:** 2026-10-09 23:12:43

```
registered: [('backup-cloud', ['0 18 * * */Asia/Kolkata']), ('backup-lan', ['0 1 * * */Asia/Kolkata']), ('integrity-audit', ['0 3 * * SUN/Asia/Kolkata']), ('monthly-report', ['0 8 1 * */Asia/Kolkata']), ('rollover-check', ['0 6 * * */Asia/Kolkata']), ('weekly-report', ['0 8 * * MON/Asia/Kolkata'])]
config expects: {'backup-cloud': ('0 18 * * *', 'Asia/Kolkata'), 'backup-lan': ('0 1 * * *', 'Asia/Kolkata')}
```

### `DEPLOYED.zb_service_account` — the LAN leg's /ZB behaviour matches the service Log On account

- **Verdict:** OK - the LAN leg is copying (no 16s)
- **When:** 2026-10-09 23:42:48

```
AamBackupAgent logs on as: .\innovizta_2 | whoami /priv for THIS session: SeBackupPrivilege=False | recent LAN runs (status, exit_code)=[('LAN_FAILED', -1), ('LAN_FAILED', -1)] | exit 16 = /ZB hard failure, exit 1/3 = copy happened
```

### `DEPLOYED.watchdog_recovery` — the watchdog restarts a stopped Prefect server (no manual action)

- **Verdict:** OK - self-healing works
- **When:** 2026-10-09 23:42:53

```
stopped AamPrefectServer -> back to RUNNING=True after ~0s (detection is 5 x 60s + restart)
```

### `DEPLOYED.lan_backup` — a LAN backup triggered through the deployment API really copies the files

- **Verdict:** ISSUE - state=FAILED, missing=[], canary=True
- **When:** 2026-10-09 20:24:45

```
flow run state=FAILED | source files=0 | files at destination before=1 after=1 (canary kept=True) | MISSING at destination=[] | run_history=['LAN_FAILED'] | exit_code=-1 files_failed=0
```

### `DEPLOYED.cloud_backup` — a cloud backup triggered through the deployment API really uploads to GCS

- **Verdict:** ISSUE - state=FAILED, missing=[]
- **When:** 2026-10-09 20:24:59

```
flow run state=FAILED | FY prefix=FY26-27 | bucket=aam-backup-demo-innovizta | objects in GCS=687 -> ['AAM_NIGHTLY_CANARY_20260821/nested/sub/nightly_02_nested.txt', 'AAM_NIGHTLY_CANARY_20260821/nightly_01_notes.txt', 'AAM_NIGHTLY_CANARY_20260821/nightly_03_block.bin', 'CrystalDiskInfo_20260513171206.png', 'E2E_TEST_FY/.AAM_TARGET_MOUNTED', 'E2E_TEST_FY/SubFolder/nested.txt', 'E2E_TEST_FY/locked_document.txt', 'E2E_TEST_FY/test_large.bin'] | source files=0 MISSING from GCS=[] | run_history=['CLOUD_FAILED']
```

### `DEPLOYED.stale_lock_recovery` — a stale lock from a crashed run does not block the next backup

- **Verdict:** ISSUE - state=FAILED, lock_present=False
- **When:** 2026-10-09 20:25:16

```
planted stale lock C:\BackupAgent\backup.lock -> flow state=FAILED | lock still present after run=False | run_history=['LAN_FAILED']
```

### `LOCK.db_inserts_flaky` — insert-only writers never lock

- **Verdict:** INTERMITTENT: insert-only writers did hit a lock this run
- **When:** 2026-10-10 22:01:08

```
['B: OperationalError: database is locked']
```

### `W6.alert_policy` — a failed cloud run raises an ExceptionGroup and alerts exactly once

- **Verdict:** FINDING — the verify-gate failure PAGES TWICE: the in-pipeline gate alert ('Cloud integrity verification FAILED…') AND backup()'s generic 'Backup completed with errors' alert, because the raised exception is a plain RuntimeError and only PartialRun suppresses the generic one
- **When:** 2026-10-13 14:34:16

```
raised=ExceptionGroup | sub-exceptions=['RuntimeError'] | is_partial=False | alerts emitted=2 | run_history=['CLOUD_VERIFY_FAILED'] | alert subjects=['Cloud integrity verification FAILED after sync: rclone check found dif', 'Cloud integrity verification FAILED after sync: rclone check found dif']
```

### `W6.rollover_flow` — rollover_check_flow performs the real rollover and rewrites config.yaml

- **Verdict:** OK — real rollover executed through the flow (config atomically rewritten to the new FY)
- **When:** 2026-10-13 14:34:28

```
flow returned='ROLLOVER_COMPLETED' | old source='C:\\Users\\malliath\\AppData\\Local\\Temp\\opencode\\aam_lab\\case_9cmq96s5\\source\\FY26-26' | new source='C:\\Users\\malliath\\AppData\\Local\\Temp\\opencode\\aam_lab\\case_9cmq96s5\\source\\FY26-27' | new lan='\\\\127.0.0.1\\C$\\Users\\malliath\\AppData\\Local\\Temp\\opencode\\aam_lab\\case_9cmq96s5\\nas\\FY26-27' | expected FY=FY26-27 | new source folder exists=True | run_history=[]
```

### `W6.integrity_audit_flow` — integrity_audit_flow records a durable audit row for the LAN leg

- **Verdict:** OK — audit row written through the real flow (divergence -> alert + failed flow)
- **When:** 2026-10-13 14:34:40

```
raised=ExceptionGroup: Integrity audit found divergence (1 sub-exception) | latest_audit(lan) status=VERIFICATION_FAILED detail='capability=HASH_MD5_RCLONE_LOCAL content mismatch detected (missing-from-dest=60 extra-in-dest=30 content-changed=0 elap' | alerts=1
```

### `W8.orphan_cancel` — the startup cleanup cancels the orphaned runs of a crashed agent, but never cancels a RUNNING backup whose lock is held by a live process

- **Verdict:** OK — PENDING always reaped; RUNNING preserved while its lock is live, cancelled once the lock is gone
- **When:** 2026-10-13 14:36:24

```
planted RUNNING=cbd0555f-3b9f-4e67-a38f-8046493822d2 PENDING=88f87b51-d2d2-402c-ab91-2cd1048c39dd | with a LIVE lock: running->RUNNING pending->CANCELLED | with the lock released: running->CANCELLED pending->CANCELLED | lock after cleanup: exists=False alive=False
```

### `W8.concurrent_serialise` — two LAN backups started at the same moment serialise on the global 'aam-backup' concurrency limit (limit=1) instead of interleaving

- **Verdict:** OK — the two mirrors ran back-to-back (no overlap), every file copied exactly once, both runs recorded
- **When:** 2026-10-13 14:36:46

```
child exit codes=[0, 0] | child outcomes={1: 'OK', 2: 'OK'} | mirror intervals (child, enter, exit)=[(2, 1791882399.3, 1791882401.082), (1, 1791882403.884, 1791882403.966)] | queue wait per child (mirror start - flow start, s)={1: 6.28, 2: 1.7} | intervals overlap=False | files on NAS=800/800 | run_history=['LAN_COMPLETE', 'LAN_COMPLETE'] | manifest lan rows=801
```

### `W8.report_flows` — the weekly/monthly report flows run as real flow runs and contain an SMTP outage (the flow still COMPLETES; the report is simply not sent)

- **Verdict:** OK — SMTP retry storm contained inside core.report; both report flows COMPLETED against the real Prefect API
- **When:** 2026-10-13 14:37:00

```
smtp=127.0.0.1:59868 (refusing) | weekly_report_flow raised=None | monthly_report_flow raised=None | run_history rows (reports never write backup rows)=[]
```

### `W8.leg_scoped_modes` — each leg's deployment runs only its own pipeline (mode parameter) and records exactly one run_history row

- **Verdict:** OK — one cloud row and one LAN row, no cross-leg writes
- **When:** 2026-10-13 14:37:20

```
mode=cloud raised=None | mode=lan raised=None | run_history=['lan:LAN_COMPLETE', 'cloud:CLOUD_COMPLETE'] | modes present=['cloud', 'lan'] | manifest lan_rows=801 cloud_rows=800
```

### `W8.dashboard_trigger` — a run created through the deployment API is seen as active, so the dashboard refuses a duplicate trigger instead of queueing a second backup

- **Verdict:** OK — queued run visible to the guard; duplicate click refused with 400 already_running (no second backup queued)
- **When:** 2026-10-13 14:35:21

```
arun_deployment('aam-backup/backup-lan') -> run c192dc2b-44d4-4d1c-b236-85cd01078c65 state=SCHEDULED mode=lan | ui._is_running('lan')=True | second POST /trigger/lan -> 400 {'status': 'already_running', 'detail': 'LAN backup is already in progress.'}
```

### `W8.deployments` — the production deployment set registers on a real Prefect server with the config's cron + timezone

- **Verdict:** OK — all 6 deployments registered, schedules match config.yaml
- **When:** 2026-10-13 14:35:08

```
serve.deployments() -> ['backup-cloud', 'backup-lan', 'weekly-report', 'monthly-report', 'rollover-check', 'integrity-audit'] | registered on server: [('backup-cloud', [('0 18 * * *', 'Asia/Kolkata')]), ('backup-lan', [('0 1 * * *', 'Asia/Kolkata')]), ('integrity-audit', [('0 3 * * SUN', 'Asia/Kolkata')]), ('monthly-report', [('0 8 1 * *', 'Asia/Kolkata')]), ('rollover-check', [('0 6 * * *', 'Asia/Kolkata')]), ('weekly-report', [('0 8 * * MON', 'Asia/Kolkata')])] | problems=[]
```

### `W8.trigger_fail_closed` — with the Prefect API unreachable the dashboard refuses to trigger (503) and /status degrades to run_state=unknown — it never claims idle

- **Verdict:** OK — both triggers refused with 503 (fail-closed), /status degrades to 'unknown' with running=False (fail-open display only)
- **When:** 2026-10-13 14:36:11

```
PREFECT_API_URL=http://127.0.0.1:51300/api | POST /trigger/lan -> 503 {'detail': 'Prefect API unavailable - cannot verify active runs; trigger refused. Retry shortly.'} | POST /trigger/cloud -> 503 {'detail': 'Prefect API unavailable - cannot verify active runs; trigger refused. Retry shortly.'} | GET /status -> 200 lan=unknown cloud=unknown running=False
```

### `W6.backup_flow_all` — the production flow.backup(mode='all') works end-to-end through Prefect

- **Verdict:** OK — real Prefect flow run with real task runs for both legs
- **When:** 2026-10-13 14:33:19

```
flow run state=COMPLETED | task runs recorded: ['cloud-preflight-555', 'cloud-publish-artifact-f1a', 'cloud-record-40f', 'cloud-sync-b36', 'cloud-verify-and-report-19b', 'health-check-3c4', 'health-check-7c6', 'lan-preflight-e94', 'lan-publish-artifact-ba0', 'lan-record-096', 'lan-shutdown-a8e', 'lan-snapshot-after-3d5', 'lan-snapshot-before-cf1', 'lan-sync-01f', 'wol-check-0f0'] | run_history=['LAN_COMPLETE', 'CLOUD_COMPLETE'] | manifest lan_rows=61 cloud_rows=60 | alerts=0 | elapsed=3s
```

### `W6.task_retry` — the cloud preflight task's configured retry (retries=1) re-runs it once

- **Verdict:** OK — the retry policy executed the failing task a second time and the run recovered
- **When:** 2026-10-13 14:34:03

```
preflight invocation count=2 (1st failed, 2nd real) | run_history=['CLOUD_COMPLETE'] | manifest cloud rows=60
```

### `W6.deployments` — deployments/schedules come from config, and a disabled leg is PAUSED server-side

- **Verdict:** OK — registration + pause reconciliation work against the real Prefect server
- **When:** 2026-10-13 14:34:54

```
deployments=['backup-cloud', 'backup-lan', 'weekly-report', 'monthly-report', 'rollover-check', 'integrity-audit'] | backup-lan schedule=0 1 * * * timezone=Asia/Kolkata | reconcile output={'backup-lan': 'PAUSED (leg disabled)', 'backup-cloud': 'active', 'integrity-audit': 'active'} | server-side paused=True
```

### `W9.lan_random_tree` — a Hypothesis-random source tree mirrored by robocopy /MIR is byte-identical at the destination

- **Verdict:** OK — lossless + honest (I1, I3, I4 hold)
- **When:** 2026-10-13 14:37:36

```
files=5 bytes=34 | run1 status=LAN_COMPLETE | src files=4 dest files=4 | missing=[] | different=[]
```

### `W9.lan_convergence` — a second run over the same tree is a no-op (idempotent mirror)

- **Verdict:** OK — second run changed nothing (I2 holds)
- **When:** 2026-10-13 14:37:37

```
run2 status=LAN_COMPLETE | dest files run1=4 run2=4 | converged=True
```

### `W9.fault_lan` — under fault injection a COMPLETE status never hides missing or altered data

- **Verdict:** OK — honest: failure status, or COMPLETE with a byte-identical destination (I4 holds)
- **When:** 2026-10-13 14:38:35

```
fault=share-none lock (could not hold lock: [Errno 32] CreateFileW failed) | status=RAISED PartialRun raised=PartialRun | src=2 dest=2 | missing=[] | different=[]
```

### `W9.mutation_repair` — a random destination mutation is repaired by the next nightly run

- **Verdict:** OK — mutation repaired by the next run
- **When:** 2026-10-13 14:38:53

```
mutation=delete (destination file deleted) | status=LAN_COMPLETE raised=None | src=3 dest=3 | repaired=True | still-different=[]
```

### `W9.junction_excluded` — a junction inside the source is excluded (/XJ): no duplication and no recursion, while the real files still mirror byte-identically

- **Verdict:** OK — junction excluded (0 files through it) and the real tree mirrored exactly
- **When:** 2026-10-13 14:38:36

```
mklink rc=0 | status=LAN_COMPLETE raised=None | files copied through the junction=0 | real files mirrored=12/12 identical=True
```

### `W9.db_concurrency_stress` — randomised concurrent writers on manifest.db lose no rows

- **Verdict:** REAL FINDING — concurrent ManifestDB opens lose run-history rows. The loser raises OperationalError('database is locked') inside _get_conn()'s executescript(DDL) (core/manifest.py:163) even with busy_timeout_ms=8000, because PRAGMA busy_timeout is applied only AFTER the legacy dedup DELETE + commit (manifest.py:150-162). record_run_history() catches that and returns False, so the run-history row is dropped with no trace — the backup's audit trail silently loses runs.
- **When:** 2026-10-13 14:38:54

```
round 0: threads=4 x12 -> 36/48 LOST=12 raised=1
round 1: threads=2 x28 -> 56/56
round 2: threads=4 x15 -> 45/60 LOST=15 raised=1
round 3: threads=5 x15 -> 75/75
round 4: threads=4 x13 -> 52/52
round 5: threads=3 x10 -> 20/30 LOST=10 raised=1
round 6: threads=5 x29 -> 145/145
round 7: threads=5 x5 -> 25/25
round 8: threads=6 x21 -> 126/126
round 9: threads=4 x15 -> 45/60 LOST=15 raised=1
round 10: threads=5 x24 -> 120/120
round 11: threads=3 x11 -> 33/33
```

### `W10.schedules` — every deployment's cron fires at the intended local time in the configured timezone, and the two backup legs never collide

- **Verdict:** OK — all 6 schedules fire at the intended local time; the legs never overlap
- **When:** 2026-10-13 14:19:12

```
backup-cloud    0 18 * * *   -> Sat 2026-10-10 18:00 Asia/Kolkata, Sun 2026-10-11 18:00 Asia/Kolkata, Mon 2026-10-12 18:00 Asia/Kolkata
backup-lan      0 1 * * *    -> Sat 2026-10-10 01:00 Asia/Kolkata, Sun 2026-10-11 01:00 Asia/Kolkata, Mon 2026-10-12 01:00 Asia/Kolkata
weekly-report   0 8 * * MON  -> Mon 2026-10-12 08:00 Asia/Kolkata, Mon 2026-10-19 08:00 Asia/Kolkata, Mon 2026-10-26 08:00 Asia/Kolkata
monthly-report  0 8 1 * *    -> Sun 2026-11-01 08:00 Asia/Kolkata, Tue 2026-12-01 08:00 Asia/Kolkata, Fri 2027-01-01 08:00 Asia/Kolkata
rollover-check  0 6 * * *    -> Sat 2026-10-10 06:00 Asia/Kolkata, Sun 2026-10-11 06:00 Asia/Kolkata, Mon 2026-10-12 06:00 Asia/Kolkata
integrity-audit 0 3 * * SUN  -> Sun 2026-10-11 03:00 Asia/Kolkata, Sun 2026-10-18 03:00 Asia/Kolkata, Sun 2026-10-25 03:00 Asia/Kolkata
timezone=Asia/Kolkata | leg collisions=[]
```

### `W10.line_coverage` — line coverage of the production code during one real LAN + cloud run

- **Verdict:** coverage map recorded (see LEDGER for the never-executed lines)
- **When:** 2026-10-13 14:20:57

```
summary: lan_sync.py: 101/160 (63%) | cloud_sync.py: 91/94 (97%) | cloud_verify.py: 49/67 (73%) | manifest.py: 114/222 (51%) | health.py: 49/80 (61%) | integrity.py: 0/145 (0%) | lan_preflight.py: 38/56 (68%) | cloud_preflight.py: 28/43 (65%) | flow.py: 397/569 (70%)
core/lan_sync.py: executed 101/160 lines; never executed: [1, 7, 8, 9, 10, 11, 13, 15, 16, 27, 28, 34, 37, 46, 50, 55, 59, 67, 74, 80, 90, 91, 92, 93, 94]
core/cloud_sync.py: executed 91/94 lines; never executed: [1, 6, 7, 8, 9, 10, 12, 14, 15, 19, 22, 26, 40, 45, 47, 48, 49, 54, 85, 96, 97, 98, 114, 121, 161]
core/cloud_verify.py: executed 49/67 lines; never executed: [1, 23, 24, 26, 28, 31, 32, 37, 39, 42, 46, 61, 68, 84, 85, 93, 94, 102, 103, 108, 122, 177, 178, 180, 182]
core/manifest.py: executed 114/222 lines; never executed: [1, 7, 8, 9, 10, 12, 14, 19, 21, 95, 105, 114, 129, 160, 167, 179, 180, 186, 211, 212, 215, 216, 223, 224, 225]
core/health.py: executed 49/80 lines; never executed: [1, 3, 4, 5, 6, 8, 9, 11, 15, 22, 33, 44, 52, 54, 61, 72, 80, 86, 91, 94, 96, 104, 121, 128, 136]
core/integrity.py: executed 0/145 lines; never executed: [1, 35, 36, 37, 38, 39, 40, 41, 43, 45, 54, 55, 56, 57, 70, 91, 92, 96, 97, 102, 106, 107, 108, 112, 116]
core/lan_preflight.py: executed 38/56 lines; never executed: [1, 7, 8, 9, 11, 12, 14, 15, 19, 25, 37, 45, 49, 67, 72, 73, 81, 82, 83, 87, 97, 98, 130, 131, 132]
core/cloud_preflight.py: executed 28/43 lines; never executed: [1, 23, 24, 26, 28, 29, 42, 65, 66, 67, 79, 85, 86, 87, 124, 125, 126, 128, 129, 130, 132, 133, 134, 140, 141]
flow.py: executed 397/569 lines; never executed: [1, 13, 14, 15, 16, 17, 18, 20, 21, 22, 23, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38]
```

### `W9.cloud_random_tree` — a Hypothesis-random source tree synced by rclone is byte-identical in the bucket

- **Verdict:** OK — lossless + honest (I1, I3, I4 hold)
- **When:** 2026-10-13 14:38:17

```
files=7 bytes=26 | run1 status=CLOUD_COMPLETE exit=0 | src=7 bucket=7 | missing=[] | different=[]
```

### `W9.cloud_convergence` — a second cloud run over the same tree is a no-op

- **Verdict:** OK — second run changed nothing (I2 holds)
- **When:** 2026-10-13 14:38:19

```
run2 status=CLOUD_NO_CHANGES_COMPLETE exit=9 | bucket files run1=7 run2=7 | converged=True
```

### `W11.log_corpus_replay` — every real robocopy log the lab produced parses to a self-consistent, correctly-classified verdict

- **Verdict:** OK — all replayed logs classify consistently
- **When:** 2026-10-13 14:20:57

```
logs replayed=14
robocopy_sync_omelbbsf.log: 30KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_tg22hd68.log: 238KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_2zauoyud.log: 175KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_3xhqios9.log: 29KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_1ubsopde.log: 239KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_zwt21rsl.log: 171KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_r5o0gcee.log: 34KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_xy6dffqs.log: 229KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_tnimz_7u.log: 168KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_eikxlcbm.log: 215KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_3gyz9in8.log: 175KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_zx2nn7us.log: 34KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_i5itizmi.log: 223KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
robocopy_sync_aei6skcx.log: 145KB status=LAN_SUSPECT summary=False counts=None failed_lines=0
```

### `W11.audit_branches` — the weekly audit engine's real branches: clean, missing, extra, same-size corruption and canary filtering

- **Verdict:** OK — same-size corruption DETECTED by the audit; canary filtered
- **When:** 2026-10-13 14:20:58

```
  clean                  -> ('VERIFIED', 'HASH_GCS_ADVERTISED')
  missing-from-dest      -> ('VERIFICATION_FAILED', 'HASH_GCS_ADVERTISED')
  extra-in-dest          -> ('VERIFICATION_FAILED', 'HASH_GCS_ADVERTISED')
  same-size corruption   -> ('VERIFICATION_FAILED', 'HASH_GCS_ADVERTISED')
  canary present         -> ('VERIFIED', 'HASH_GCS_ADVERTISED')
```

### `W11.temp_config` — write_temp_config() produces a config rclone itself accepts

- **Verdict:** OK — rclone parses the generated config
- **When:** 2026-10-13 14:20:59

```
config:
[aam_gcs]
type = google cloud storage
service_account_file = C:/Users/malliath/AppData/Local/Temp/w11key_2izpqdmf/svc.json
project_number = 123456789
object_acl =
bucket_acl =
bucket_policy_only = true
location = asia-south1
storage_class = STANDARD

rclone config show rc=0 stdout[:200]=[aam_gcs]
type = google cloud storage
service_account_file = C:/Users/malliath/AppData/Local/Temp/w11key_2izpqdmf/svc.json
project_number = 123456789
object_acl = 
bucket_acl = 
bucket_policy_only = t
```

### `W11.record_sync_results` — record_sync_results() normalises both rclone-lsjson and os.walk entry shapes into manifest rows

- **Verdict:** OK — both entry shapes accepted without error
- **When:** 2026-10-13 14:20:59

```
rclone-shaped entries=['a/b.txt', 'c.txt'] | walk-shaped entries=['d/e.txt'] | db read-back API available=False
```

### `W11.summary_round_trip` — a robocopy job summary rendered from known counts parses back to exactly those counts (locale/format-drift canary)

- **Verdict:** OK — round-trip exact
- **When:** 2026-10-13 14:20:59

```
known={'total': 1234, 'copied': 1200, 'skipped': 3, 'mismatch': 1, 'failed': 4, 'extras': 26}
parsed={'total': 1234, 'copied': 1200, 'skipped': 3, 'mismatch': 1, 'failed': 4, 'extras': 26}
row values=[1234, 1200, 3, 1, 4, 26]
summary_complete=True failed_file_count=4
```

### `W11.gate_messages` — the evidence gate's failure message names the ACTUAL primary cause rather than a fixed template (F-T3-2)

- **Verdict:** OK — each of the three documented categories is attributed distinctly
- **When:** 2026-10-13 14:20:59

```
  abnormal termination       -> Cloud integrity verification FAILED after sync: verification did not complete normally (te
  real divergence            -> Cloud integrity verification FAILED after sync: rclone check found differences vs source (
  manifest evidence error    -> Cloud integrity verification FAILED after sync: manifest/evidence error (manifest_error='l
  exit 0    -> 
  exit 1    -> Integrity mismatch — source and GCS file counts or sizes differ: detai
  exit 2    -> Rclone check failed with exit code 2 — check rclone logs for details: 
  exit 7    -> Rclone check failed with exit code 7 — check rclone logs for details: 
  exit 123  -> Rclone check failed with exit code 123 — check rclone logs for details
  category hits: {'abnormal termination': (True, False, False), 'real divergence': (False, True, False), 'manifest evidence error': (False, False, True)}
```

### `W11.clock_skew_fail_open` — check_clock_skew() fails OPEN when the Google Date header cannot be fetched (network unavailable) — the health gate still passes

- **Verdict:** DOCUMENTED BEHAVIOUR (fail-open): an unreachable network means the clock check passes, so clock skew — which breaks GCS JWT auth — is invisible exactly when the network is flapping. Decide deliberately whether that is the intended trade-off.
- **When:** 2026-10-13 14:20:59

```
check_clock_skew(timeout=2) -> ok=True reason=''
```

### `W11.fresh_corpus` — real robocopy runs with known outcomes classify correctly through the production parser

- **Verdict:** OK — copy/purge/prune/missing-source classified as the run really was, AND a summary-less log is refused even when the copy succeeded
- **When:** 2026-10-13 14:20:57

```
  copy 5 files                 exit=1   status=LAN_COMPLETE   counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0} failed=0
  dirs-only /MIR purges        exit=2   status=LAN_SUSPECT    counts=None failed=0
  /MIR prunes orphan           exit=3   status=LAN_SUSPECT    counts=None failed=0
  missing source               exit=16  status=LAN_FAILED     counts=None failed=0
  copy with /NJS (no summary)  exit=1   status=LAN_SUSPECT    counts=None failed=0
  /NJS run actually copied 5 files but was classified LAN_SUSPECT (fail-closed on a missing summary)
```

### `W12.fy_boundary` — get_fy_prefix() flips exactly at 1 April 00:00 IST, including the UTC-vs-IST trap (00:00 IST == 18:30 UTC the previous day)

- **Verdict:** OK - the FY boundary is IST-anchored and off-by-5h30m-free
- **When:** 2026-10-13 14:20:59

```
2027-03-31T23:59:59.999999+05:30 -> FY26-27  (expected FY26-27)
2027-04-01T00:00:00+05:30        -> FY27-28  (expected FY27-28)
2027-03-31T18:29:59Z             -> FY26-27  (expected FY26-27)
2027-03-31T18:30:00Z             -> FY27-28  (expected FY27-28)
2026-03-31T23:59:59+05:30        -> FY25-26  (expected FY25-26)
2026-04-01T00:00:00+05:30        -> FY26-27  (expected FY26-27)
2027-03-31T12:00:00+05:30        -> FY26-27  (expected FY26-27)
```

### `W12.detect_rollover` — detect_rollover() flips exactly at the 1 April 00:00 IST boundary, the G2 no-FY-folder opt-out stays a no-op, and disagreeing FY labels still trigger

- **Verdict:** OK - boundary, opt-out and disagreement all behave
- **When:** 2026-10-13 14:20:59

```
23:59:59.999 IST on 31 Mar 2027      -> detect_rollover=False (expected False)
00:00:00 IST on 1 Apr 2027           -> detect_rollover=True (expected True)
same instant in UTC (18:30 UTC 31 Mar) -> detect_rollover=True (expected True)
mid-year (1 Oct 2026)                -> detect_rollover=False (expected False)
no FY folder in either path          -> detect_rollover=False (expected False)
source/LAN FY labels disagree        -> detect_rollover=True (expected True)
```

### `W12.purge_retention` — purge_old_runs() under a frozen clock: the 89/90-day rows are kept, 91+ purged, today's row kept, file_entries untouched, retention_days=0 clears the run log, and the VACUUM branch does not raise

- **Verdict:** OK - retention boundaries are exact and file_entries survive
- **When:** 2026-10-13 14:21:27

```
frozen now  : 2026-10-10T02:00:00+05:30
rows before : ['r-000', 'r-001', 'r-089', 'r-090', 'r-091', 'r-365']
rows kept   : ['r-000', 'r-001', 'r-089', 'r-090']
rows purged : ['r-091', 'r-365']
file_entries rows still present: 2/2
```

### `W12.rollover_flow` — the real 1-April rollover runs through flow.rollover_check_flow with a frozen clock: final backup, new folders, LAN canary, config rewritten

- **Verdict:** OK - the 1-April event completes and rewrites config.yaml to the new FY
- **When:** 2026-10-13 14:21:13

```
flow returned        : 'ROLLOVER_COMPLETED'
old FY folders       : source=FY26-27 lan=FY26-27
new source_drive     : C:\Users\malliath\AppData\Local\Temp\opencode\aam_lab\case_9yh4tppv\source\FY27-28
new lan_destination  : \\127.0.0.1\C$\Users\malliath\AppData\Local\Temp\opencode\aam_lab\case_9yh4tppv\nas\FY27-28
new source exists    : True
new LAN folder exists: True
new LAN canary       : True
run_history after    : ['LAN_COMPLETE', 'CLOUD_COMPLETE']
alerts               : 0
runs before rollover: ['LAN_COMPLETE', 'CLOUD_COMPLETE']
```

### `W12.fy_divergence` — v1.1.3 _effective_fy_prefix keeps the cloud leg on the configured source leaf, so a missed rollover no longer splits the cloud and LAN FY trees (M-4 fixed)

- **Verdict:** OK - M-4 FIXED: both legs wrote the source-leaf FY, no calendar divergence
- **When:** 2026-10-13 14:21:27

```
cloud prefixes present in bucket : ['FY26-27', 'FY27-28']
objects written under FY26-27  : 12
objects written under FY27-28  : 0
LAN destination FY (from config)  : FY26-27
LAN destination still populated   : True
run_history                       : ['LAN_COMPLETE', 'CLOUD_COMPLETE']
```

### `W12.clock_skew` — check_clock_skew() branch coverage: within limit, +601 s, -900 s, -3600 s, missing header, unparseable header, network unreachable

- **Verdict:** OK - fails closed on real skew, fails open on a missing/unreachable network (documented trade-off) - all branches now covered
- **When:** 2026-10-13 14:21:27

```
exact match                        -> ok=True reason=''
+599 s (inside the 600 s limit)    -> ok=True reason=''
+600 s (exactly at the limit)      -> ok=True reason=''
+601 s (1 s over)                  -> ok=False reason='System clock skew detected: 601s difference from Google time (max allo'
-900 s (clock behind)              -> ok=False reason='System clock skew detected: 900s difference from Google time (max allo'
-3600 s (an hour behind)           -> ok=False reason='System clock skew detected: 3600s difference from Google time (max all'
no Date header                     -> ok=False reason='Could not retrieve Date header from Google'
unparseable Date header            -> ok=True reason=''
Google unreachable (OSError)       -> ok=True reason=''
garbage numeric Date header        -> ok=True reason=''
```

### `W12.seven_day_cycle` — seven consecutive nights (26 Mar - 1 Apr 2027) plus the boundary rollover and a converging night 8, EVERY night driven through the production Prefect flows: byte-identical destinations on both legs, one success row per leg per night, a convergent re-run, a passing weekly audit, and the FY split (M-4) visible exactly at the boundary

- **Verdict:** OK - the full nightly cycle is lossless, honest and convergent on both legs; the only anomaly is the FY split at the boundary (M-4)
- **When:** 2026-10-13 14:22:10

```
nights 1-7 (26 Mar .. 1 Apr): night 1 2027-03-26 01:00 Asia/Kolkata: calendar FY=FY26-27; night 2 2027-03-27 01:00 Asia/Kolkata: calendar FY=FY26-27; night 3 2027-03-28 01:00 Asia/Kolkata: calendar FY=FY26-27; night 4 2027-03-29 01:00 Asia/Kolkata: calendar FY=FY26-27; night 5 2027-03-30 01:00 Asia/Kolkata: calendar FY=FY26-27; night 6 2027-03-31 01:00 Asia/Kolkata: calendar FY=FY26-27; night 7 2027-04-01 01:00 Asia/Kolkata: calendar FY=FY27-28
re-run + audit lines: re-run on night 3: statuses=['CLOUD_COMPLETE', 'LAN_COMPLETE'] (no-op); weekly audit after night 4: ['VERIFIED', 'VERIFIED']
boundary night 7: cloud FY=FY26-27 (13 files), LAN FY=FY26-27 (13 files) — both legs on the source leaf (M-4 fixed by v1.1.3 _effective_fy_prefix)
rollover flow returned: 'ROLLOVER_COMPLETED'
new FY folders: source=True lan=True canary=True
old FY objects retained in bucket: 13
night 8: source=3 nas=3 cloud=3 | run rows added=['CLOUD_COMPLETE', 'LAN_COMPLETE']
alerts during the whole cycle: 0
cleanup_orphaned_robocopy_logs called by flow.backup(): 9 times
```

### `W13.decide_lan_matrix` — decide_lan_result() classifies every documented exit-code/log-evidence combination, including the contradictory-evidence and non-integer cases

- **Verdict:** OK - all 22 branches correct
- **When:** 2026-10-13 14:22:10

```
exit=     0 log=clean              -> LAN_COMPLETE  normal    log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=     1 log=clean              -> LAN_COMPLETE  normal    log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=     2 log=clean              -> LAN_COMPLETE  normal    log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=     3 log=clean              -> LAN_COMPLETE  normal    log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=     4 log=clean              -> LAN_COMPLETE  normal    log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=     7 log=clean              -> LAN_COMPLETE  normal    log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=     8 log=clean              -> LAN_PARTIAL   normal    log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=     9 log=clean              -> LAN_PARTIAL   normal    log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=    15 log=clean              -> LAN_PARTIAL   normal    log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=    16 log=clean              -> LAN_FAILED    abnormal  log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=    17 log=clean              -> LAN_FAILED    abnormal  log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=   999 log=clean              -> LAN_FAILED    abnormal  log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=    -1 log=clean              -> LAN_FAILED    abnormal  log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=  None log=clean              -> LAN_FAILED    abnormal  log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=   '0' log=clean              -> LAN_FAILED    abnormal  log_complete=True  counts={'total': 5, 'copied': 5, 'skipped': 0, 'mismatch': 0, 'failed': 0, 'extras': 0}
exit=     0 log=empty              -> LAN_SUSPECT   abnormal  log
```

### `W13.lan_sync_parsers` — summary-row parsing (separators, short rows, CRLF, non-numeric), log-completeness detection, failed-file counters with the bit-3 floor, the /NC guard and the bounded log tail reader

- **Verdict:** OK - every parser branch returns the documented value and fails closed
- **When:** 2026-10-13 14:22:10

```
complete summary                           -> [5, 5, 0, 0, 0, 0] (want [5, 5, 0, 0, 0, 0])
thousands separators                       -> [1234, 1200, 3, 1, 4, 26] (want [1234, 1200, 3, 1, 4, 26])
decimals stripped (localised separators)   -> [1234, 1, 0, 0, 0, 0] (want [1234, 1, 0, 0, 0, 0])
short row                                  -> [1, 2, 3] (want [1, 2, 3])
no Files row at all                        -> None (want None)
non-numeric token                          -> None (want None)
bare 'Files :' with nothing after          -> None (want None)
CRLF line endings                          -> [5, 5, 0, 0, 0, 0] (want [5, 5, 0, 0, 0, 0])
Dirs row only (must not be read as Files)  -> None (want None)
failed_file_count[clean log, exit 0] -> 0 (want 0)
failed_file_count[clean log, exit 8 (bit3 floor)] -> 1 (want 1)
failed_file_count[clean log, exit 9 (bit3 floor)] -> 1 (want 1)
failed_file_count[summary FAILED=2, exit 0] -> 2 (want 2)
failed_file_count[summary FAILED=2, exit 8 -> max(2,1)] -> 2 (want 2)
failed_file_count[no summary, exit 8 -> floor 1] -> 1 (want 1)
failed_file_count[no summary, exit 0 -> 0] -> 0 (want 0)
_read_log_tail(100 of 5014 bytes) -> 100 chars, starts with 'xxxxxxxx'
```

### `W13.exit_code_bool` — a boolean exit code must be rejected by BOTH result deciders (bool is an int subclass, so isinstance(code, int) is not enough)

- **Verdict:** RED - the LAN decider accepts True as a successful copy (exit 1) while the cloud decider rejects it; the two disagree on the same input class
- **When:** 2026-10-13 14:22:10

```
decide_lan_result(True, ...)         -> LAN_COMPLETE / normal
decide_cloud_verify_result(True, ...) -> verified=False / abnormal reason='unusable exit code True (timeout/spawn-failure sentinel)'
```

### `W13.command_contracts` — build_robocopy_command() and build_rclone_sync_command() carry the flags the parsers, the junction/canary exclusions and the fail-closed contract depend on, and honour every config tunable

- **Verdict:** OK - both command builders match their documented flag contract
- **When:** 2026-10-13 14:22:10

```
robocopy: C:\WINDOWS\system32\robocopy.EXE C:\Users\malliath\AppData\Local\Temp\opencode\aam_lab\case_ri7qrtbt\source\FY26-26 C:\Users\malliath\AppData\Local\Temp\opencode\aam_lab\case_ri7qrtbt\nas\FY26-26 /MIR /Z /ZB /XJ /MT:6 /R:3 /W:7 /V /TS /FP /NJH /NDL /NP /XF .AAM_TARGET_MOUNTED /XD System Volume Information $RECYCLE.BIN
rclone   : C:\Users\malliath\Desktop\aam_backup\deploy\bin\rclone.exe sync C:\Users\malliath\AppData\Local\Temp\opencode\aam_lab\case_ri7qrtbt\source\FY26-26 aam_gcs:lab-bucket/FY26-26 --config C:\Users\malliath\AppData\Local\Temp\opencode\aam_lab\case_ri7qrtbt\runtime\rclone.conf --fast-list --gcs-no-check-bucket --gcs-storage-class STANDARD --error-on-no-transfer --modify-window 2s --bwlimit 1000M --transfers 3 --checkers 4 --retries 2 --retries-sleep 30s --check-first --buffer-size 16M --use-json-log --log-level INFO --stats 60s
```

### `W13.rclone_classification` — classify_rclone_exit() covers every documented code, scan_rclone_log_for_errors() separates JSON ERROR/CRITICAL from plaintext fatal markers and noise, and resolve_max_duration_seconds() implements the documented precedence

- **Verdict:** OK - all rclone classification branches correct
- **When:** 2026-10-13 14:22:10

```
exit    0 -> CLOUD_COMPLETE (want CLOUD_COMPLETE)
exit    1 -> CLOUD_FAILED (want CLOUD_FAILED)
exit    2 -> CLOUD_FAILED (want CLOUD_FAILED)
exit    3 -> CLOUD_FAILED (want CLOUD_FAILED)
exit    4 -> CLOUD_PARTIAL (want CLOUD_PARTIAL)
exit    5 -> CLOUD_PARTIAL (want CLOUD_PARTIAL)
exit    6 -> CLOUD_FAILED (want CLOUD_FAILED)
exit    7 -> CLOUD_FAILED (want CLOUD_FAILED)
exit    8 -> CLOUD_FAILED (want CLOUD_FAILED)
exit    9 -> CLOUD_NO_CHANGES_COMPLETE (want CLOUD_NO_CHANGES_COMPLETE)
exit   10 -> CLOUD_PARTIAL (want CLOUD_PARTIAL)
exit   -1 -> CLOUD_FAILED (want CLOUD_FAILED)
exit   11 -> CLOUD_FAILED (want CLOUD_FAILED)
exit  123 -> CLOUD_FAILED (want CLOUD_FAILED)
error-scan[clean INFO stream] -> False
error-scan[single ERROR entry] -> True
error-scan[CRITICAL entry] -> True
error-scan[NOTICE level only] -> False
error-scan[plaintext fatal marker] -> True
error-scan[plaintext benign] -> False
error-scan[bandwidth noise] -> False
error-scan[empty log] -> False
max-duration[None + 3600s window -> 3300] -> 3300
max-duration[None + small window (margin does not fit)] -> None
max-duration[None + 300s window -> None] -> None
max-duration[0 -> disabled] -> None
max-duration[negative -> disabled] -> None
max-duration[600 -> used as-is] -> 600
max-duration[60 -> used as-is even though below the margin] -> 60
```

### `W13.cloud_verify_branches` — decide_cloud_verify_result() classifies clean/diverged/forged-exit-0/killed/error-line/transport cases, and _build_error_message() names the exit code for every non-zero code

- **Verdict:** OK - every verify branch is correct and never VERIFIED without a normal termination
- **When:** 2026-10-13 14:22:10

```
exit 0, clean summary                      -> verified=True  normal    diff=0 reason=''
exit 1, 3 differences                      -> verified=False normal    diff=3 reason='rclone reported 3 difference(s) (exit 1)'
exit 0, 5 differences (forged)             -> verified=False abnormal  diff=5 reason='exit 0 contradicts its own summary (5 difference'
exit 0, clean summary but ERROR lines      -> verified=False abnormal  diff=0 reason='exit 0 with a clean summary contradicts its own '
exit 0, clean summary but a failed verdict -> verified=False abnormal  diff=0 reason='exit 0 with a clean summary contradicts its own '
no summary at all (killed)                 -> verified=False abnormal  diff=None reason="no rclone completion summary ('N differences fou"
exit 3, no summary (directory not found)   -> verified=False abnormal  diff=None reason="no rclone completion summary ('N differences fou"
exit 2, clean summary (transport error)    -> verified=False normal    diff=0 reason='rclone exited 2 with a clean summary — verificat'
negative exit sentinel                     -> verified=False abnormal  diff=None reason='unusable exit code -1 (timeout/spawn-failure sen'
non-integer exit code                      -> verified=False abnormal  diff=None reason="unusable exit code '0' (timeout/spawn-failure se"
bool exit code                             -> verified=False abnormal  diff=None reason='unusable exit code True (timeout/spawn-failure s'
exit 1   -> Integrity mismatch — source and GCS file counts or sizes differ: detail text
exit 2   -> Rclone check failed with exit code 2 — check rclone logs for details: detail tex
exit 3   -> Rclone check failed with exit code 3 — check rclone logs for details: detail tex
exit 4   -> Rclone check failed with exit code 4 — check rclone logs for details: detail tex
exit 5   -> Rclone check failed with exit code 5 — check rclone logs for details: detail tex
exit 6   -> Rclone check failed with exit code 6 — check rclone logs for details: detail tex
exit 7   -> Rclone check failed with exit code 7 — check rclone logs for details: detail tex
exit 8   -> Rclone check failed with exit code 8 — check rclone logs for details: detail tex
exit 9   -> Rclone check failed with exit code 9 — check rclone logs for details: detail tex
exit 10  -> Rclone check failed with exit code 10 — check rclone logs for details: detail te
exit 11  -> Rclone check failed with exit code 11 — check rclone logs for details: detail te
exit 123 -> Rclone check failed with exit code 123 — check rclone logs for details: detail t
```

### `W13.verify_failure_paths` — verify_cloud_integrity() fails closed (verified=False, no exception) for a missing source, a missing bucket/prefix, an empty prefix and a missing rclone binary

- **Verdict:** OK - every verify failure path degrades to verified=False without raising
- **When:** 2026-10-13 14:22:11

```
missing source directory       -> verified=False exit=3 term=normal reason='rclone exited 3 with a clean summary — verification failed ('
missing bucket prefix          -> verified=False exit=1 term=normal reason='rclone reported 1 difference(s) (exit 1)'
empty fy prefix                -> verified=False exit=1 term=normal reason='rclone reported 1 difference(s) (exit 1)'
rclone binary missing          -> verified=False exit=-1
```

### `W13.audit_lan_branches` — audit_lan() - the LAN half of the weekly integrity engine, never exercised by any earlier wave (W10 mapped integrity.py at 0/145): clean, missing, extra, same-size corruption, canary filter and an unreadable source file

- **Verdict:** OK - the LAN audit detects every divergence, filters the canary and never VERIFIED on an unreadable source
- **When:** 2026-10-13 14:22:11

```
clean                      -> VERIFIED               mismatches=  0 cap=HASH_MD5_RCLONE_LOCAL
missing-from-dest          -> VERIFICATION_FAILED    mismatches=  1 cap=HASH_MD5_RCLONE_LOCAL
extra-in-dest              -> VERIFICATION_FAILED    mismatches=  1 cap=HASH_MD5_RCLONE_LOCAL
same-size corruption       -> VERIFICATION_FAILED    mismatches=  1 cap=HASH_MD5_RCLONE_LOCAL
canary present             -> VERIFIED               mismatches=  0 cap=HASH_MD5_RCLONE_LOCAL
unreadable source file     -> VERIFICATION_FAILED    mismatches=  1 cap=HASH_MD5_RCLONE_LOCAL
```

### `W13.integrity_helpers` — _parse_combined_file() classifies every rclone --combined status marker and fails closed on a missing file, _norm_rel()/_is_canary() normalise and filter as documented, and new_audit_id() is unique and traceable

- **Verdict:** OK - the integrity helpers behave as documented
- **When:** 2026-10-13 14:22:12

```
parse: added=['Clients/new.txt', 'dot/leading.txt'] removed=['Clients/extra.txt'] modified=['Clients/changed.txt'] unchanged=['Clients/same.txt'] errors=['Clients/unreadable.txt'] present=True
_norm_rel('a\\b\\c.txt') -> 'a/b/c.txt'
_norm_rel('/leading.txt') -> 'leading.txt'
_norm_rel('//deep//x.txt') -> 'deep//x.txt'
_norm_rel('./rel.txt') -> 'rel.txt'
_norm_rel('') -> '.'
_norm_rel('   ') -> '   '
_norm_rel('.') -> '.'
```

### `W13.health_branches` — check_source_drive() (empty, dirs-only = C-1's fail-open, missing, permission denied, low space), check_gcs_key() (missing/empty/valid) and pre_backup_health() (invalid mode, missing binaries, happy path)

- **Verdict:** OK - every health branch returns the documented reason; the dirs-only source still passes (C-1 remains observable)
- **When:** 2026-10-13 14:22:12

```
empty source                -> ok=False reason='Source drive appears empty: C:\\Users\\malliath\\AppData\\Local\\'
dirs-only source (C-1)      -> ok=True reason=''
missing path                -> ok=False reason='Source drive not accessible: C:\\Users\\malliath\\AppData\\Local'
min_free_gb=100000          -> ok=False reason='Source drive critically low on space: 109.0 GB free (minimum'
PermissionError on iterdir  -> ok=False reason='Source drive permission denied: C:\\Users\\malliath\\AppData\\Lo'
check_gcs_key(missing.json) -> ok=False reason='GCS key file not found: C:\\Users\\malliat'
check_gcs_key(empty.json  ) -> ok=False reason='GCS key file is empty: C:\\Users\\malliath'
check_gcs_key(good.json   ) -> ok=True reason=''
invalid mode               -> HealthError: "Invalid mode 'bogus-mode' - expected one of: all, cloud, lan"
rclone missing, mode=cloud -> HealthError: 'rclone not found in PATH'
robocopy missing, mode=lan -> HealthError: 'robocopy not found in PATH'
mode=all with binaries present -> no exception (rclone=True robocopy=True)
```

### `W13.fy_path_helpers` — _fy_name()/_parent_path()/_child_path() handle UNC paths, forward slashes, trailing separators, the 4-digit FY trap and lowercase input

- **Verdict:** OK - the rollover path helpers are total and separator-preserving
- **When:** 2026-10-13 14:22:12

```
_fy_name('C:\\SOURCE\\FY26-27'       ) -> 'FY26-27' (want 'FY26-27')
_fy_name('\\\\NAS\\share\\FY26-27'   ) -> 'FY26-27' (want 'FY26-27')
_fy_name('C:/SOURCE/FY26-27'         ) -> 'FY26-27' (want 'FY26-27')
_fy_name('C:\\SOURCE\\FY26-27\\'     ) -> 'FY26-27' (want 'FY26-27')
_fy_name('C:\\SOURCE\\FY26-27\\Clients') -> None (want None)
_fy_name('C:\\SOURCE\\FY2627'        ) -> None (want None)
_fy_name('C:\\SOURCE\\FY26-27x'      ) -> None (want None)
_fy_name('C:\\SOURCE'                ) -> None (want None)
_fy_name(''                          ) -> None (want None)
_fy_name('lowercase'                 ) -> None (want None)
_fy_name('C:\\data\\fy26-27'         ) -> 'FY26-27' (want 'FY26-27')
_parent_path('C:\\SOURCE\\FY26-27'     ) -> 'C:\\SOURCE' (want 'C:\\SOURCE')
_parent_path('\\\\NAS\\share\\FY26-27' ) -> '\\\\NAS\\share' (want '\\\\NAS\\share')
_parent_path('C:\\SOURCE\\FY26-27\\'   ) -> 'C:\\SOURCE' (want 'C:\\SOURCE')
_parent_path('/mnt/data/FY26-27'       ) -> '/mnt/data' (want '/mnt/data')
_parent_path('C:\\'                    ) -> 'C:' (want 'C:')
round-trip('C:\\SOURCE\\FY26-27'   ) -> 'C:\\SOURCE\\FY27-28' (want 'C:\\SOURCE\\FY27-28')
round-trip('\\\\NAS\\share\\FY26-27') -> '\\\\NAS\\share\\FY27-28' (want '\\\\NAS\\share\\FY27-28')
round-trip('/mnt/data/FY26-27'     ) -> '/mnt/data/FY27-28' (want '/mnt/data/FY27-28')
round-trip(C:\\                    ) -> 'C:/FY27-28' (drive-root edge case, unreachable via detect_rollover)
_child_path('C:\\SOURCE'        , FY27-28) -> 'C:\\SOURCE\\FY27-28'
_child_path('\\\\NAS\\share'    , FY27-28) -> '\\\\NAS\\share\\FY27-28'
_child_path('/mnt/data'         , FY27-28) -> '/mnt/data/FY27-28'
_child_path('C:\\SOURCE\\'      , FY27-28) -> 'C:\\SOURCE\\FY27-28'
```

### `W13.create_fy_folders` — create_new_fy_folders() creates the source folder (mandatory) and the LAN folder with its canary, and stays non-blocking when the LAN root cannot be written

- **Verdict:** OK - the source folder is created either way and a dead NAS never blocks the rollover
- **When:** 2026-10-13 14:22:12

```
created: {'source': 'C:\\Users\\malliath\\AppData\\Local\\Temp\\opencode\\aam_lab\\case__vfv3qm0\\source\\FY27-28', 'lan': 'C:\\Users\\malliath\\AppData\\Local\\Temp\\opencode\\aam_lab\\case__vfv3qm0\\nas\\FY27-28'}
LAN offline: created={'source': 'C:\\Users\\malliath\\AppData\\Local\\Temp\\opencode\\aam_lab\\case__vfv3qm0\\source\\FY28-29'} (no exception raised)
```

### `W13.update_config_yaml` — update_config_yaml() preserves operator comments, rewrites both paths through the ruamel round-trip, re-raises on a failed atomic replace and leaves neither a modified config nor a temp file behind

- **Verdict:** OK - the atomic rewrite is comment-preserving and fails clean
- **When:** 2026-10-13 14:22:12

```
after rollover:
# AAM backup configuration - operator comments must survive a rollover
firm_name: LAB
paths:
  source_drive: 'C:\BackupAgent\source\FY27-28'
  lan_destination: '\\NAS\share\FY27-28'
  runtime_dir: 'C:\BackupAgent\runtime'

replace failure -> raised=OSError | config unchanged=True | temp files left=[]
```

### `W13.archive_transition` — run_archive_transition() is non-blocking: returns True on success, False on a failed auth/transition, False on a timeout and False when gcloud is not installed - and never raises

- **Verdict:** OK - the archive transition can never block or break a rollover
- **When:** 2026-10-13 14:22:22

```
exit 0                               -> True (want True)
exit 1 (auth or transition failed)   -> False (want False)
hangs -> timeout                     -> False (want False)
gcloud not installed                 -> False (want False)
```

### `W13.manifest_run_history` — insert_run() upserts on run_id (no duplicate rows), last_run() and last_successful_run() are mode-scoped and success-filtered, and wal_checkpoint() preserves data

- **Verdict:** OK - run history writes and reads are consistent and mode-scoped
- **When:** 2026-10-13 14:22:23

```
rows after re-inserting run_id r1: ['r1', 'r2', 'r3', 'r4']
last_run()=r4 last_run('lan')=r4 last_run('cloud')=r2
last_successful_run('lan')=r4 (LAN_COMPLETE)
```

### `W13.record_run_history_failure` — record_run_history() returns False and logs at ERROR when the DB write fails - the log line is the only trace of a silently dropped run-history row (M-6)

- **Verdict:** OK - a dropped row is at least loud in the log
- **When:** 2026-10-13 14:22:23

```
return value                -> False (want False)
ERROR-level log lines       -> ['Failed to record run history: simulated write failure']
DEBUG-level log lines       -> 0
NOTE: a DB write failure inside record_run_history() is exactly the M-6 shape - the row disappears and only a log line records it.
```

### `W13.legacy_schema_migration` — opening a pre-versioning manifest.db runs the version-gated migration once: the extended_metrics column is added, user_version is stamped to 1, and inserts work afterwards

- **Verdict:** OK - the legacy migration is complete and idempotent
- **When:** 2026-10-13 14:22:23

```
columns after migration: ['bytes_copied', 'duration_seconds', 'ended_at', 'error_message', 'exit_code', 'extended_metrics', 'files_copied', 'files_failed', 'id', 'mode', 'run_id', 'started_at', 'status']
user_version=1
post-migration insert: ['LAN_COMPLETE']
```

### `W13.cloud_reporter` — get_cloud_manifest()/get_cloud_diff()/get_cloud_size() against the local backend: an empty prefix reports count 0 without dividing by zero, a clean pair reports no diff, a deleted object is reported as added, and a failing lsjson raises instead of returning []

- **Verdict:** OK - the reporter's success and failure paths both behave
- **When:** 2026-10-13 14:22:24

```
get_cloud_size(empty)      -> {'count': 0, 'bytes': 0, 'sizeless': 0}
get_cloud_manifest(empty)  -> []
get_cloud_manifest(2 files)-> 2 objects ['Clients/a.txt', 'Clients/b.txt']
get_cloud_size(2 files)    -> count=2 bytes=30
get_cloud_diff(clean)      -> {'added': [], 'removed': [], 'modified': [], 'unchanged': ['Clients/a.txt', 'Clients/b.txt']}
get_cloud_diff(1 missing)  -> added=['Clients/b.txt']
get_cloud_manifest(bad bucket) -> raised CloudReporterError
get_cloud_size(odd prefix)  -> returned a dict
```

### `W13.robocopy_log_cleanup` — cleanup_orphaned_robocopy_logs() reaps only logs older than the age gate (including nested ones), never touches a fresh log or a non-matching file, and is a no-op on a second pass

- **Verdict:** OK - the G8 reaper is age-gated and safe
- **When:** 2026-10-13 14:22:24

```
first pass  -> removed=1 fresh=True stale=False other=True nested=True
second pass -> removed=0 (want 0)
empty dir   -> removed=0 (want 0, no exception)
```

### `W5c.canary_alert_delegation` — a pre-flight (missing canary) failure inside _run_lan_pipeline() raises LAN_FAILED but sends NO alert of its own - alerting is delegated to backup()'s generic path, so only the production entry point pages the operator

- **Verdict:** OBSERVATION - the destination-side fault fails closed, but the specific reason reaches the operator only through backup()'s generic alert; a direct pipeline call (manual run/script) is silent
- **When:** 2026-10-13 14:32:59

```
raised=HealthError | run_history=['LAN_FAILED'] | in-pipeline alerts=0 | destination files=0
```

### `W5c.smb_probe_backstop` — what actually stands behind the WoL probe when it is wrong: on this rig the share is real, so the pipeline succeeds; the canary + robocopy access errors remain the documented backstop (L-5)

- **Verdict:** DESIGN NOTE (L-5) - the probe is a bare TCP connect; the canary and robocopy's own access errors are what actually protect the destination
- **When:** 2026-10-13 14:33:00

```
_smb_port_open(445)=True | _smb_port_open(decoy 54067)=True | ensure_server_online()=True | pipeline raised=None | files at destination=51
```

### `W14.h3_tristate` — _prefect_has_active_run() implements the H3 tri-state: matching RUNNING/PENDING/due-now SCHEDULED runs count, integrity audits are excluded, an unrecognised state on a MATCHING run fails closed, and an unreachable API returns None so callers can choose fail-open (display) vs fail-closed (trigger)

- **Verdict:** OK - the duplicate-run guard's filter and tri-state both behave
- **When:** 2026-10-13 14:22:24

```
running, no tags at all                    -> False
integrity tag only (must be ignored)       -> False
matching tag -> running                    -> True
parameters mode=lan                        -> True
mode=all counts for either                 -> True
SCHEDULED due now                          -> True
SCHEDULED far future (explicit ts)         -> False
unrecognised state on a match              -> True
```

### `W14.integrity_summary` — H-1 labelling: a COMPLETE backup with NO integrity_audits row renders NOT_VERIFIED, a VERIFIED row renders VERIFIED with its timestamp, a failing row keeps its detail, and each leg is judged independently

- **Verdict:** OK - backup result and integrity status are reported as separate facts, and absence of an audit never reads as verified
- **When:** 2026-10-13 14:22:24

```
no audit row            -> {'status': 'NOT_VERIFIED', 'checked_at': None, 'detail': None}
VERIFIED audit row      -> {'status': 'VERIFIED', 'checked_at': '2026-10-11T03:05:00+05:30', 'detail': 'capability=HASH_GCS_ADVERTISED'}
FAILED audit row        -> {'status': 'VERIFICATION_FAILED', 'checked_at': '2026-10-12T03:05:00+05:30', 'detail': 'content mismatch detected (content-changed=1)'}
LAN leg, no audit       -> {'status': 'NOT_VERIFIED', 'checked_at': None, 'detail': None}
_get_last_success(cloud) -> '2026-10-10T18:05:00+05:30'
_last_run_summary(lan)   -> None
_last_run_summary(lan, failed, long error) -> status=LAN_FAILED error_len=2003 duration='13s' files=3 failed=1
```

### `W14.http_endpoints` — the dashboard's HTTP surface against a lab config + DB: / renders, /status reports recent runs, manifest counts, per-leg integrity and a degraded (not crashed) tri-state when Prefect is unreachable, /health answers, and /report/* serve attachments

- **Verdict:** OK - the dashboard renders and reports without a live Prefect or SMTP
- **When:** 2026-10-13 14:22:24

```
GET /            -> 200 len=4050
GET /status      -> 200
  body keys      -> ['cloud', 'firm', 'fy_prefix', 'health', 'lan', 'manifest', 'recent_runs', 'schedule']
  cloud          -> {'running': False, 'run_state': 'unknown', 'enabled': True, 'last_success': None, 'last_run_formatted': '2026-10-10 18:00:00', 'integrity': {'status': 'NOT_VERIFIED', 'checked_at': None, 'detail': None}}
  lan            -> {'running': False, 'run_state': 'unknown', 'enabled': True, 'last_success': None, 'last_run_formatted': '2026-10-10 01:00:00', 'integrity': {'status': 'NOT_VERIFIED', 'checked_at': None, 'detail': None}}
GET /health      -> 200 {"status":"healthy","source_drive":"C:\\Users\\malliath\\AppData\\Local\\Temp\\opencode\\aam_lab\\case_bicajcdt\\source\
GET /report/weekly  -> 200 cd=attachment; filename="LAB_Weekly_Report_2026-10-13.html"
GET /report/monthly -> 200 cd=attachment; filename="LAB_Monthly_Report_2026-10-13.html"
```

### `W14.report_no_data` — the report endpoints degrade honestly when there is nothing to report: a missing manifest DB answers 503 and an empty one answers 404, so an empty 200 can never be mistaken for a valid report

- **Verdict:** OK - no-data paths are distinguishable from a real report
- **When:** 2026-10-13 14:22:24

```
missing DB, /report/weekly -> 503
empty DB, /report/weekly  -> 404 '<p>No runs found in the last 7 days.</p>'
```

### `W14.auth_rate_limit` — dashboard auth and rate limiting: session create/validate/expire, the API-key header (correct/wrong/missing/disabled), _require_auth's 303 vs 401 split, and the per-IP trigger rate window including expiry

- **Verdict:** OK - auth and rate limiting are enforced per branch
- **When:** 2026-10-13 14:22:24

```
sessions: create/validate/unknown/expired all behave (store size now 0)
api key: correct accepted, wrong/missing rejected, ignored when auth off
_require_auth(browser, no session) -> 303 Location=/login
_require_auth(api, no session)     -> 401
_require_auth(valid session) -> passes
_check_rate_limit(max 5) -> 5 of 8 allowed
rate limit: window enforced, per-IP, expired window resets
```

### `W14.csv_injection` — _csv_safe() neutralises every documented CSV-formula prefix and leaves benign/numeric/None values untouched, and _generate_csv_data() escapes free-text columns while keeping numeric columns numeric

- **Verdict:** OK - CSV injection is neutralised and the header is stable
- **When:** 2026-10-13 14:22:24

```
_csv_safe("=cmd|'/c calc'!A1"         ) -> "'=cmd|'/c calc'!A1"
_csv_safe('+1+1'                      ) -> "'+1+1"
_csv_safe('-2+3'                      ) -> "'-2+3"
_csv_safe('@SUM(1+1)'                 ) -> "'@SUM(1+1)"
_csv_safe('\t=1+1'                    ) -> "'\t=1+1"
_csv_safe('\r=1+1'                    ) -> "'\r=1+1"
_csv_safe('=HYPERLINK("http://x")'    ) -> '\'=HYPERLINK("http://x")'
_csv_safe("+CMD('calc')"              ) -> "'+CMD('calc')"
_csv_safe(None) -> '' (empty, not 'None')
CSV:
Started At,Ended At,Mode,Status,Files Copied,Files Failed,Bytes Copied,Duration (sec),Exit Code,Error Message,Extended Metrics
2026-10-10T18:00:00+05:30,2026-10-10T18:05:00+05:30,cloud,CLOUD_COMPLETE,1200,0,999999,300,0,,"{""a"": 1}"
2026-10-11T01:00:00+05:30,2026-10-11T01:02:00+05:30,lan,LAN_FAILED,3,1,10,120,16,'=rm -rf / (attempted injection),"{""status"": ""LAN_FAILED""}"

```

### `W14.report_html` — generate_report_html() returns falsy for an empty DB, renders statuses, file counts, exit codes and failure reasons for a populated one, carries the integrity-audit verdict, and produces a distinct email variant

- **Verdict:** OK - the report renders every run state it was given
- **When:** 2026-10-13 14:22:24

```
empty DB          -> ''
populated DB      -> 4337 chars of HTML
is_email=True     -> 4467 chars (email wrapper present)
```

### `W14.report_email` — send_weekly_report()/send_monthly_report() against a refusing local SMTP socket: both return False without raising, and an empty DB returns False without attempting a connection

- **Verdict:** OK - a report outage is contained (the flow still COMPLETES)
- **When:** 2026-10-13 14:22:24

```
send_weekly_report  -> returned False (no exception)
send_monthly_report  -> returned False (no exception)
no runs           -> returned False
```

### `W16.lan_flow_random_tree` — a Hypothesis-random source tree mirrored by flow.backup(mode='lan') is byte-identical at the destination

- **Verdict:** OK — lossless + honest (I1, I3, I4 hold)
- **When:** 2026-10-13 14:22:50

```
files=6 bytes=55 | flow raised=None | run_history status=LAN_COMPLETE | src files=6 dest files=6 | missing=[] | different=[]
```

### `W16.lan_flow_convergence` — a second flow.backup over the same tree is a no-op (idempotent mirror)

- **Verdict:** OK — second run changed nothing (I2 holds)
- **When:** 2026-10-13 14:22:51

```
dest files run1=6 run2=6 | converged=True
```

### `W16.cloud_flow_random_tree` — a Hypothesis-random source tree synced by flow.backup(mode='cloud') is byte-identical in the bucket

- **Verdict:** OK — lossless + honest (I1, I3, I4 hold)
- **When:** 2026-10-13 14:23:43

```
files=1 bytes=3 | flow raised=None | run_history status=CLOUD_COMPLETE | src=1 bucket=1 | missing=[] | different=[]
```

### `W16.cloud_flow_convergence` — a second cloud flow.backup over the same tree is a no-op

- **Verdict:** OK — second run changed nothing (I2 holds)
- **When:** 2026-10-13 14:23:45

```
bucket files run1=1 run2=1 | converged=True
```

### `W16.flow_mutation_repair` — a random destination mutation is repaired by the next flow.backup() run

- **Verdict:** OK — mutation repaired by the next run
- **When:** 2026-10-13 14:24:23

```
mutation=delete (destination file deleted) | flow raised=None | src=5 dest=5 | repaired=True | still-different=[]
```

### `W16.flow_fault_injection` — under fault injection a COMPLETE status never hides missing or altered data

- **Verdict:** OK — honest: failure status, or COMPLETE with a byte-identical destination (I4 holds)
- **When:** 2026-10-13 14:24:41

```
fault=deep path () | status=LAN_COMPLETE raised=None | src=7 dest=7 | missing=[] | different=[]
```

### `W16.hashing_md5` — compute_md5() matches rclone hashsum md5 output

- **Verdict:** OK — compute_md5 matches rclone; verify_checksum is exact and refuses PENDING_CHECKSUM
- **When:** 2026-10-13 14:24:41

```
file=test.bin size=262144 | compute_md5=988678538f1475f6e36eb0159320ab02 | rclone hashsum md5=988678538f1475f6e36eb0159320ab02 | verify_checksum(correct)=True | verify_checksum(wrong)=False | verify_checksum(PENDING)=False
```

### `W16.logging_rotation` — configure() creates the log directory and file

- **Verdict:** OK — log file created
- **When:** 2026-10-13 14:24:41

```
log_dir=C:\Users\malliath\AppData\Local\Temp\tmpnztvp4k8\logs | log_files=['backup_2026-10-13.log']
```

### `W16.backup_repository` — record_sync_results normalises both entry shapes and prunes stale; record_run_history never raises

- **Verdict:** OK — both entry shapes recorded, prune works, run history recorded
- **When:** 2026-10-13 14:24:42

```
cloud entries=2 (after prune=1) | lan entries=1 | record_run_history returned=True
```

### `W16.watchdog_h2` — watchdog._alert_wedged_lock() imports from core.report (H-2 fixed in v1.1.3)

- **Verdict:** OK — H-2 fixed: import from core.report succeeds, no ModuleNotFoundError
- **When:** 2026-10-13 14:24:42

```
find_spec('core.notifications')=None | _alert_wedged_lock returned=None | error lines: []
```

### `W16.collect_config_data` — get_network_info returns only active non-loopback IPv4 interfaces; verify_with_pydantic validates against AppConfig

- **Verdict:** OK — network discovery and pydantic validation both behave
- **When:** 2026-10-13 14:24:42

```
networks=[('Ethernet', '10.10.151.53', '6C:4B:90:25:70:5F')] | valid_snippet=True | invalid_snippet=False
```

### `W16.schedule_collisions` — cronsim: no two deployments fire in the same minute across a year

- **Verdict:** ISSUE — collisions: [('weekly-report', 'monthly-report', [datetime.datetime(2027, 2, 1, 8, 0), datetime.datetime(2027, 3, 1, 8, 0), datetime.datetime(2027, 11, 1, 8, 0)])]
- **When:** 2026-10-13 14:24:46

```
collisions=[('weekly-report', 'monthly-report', [datetime.datetime(2027, 2, 1, 8, 0), datetime.datetime(2027, 3, 1, 8, 0), datetime.datetime(2027, 11, 1, 8, 0)])]
```

### `W16.flow_all_mode` — flow.backup(mode='all') records exactly one run_history row per leg

- **Verdict:** OK — one cloud row and one LAN row
- **When:** 2026-10-13 14:24:48

```
run_history=['lan:LAN_COMPLETE', 'cloud:CLOUD_COMPLETE']
```

### `W16.config_validation_guards` — models/config.py cross_field_validation rejects FY mismatch, 4-digit FY, non-UNC LAN destination, both-legs-disabled, and cloud-without-key

- **Verdict:** OK — all 5 guards fire, valid config passes
- **When:** 2026-10-13 14:24:48

```
problems=[]
```

### `W16.watchdog_main_loop` — watchdog.main() deferral logic, circuit breaker, and service actions with scripted health/state sequences

- **Verdict:** OK — sc stop on hung service, sc start on stopped service, deferral on lock/transfer
- **When:** 2026-10-13 14:24:48

```
actions=['sc stop AamPrefectServer', 'sc start AamPrefectServer'] | service_start_log={'AamPrefectServer': [1791881688.605789]}
```

### `W16.watchdog_circuit_breaker` — _start_allowed() allows MAX_STARTS_PER_WINDOW starts then suppresses

- **Verdict:** OK — breaker allows 3 then suppresses, per-service isolation
- **When:** 2026-10-13 14:24:48

```
allowed=[True, True, True] | suppressed=True | other_service=True
```

### `W16.ui_trigger_verify` — POST /trigger/verify/{mode}: 503 on unreachable Prefect, 400 on running backup, 400 on invalid mode, 409 on disabled leg

- **Verdict:** OK — all 4 branches behave
- **When:** 2026-10-13 14:24:48

```
problems=[]
```

### `W16.ui_email_report_triggers` — POST /trigger/report/{weekly,monthly}/email: 404 on no runs, 500 on SMTP failure

- **Verdict:** OK — 404 on empty DB, 500 on SMTP failure
- **When:** 2026-10-13 14:25:25

```
problems=[]
```

### `W16.ui_config_reload` — ui._cfg() TTL refresh picks up config changes and evicts the cached DB when database_path changes

- **Verdict:** OK — TTL refresh + DB eviction work
- **When:** 2026-10-13 14:25:26

```
problems=[]
```

### `W16.logging_bridge` — configure_prefect_bridge() is idempotent and never raises

- **Verdict:** OK — idempotent
- **When:** 2026-10-13 14:24:42

```
called twice without error
```

### `W16.shutdown_contract` — shutdown_server() returns the documented dict shape, never raises, and builds the exact command `shutdown /s /m \\<ip> /t 300 /f`

- **Verdict:** OK — documented dict + exact command; both branches safe
- **When:** 2026-10-13 14:24:41

```
failed-run result={'shutdown_initiated': False, 'server_ip': '127.0.0.1', 'error': 'Access is denied.'} cmd=['shutdown', '/s', '/m', '\\\\127.0.0.1', '/t', '300', '/f'] | missing-binary result={'shutdown_initiated': False, 'server_ip': '127.0.0.1', 'error': 'shutdown.exe not found'}
```

### `W17.smb_port_clamp` — invalid ports are refused before any socket work

- **Verdict:** OK - never raises, never connects
- **When:** 2026-10-13 14:25:26

```
rejected: '445', None, 70000, -1, 1.5, 65536 | sockets opened=0
```

### `W17.magic_packet_contract` — magic packets go to global + subnet broadcast, repeat rounds apart

- **Verdict:** OK - 3 rounds x 2 targets, 2 intervals
- **When:** 2026-10-13 14:25:26

```
packets=6 targets=['192.168.1.255', '255.255.255.255'] sleeps=[5, 5]
```

### `W17.backup_lock_states` — G6: absent / live / stale / corrupt lock are all handled without raising

- **Verdict:** OK - the main loop can never be crashed by a bad lock file
- **When:** 2026-10-13 14:25:27

```
absent->False | live->True | stale->False | corrupt(OverflowError)->False
```

### `W17.main_transfer_deferral` — an in-flight rclone/robocopy defers the restart (8h cap logic)

- **Verdict:** OK - restart deferred while a real transfer runs
- **When:** 2026-10-13 14:25:27

```
transfer probes=2 stops=[] starts=[]
```

### `W17.main_transfer_cap_live_lock` — at the transfer cap with a live lock owner: CRITICAL + alert, no unlink, no restart

- **Verdict:** OK - fail-closed under a live backup
- **When:** 2026-10-13 14:25:27

```
alert_pids=[77] stops=[] starts=[] lock_exists=True
```

### `W17.main_transfer_cap_dead_lock` — at the transfer cap with a DEAD lock owner: the stale lock is unlinked and the Prefect service is started

- **Verdict:** OK - zombie path recovers
- **When:** 2026-10-13 14:25:27

```
started=['AamPrefectServer', 'AamPrefectServer'] lock_exists=False
```

### `W17.main_hung_restart` — service RUNNING but API dead -> sc stop (SCM/NSSM failure actions revive it)

- **Verdict:** OK - the genuine hung state triggers the restart
- **When:** 2026-10-13 14:25:27

```
stopped=['AamPrefectServer', 'AamPrefectServer']
```

### `W17.wedged_lock_alert` — the deferral-cap alert is delivered and its failure is contained

- **Verdict:** OK - alert delivered, outage contained
- **When:** 2026-10-13 14:25:27

```
delivered message pid=99 status=LIVE_LOCK_CAP_EXCEEDED | smtp outage -> no exception
```

### `W18.launch_main_happy` — launch.main() boots in order: API wait -> dashboard -> rollover -> limits -> orphan cleanup -> reconcile -> serve, and shuts down clean

- **Verdict:** OK - the production startup sequence is exercised end to end
- **When:** 2026-10-13 14:25:27

```
serve_deps=('dep-aam-backup',) serve_kw={'pause_on_shutdown': False}
```

### `W18.network_discovery` — loopback, APIPA (169.254.x) and down interfaces are excluded; MACs are normalised to colon-uppercase

- **Verdict:** OK - only routable interfaces are offered as config snippets
- **When:** 2026-10-13 14:25:27

```
networks=[('Ethernet', '192.168.1.50', 'AA:BB:CC:DD:EE:FF')]
```

### `W18.cloud_dry_run_branches` — run_cloud_dry_run() fails closed on every probe failure and passes only when source + bucket are both reachable

- **Verdict:** OK - all 8 branches behave as documented
- **When:** 2026-10-13 14:25:27

```
source-missing | empty (warns, probes) | unreadable | timeout | no-rclone | OS error | exit!=0 | exit=0
```

### `W18.lan_dry_run_branches` — run_lan_dry_run() gates on SMB reachability + canary first, then maps every robocopy outcome to an honest dict

- **Verdict:** OK - all 8 branches behave as documented
- **When:** 2026-10-13 14:25:27

```
smb-unreachable->HealthError | canary missing->HealthError | canary unreadable->HealthError | exit<8 ok | exit>=8 fail | timeout | no-robocopy | OS error
```

---

## V. What the lab PROVED (my audit hypotheses confirmed)

| # | Hypothesis before the lab | Empirical result | Severity |
|---|---|---|---|
| **D1** | An empty / dirs-only source wipes the mirror and reports success | **CONFIRMED on BOTH legs.** robocopy `/MIR` → 12 destination files → **0**, exit 2 → `LAN_COMPLETE`; rclone `sync` → all objects deleted, exit 9 → **`CLOUD_NO_CHANGES_COMPLETE`** (also counted as a *success* by `last_successful_run()` and the reports). The only reason this wasn't already happening: robocopy dies earlier on `/ZB` (see below). `check_source_drive()` returns **OK** because it only checks that the source root has ≥1 entry. | **CRITICAL** |
| **ZB** | *(not in the original audit)* `/ZB` needs `SeBackupPrivilege` | **CONFIRMED as a deployment blocker.** With `/ZB`, a completely ordinary, fully-readable copy fails with `ERROR : You do not have the Backup and Restore Files user rights.` and **exit 16** before a single byte is copied. `whoami /priv` on this account shows no `SeBackupPrivilege` at all. Same command **without** `/ZB`: exit 1 (success). This also breaks 5 tests in the repo's own suite and is the reason the LAN leg looked "protected" in audit pass 1. | **CRITICAL — day 1** |
| **B** | The daily `--size-only` gate cannot see same-size content changes | **CONFIRMED.** A cloud copy with the *same size and same mtime* but different bytes: daily gate → `verified=True`; weekly hash audit → `VERIFICATION_FAILED`, `content-changed=1 [Clients/ledger.txt]`. The daily gate is an *accounting* check, not an integrity check — and the dashboard labels it "Verification Passed". | **HIGH** |
| **BUG-1** | `watchdog._alert_wedged_lock` is dead code | **CONFIRMED.** `find_spec('core.notifications') = None`; the call logs `No module named 'core.notifications'` at DEBUG and returns `None`. The wedged-lock alert is *never* delivered. | **HIGH** |
| **BUG-2** | A failed run is recorded with a success status | **CONFIRMED (narrower than I thought).** Cloud variant is already handled (`flow.py:661` rewrites `CLOUD_VERIFY_FAILED`). LAN variant confirmed: a **post-sync** exception (e.g. the DB write) leaves `run_history.status = LAN_COMPLETE` while the run raises, and `last_successful_run()` returns it as a success. The existing suite never sees this because it patches `flow._record_run`. | **MEDIUM** |
| **ALERT-2** | *(found by WAVE 6)* one failed backup = one alert email | **REFUTED — a verify-gate failure PAGES TWICE.** `_run_cloud_pipeline()` sends the specific gate alert (`Cloud integrity verification FAILED after sync…`) and *then* raises a plain `RuntimeError`, so `backup()`'s "Backup completed with errors" generic alert fires as well (only `PartialRun` suppresses it). Two emails per event. | **MEDIUM** (alert fatigue) |
| **R5** | Manifest/snapshot memory blows up at 1M+ files | **CONFIRMED and quantified.** 200k objects: +108 MB RSS, 2.2 s, 24 MB pickled per task result. At 1M objects the cloud leg holds **~1.05 GB of live Python objects** and writes **~102 MB into the Prefect result store per night** (2.5M → ~2.6 GB). Times are fine (lsjson 1.9 s @200k → ~10 s @1M, far inside the 900 s timeout). | **MEDIUM** (boxes with <8 GB) |
| **F-08/LAN** | A killed robocopy with exit code 0 is caught | **CONFIRMED.** `TerminateProcess(robocopy, 0)` at 1207/4000 files → `LAN_SUSPECT`, `log_complete=False`, forensic log retained. The LAN fail-closed contract holds. | none |
| **F-08/CLOUD** | A killed rclone with exit code 0 is caught by the *gate* | **CONFIRMED live.** `run_cloud_sync` itself returns `CLOUD_COMPLETE` (it cannot tell) — but the full pipeline: `CLOUD_VERIFY_FAILED`, no false success, **1 alert sent**. The design is sound, but it depends *entirely* on the gate. | none |
| **D2** | An unreadable source subtree is purged from the destination | **REFUTED (both legs protected).** rclone: `failed to open directory "B": Access is denied` + `not deleting files as there were IO errors` → exit 1, objects survived. robocopy: exit 9 → `LAN_PARTIAL`, subtree survived 5/5. | — |
| **CORRUPT** | Corrupt/lock-protected lock files crash the reader | **REFUTED.** 13 hostile payloads (negative PID, `1e9:1e9`, `999999999999999999999`, `1:1:2`, empty) → no exceptions; PID-reuse correctly detected; `acquire_lock` refuses a live foreign owner and collects a stale one. Concurrent DB *inserts* serialise 50/50. | — |
| **ORCH** | *(found by WAVE 8)* the orchestration layer behaves like production | **CONFIRMED end-to-end.** `serve.deployments()` registers all 6 deployments on a real Prefect server with the config's cron+timezone; `arun_deployment("aam-backup/backup-lan")` (the dashboard's exact call) creates a run that the duplicate-click guard then *refuses to duplicate* (400 `already_running`); with the Prefect API unreachable the same endpoint fails **closed** (503) while `/status` degrades to `run_state=unknown`; `launch._cancel_orphaned_runs()` reaps a crashed agent's orphaned PENDING/RUNNING runs but **spares a RUNNING one while its backup lock is live**; two simultaneous backup runs serialise on the real `aam-backup` global limit (queue wait measured, mirror windows never overlap); `weekly-report`/`monthly-report` complete as real flow runs and **contain** an SMTP outage. | — |
| **W3** | The committed suite is green as the guide claims | **REFUTED.** On a clean, unprivileged box: **34 failed / 1632 passed / 199 skipped / 40 errors**, of which **19 failures are code-level and machine-independent** (`test_f2_trigger_remediation.py` references `ui._trigger_result`, `ui._trigger_timeout`, `ui.TRIGGER_WAIT_SECONDS` — none exist in `ui.py`). The rest are environmental: a missing `\\127.0.0.1\lan_backup` share, real-GCS tests, chaos tooling, and `/ZB`. Running their suite also **appends to `docs/SCENARIO_TEST_REPORT.md` and creates a `MagicMock\…` tree in the repo root**. | **MEDIUM** |
| **MINOR** | — | `_clean_path()` is not idempotent (`"0 /"` → `"0 "` → `"0"`). Concurrent `ManifestDB` **initialisation** can raise `sqlite3.OperationalError: database is locked` even with `busy_timeout_ms=8000`, because the legacy dedup DELETE runs *before* `PRAGMA busy_timeout` is set (`core/manifest.py:150-162`). A cloud preflight killed with exit 0 passes preflight. | LOW |

---

## VI. What all of this changes in my earlier ranking

1. **`/ZB` is the single most important new finding.** It is a *guaranteed* LAN failure for the deployment pattern the guide prescribes (service Log On = domain user with share write access) — and it accidentally masks D1 on the LAN leg. Verify `SeBackupPrivilege` (or run as LocalSystem, or drop `/ZB`) **before** the first production run.
2. **D1 must be fixed even though the LAN failure looks "safe"**: the cloud leg already deletes everything and reports `CLOUD_NO_CHANGES_COMPLETE`.
3. **R1 was both right and wrong**: preflight does cover destination-side faults and does email; it cannot see a run that never happens (and the LAN leg can't even start without `/ZB` fixed).
4. **R5 is real but bounded**: ~1 GB at 1M objects. Fine on a 24 GB dev box; a genuine constraint on a 2-core/4-8 GB server.

---

## VII. Reproducing

```powershell
# everything is offline; nothing real is touched
.\.venv\Scripts\python.exe -m pytest audit_lab -q -s --timeout=900
.\.venv\Scripts\python.exe audit_lab\make_ledger.py     # regenerate this file

# only the Prefect orchestration wave (deployments, triggers, concurrency,
# crash recovery, report flows) — runs everywhere, no services needed
.\.venv\Scripts\python.exe -m pytest audit_lab/test_w8_prefect.py -q -s --timeout=1200
```

* Harness rules: all writes under `%TEMP%\opencode\aam_lab`, `audit_lab/labconf.py::assert_lab_path` enforces it,
  `build_config()` never reads `config.yaml`, WoL/shutdown are force-disabled *and* monkeypatched to raise,
  and the fake GCS is an rclone **local-backend** remote named `aam_gcs` (the name the production code hardcodes),
  so `run_cloud_sync` / `verify_cloud_integrity` / `get_cloud_manifest` / `get_cloud_diff` / `audit_cloud` all
  execute for real with zero network and zero cost.
* WAVE 8 additionally starts a **real ephemeral Prefect API server** (`prefect_test_harness()`), creates the
  production `aam-backup` global concurrency limit with `launch._ensure_concurrency_limit()`, registers the
  production deployments with `serve.deployments()` + `Deployment.aapply()`, and drives them with
  `arun_deployment()` — the same call `ui.py`'s "Run Backup" endpoint makes.
* WAVE 7 is the only opt-in wave: it needs the deployed box (`audit_lab\bootstrap_deployed.ps1`, run as
  Administrator). With `AamBackupAgent`/`AamWatchdog` stopped it fails at `_require_services()` /
  `_trigger_and_wait()` (no worker → the flow run stays SCHEDULED) — that is an environment state, not a
  code finding.
