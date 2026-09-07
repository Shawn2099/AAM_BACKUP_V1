# Chaos Harness Batch 1 Report

## 1. Existing capabilities discovered

Test framework: pytest (100+ files in `tests/`, `conftest.py` real-infra
fixtures, scenario catalog `test_scen_branch_*`, RT suites).
Chaos tools (`C:\ChaosTest\tools`): `killrob.py` (taskkill/wmi/nt/ctrl),
`killrcl.py` (taskkill/wmi/nt/tp0, sync+check verbs), `singlekill.py`,
`orphankill.py`, `holdlock.py`/`lockguard*.py` (share-none locks),
`netchaos.py`, `cfgfault.py`, `hashpair.py`, `inv.py` (canary-aware
inventory), `sizes.py`, `snap.py`, `dbpeek*.py`, `pfx.py` (real Prefect
client: deps/fruns/run/conc/trigger), `dblkill.py`/`dblkill_postrem.py`
(full-pipeline reference orchestrators), prod no-touch checkers
(`prodstate*`, `prodtruth.py`, `closure_prod_*`). Campaign procedures:
T3 `killrcl_T3.py` + verify-kill method, T4 `inject_t4.py` + audit
method, T1/T2 lock-fault methods. Remediation regression:
`test_post_adversarial_remediation.py`, `test_audit_remediation.py`,
`test_f08_verify_liveness.py`, `test_cloud_evidence_gate.py`.

## 2. Existing tests/tools reused

All fault mechanisms (kills, locks, config faults), all observers
(`pfx.py`, `dbpeek*`, `inv.py`, `hashpair.py`, snapshots), all triggers
(Prefect deployments), and the full pytest framework are reused
unchanged — see `HARNESS_REUSE_MATRIX.md` for the per-capability map.
No existing test was rewritten.

## 3. New code created

`chaos_harness/` (6 modules, ~600 lines): `safety.py`, `evidence.py`,
`faults.py`, `scenarios.py`, `assertions.py`, `runner.py`.
`tests/test_chaos_harness_batch1.py` (25 dry-run tests).

## 4. Why every new file was necessary

- `safety.py`: no importable chaos-only guard existed (prod* scripts
  are point checks, not enforcement). Required before any fault/trigger.
- `evidence.py`: tools log to hardcoded paths; no reusable per-run
  packager existed.
- `faults.py`: `inject_t4.py`/`plant_fault*.py` hardcode one file + one
  evidence dir; minimal parameterized adaptation (logic unchanged).
  Kills/locks delegate to existing tools via subprocess.
- `scenarios.py`: campaign determinations are prose, not executable;
  metadata only, zero business logic.
- `assertions.py`: centralizes observed-state == contract-literal
  checks (never recomputes verdicts).
- `runner.py`: `dblkill*.py` are single-scenario scripts, not a
  reusable runner; sequencing only, shells to existing tools.
- `tests/test_chaos_harness_batch1.py`: dry-run proof obligation.

## 5. Exact real application entrypoints exercised (live)

- H1: `flow.backup(mode='lan')` → `_run_lan_pipeline` →
  `run_lan_sync` → `decide_lan_result` → `_record_run`
- H2: `flow.backup(mode='cloud')` → `_run_cloud_pipeline` →
  `run_cloud_sync` → `verify_cloud_integrity` →
  `decide_cloud_verify_result` → evidence gate → (`cloud_record_task`
  must NOT run)
- H3: daily `verify_cloud_integrity` (--size-only) + weekly
  `audit_cloud` (hash-capable)
- H4: `integrity_audit_flow(mode='lan')` → `audit_lan` →
  `_run_rclone_check` → `record_audit`
- H5: `flow.backup` + `ManifestDB` record paths + error/alert paths
- Harness-only per scenario: arming, capture, assertions (see matrix).

## 6. H1–H5 scenario definitions

In `chaos_harness/scenarios.py` (H1 robocopy kill, H2 verify kill,
H3 same-size cloud, H4 audit divergence with T4-A/B/C structure,
H5 persistence fault with mechanism-review precondition).

## 7. Contract assertions

`assertions.py`: `assert_status` (app-recorded status == literal),
`assert_unchanged` (counts before/after), `assert_contains/
assert_not_contains` (message attribution, e.g. "content mismatch"
present / "file read or check errors" absent), `summarize`.
Readers (`latest_run`, `latest_audit`, `count_table`) open the chaos
DB read-only; `_ro_conn` refuses non-chaos paths.

## 8. Safety controls

`assert_chaos_path` (allow `C:\ChaosTest`, `C:\ChaosRuntime`,
`\\127.0.0.1\aam_test`; deny `C:\AAMBackup`, `C:\BackupAgent`,
`E:`, `F:`, prod NAS), `assert_chaos_bucket` (exactly
`aam-chaos-67q1zs`), `assert_chaos_prefect` (exactly chaos endpoint),
`assert_no_shutdown_target` (loopback only). Enforced in every fault,
evidence, trigger, and cleanup path. `runner.run` additionally
requires `live=True` + explicit immutable `app_commit` SHA.

## 9. Evidence structure

`C:\ChaosTest\evidence\CHAOS_HARNESS\<SID>\<run>\`:
`targets.json`, `scenario.json`, `cleanup_proof.json`, captured
command outputs / DB exports / logs, `manifest.json` with verdict.
No fabrication — only copies of real outputs.

## 10. Dry-run results

- ruff: clean on all new files.
- `tests/test_chaos_harness_batch1.py`: 25 passed (safety accept/
  reject, registry, entrypoint linkage to real code, runner gates,
  corrupt+restore on chaos scratch with source-untouched proof,
  evidence lifecycle + prod-refusal, assertion logic, chaos-DB-only
  readers).
- `runner.dry_run` H1–H5 on the real evidence root: 5× DRYRUN_OK,
  manifests written under `CHAOS_HARNESS\<SID>\dryrun\`.
- `resolve_targets('H4')`: chaos config, chaos DB, chaos lock,
  `C:\ChaosTest\source`, `\\127.0.0.1\aam_test\CHAOS01`,
  `aam-chaos-67q1zs`, chaos Prefect — all proven, no production.
- Scratch space removed after tests (`harness_scratch` absent).
- No destructive execution performed (no triggers, no live faults).

## 11. Tests run against the harness

`tests/test_chaos_harness_batch1.py` (25 passed) + ruff + 5 live-path
dry-runs + target-resolution proof. Existing product suites were not
re-run here (harness adds no product code; product diff unchanged —
see §12).

## 12. Intentionally NOT recreated

Backup execution, robocopy/rclone invocation, verification, auditing,
classification, persistence, Prefect/lock/alert/retry/shutdown/config
logic. Product worktree diff is byte-identical to the pre-harness
state (4 files, +203/−33 — the uncommitted remediation, untouched).

## 13. Remaining gaps

- H5 DB-fault mechanism needs blast-radius review before live use
  (scenario carries an explicit precondition to that effect).
- `inv.py`/`snap.py`/`killrob.py` hardcode a stale
  `C:\lan_dest_test\CHAOS01` default — harness always passes explicit
  paths; upstream fix is chaos-tool maintenance, out of scope.
- Live revalidation still requires the remediation commit before H2/H4
  can run pinned (`runner.run` will refuse otherwise — by design).

## 14. Recommended Batch 2

Lock-contention (concurrent flow vs audit), watchdog-deferral
interaction, alert-delivery proof (SMTP sink), Prefect-concurrency
slot exhaustion, GCS rate-limit/transport errors, clock-skew
(modify-window) probes — each mapped to existing tools first, using
this Batch-1 skeleton (scenario + assertions + dry-run) with no new
systems unless the matrix shows a true gap.

Acceptance: the harness invokes real app code and existing tools for
every behavior, adds only orchestration/safety/evidence/assertions,
and proves it with dry runs — no duplicated business logic, no
rewritten tests, no destructive execution in this task.
