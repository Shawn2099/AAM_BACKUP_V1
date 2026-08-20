# SESSION 2 — Audit Progress

## Phase status
| Phase | Status |
|---|---|
| 1. Repo discovery | DONE (prior span) |
| 2. Independent file-by-file audit | DONE (this span finished: deploy scripts, restore/, docs, config model, all core/*, flow/launch/serve/ui/watchdog) |
| 3. Interaction tracing | DONE (prior span + this span: Prefect concurrency source, pathlib.exists internals, NSSM live config, live Prefect API) |
| 4. Real execution / testing | DONE — see evidence table below |
| 5. Own audit report | DONE → **`SESSION_2_INDEPENDENT_AUDIT.md`** |
| 6. Read prior audit material | DONE (SESSION_1_HANDOFF, AAM_AUDIT_REPORT_2026-08-18, docs/audit/*, archive/*, .planning/*, _regression_full*.log, REAL_WORLD_TEST_PLAN*) |
| 7. Compare | DONE |
| 8. Classify confirm/disagree/NEW | DONE (18 confirmed, 6 disagreements, ~20 new) |
| 9. Final reconciliation report | DONE → **`SESSION_2_FINAL_RECONCILIATION.md`** (verdict: PRODUCTION READY — WITH CONDITIONS) |

## This span — new evidence collected
1. **Full suite, pristine 3.12.3 env** (`.venv_audit`, uv): **1439 passed / 53 skipped / 0 failed**, reproduced twice (190 s / 194 s). Prior claim was 1415/53 — suite grew; green status reproducible. `_s2_pytest_full2.log`, `_s2_pytest_skips.log`.
2. **Skip analysis:** all 53 skips = `AAM_RUN_REAL_HARDWARE`-gated tests (rt_01/02/03/05/06/07/08, e2e_real_hardware). `_s2_skip_reasons.txt`.
3. **Real-hardware suite** (from `C:\AAMBackup`, prod venv 3.12.3, gate on, `-k "not test_fy_07"`): **47 passed / 1 failed / 1 deselected** (303 s). Failure = `test_wd_05` — my concurrent E1 rclone detected by `_transfer_process_running()`; isolated re-run PASSED (15.6 s). `_s2_rthw.log`.
4. **E1 (`--modify-window 2s`)** — CONFIRMED: same-size resave within 2 s mtime window → sync exit 9, object byte-verified STALE; persists across repeated syncs; size-change and +3 s mtime controls both re-upload. `scratch_q_e1.py`.
5. **E5 (NSSM stop orphans)** — NEGATIVE: production chain + stop methods, stop mid-transfer → entire tree dead ≤5 s; no partial GCS object. Scratch service `AamAuditScratch` removed after use. `_s2_e5\`.
6. **E8 (SMTP partial refusal)** — CONFIRMED false success: local STARTTLS server, 1/2 recipients refused (550), real `_send_email_with_attachments` returned True + "Email sent" log. `scratch_q_e8.py`.
7. **WinError 5 mystery RESOLVED:** live logs (07-10/11 21:00 runs) show raw `PermissionError [WinError 5]` from `lan-preflight` task; CPython 3.12.3 `Path.exists()` only swallows errno {2,20,9,40} / winerror {21,123,1177} — WinError 5 RAISES. Unguarded at `lan_preflight.py:36` + `cloud_preflight.py:64`.
8. **rclone version drift:** `C:\Windows\system32\rclone.exe` v1.74.2 (bare-PATH sync uses this under LocalSystem) vs `deploy\bin\rclone.exe` v1.74.3 (resolve_binary: preflight/verify).
9. **Live Prefect:** 5 active deployments (lan 21:00, cloud 22:00, weekly Mon 08:00, monthly 1st 08:00, rollover-check daily 06:00 — 2bb8024 fix live). `_s2_deployments.json`.
10. **Secrets:** production SMTP app password in plaintext in untracked `restore/PROD_config.yaml.2026-08-20` + dev `config.yaml`; NOT in git (verified `git ls-files`/`git grep`).
11. **`test_fy_07` destructiveness:** finally-block rmtrees `E:\FY26-27` + `\\nas\lan_backup\FY26-27` on this machine — excluded from real-hardware run (would destroy live source).
12. **OS confirmed:** Windows Server 2016 Standard 10.0.14393 (the documented EOL target; guide notes ES ends 2027-01-12).
13. NSSM live config read: all 3 services `uv.exe run python <app>`, stop methods console/window/threads 15000, NO AppKillProcessTree, NO AppExit config (default auto-restart — observed live with scratch service).
14. DEPLOYMENT_GUIDE.md: wrong config key names (`notification.*` vs `notifications.*`), stale schedules, stale rclone pin, incorrect "LocalSystem cannot access UNC" claim (deployment runs LocalSystem and LAN backup works).
15. archive/ARCHITECTURE.md: stale in ~8 ways (Task Scheduler vs NSSM, preflight mechanics, exit table, test counts, schedules…).

## Artifacts created (repo root / scratch)
- `SESSION_2_INDEPENDENT_AUDIT.md` — Phase-5 findings register (9 Medium, 16 Low, 5 Info/negative, positive confirmations, open items)
- `SESSION_2_AUDIT_PROGRESS.md` — this file
- `scratch_q_e1.py`, `scratch_q_e8.py` — experiments (repo root, untracked)
- `_s2_e1_src\`, `_s2_e5\` — experiment data (outside repo: `C:\Users\Administrator\Desktop\testing\_s2_e5`)
- `_s2_pytest_full2.log`, `_s2_pytest_skips.log`, `_s2_skip_reasons.txt`, `_s2_rthw.log`, `_s2_deployments.json`
- GCS test prefixes: `AUDIT_S2_E1/`, `AUDIT_S2_E5/` (isolated; cleanup pending end of audit)

## Phase 6 targets (not yet read)
- `CODE_AUDIT.md`, `AUDIT_PROGRESS.md`, `SESSION_1_HANDOFF.md`
- `docs/audit/*`, `.planning/*`, `archive/production_hardening_review_final.md`
- `_regression_full*.log`, `tests/REAL_WORLD_TEST_PLAN*.md`
- remediation reports (locate)

## Constraints held
- No product code modified (experiments = new scratch files only; one monkeypatch confined to my own E8 process).
- No writes to `C:\AAMBackup` except test-run artifacts inside isolated `E2E_TEST_*` dirs via the project's own test suites (authorized).
- No parallel subagents; single self-hosted slot.
