# F-08 Verification-Liveness Remediation Report

- Date: 2026-09-06 (dev session, OpenCode + Muse Spark)
- Scope: `C:\Users\Administrator\Desktop\development` (branch `main`, HEAD `9e82540`)
- Production: **NO PRODUCTION DEPLOYMENT PERFORMED** — `C:\AAMBackup`, `C:\BackupAgent`,
  `C:\ChaosTest` were never written; chaos evidence only read.

## 1. F-08 root cause

`core/cloud_verify.py` computed `verified = (returncode == 0)`. P8-VERIFY-ONLY killed the
live `rclone check` (PID 8924) via `TerminateProcess(pid, 0)`; the OS reported exit 0
with a truncated log. Because the destination happened to be clean, the independent
diff/size listings in `flow.py` were also legitimately clean, so the C-DK-001 gate passed,
and `verify_liveness = verified and exit_code == 0` was forged too. Net: a check that never
completed was recorded `CLOUD_NO_CHANGES_COMPLETE / verified=true / verify_liveness=true`.

Deeper cause: no **process-completion contract** existed for the cloud verifier (unlike LAN,
where `decide_lan_result` requires the robocopy job summary). The same class of defect also
existed in the weekly audit: `_run_rclone_check` trusted the pre-created `--combined` diff
file's presence, but `mkstemp` creates that file *before* rclone runs, so an exit-0 kill
leaves an empty diff file that parses as "0 mismatches → VERIFIED".

## 2. Exact code changes

- `core/cloud_verify.py` — new `decide_cloud_verify_result(exit_code, stderr, stdout)`:
  VERIFIED requires exit 0 **AND** rclone's own completion summary (`N differences found`,
  N == 0, no `Failed to check` verdict, no anchored `ERROR`/`CRITICAL` log lines).
  No summary (killed/crashed/truncated) → `verified=False, termination=abnormal` regardless
  of exit code. Contradictory evidence fails closed. `verify_cloud_integrity` returns the
  decision as additive keys (`termination`, `completion`, `differences`, `reason`);
  the rclone invocation is **unchanged** (no new flags, no extra scan).
- `flow.py` — evidence gate additionally requires
  `verify_termination == "normal"` (fail-closed default `"abnormal"` for legacy payloads);
  `verify_liveness` in `extended_metrics` is now derived from `termination`, never the exit
  code; `cloud_verify_and_report_task` passes the new fields through.
- `flow.py` (integrity-audit flow) — `record_audit` failures now append to `excs`
  (alert + FAILED flow) instead of propagating silently; a VERIFIED verdict is durable only
  once persisted.
- `core/integrity.py` — `_run_rclone_check` requires the `N differences found` summary for
  any non-error outcome, cross-checks summary count vs parsed diff
  (measured rclone v1.74.2: summary N == added+modified+removed), fails closed on
  missing summary or contradiction; all results carry `termination`.
- `core/fy_rollover.py` — final LAN backup uses `run_lan_sync`'s decided `status` instead
  of re-deriving success from the killable exit code via `classify_exit_code` (T04/A1 path).

## 3. Verification completion contract

`verifier started → remained alive (own summary emitted) → summary clean AND consistent →
exit interpreted → independent destination evidence → VERIFIED`. The summary anchor is
`(\d+)\s+differences found` in the process log stream (stderr, stdout fallback), the ERROR
anchor is timestamp-anchored log-level lines (filenames can't false-positive), and rclone
logs are English-only. Documented in both module docstrings with the measured version.

## 4. C-DK-001 preservation

The diff/size/manifest-error gate clauses are untouched; the termination clause is additive.
Divergent-destination + killed-verifier still fails via diff evidence (independent of the
new clause). Covered by existing `test_verified_true_with_missing_objects_is_verify_failed`
(now with explicit `verify_termination: normal` so it exercises the C-DK-001 path, not the
new default) plus new F-08 gate tests.

## 5. LAN regression analysis

`core/lan_sync.py` untouched: `decide_lan_result` (summary + `Ended:` requirement,
contradiction → SUSPECT) already implements the completion contract; `run_lan_sync` enforces
it; pipeline raises on SUSPECT/PARTIAL. `classify_exit_code` remains a pure bitmask helper
(tests + `fy_rollover` fallback only). `test_lan_result_contract.py` passes unchanged.

## 6. Weekly audit review

Read-only (`check` only, no sync/copy/delete flags, `--checkers 1`) — unchanged. Now
kill-proof (summary + consistency requirements), size-only still can never yield VERIFIED,
audit rows still never rewrite `run_history`, failed persistence now fails the audit loudly.
Resource profile unchanged: no new flags, no second scan, no concurrency change.

## 7. Integrity state semantics

Backup result (`COMPLETE/PARTIAL/SUSPECT/FAILED`) vs integrity (`VERIFIED/...` in
`integrity_audits`, `NOT_VERIFIED` = no row) separation preserved in manifest, UI
(`_integrity_summary`), and flows. No daily full reconciliation or hashing added.

## 8. Resource impact

None: identical subprocess invocations, O(1) regex over already-captured stderr, no new
dependencies, `--checkers 1` weekly model kept.

## 9. Tests added/modified

- New `tests/test_f08_verify_liveness.py` (21 tests): `decide_cloud_verify_result` matrix
  (clean/diverged/killed-clean F-08/killed-diverged/timeout/None-exit/crash/no-summary/
  both contradiction directions/stdout fallback), mocked `verify_cloud_integrity` F-08 cases,
  **live** `TerminateProcess(handle, 0)` kill of a real `rclone check` asserting the OS
  really returned exit 0 and the verdict is not-VERIFIED/abnormal, completed-run positive
  control, weekly-audit no-summary / summary-diff-contradiction / timeout cases.
- Updated fixtures to the contract: `test_cloud_verify.py`, `test_cloud_verify_comprehensive.py`
  (realistic v1.74.2 stderr, superset key assertions), `test_cloud_evidence_gate.py` (new
  termination fields; ex-`RT-3` "lucky pass completes" test now asserts `CLOUD_VERIFY_FAILED`;
  + forged-boolean, missing-keys fail-closed, liveness-true tests), `test_flow_orchestration.py`,
  `test_flow_status_semantics.py` (termination fields in mocks).

## 10. Test results

- Targeted: F-08 + verify + evidence-gate + flow-status + orchestration + LAN-contract:
  **136 passed**. Audit/reporter/manifest: **86 passed**.
- Full suite: **1479 passed, 202 skipped, 3 failed, 40 errors** —
  - 2 failed: `test_h4_watchdog_counters` timing tests — **pre-existing** (reproduced on clean
    HEAD via `git stash`).
  - 1 failed: `test_lan_sync.py::TestRunLanSyncReal::test_real_timeout_kills_run` — network-timing
    flake (`elapsed 26.8s < 2s` vs unreachable host), passes in isolation, in code untouched
    by this change.
  - 40 errors: environment-only — missing `\\127.0.0.1\lan_backup` SMB share, undefined
    `gcs_sandbox` fixture / no live-GCS creds (all at fixture setup, none in changed paths).

## 11. Ruff / format / type checks

- `ruff check` on all 10 changed/new files: **19 errors, identical to HEAD baseline — zero new**.
- `ruff format`: new test file clean; `core/*.py` kept in existing file style (whole-file
  reformat deliberately reverted to avoid churn; baseline is not format-enforced: 8/9 files
  drift at HEAD).
- mypy/pyright: **not installed in this environment** — no type check could be run.

## 12. Known limitations / remaining risks

- The summary match assumes rclone's English log wording (`differences found`, `Failed to
  check`); a future rclone that rewords these lines would fail closed (safe direction) but
  needs fixture updates — live-rclone tests (`test_integrity_audit.py`, F-08 positive
  control) will catch it.
- A supervisor that both kills the verifier AND fabricates its stderr could still forge a
  pass — outside the threat model (same as LAN log trust).
- Daily cloud verify remains `--size-only` (operational, not deep verification) by design;
  same-size corruption is the weekly hash audit's job (verified MD5-capable on local backend).
- GitNexus MCP `impact`/`detect_changes` tools are not available in this session; blast-radius
  analysis was done via exhaustive grep of all readers/writers (flow, report, ui, manifest,
  fy_rollover, all tests) instead.

## 13. Files changed

`core/cloud_verify.py`, `core/integrity.py`, `core/fy_rollover.py`, `flow.py`,
`tests/test_cloud_verify.py`, `tests/test_cloud_verify_comprehensive.py`,
`tests/test_cloud_evidence_gate.py`, `tests/test_flow_orchestration.py`,
`tests/test_flow_status_semantics.py`, new `tests/test_f08_verify_liveness.py`,
new `F08_REMEDIATION_REPORT.md` (this file).

## 14. Deployment status

**NO PRODUCTION DEPLOYMENT PERFORMED. No commit created.** Changes are uncommitted in the
working tree for review. `git status`: 9 modified + 1 new test file + this report
(plus pre-existing untracked `2`, `qdescription`, `query`, untouched).
