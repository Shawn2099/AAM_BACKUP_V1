# Brief WS-G: release-candidate gate, docs merge, handoff

Paste this whole file to a fresh agent (integrator session on `rclone-engine`, worktree `C:\Users\malliath\Desktop\aam_rclone`).
Master plan: `docs/PLAN_MASTER_PARALLEL_2026-11-25.md`. Starts after G3 (WS-A, WS-B, WS-C merged; WS-D D0-D6 merged; WS-E E4 merged).

## Context
Nightly backup automation for an accounting firm; branch `rclone-engine` = rclone on both legs, Phase 1 + review amendments,
Phase 1b (copy-only, reminders, versions), Phase 2 (cloud trims, one retry level, S6 stall watchdog, lab sweep). This
workstream proves the whole branch, merges the per-workstream doc notes into the shared docs, writes the handoff and tags
`rclone-rc1` for the harness run (WS-D D8) and the shadow trial (WS-F).

## Rules (owner, binding)
- No product code changes here except mechanical fixes found by the gate; anything else goes back to its workstream (Sonnet) or Opus.
- Never weaken, skip or xfail a test to make the gate green. Never touch the box or the real NAS. Never power off this PC.
- Repo is PUBLIC: scan the diff for secrets (passwords, tokens, keys, box IPs in new files) before any push.
- Commit trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Never bare `git stash`. GitNexus `detect_changes --scope compare --base-ref baseline-v1-overlap` before tagging (re-run if `partial`/`truncated`).
- Model split: Haiku runs the suites and writes the doc merges; Sonnet fixes mechanical breakage; Opus does the mutation review and signs G4.

## Packages
| ID | Work | Acceptance |
|---|---|---|
| GT1 | Suites on the merged HEAD: fast loop, slow file, targeted labs (`test_w25`, `test_w26`, `test_w4`, `test_fy_overlap_lab`, `test_w6b`), then the FULL audit lab alone (no other agent running a lab), harness tests; `ruff check` on every file changed since `baseline-v1-overlap` | all green except the listed known reds; counts recorded in `agent_runs/gt1_gate.md` |
| GT2 | Opus mutation review: P1 R5 list (exit 1 → COMPLETE, floor max→min, arrival `>=`→`>`, dry-run/sync argv equality, final-attempt ERROR scan), review-matrix mutations, 1b (copy vs sync on G3-only, ladder rungs, 91-day `>=`, versions inside mirror refused, space sum), Phase 2 (stall comparator, retry level, combined-count mapping), E4 wake gate. Each must turn a test red; survivors get a new test (test agent) before G4 | `agent_runs/gt2_mutation.md` |
| GT3 | Docs merge from `docs/PROGRESS_WS-*_2026-11-25.md`: `docs/ARCHITECTURE.md` (§10 gates LAN/cloud rows rewritten for rclone, verify phase, copy-only, versions, S6), `docs/EMAIL_CONTENT_SPEC.md` (new keys: reminder ladder, versions, LAN-03/04, P1/P7/P8 changes), `config.example.yaml` comments, `SESSION_HANDOFF.md` new top block (position, what is on the box = still `dc50265`, next steps D8 → F3/F4 → deploy, owner decisions open, environment caveats), `CLAUDE.md` only if commands/paths changed; then tag `rclone-rc1` and push branch + tag | docs committed; tag pushed |

## How to run
```
cd C:\Users\malliath\Desktop\aam_rclone
$env:PYTHONPATH='.'; $env:PREFECT_HOME="$env:TEMP\aam_ph_wsg"; Remove-Item -Recurse -Force $env:PREFECT_HOME -ErrorAction SilentlyContinue
$env:AAM_LAB_ROOT="$env:TEMP\aam_lab_wsg\lab"
$py='C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe'
& $py -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600 -m "not slow" --basetemp="$env:TEMP\aam_bt_wsg"
& $py -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=1200 -m slow --basetemp="$env:TEMP\aam_bt_wsg_slow"
& $py -m pytest audit_lab --ignore=audit_lab/test_w7_deployed.py -q -s --timeout=1800 -p no:cacheprovider -o addopts=""
& $py -m pytest tools/livebox/tests tests/test_review2_driver.py tests/test_review2_rig.py -q -p no:cacheprovider -o addopts="" --deselect tools/livebox/tests/test_wire_injections.py
C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\ruff.exe check <changed files>
git grep -n -i robocopy -- core flow.py models     (only allow-listed comments)
```
Known reds: `test_w16_schedule_collisions_across_year` (WP8); `test_v113_round3::test_r3_repo_config_timeouts`/`::test_r3_loaded_runtime_timeouts` (no repo config.yaml); `test_review2_driver.py` on a dirty tree.

## Definition of done (G4)
GT1-GT3 done, Opus signs G4 in `SESSION_HANDOFF.md`, tag `rclone-rc1` pushed, owner told which decisions are still open (OD1, OD3, OD6, OD7, OD9, OD10).

## Report back (<= 15 lines)
Suite counts, lab result, ruff, mutation survivors and their new tests, tag sha, open owner decisions.
