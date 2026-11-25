# Brief WS-C: Phase 2 (audit-lab sweep, cloud guard trims, one retry level, S6 stall watchdog, `--retries` measurement)

Paste this whole file to a fresh agent. Master plan: `docs/PLAN_MASTER_PARALLEL_2026-11-25.md` (§3 tokens, §6 parallel safety).

## Context
Nightly backup automation for an accounting firm on Windows Server 2016. Branch `rclone-engine`: both legs use rclone
(cloud → GCS, LAN → NAS over SMB); robocopy was removed in P1. Prefect 3 flows in `flow.py`, logic in `core/`, config in
`models/config.py`. The audit lab (`audit_lab/`) runs the REAL rclone against a sandbox (local disk → loopback admin share
`\\127.0.0.1\C$\...` under `AAM_LAB_ROOT`). Many lab files were written for robocopy and are red now (P1 plan, Deferred table).
Phase 2 items come from `docs/OPUS_AUDIT_PREFLIGHT_FLOW_2026-11-25.md` (rows 7, 10, 18, 24, 25; "trim the walks") and
`docs/OPUS_DESIGN_RCLONE_ONLY_NO_PREFECT_2026-11-25.md` (Q3), and the P1 plan's Deferred table (review items S6, S8c, S8f, F1-F3;
measured: `--timeout`/`--contact-timeout` have no effect on the local/SMB backend, so S6 is the only guard against a hung SMB read
below the hard timeout). Prefect is NOT removed in this phase.

## Rules (owner, binding)
- Reliability first, no guesses; fail closed; never auto-proceed. A trim must never remove a check that is the only proof of something (evidence gate inputs stay equivalent).
- Every tunable is a config key (default, bounds, `core/config_help.py`), added with the code reading it; removed keys go to `DEPRECATED_KEYS`.
- Reuse `core/tool_runner.run_tool` (only subprocess wrapper), `core/statuses.py`, `core/run_artefacts.py`, `core/notify.py`. No new dependencies without owner approval.
- Test first by a different agent; the code agent never edits those tests. Lab rewrites are tests-only: never change product code in a CL package; a product bug found by the lab is logged and escalated to Opus.
- Never touch the real NAS or the trial box. Never power off this PC. No reboot/shutdown.
- GitNexus `impact` before editing; `detect_changes --scope all` before committing.
- Commit per chunk with its ID; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Never bare `git stash`.
- Escalation: Haiku 2 attempts → Sonnet; Sonnet blocked / interface change → Opus. Scope guard on `git diff --stat`; no weakened asserts, no new skips/xfails.
- Model split: Opus matrix + review; Haiku tests, lab rewrites, measurement runs, docs; Sonnet code + debug.

## Read first
`CLAUDE.md` (audit lab command, gotchas); `docs/PLAN_P1_RCLONE_LAN_2026-11-25.md` (§2.1 flags, fact sheet, Deferred table);
`docs/OPUS_AUDIT_PREFLIGHT_FLOW_2026-11-25.md`; `docs/ARCHITECTURE.md` §10 (CL* gates); `agent_runs/p1_sweep.md`; code
`core/cloud_preflight.py`, `core/cloud_sync.py`, `core/cloud_verify.py`, `core/cloud_reporter.py`, `core/guards.py`,
`core/tool_runner.py`, `flow.py` (`_run_cloud_pipeline`, cloud guard/verify tasks, task `retries=`), `audit_lab/labconf.py`, `audit_lab/test_w25_rclone_lan.py` (the rclone-era lab template).

## Packages
**Lab sweep (audit_lab only; can start NOW, in parallel with everything; never edit `test_w25_rclone_lan.py` (WS-A) or `test_w26_versions_lab.py` (WS-B)):**
| ID | Files | Work |
|---|---|---|
| CL0 | `audit_lab/labconf.py` (`robocopy_local`, `strip_zb` → remove or rclone helpers), `conftest.py`, `make_ledger.py`, `gap_map.py`; triage run of every lab file alone | write `agent_runs/p2_lab_triage.md`: file, red/green, cause (robocopy symbol / behaviour / product) |
| CL1 | `test_w13_branches.py` (52 robocopy refs) | rewrite |
| CL2 | `test_w5a_paths.py`, `test_w5b_scenarios.py`, `test_w5c_ops.py` | rewrite (keep the ctypes `CreateFileW` share-mode-0 lock pattern) |
| CL3 | `test_w11_wrapper.py`, `test_w12_clock.py`, `test_w8_prefect.py`, `test_w10_frameworks.py` | rewrite |
| CL4 | `test_w1a_diag.py`, `test_w1a_purge.py`, `test_w1bd.py`, `test_w1c_controls.py`, `test_w1d2.py` | rewrite |
| CL5 | `test_w9_stress.py`, `test_w16_flow_first.py`, `test_w18_ops.py` + every triage file CL0 found red among `test_w1e_bugs`, `test_w1f_props`, `test_w2_scale`, `test_w2b_costs`, `test_w6_flow`, `test_w14_ui`, `test_w17_ops`, `test_w21_permissions`, `test_w22_hardkill` (+`w22_child.py`), `test_w23_cancel` | rewrite |
Rewrite rule per test: (a) behaviour still meaningful → rewrite for the rclone LAN steps (`rclone-lan-plan`, `-sync`, `-check`); (b) robocopy-only (exit bitmask, `/UNILOG` UTF-16 logs, `/ZB`, SeBackupPrivilege, `lan_manifest`, snapshots) → delete, log in `agent_runs/p2_lab_sweep.md` with the P1 row that now covers it (L/W/D/G) or "no replacement, Opus decides"; (c) red because of product behaviour → finding, escalate. Known intentional red: `test_w16_schedule_collisions_across_year`. `test_w7_deployed.py` stays excluded.

**Code (module first; `flow.py` wiring only with the flow token, after G2):**
| ID | Goal | Files (approx lines) | Starts |
|---|---|---|---|
| C0 | Opus: `docs/TESTMATRIX_P2_2026-11-25.md`. Must settle: which single retry level stays per step (Prefect task `retries=` vs rclone `--retries` vs flow `max_attempts`); exact cloud verify inputs before/after merge (evidence gate equivalence table); **[VERIFY] with the real `deploy\bin\rclone.exe`: does rclone keep printing JSON stats lines while blocked (e.g. destination share paused / unreachable)?** If yes, S6 must watch progress counters (checks+transfers+bytes) not log growth. | doc + `agent_runs/c0_stall_probe.md` | NOW |
| C1 | Cloud dry-run uses `--combined` (P1 parsers `combined_counts`, `planned_copy_bytes`) replacing the `rclone size` source/bucket walks; drop G2 on the cloud leg | `core/cloud_sync.py`, `core/guards.py` (~80); C1w `flow.py` (~60) | C1 module after C0 (and after WS-A for cloud_sync/guards); C1w after G2 |
| C2 | Remove cloud preflight probe A (duplicate of `health.py` source check) | `core/cloud_preflight.py` (~30) | after C0 |
| C3 | Cloud write probe removed (default off → key to `DEPRECATED_KEYS`) | `flow.py`, config files (~40) | after C1w |
| C4 | Cloud verify: one `rclone check --combined --size-only` + one size, replacing check + size + `lsjson -R` + check | `core/cloud_verify.py`, `core/cloud_reporter.py` (~100); C4w `flow.py` | module after C0; wiring after C3 |
| C5 | One retry level per step as decided in C0; config keys for any retained count | `flow.py`, config files (~40) | after C4w |
| C6 | S6 stall/progress watchdog in `run_tool`: no progress for `tools.stall_timeout_seconds` (OD2; default 1800, 0 = off) → kill tree, result `stalled=True`, clear `describe_failure`; C6w maps it in `run_lan_mirror`/`run_cloud_sync` results to FAILED retryable (per OD2), never COMPLETE | `core/tool_runner.py`, config files (~90); C6w `core/lan_sync.py`, `core/cloud_sync.py`, `flow.py` (~40) | after C5 and OD2 |
| C8 | Review F2: cloud `stopped = ... or (limited and timed_out)` pattern (flow.py ~966) fixed with the WS-A R6 rule (hang after cutoff is not "budget stopped") | `flow.py` (with the C1w token, ~20) | after C1w |
| C9 | Review F3: `decide_lan_arrival` parses `line[2:].strip()` → `rstrip("\r\n")` (paths with leading/trailing spaces); S8c explicit `--delete-after` in the LAN builder with never/always-list test | `core/lan_sync.py` (~20) | after C6w |
| C10 | Review F1: `stats.deletes` counts each failed delete attempt (X3) → `files_deleted` overstated; take real deletes from `Deleted` log entries; S8f mark last stats of a killed run as "at least" in metrics/dashboard | `core/lan_sync.py`, `core/cloud_sync.py`, `ui.py` (~60) | after C9 |
| C7 | `--retries` measurement: locked-file night on 10k and 50k-file lab trees, `lan.retry_count` 1/2/3: wall time, checks, deletes held. Recommendation (keep 3 or lower to 2) to Opus | `agent_runs/c7_retries/` only | after CL2 |

## How to run tests (worktree has no .venv)
```
git worktree add C:\Users\malliath\Desktop\aam_ws_c -b ws-c-phase2 rclone-engine    (once)
cd C:\Users\malliath\Desktop\aam_ws_c
$env:PYTHONPATH='.'; $env:PREFECT_HOME="$env:TEMP\aam_ph_wsc"; Remove-Item -Recurse -Force $env:PREFECT_HOME -ErrorAction SilentlyContinue
$env:AAM_LAB_ROOT="$env:TEMP\aam_lab_wsc\lab"
$py='C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe'
& $py -m pytest audit_lab/test_w13_branches.py -q -s --timeout=1800 -p no:cacheprovider -o addopts="" --basetemp="$env:TEMP\aam_bt_wsc"
& $py -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600 -m "not slow" --basetemp="$env:TEMP\aam_bt_wsc"
& $py -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=1200 -m slow --basetemp="$env:TEMP\aam_bt_wsc_slow"
C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\ruff.exe check <changed files>
```
Run ONE lab file at a time; at most 2 agents on the PC run labs at once (master plan §6). Delete `PREFECT_HOME` before each lab run. Do not edit modules while a lab runs in this worktree.

## Definition of done (gate G3)
Every listed lab file green or its deleted tests logged with the covering row; C1-C6 rows green; cloud evidence-gate tests (`test_cloud_evidence_gate*`) unchanged and green; fast loop + slow file green; a real hanging child process (e.g. a python child that sleeps without output) is killed by S6 in a unit test; ruff clean on changed files; Opus mutation checks (stall comparator, retry count, combined-count mapping) turn tests red; findings in `docs/PROGRESS_WS-C_2026-11-25.md`.

## Report back (<= 20 lines)
Lab table (file → green / deleted N with covering rows / finding), commits per package, removed/added config keys, retry levels before/after, S6 VERIFY answer, C7 numbers + recommendation, open owner questions.
