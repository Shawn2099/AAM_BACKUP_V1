# Brief WS-A: Phase 1 review amendments (closes the Phase 1 gate G1)

Paste this whole file to a fresh agent. Master plan: `docs/PLAN_MASTER_PARALLEL_2026-11-25.md`.

## Context
Nightly backup automation for an accounting firm (Windows Server 2016): `rclone sync` to GCS (cloud leg) and, on branch
`rclone-engine`, `rclone sync` to a NAS over SMB (LAN leg; robocopy was removed in P1 chunks C0-C12, all committed, HEAD 65a16a0).
Prefect 3 orchestrates. An Opus review of P1 produced amendments **M2, S2, S1, M3, S4, S5, S7, S3 (nightly hash check), L12**.
Their exact meaning, files, test rows and chunk IDs are in `docs/TESTMATRIX_P1_REVIEW_2026-11-25.md` and the amended
`docs/PLAN_P1_RCLONE_LAN_2026-11-25.md` (both owned by the WS-A Opus agent; nobody else edits them). Do not re-plan; follow them.

## Rules (owner, binding)
- Reliability first, no guesses. Fail closed: record `*_BLOCKED`/FAILED, email "open the dashboard", approve on the dashboard; never auto-proceed.
- Every tunable is a config key (default + bounds in the pydantic model, `core/config_help.py` entry) added in the same commit as the code reading it.
- Reuse tools/locked libs; new dependencies need owner approval. Shared building blocks: `core/statuses.py`, `core/tool_runner.run_tool`, `core/run_artefacts.py`, `ManifestDB.from_config`, `core/notify.py`.
- Test first: tests written by a different agent than the code, shown red on old code; the code agent never edits those tests.
- Never touch the real NAS or the trial box (`innoviztawinserver2016`) without the owner's go. Never power off this PC (loopback NAS shutdown would). No reboot/shutdown.
- GitNexus `impact` before editing a symbol; `detect_changes --scope all` before committing.
- Commit per chunk citing the chunk ID; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Never bare `git stash`.
- Escalation: Haiku 2 failed attempts → Sonnet; Sonnet blocked or needing an interface/schema change → Opus. Scope guard: compare `git diff --stat` with the scoped files/lines.

## Files owned (others must not edit while WS-A runs)
Chunks (from the matrix): R1 M2 arrival tolerance, R2 S2 errors beyond differences, R3 S1 failed object paths, R4 M3
backup-dir counts, R5 S4 integrity audit filters, R6 S5 hang after SOFT cutoff, R7 S7 reserve description, R8a-R8d S3
hash check of tonight's transfers (step `rclone-lan-hash`, keys `lan.arrival_hash_transferred/_max_gb/_timeout_seconds`),
R10 lab rows L12-L14; R9/R11 as the plan's "Review amendments" section defines them. Files: `core/lan_sync.py`,
`core/cloud_sync.py` (parsers), `core/integrity.py` (R5), `flow.py` (R6, R8 wiring), `models/config.py`, `core/config_help.py`,
`config.example.yaml`, `test_support/fake_tools.py` (R8b step `rclone-lan-hash`), `test_support/rclone_logs/*` (new fixtures),
every flow test using `fake_rclone_lan` (R9 sweep), `audit_lab/test_w25_rclone_lan.py`, new `tests/test_p1_r*.py`.
WS-A holds the first token on every hotspot (master plan §3). If the matrix names `core/notify.py`, say so to the integrator: WS-B B1 then waits.

## How to run tests (worktree has no .venv)
```
cd C:\Users\malliath\Desktop\aam_rclone          (or your ws worktree)
$env:PYTHONPATH='.'; $env:PREFECT_HOME="$env:TEMP\aam_ph_wsa"; Remove-Item -Recurse -Force $env:PREFECT_HOME -ErrorAction SilentlyContinue
$py='C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe'
& $py -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600 -m "not slow" --basetemp="$env:TEMP\aam_bt_wsa"
& $py -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=1200 -m slow --basetemp="$env:TEMP\aam_bt_wsa_slow"
$env:AAM_LAB_ROOT="$env:TEMP\aam_lab_wsa\lab"; & $py -m pytest audit_lab/test_w25_rclone_lan.py audit_lab/test_w4_pipeline.py audit_lab/test_fy_overlap_lab.py audit_lab/test_w6b_rollover_nas_off.py -q -s --timeout=1800 -p no:cacheprovider -o addopts=""
C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\ruff.exe check <changed files>
```
Known environment reds (not code): `tests/test_v113_round3.py::test_r3_repo_config_timeouts`/`::test_r3_loaded_runtime_timeouts` (need repo config.yaml), `tests/test_review2_driver.py` (refuses a dirty tree).

## Definition of done = gate G1
P1 plan §6 gate 1-6 plus every review-matrix row green; R5 mutation checks plus the ones the review matrix lists each turn a test red; `agent_runs/` logs; findings appended to `docs/PROGRESS_WS-A_2026-11-25.md`.

## Report back (<= 20 lines)
Commits (sha + chunk ID), rows covered, suite counts, mutation results, anything deferred, whether `core/notify.py` was touched, which hotspot files changed (so the integrator hands the token on).
