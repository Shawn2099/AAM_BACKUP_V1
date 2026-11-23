# Full audit lab, final run (2026-11-23)

Run: `pytest audit_lab --ignore=audit_lab/test_w7_deployed.py`, one process, clean PREFECT_HOME, branch fy-overlap (unit suite fix12b green: 4311 passed). Output: agent_runs/audit_full_final.txt.

Final summary line: **7 failed, 256 passed, 3 skipped, 2166 warnings in 4362.36s (1:12:42)**

test_w16_schedule_collisions_across_year (known red) is NOT in the failure list.

| # | Test | Status | Classification | Evidence |
|---|------|--------|----------------|----------|
| 1 | test_w26b_names::test_l2_unicode_case_normalization_churn | FAILED | Known acceptable (Unicode merge, L2) | assertion `_assert_case_only_rename_recorded`: others_dest != others_src |
| 2 | test_w26e_acl::test_w26e_L7_acl_denied_subfolder_without_backup_mode | FAILED | Known acceptable (test cleanup, L7) | PermissionError reading audit_0.txt after icacls deny; test cleanup did not remove the deny |
| 3 | test_w10_frameworks::test_w10_production_line_coverage_during_real_run | FAILED | UNSURE, leaning TEST RIG (coverage floor) | `core/manifest.py: 29% executed, floor is 30%` (assert 29.14 >= 30). Coverage tracer measurement; a 1-point miss on a real run |
| 4 | test_w17_ops::test_w17_service_state_parsing | FAILED | UNSURE, leaning TEST RIG | line 303 `_service_state("x") == "4"` returned ''. The product now reads state through `services.query` (watchdog.py:249) and the test patches `wd.services.run_capture`. Needs a check of whether the one-token fallback was dropped on purpose |
| 5 | test_w19_ui::test_w19_trigger_cloud_branches | FAILED | TEST RIG (fixture missing last_polled) | line 385: POST /trigger/cloud returned 503 service_not_running instead of 202. ui.py:898-915 now refuses a press when the Prefect deployment's last_polled is older than agent_max_silence_seconds. test_w19_ui.py has 0 references to last_polled, so the fake deployment is stale |
| 6 | test_w19_ui::test_w19_trigger_lan_branches | FAILED | TEST RIG (same cause as #5) | line 423: POST /trigger/lan returned 503 instead of 202 (same service_not_running gate) |
| 7 | test_w5c_ops::test_w5c_missing_canary_blocks_then_one_approved_run | FAILED | UNSURE (test expects old wording) | line after 4th run: `"already used" in str(exc4)`. exc4 is a GuardBlocked with the new G5 text ("Fix the cause ..."), because the canary was missing again. Either the test expects the old used-approval message, or a used approval should say "already used" |

Notes:
- Items 5 and 6 are the same cause: a trigger-gate change (F1 last_polled check) that the lab fixture does not satisfy. Both are rig findings, not a backup failure. The production gate itself is covered by unit tests (test_f1*).
- No failure was approved or resolved by hand. No guard approval was given in this run.
- Real-run errors inside the lab (robocopy exit 11, rclone size exit 6, G5 blocks, CLOUD_ABANDONED rows from the lab's kill tests) are expected parts of the scenarios; they are not counted as failures.

Test stack and host state at the end:
- AamPrefectServer, AamBackupAgent: Running. AamWatchdog restarted via queue 097 at 13:17:06 (Running). /health 200 at 13:17.
- Shutdown and WoL never enabled.
