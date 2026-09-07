# Chaos Harness — Reuse Matrix (Batch 1)

Rule: for every capability, «does this already exist?» YES → reuse. Only
missing capabilities get new code, and new code is orchestration / safety /
evidence / assertions only — never backup business logic.

## Capability inventory

| Capability | Existing implementation | Existing path | Reuse method | New code required? | Reason |
|---|---|---|---|---|---|
| Robocopy kill (taskkill/wmi/nt/ctrl) | killrob.py | `C:\ChaosTest\tools\killrob.py` | subprocess, reuse directly | No | Proven T04A mechanism |
| Rclone kill (taskkill/wmi/nt/tp0, sync+check verbs) | killrcl.py | `C:\ChaosTest\tools\killrcl.py` | subprocess, reuse directly | No | Proven T3B mechanism (`--check` mode) |
| One-shot kills | singlekill.py / orphankill.py | `C:\ChaosTest\tools\` | reuse directly | No | Already general |
| Share-none file locks | holdlock.py / lockguard.py / lockguard2.py / lockprobes.py | `C:\ChaosTest\tools\` | subprocess, reuse directly | No | T1/T2 fault basis |
| Network chaos | netchaos.py / clumsy | `C:\ChaosTest\tools\` | reuse directly | No | Out of Batch-1 scope, retained |
| Config faults | cfgfault.py | `C:\ChaosTest\tools\cfgfault.py` | reuse directly | No | W1 mechanism retained |
| Same-size dest corruption | inject_t4.py | `...\T4_AUDIT_INTEGRITY\inject_t4.py` | ADAPT (parameterize) | Yes — `chaos_harness/faults.py:same_size_corrupt` | Existing script hardcodes one file + one evidence dir; logic copied once, parameterized, semantics unchanged |
| Same-size cloud plant | plant_fault*.py | `...\T3_CLOUD_NO_RECORD\plant_fault*.py` | ADAPT (parameterize) | Yes — `chaos_harness/faults.py:same_size_plant` | Same reason: hardcoded dataset paths |
| Fault restore | ORIGINAL.bin preserve + restore blocks in above scripts | same | ADAPT | Yes — `chaos_harness/faults.py:restore` | Same reason |
| Real backup invocation | Prefect deployments + pfx.py trigger | `C:\ChaosTest\tools\pfx.py` | subprocess, reuse directly | No | Exercises real `flow.backup` on chaos runtime |
| Real audit invocation | integrity_audit_flow via deployment | `pfx.py trigger integrity-audit` + `flow.py:integrity_audit_flow` | reuse directly | No | Real `core.integrity.audit_lan` path |
| Prefect observation | pfx.py deps/fruns/run/conc | `C:\ChaosTest\tools\pfx.py` | subprocess, reuse directly | No | Real client, version-robust |
| Flow-run triggering | pfx.py trigger | same | reuse directly | No | — |
| Src/dst inventory + sha | inv.py / hashpair.py / sizes.py / snap.py | `C:\ChaosTest\tools\` | reuse directly (explicit paths) | No | NOTE: inv.py/snap.py/killrob.py hardcode stale `C:\lan_dest_test\CHAOS01`; harness always passes explicit chaos UNC paths and never relies on those defaults |
| Run-history/file-entries/audit rows | dbpeek*.py / db_inspect.py / db_tables.py | `C:\ChaosTest\tools\` + `...\POST_REMEDIATION\tools\` | reuse directly | No | Read-only sqlite observers |
| Baseline/snapshot | chaos_snapshot.py / collect_baseline.py / psnap.py | tools + POST_REMEDIATION\tools | reuse directly | No | — |
| Production no-touch proof | prodstate*.py / prodtruth.py / prodmanifest.py / closure_prod_*.py | tools + POST_REMEDIATION\tools | reuse pattern | No | Harness safety asserts chaos-only targets before acting |
| Full-pipeline orchestration reference | dblkill.py / dblkill_postrem.py | `C:\ChaosTest\tools\` + POST_REMEDIATION\tools | reference only | Yes — `chaos_harness/runner.py` | dblkill is a single-scenario script, not a reusable multi-scenario runner; runner reuses its trigger/kill/collect PATTERN, not its code |
| Chaos-wide safety guard | — | — | — | Yes — `chaos_harness/safety.py` | No importable guard exists (prod* scripts are point checks, not enforcement); runner refuses non-chaos targets |
| Per-scenario evidence package | — | — | — | Yes — `chaos_harness/evidence.py` | Existing tools log to fixed paths (e.g. killrcl.log → evidence\CLOUD); no reusable per-run packager |
| H1–H5 scenario definitions | campaign determinations (prose) | POST_ADVERSARIAL_VALIDATION | encode | Yes — `chaos_harness/scenarios.py` | Prose is not executable; definitions contain metadata only, zero business logic |
| Contract assertions on observed state | scenario tests assert inline | tests/test_scen_branch_*.py | pattern reuse | Yes — `chaos_harness/assertions.py` | Centralizes "observed row/state == contract literal" checks; asserts on app output, never recomputes it |
| Unit/integration coverage | pytest suite (100+ files), conftest fixtures | tests/ | reuse framework + fixtures | Yes — `tests/test_chaos_harness_batch1.py` | Dry-run-only harness tests; no destructive execution |
| Weekly/daily app entrypoints | flow.backup, integrity_audit_flow, _run_cloud_pipeline, audit_lan/audit_cloud, decide_*, ManifestDB | core/*, flow.py, models/* | invoked (live) / imported (dry-run target resolution only) | No | Harness never reimplements these |

## Explicitly NOT recreated

Backup execution, robocopy/rclone invocation, cloud verify, integrity
auditing, result classification, manifest/run-history writes, Prefect
handling, locking, alerts, retries, shutdown policy, config loading —
all exercised via the real app (`C:\ChaosTest\app` + chaos Prefect) or
the real deployment triggers. Harness asserts on observed outcomes only.
