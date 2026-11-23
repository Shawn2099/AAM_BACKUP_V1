# Baseline — branch `mvp-hardening` (M0)

**Date:** 2026-10-14 · **Base commit:** `6c7c052` (plan) on top of `ce01d7c`
**Production code changed in M0:** none. Only tests, test fixtures and `.gitignore`.

## Committed suite (`tests/`)

| | Before M0 | After M0 |
|---|---|---|
| passed | 1311 | 1335 |
| failed | 24 | **0** |
| skipped | 16 | 19 |

Command: `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600`

### What the 24 failures were and how they were resolved
| Cause | Tests | Resolution |
|---|---|---|
| Chaos tools absent (`C:\ChaosTest\tools`) | 3 (batch1/2/3) | `skipif` with explicit reason (environment-dependent) |
| Disabled-leg guard (v1.1.3) not reflected | `TestBackupModeRouting` ×2 | Tests now enable legs and patch pipelines; **new** test asserts a disabled single leg raises |
| MagicMock config without `source_drive` str | `TestCloudPipelineSkipsRecordOnManifestError` ×2 | Real `source_drive` string |
| Orphan cleanup now scoped to AAM flows (`read_flows`) | `TestCancelOrphanedRuns` ×3 | Mocks register AAM flow ids; **new** test: non-AAM runs never cancelled |
| Signature drift | `test_rclone_config` ×4 (`gcs_key_path`), `test_time_utils` (`cutoff_iso(days)`), `test_wol_comprehensive` ×3 (`stability_wait`, `config.wol.*`) | Tests updated; WoL tests no longer touch the real network |
| Message/default drift | preflight timeout text, verify default timeout ×2, `serve.deployments` | Assert actual contract; verify default asserted against the config-model default |
| Test-order pollution: loguru sink bound to a closed pytest stderr | `test_v113_remediation`, `test_watchdog_comprehensive` | Autouse fixture resets loguru sinks after every test |
| Test-order pollution: env leak `PREFECT_API_URL=localhost:4200` | `test_prefect_integration` ephemeral test | Uses `prefect_test_harness()` |

### Test bugs found and fixed while doing this
- Two tests mocked `mkstemp` to return fd **0**, so production code ran `os.close(0)` and closed the test process's stdin (`test_production_scenarios::test_same_size_corruption`, `test_bug_reproductions::test_get_cloud_diff_default_timeout`). Fixed, and an autouse guard now **fails any test that closes fd 0/1/2**.

## Audit lab (`audit_lab/`, excluding on-box `test_w7_deployed.py`)

Run 2026-10-14 (unchanged code): **233 passed / 3 failed / 3 skipped** (19 m 48 s).
The 3 failures are the known intentional reds — they must turn green during the plan:

| Test | Finding | Plan item |
|---|---|---|
| `test_w9_stress.py::test_w9_manifest_db_concurrency_stress` | M-6 run-history rows lost under concurrent opens | WP15 / 1.3 |
| `test_w1e_bugs.py::test_w1e_two_processes_concurrent_db_writes` | M-6 root cause (`busy_timeout` after DDL) | WP15 / 1.3 |
| `test_w16_flow_first.py::test_w16_schedule_collisions_across_year` | weekly/monthly collide on Monday-the-1st | WP8 / 3.4 |
