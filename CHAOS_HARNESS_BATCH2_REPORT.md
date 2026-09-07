# Chaos Harness Batch 2 Report

## 1. Full catalog recovered

Master catalog: `TEST_MATRIX.md` phases 1–28. P-definitions:
`CLOUD_PHASE_PLAN.md` (P8–P12, P22-R3, P12/13/14/16/17),
`00_spec_extracts.md` (P9/P10), `BATCH_A_REPORT.md` (P9/P10 PASS),
`BATCH_B_REPORT.md` (P11/P12 PASS), `POST_F08_VALIDATION_REPORT.md`
(P0–P7/P11/P13-part1/P25 PASS), adversarial `FINAL_REPORT.md`
(T1 INVALID, T2 PASS, T3A INVALID, T3B PASS, T4 PASS+findings).
Q1–Q5 do not exist (Qwen was the executor role). Full map:
`CHAOS_HARNESS_COVERAGE_MATRIX.md`.

## 2. Existing coverage

Adequate: P0–P12, P13-part1, P25, T2/T3B/T4, H1–H5 shapes, all
remediation/F-08/gate regression suites. Invalid/by-design: T1, T3A.
Blocked: H5 (DB fault). Deferred: P15, P19–P24, phase 7.

## 3–4. Five selected scenarios + why

- B2-1 kill-timing matrix + network leg (phases 6+5): matrix HIGHEST
  PRIORITY; H1 proves one shape, the matrix demands progress-point ×
  mechanism coverage + natural PARTIAL(11) path. New: "no killed run
  COMPLETE" across timings.
- B2-2 C-DK-001 main + sync-only + control (phase 8): HIGHEST
  PRIORITY; H2 is only the verify-only leg. New: gate falsification
  across kill placements + clean control.
- B2-3 audit divergence classes (phase 13-part2): H4 proves one
  class; matrix demands missing/extra/size/mtime. New: per-class
  verdict + path truthfulness.
- B2-4 sharded scope isolation (phase 16): wholly new. No false
  global VERIFIED; per-scope rows.
- B2-5 state-model observability (phase 17 + P26): wholly new,
  zero-fault. Backup≠integrity surfacing, NOT_VERIFIED defaults,
  history immutability, report truthfulness.

## 5. Existing mechanisms reused

`killrob.py`, `killrcl.py`, `inv.py`, `hashpair.py`, `pfx.py`
(trigger + run states), `dbpeek*`, `dblkill_postrem.py` (pattern
reference), `ph22_xbackend.py` (deferred-use noted),
`faults.same_size_corrupt` (Batch 1), `audit_lan(scope_prefixes)`,
`ui._integrity_summary`, `core.report.generate_report_html`,
`ManifestDB` readers, pytest fixtures.

## 6–7. New code + justification

- `chaos_harness/scenarios_batch2.py` (NEW): five `Scenario`
  metadata records. Necessary: catalog prose is not executable;
  zero business logic.
- `chaos_harness/faults.py`: +`dest_variant`/`restore_variant`
  (NEW functions, existing symbols untouched). Necessary: B2-3
  classes had no parameterized tool (campaign scripts hardcode one
  file each); follows the Batch-1 adaptation precedent.
- `chaos_harness/scenarios.py`: `get()` falls back to batch-2
  registry via deferred import (4-line additive edit; `ALL`
  unchanged — Batch-1 registry test still passes). Necessary:
  single lookup for `runner`. Blast radius checked: callers are
  `runner.get_scenario` + tests only.
- `tests/test_chaos_harness_batch2.py` (NEW): 18 dry-run tests.
  Necessary: Step-9 proof obligation.
- Total: ~2 files + ~60 additive lines. No other new code.

## 8. Exact real entrypoints

B2-1: `backup(mode='lan')` → `_run_lan_pipeline` → `run_lan_sync` →
`decide_lan_result` → `_record_run`. B2-2: `backup(mode='cloud')` →
`_run_cloud_pipeline` → sync → `verify_cloud_integrity` →
`decide_cloud_verify_result` → 7-clause gate → record (must not run).
B2-3/B2-4: `integrity_audit_flow` → `audit_lan[/scope_prefixes]` →
`_run_rclone_check` → `record_audit`. B2-5: `last_run`/`latest_audit`,
`_integrity_summary`, `generate_report_html` (observed).
Harness-only: sequencing, arming, capture, assertions, cleanup.

## 9. Contract assertions

Batch-1 `assertions.py` reused unchanged (status/unchanged/contains/
summarize over observed rows/states). B2-1: no `LAN_COMPLETE`/success
row after kill; SUSPECT + alert-once + FAILED + retained log; leg-2
PARTIAL exit 11. B2-2: never COMPLETE+verified over partial; truthful
missing labels; control COMPLETE. B2-3: per-class verdict + paths;
history unchanged; read-only. B2-4: per-scope rows; no global
VERIFIED over dirty shard. B2-5: NOT_VERIFIED surfaced; failed audit
authoritative; history immutable; report consistent. Covers contract
rules 1–12 (rules 8/12 via lock + prod checkpoints in every run).

## 10. Safety controls

Unchanged Batch-1 model: chaos-path/bucket/Prefect/host guards on
every fault/trigger/cleanup path; `run` requires `live=True` +
immutable `app_commit`. B2 adds no new target classes (same chaos
source/dest/bucket/DB/lock). No DB-fault capability introduced
(STEP 10 honored; metadata test asserts no B2 scenario depends on one).

## 11. Evidence model

Batch-1 `EvidencePack` reused: `CHAOS_HARNESS\<B2-x>\<run>\` with
targets/scenario/variant records, killer logs, agent slices,
run_history/audit rows, bucket censuses, hash proofs, Prefect states,
manifest.json verdict. B2 dry-runs already present (no overwrite of
H1–H5 or campaign evidence).

## 12–13. Tests + dry-run results

- ruff: clean. `test_chaos_harness_batch2.py`: 18 passed;
  combined with Batch-1 suite: 43 passed.
- `runner.dry_run` B2-1..B2-5 on real evidence root: 5× DRYRUN_OK.
- Variant engage/restore proven on chaos scratch (missing/size/
  mtime/extra + source-untouched where applicable); scratch removed.
- Production rejection + live-gate refusal re-proven.
- No destructive execution; remediation still uncommitted and untouched
  (product diff identical to pre-harness state).

## 14. Remaining blocked/invalid

Blocked: H5 (unchanged). Invalid/by-design: T1, T3A (will not be
manufactured). Deferred: P15, P19–P24, phase 7, P26-standalone.

## 15. Coverage gained vs Batch 1

Batch 1 proved single-shape fault→verdict paths. Batch 2 adds:
timing/mechanism matrices (B2-1/B2-2), divergence-class × verdict
mapping (B2-3), scope correctness (B2-4), and separation-of-concerns
observability (B2-5) — the four largest unexecuted contract areas
short of the deferred long-horizon/service-risk items.

## 16. Recommended Batch 3 candidates

P19 lock+audit overlap (after concurrency semantics review), P22
xbackend serialization (existing `ph22_xbackend.py`), P20 watchdog
matrix (after watchdog-fault review), P23/P24 bounded-exhaustion +
endurance (reusing Batch-1/2 primitives), P26 report/UI full pass
(building on B2-5). H5 stays blocked until DB-fault blast radius is
understood — no workaround will be introduced to fill the slot.
