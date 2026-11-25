# Master plan: finish the rclone-engine product and test it, in parallel workstreams (2026-11-25)

Author: Opus (planner). Integration branch `rclone-engine` (worktree `C:\Users\malliath\Desktop\aam_rclone`, HEAD 65a16a0 = P1 C0..C12 done).
Binding inputs: `docs/PLAN_P1_RCLONE_LAN_2026-11-25.md` (P1 plan, Phase 1b, Deferred table), `docs/TESTMATRIX_P1_REVIEW_2026-11-25.md`
(review amendments, written by another Opus agent; WS-A chunk IDs come from it), `CLAUDE.md` (build process, escalation, scope guard).
Briefs for fresh agents: `docs/briefs/BRIEF_WS-<id>_*.md` (self-contained; paste one per session).
Owner goal: finish the product, then test it in the 728-day live-box harness, then shadow trial, then deploy.

## 0. Owner decisions (blocking items first)

| ID | Decision | Default proposed | Blocks | Needed by |
|---|---|---|---|---|
| OD1 | **Read-only files on the NAS mirror** (P1 plan R11, measured in `TESTMATRIX_P1_REVIEW` X1/X2): robocopy copied the R attribute to the NAS; rclone cannot REPLACE a read-only destination file (exit 1, PARTIAL, all deletions held that night, repeated every night the source changes); deleting one works. Options: (a) accept, the PARTIAL alert names the file (built in WS-A R3/L12); (b) one-time `attrib -R /S` on the mirror before the switch (touches backup data); (c) per-file clear inside the product (new code). | Count first in WS-F step A (read-only listing). 0 → nothing. >0 → (b) once, by hand, owner present, during the deploy window; never touch source attributes. | WS-F F2 output, F6 step 5 | count: F2; decision: before F6 |
| OD2 | **S6 stall watchdog timing** (needed: `--timeout`/`--contact-timeout` have no effect on the local/SMB backend, measured by WS-A; a hung SMB read is today only caught by the hard timeout): kill rclone when it makes no progress for N minutes; result FAILED retryable (exit-5 path) or FAILED final. | `tools.stall_timeout_seconds` default 1800 (bounds 300..14400), 0 = off; LAN and cloud; FAILED retryable once (counts against `max_attempts`). | WS-C C6 | before C6 starts (after G2) |
| OD3 | **Shadow-trial schedule** on the real box/NAS: step A (read-only dry-run vs the real mirror) date; step B (NAS test folder writes) date; cable pull date with the owner at the NAS. | A: any weekday 10:00-18:00 IST after G1; B + cable pull: one weekday after G5, owner present, outside 20:30-22:30 and 01:30-03:00 IST. | WS-F F2, F3, F4 | A: after G1; B: after G5 |
| OD4 | **2027-01-03 audit failure (lane a2y26)**: the analysis (`aam_baseline_overlap/docs/REPORT_LIVEBOX_A2Y26_2026-11-25.md`, untracked there) says HARNESS artefact (`tools/livebox/churn.py` stamps churn files at noon but writes them before 02:01; the 08:00 audit sees a "future" mtime and fails closed, `core/audit_pending.py` correct). Go to (1) commit the harness fix on `baseline-v1-overlap` + port to `rclone-engine`, (2) no product change on baseline, (3) re-run a short window lane on the box later. | Yes to all three; product unchanged. | WS-E E2/E3 | now |
| OD5 | **P6 NAS woken once per FY pass** (product defect, present on rclone-engine): fix on `rclone-engine` only (baseline stays as deployed)? | Yes, rclone-engine only (WS-E E4). | E4, WS-D spy expectation | before G2 |
| OD6 | **F-OWN-1 rollover touches the NAS** (1 Apr: wake 2, folder create): change the product to match the 2026-11-23 rule "rollover never touches the NAS", or accept the plan behaviour and fix the harness expectation. | Accept the current behaviour (the NAS FY folder must exist before the first new-FY LAN run), change `SPY_ROLLOVER_NAS` expectation in WS-D. | WS-D D6 | before WS-D D6 |
| OD7 | **Harness lane choice** for the new code on the box: code copy `C:\AAMLivebox\code_rclone` at tag `rclone-rc1`; lanes S14 smoke, A2R (728 d from 2026-10-01, 730 d retention, approver after 3 d), then XR1-XR4 + X01, X04l, X05, X08b; max 3 lanes at once. | As stated (WS-D brief, run protocol). | WS-D D8 | before D8 (after G4) |
| OD8 | **Notify fix semantics** (P1, P7, P8/R2): P1 an escalation (higher class) bypasses the 24 h cooldown of a lower one; P7 one PROBLEM mail per incident key within `alerts.duplicate_window_seconds` (default 300); P8 a WARNING-class event never gets the "ACTION NEEDED" subject. | As stated. | WS-B B1 | before B1 |
| OD9 | **Versions folder space policy**: free-space check counts the versions folder growth (V11); is there also a cap (e.g. warn at X % of NAS)? | No cap; weekly report shows size; FAIL only when free space < need (existing rule). Retention 91 d (decided). | WS-B B7 | before B7 |
| OD10 | **Deploy window and rollback acceptance**: deploy `rclone-rc1` to `C:\AAMBackup` after G6, rollback = previous tag + config backup (+ DB backup if any DDL). | As in `BRIEF_WS-F`. | WS-F F6 | after G6 |

Not planned here: Prefect removal (`docs/OPUS_DESIGN_RCLONE_ONLY_NO_PREFECT_2026-11-25.md`), I-16 old-year bulk upload, alert-controls D-G, P5 / P4 rollover fixes (Deferred table).

## 1. Workstreams

| WS | Goal | Branch / worktree | Starts | Size (chunks) |
|---|---|---|---|---|
| WS-A | Phase 1 review amendments, chunks **R1** M2 arrival tolerance, **R2** S2 errors beyond differences, **R3** S1 failed object paths, **R4** M3 backup-dir counts, **R5** S4 audit filters (`core/integrity.py`), **R6** S5 hang after cutoff, **R7** S7 reserve text, **R8a-R8d** S3 nightly hash check (step `rclone-lan-hash`, keys `lan.arrival_hash_*`), **R10** lab L12-L14, R9/R11 per the plan's "Review amendments" section; closes the **Phase 1 gate** (P1 plan §6 item 7) | `ws-a-review` / `..\aam_ws_a` (or directly on `rclone-engine`) | NOW | ~12 (R1-R11) |
| WS-B | Phase 1b: notify fixes, versions keys, `--backup-dir`, copy-only on G3-only block, reminder ladder + dashboard, purge, space accounting, lab | `ws-b-phase1b` / `..\aam_ws_b` | B0, B1 NOW; rest after G1 | 9 |
| WS-C | Phase 2: cloud guard trims, Prefect retries, audit-lab sweep, S6 stall watchdog, `--retries` measurement | `ws-c-phase2` / `..\aam_ws_c` | C0, CL* NOW; cloud wiring after G2 | 6 lab + 7 code |
| WS-D | `tools/livebox` adaptation to the rclone LAN engine + run protocol + box run | `ws-d-livebox` / `..\aam_ws_d` | D0-D3 NOW; D4-D6 after G2/G3; D8 after G4 | 8 |
| WS-E | 2027-01-03 audit failure: verify analysis, harness fix on baseline, port, regression test; P6 fix | baseline part on `baseline-v1-overlap` (`..\aam_baseline_overlap`); E4 on `ws-e-p6` / `..\aam_ws_e` | NOW (E1-E3); E4 after G1 | 4 |
| WS-F | Shadow trial on the real box/NAS (A dry-run, B test folder, cable pull), go/no-go, deploy + rollback | `ws-f-shadow` / `..\aam_ws_f` (tools/shadow + docs only) | F0-F1 NOW (docs/tool); F2 after G1+OD3; F3-F5 after G5; F6 after G6 | 6 |
| WS-G | Gate: full suite, ruff, mutation review, docs merge, SESSION_HANDOFF, tag `rclone-rc1` | `rclone-engine` (integrator) | after G3 | 3 |

## 2. Dependency graph

```
NOW ──┬─ WS-A (review amendments) ───────────────► G1 Phase-1 gate
      ├─ WS-B B0 matrix, B1 notify (notify.py only) ─┐
      ├─ WS-C C0 matrix, CL0..CL5 lab sweep (audit_lab only)
      ├─ WS-D D0..D3 harness compat (tools/livebox only)
      ├─ WS-E E1..E3 (baseline harness fix + port via D0)
      └─ WS-F F0 runbook, F1 tools/shadow script
G1 ──┬─ WS-E E4 (P6, flow.py) ─► WS-B B2..B8 (config, lan_sync, flow, approvals, ui) ─► G2 Phase-1b gate
      └─ WS-F F2 step A (owner go, read-only on NAS)  ──► results feed OD1, OD2, C7
G2 ──┬─ WS-C C1..C5 wiring (flow.py cloud) + C8, C6 S6 (tool_runner + callers), C9, C10, C7 measure ─► G3 Phase-2 gate
      └─ WS-D D4 (versions/copy-only/ladder oracles), D5 (hash check), D6 (spy expectations)
G3 ──► WS-G gate ─► G4 RC tag rclone-rc1 ─► WS-D D8 box harness run (728 d) ─► G5 harness verdict
G5 ──► WS-F F3 step B, F4 cable pull, F5 go/no-go ─► G6 ─► WS-F F6 deploy (owner go) ─► G7 first 7 nights watched
```

**Critical path:** WS-A → G1 → E4 → WS-B B2-B7 (flow.py) → G2 → WS-C C1-C6 (flow.py, tool_runner) → G3 → WS-G → G4 → D8 728-day box run (~1 day wall clock, pauses around production jobs) → G5 → F3/F4 (owner at NAS) → G6 → deploy.
The serial resource on the critical path is `flow.py` (and `models/config.py`); everything that does not touch them runs beside it.

**Start-now parallel set (no shared files):** WS-A (other agent), WS-B B0+B1, WS-C C0+CL0..CL5, WS-D D0..D3, WS-E E1..E3, WS-F F0+F1. That is 6 sessions; cap below says at most 4 building at once, so start WS-A, WS-D, WS-C (lab), WS-E first; WS-B B1 and WS-F F0/F1 next (they are small and docs-heavy).

## 3. File ownership (no two parallel packages edit one file)

Hotspots are handed over as a token, in this order. A package waits until the previous holder is merged into `rclone-engine` and then rebases.

| File | Token order | Note |
|---|---|---|
| `flow.py` | WS-A → E4 → B4 → B5 → B6 → B7 → C1w+C8 → C3 → C4w → C5 → C6w | Module-first pattern: WS-C builds `core/cloud_*` changes before its token, wires afterwards ("w" = wiring chunk). |
| `models/config.py`, `core/config_help.py`, `config.example.yaml`, `core/config_edit.py` | WS-A → B2 (all 1b keys at once) → C3 → C5 → C6 | Keys added in the same commit as the code that reads them (owner rule). B2 adds every `lan.versions_*` and reminder key together with the validator that reads them. |
| `core/lan_sync.py` | WS-A → B3 → C6w → C9 → C10 | |
| `core/integrity.py` | WS-A (R5) | nobody else plans to edit it |
| `test_support/fake_tools.py`, `test_support/rclone_logs/*`, flow tests using `fake_rclone_lan` | WS-A (R8b, R9 sweep) → B → C | until G1 other workstreams add NEW test files only and do not touch the fake |
| `core/cloud_sync.py` | WS-A (if it touches parsers) → C1 | |
| `core/guards.py` | WS-A → B4 → C1 | |
| `core/notify.py` | B1 only (if the review matrix names `notify.py` for WS-A, B1 waits for WS-A) | |
| `core/approvals.py`, `ui.py`, `static/js/dashboard.js` | B5 | |
| `core/permissions.py`, `core/report.py` | B7 | |
| new `core/lan_versions.py` | B6 | |
| `core/cloud_preflight.py`, `core/cloud_verify.py`, `core/cloud_reporter.py` | C1, C2, C4 (one at a time inside WS-C) | |
| `core/tool_runner.py` | C6 | |
| `audit_lab/labconf.py`, `audit_lab/conftest.py`, `audit_lab/make_ledger.py`, all robocopy-era lab files | WS-C (CL*) | |
| `audit_lab/test_w25_rclone_lan.py` | WS-A (L12) | WS-C does not touch it |
| new `audit_lab/test_w26_versions_lab.py` | B8 | must not edit labconf; asks WS-C for helpers |
| `tools/livebox/**` | WS-D (E2 port lands as D0) | |
| `tools/shadow/**` (new), `docs/SHADOW_TRIAL_*.md` | WS-F | |
| `docs/PLAN_P1_RCLONE_LAN_2026-11-25.md`, `docs/TESTMATRIX_P1_REVIEW_2026-11-25.md` | WS-A Opus only | |
| `docs/TESTMATRIX_P1B_2026-11-25.md` | WS-B Opus | `docs/TESTMATRIX_P2_2026-11-25.md`: WS-C Opus; `docs/TESTMATRIX_LIVEBOX_RCLONE_2026-11-25.md`: WS-D Opus |
| `SESSION_HANDOFF.md`, `docs/ARCHITECTURE.md`, `docs/EMAIL_CONTENT_SPEC.md`, `CLAUDE.md` | WS-G only | Others append findings to their own `docs/PROGRESS_WS-<x>_2026-11-25.md` (owner rule: document every finding) |
| Existing `tests/*.py` | the package holding the token of the module under test | New tests always go in NEW files `tests/test_<pkg-id>_*.py` |

## 4. Work packages (summary; details in the briefs)

Roles everywhere: **Opus** writes the test matrix and reviews (diff, scope, mutation checks); **Haiku** writes tests first from matrix rows (shows them red on old code), runs test loops, monitoring and docs; **Sonnet** writes code and debugs. Tests and code by different agents. Escalation per CLAUDE.md (Haiku 2 attempts → Sonnet; Sonnet blocked or interface change → Opus).

| ID | Goal | Owned files | Tests first | Acceptance | Chunks | Depends |
|---|---|---|---|---|---|---|
| A* | Review amendments (IDs/rows in `TESTMATRIX_P1_REVIEW`) | per that matrix | yes | its rows green + full fast loop + slow file | per matrix | - |
| B0 | Opus matrix for 1b: V1-V13, ladder rows N1-N6, notify rows P1/P7/P8 | `docs/TESTMATRIX_P1B_2026-11-25.md` | - | exists, reviewed | 1 | - |
| B1 | Notify fixes P1, P7, P8/R2 | `core/notify.py`, `tests/test_b1_notify_fixes.py` | yes | rows green; `test_m3_alerts*`, `test_notify*` green | 1 | B0, OD8 |
| B2 | Config: `lan.versions_enabled/_folder/_retention_days`, `approvals.reminder_days`, validator V10 | config files (token) | yes | K-style rows + V10 | 1 | G1 |
| B3 | Builder: `--backup-dir` per run+FY, `copy` mode argv | `core/lan_sync.py` | yes | V6-V8 argv rows | 1 | B2 |
| B4 | Copy-only on a G3-only block, both legs | `flow.py`, `core/guards.py` | yes | V1, V2, V5 | 1-2 | B3 |
| B5 | Reminder ladder + dashboard "N deletions waiting, D days" | `core/approvals.py`, `flow.py`, `ui.py`, `static/js/dashboard.js` | yes | V3, N1-N6 | 2 | B1, B4 |
| B6 | Versions purge (`rclone delete --min-age 91d` + `rmdirs`) | new `core/lan_versions.py`, `flow.py` hook | yes | V9 | 1 | B3 |
| B7 | Space accounting + weekly report size | `core/permissions.py`, `core/report.py`, `flow.py` | yes | V11 | 1 | B6, OD9 |
| B8 | Real-rclone lab V12 (+ V4 flow) | `audit_lab/test_w26_versions_lab.py` | (is the test) | green with clean PREFECT_HOME | 1 | B7 |
| C0 | Opus matrix for Phase 2 (cloud trims, retries, S6) | `docs/TESTMATRIX_P2_2026-11-25.md` | - | exists | 1 | - |
| CL0-CL5 | Rewrite robocopy-era lab files (list in `BRIEF_WS-C`) | `audit_lab/**` except w25/w26 | (are the tests) | each file green or a logged product finding | 6 | C0 |
| C1/C1w | Cloud dry-run `--combined` replaces `rclone size` walks; drop G2 on cloud | `core/cloud_sync.py`, `core/guards.py` then `flow.py` | yes | P2 rows | 2 | G2 for C1w |
| C2 | Drop cloud preflight probe A | `core/cloud_preflight.py` | yes | rows | 1 | C0 |
| C3 | Cloud write probe off/removed (deprecate key) | `flow.py`, config files | yes | rows + K1 | 1 | C1w |
| C4/C4w | Merge cloud verify: one `check --combined` + one size | `core/cloud_verify.py`, `core/cloud_reporter.py` then `flow.py` | yes | evidence-gate rows unchanged | 2 | C3 |
| C5 | One retry level: Prefect retries on preflight/verify, cloud `max_attempts` | `flow.py`, config files | yes | `test_task_retry_config` + rows | 1 | C4w |
| C6/C6w | S6 stall watchdog in `run_tool` + mapping in LAN/cloud results | `core/tool_runner.py`, config files, then `core/lan_sync.py`, `core/cloud_sync.py`, `flow.py` | yes | rows incl. a real hanging child process | 2 | OD2, C5 |
| C7 | `--retries` measurement (locked-file night cost, 3 vs 2; lab numbers, confirmed in WS-F step B = review S8a) | `agent_runs/c7_retries/*` (report only) | - | numbers + recommendation to Opus | 1 | CL done |
| C8 | Review F2: cloud `stopped = ... or (limited and timed_out)` (flow.py ~966) fixed with the R6 rule | `flow.py` (with C1w token) | yes | W14-style cloud row | 1 | C1w |
| C9 | Review F3 (arrival parse `rstrip("\r\n")` not `strip()`), S8c explicit `--delete-after` (never/always-list test) | `core/lan_sync.py` | yes | rows from C0 | 1 | C6w (lan_sync token) |
| C10 | Review F1 (`stats.deletes` counts failed delete attempts → `files_deleted` overstated) + S8f ("at least" on killed-run stats) | `core/lan_sync.py`/`core/cloud_sync.py` metrics, `ui.py` text | yes | rows from C0 | 1 | C9 |
| D0-D8 | Harness: churn fix port, compat audit, rclone spies, oracles, 1b oracles, hash check, spy expectations, protocol, box run | `tools/livebox/**`, `docs/TESTMATRIX_LIVEBOX_RCLONE_2026-11-25.md` | yes (tools/livebox/tests) | harness dev tests green; box run per protocol | 8 | see brief |
| E1-E4 | Verify a2y26 analysis; harness fix on baseline; port; P6 fix | baseline `tools/livebox/churn.py` + test; `flow.py` (E4) | yes | rows E-R1..E-R4 | 4 | E4 after G1 |
| F0-F6 | Shadow runbook, shadow tool, step A, step B, cable pull, go/no-go, deploy+rollback | `docs/SHADOW_TRIAL_2026-11-25.md`, `tools/shadow/**` | tool tests yes | per brief | 6 | see graph |
| GT1-GT3 | Gate: suites, ruff, mutation review, docs merge, handoff, tag | `SESSION_HANDOFF.md`, `docs/ARCHITECTURE.md`, `docs/EMAIL_CONTENT_SPEC.md` | - | gate list in `BRIEF_WS-G` | 3 | G3 |

Review-derived items placed outside WS-A: V13 (backup-dir counters/backstop/space, `TESTMATRIX_P1_REVIEW` §10) → WS-B B3/B7; M4 timeouts (3x measured dry-run/check/hash, reserve minutes) → WS-F F2 measures, F6 step 5 sets the keys; S8a `--retries` → C7 + WS-F F3; S8b `--fix-case` → WS-F F3 measure; S8d rotating sample hash → only if the weekly audit runs out of budget (not planned); S8e timeout = SUSPECT kept, revisit with WS-F timings.

## 5. Git strategy

- One branch + one worktree per workstream, created off the current `rclone-engine` HEAD:
  `git worktree add ..\aam_ws_b -b ws-b-phase1b rclone-engine` (same for a/c/d/e/f). WS-E E1-E3 work in `..\aam_baseline_overlap` on `baseline-v1-overlap` (owner go OD4 before its commit).
- Commit per chunk, message cites the chunk ID (e.g. `feat(1b B3): ...`), trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (Opus/Haiku commits use their own model name).
- **Who merges:** one integrator session (Sonnet), only after the Opus review of that package passed (scope check `git diff --stat` vs scoped files/lines, no weakened/removed asserts, no new skips, mutation checks). Merge with `git merge --no-ff ws-x` into `rclone-engine`, run fast loop + slow file on the merge result, then every open WS branch rebases (`git rebase rclone-engine`).
- **Merge order:** WS-A → E4 → (D0-D3 any time, tools/livebox only) → B1 (any time after B0) → B2..B8 → CL* (any time, audit_lab only) → C1..C6 → D4-D6 → WS-G.
- **Conflict hotspots:** `flow.py`, `core/lan_sync.py`, `models/config.py`, `core/config_help.py`, `config.example.yaml`, `core/guards.py`, shared tests (`test_m2_guard_wiring.py`, `test_task_retry_config.py`, `test_config_validation.py`, `test_m4_config_example.py`). Token rule in §3; new tests in new files.
- GitNexus: before editing a symbol run `impact` (CLAUDE.md "Always Do"); before each commit `detect_changes --scope all`. Re-index in the worktree if stale (`node .gitnexus/run.cjs analyze --index-only`).
- Never bare `git stash` (shared across worktrees). Push regularly (repo is PUBLIC: scan diffs for secrets first).

## 6. Running agents in parallel safely

- **Cap: 4 building agents at once on the dev PC**, of which at most **2 run an audit-lab file** at a time; **1 agent at a time on the box** (ssh), and on the box at most 3 harness lanes (safety S13).
- Interpreter: `C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe` (worktrees have no `.venv`), `PYTHONPATH=.`. Never `uv sync`/`pip install` into that venv while others run.
- Per agent, unique: `PREFECT_HOME=%TEMP%\aam_ph_<ws>` (delete before each lab run), `AAM_LAB_ROOT=%TEMP%\aam_lab_<ws>\lab` (labconf reads it; the loopback UNC `\\127.0.0.1\C$\...` and `evidence.jsonl` derive from it, so they are unique too), pytest `--basetemp=%TEMP%\aam_bt_<ws>` (pytest's default basetemp is shared per user and pruned by concurrent runs).
- Never run `tools\run_audit_lab.ps1` (uses default dirs) while another agent runs a lab. While a lab runs in a worktree, do not edit modules in that worktree (lab child processes import fresh code).
- The full audit lab (~20 min, heavy) runs only in WS-G, alone.
- Box: no product changes, no service restarts, no reboot/shutdown, never power off the NAS or this PC; harness lanes only after the owner's go (OD7); shadow trial only after OD3.

## 7. Ordering and checkpoints (Opus review gates)

| Gate | When | Opus checks | Then |
|---|---|---|---|
| G0 | now | owner reads §0 and answers OD4, OD5, OD8 (others can wait) | start the parallel set |
| G1 Phase 1 | WS-A done | P1 §6 gate items 1-6 + review rows; mutation checks R5 | flow token to E4, then WS-B; F2 step A may run |
| G2 Phase 1b | B8 green | V1-V13, N rows, mutation: copy vs sync selection, ladder intervals, versions-in-mirror refusal, `--min-age` days, space sum | flow token to WS-C; WS-D D4 |
| G3 Phase 2 | C6 green, CL* done | cloud evidence gate unchanged, retries one level, S6 real hang test, lab ledger | WS-G |
| G4 RC | WS-G done | full suite, slow file, full audit lab, ruff on changed files, handoff | tag `rclone-rc1`, push |
| G5 harness | D8 report | FAIL 0, every FINDING classified, production unchanged | WS-F F3/F4 |
| G6 go/no-go | F5 | shadow criteria in `BRIEF_WS-F` | owner go → F6 deploy |
| G7 | 7 nights after deploy | every night COMPLETE or explained, no false ACTION mail | close |

Realistic order (sessions, not days): wave 1 (now) A + D0-D3 + CL0-CL2 + E1-E3; wave 2 A finishing + B0/B1 + CL3-CL5 + F0/F1; wave 3 (after G1) E4 then B2-B8 serially on flow.py while C2, C4 (modules), D docs, F2 run beside it; wave 4 (after G2) C1w-C6w + D4-D6; wave 5 WS-G → G4 → D8 box run → F3-F5 → deploy.

---

## Owner rule, 2026-11-25 (later): no fixes on other branches, document only

- Nothing is changed or committed on `baseline-v1-overlap` (or any branch other than `rclone-engine` and its WS worktrees). Findings on other branches are DOCUMENTED, not fixed.
- WS-E E2 (churn mtime fix on `baseline-v1-overlap`) is therefore NOT applied there. E1 (verify the report against code) and the written fix description stay as docs; the fix is built only on `rclone-engine` through WS-D D0 as a new change to `tools/livebox/churn.py` (not a cherry-pick from baseline). OD4 is superseded by this rule.
- The A2Y26 analysis report in the baseline worktree is left uncommitted, as is. Copy it into this branch's `docs/` if it must be kept.
- Product defects found on other branches (e.g. P6 on baseline) are listed in the handoff only; P6 is fixed in E4 on `rclone-engine`, which is this branch.
