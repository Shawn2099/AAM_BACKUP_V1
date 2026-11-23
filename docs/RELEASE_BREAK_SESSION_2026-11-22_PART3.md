# Release break-it session - PART 3 (newest; read Part 1 and Part 2 first)

Written 2026-11-23 ~00:10 IST after the usage-limit reset.

## State now
- **Everything is merged**: branch `fy-overlap` (main checkout) HEAD `64ce20e` = all fixes (release-candidate `ec0b589` merged). The untracked test files were removed from the main checkout first and now come from git.
- **Audit lab (real robocopy/rclone): 249 passed, 3 skipped, 0 failed.** The first run had 2 failures (`test_w23_cancel_mid_sync[cloud|lan]`) caused ONLY by pytest being missing from the venv while the readiness script had stripped dev packages (the lab child process does `import pytest`); rerun alone with pytest restored: 2 passed. `test_w25_fy_overlap.py` (15-month scenario) passes.
- **Full unit suite after the merge**: run started in the background, output `agent_runs/unit_full_after_merge.txt` (read its last lines; before the merge the fixer worktree saw 3921 passed with only worktree-config.yaml noise + the m2 rig issue, now fixed). Re-run if in doubt: `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600`.
- **Test services restarted** on the merged code (AamPrefectServer / AamBackupAgent / AamWatchdog, code dir `C:\AAMTest\code` = branch release-candidate). Prefect health true; dashboard /health = 503 with status "attention" because the LAN backup is BLOCKED by G4 awaiting approval (correct, intended); the ACTION NEEDED alert mail arrived in `C:\AAMTest\mails.jsonl` at 22:29 -> alert path works end to end.

## Fixes added since Part 2 (all in fy-overlap, each with tests; xfails flipped to hard tests)
- D6 `switch_pending` uses `rollover_journal.is_finished`.
- D1 `flow._rederive_after_slot`: active set re-read after the backup slot is acquired (backup + integrity audit); a year no longer active is skipped, its pre-wait RUNNING row deleted (`ManifestDB.delete_running_row`).
- D3 deferred early-year activation: `fy_overlap.deferred_state`, red/amber attention item, repeat every `alerts.stale_repeat_hours`, immediate re-mail on a changed reason, one RESOLVED mail on activation (mode "rollover", key `recovered:overlap:activation:<fy>`; its incident close is per mode and could also close other rollover-mode incidents of that FY: low risk, noted).
- D2 `ManifestDB.rollover_journals()` + `_close_finished_old_fys` at the top of every `rollover_check_flow` run (idempotent close of finished years' approvals/incidents).
- `core/services.py` `_parse_queryex`: `sc queryex` parsed by numeric state code (works with localized labels); tests with German/French labels in `tests/test_m4_watchdog.py`.
- D9 (approve for an inactive FY -> 409) is fix #13 from Part 1; its fallout (7 failures in `tests/test_m2_approval_dashboard.py`) was only the test rig lacking `_configured_fy` (fake SimpleNamespace config) - fixed in the rig, product code unchanged and safe (`_active_fy_list` falls back to the configured FY).
- Two existing assertions were changed by D3 because they encoded the old single-mail behaviour (`test_ov2_activation`, T-a6 now expects 3 mails incl. RESOLVED).
- Deploy fixes in release-candidate: `deploy/_common.ps1 -split '\\'` (PS 5.1 regex bug), `deploy/09_restore_from_gcs.bat` without wmic.

## Still xfail (documented, owner decisions - not fixed on purpose)
D7 (final-backup SLOT_TIMEOUT -> approvable G6 block), D8 (`close_fy` leaves audit/overlap incidents open), D10 (report window of an FY that left the active set without a switch), D12 (raising `early_start_month` after activation drops the early FY), D14 (second folder's run row dated at its pipeline start across midnight), D5 (vanished early NAS folder silently re-created, xfail P-3). D4 (early upload starving the configured FY) is documented by a characterization test. D13: GCS lifecycle `age 365 -> ARCHIVE`, apply `deploy/gcs_lifecycle.fy_overlap_proposed.json` after owner review. Recommendations are in Part 2 section B.

## TODO (remaining)
1. Check `agent_runs/unit_full_after_merge.txt`; fix anything red (none expected).
2. **Unblock the LAN backup on the test stack** (owner approves on http://127.0.0.1:8080, type APPROVE; or set `guards.forbid_local_destination: false` in `C:\AAMTest\config\config.yaml` and restart AamBackupAgent), run both legs, verify bucket `aam-backup-qa-bilcvx` (as the service account: the key file is locked from user malliath), NAS files under `C:\AAMTest\nas\FY26-27`, mails.
3. Live fault runbook (docs/TEST_DESIGN_RELEASE_2026-11-22.md section C): LS2 kill agent mid-run, LS3 NAS share removed mid-run, LS8/8b manifest.db renamed / old schema (is a mail sent? SILENCE-3), LS9 watchdog killed, LS10 power loss, LS11 SMTP sink down, LS12 secret scan, LS13 bad config at boot (expect the SILENCE-2 mail), LS6 overlap Jan-Apr via a patched run date (never the system clock) + a real browser check of the dashboard (Origin middleware).
4. Opus final verification against the release gate (TEST_DESIGN section D3); then the owner report (decisions above; NOT verified: real runs on Windows Server 2016 / 2019).
5. Cleanup when finished: uninstall test services (`deploy\08_uninstall_services.bat` via the elevated queue), remove user `aamtest` + share `AAMTestNAS`, delete bucket `aam-backup-qa-bilcvx`, **restore AAMMerger** (Part 1 section 6), `git worktree remove C:\AAMTest\code` and the agent worktrees under `.claude\worktrees`, stop the SMTP sink, update `SESSION_HANDOFF.md`.
