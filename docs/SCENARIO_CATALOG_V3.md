# Scenario Catalog V3 — DELTAS from V2

V2 (204 scenarios) remains the base contract. This document records every
EXPECTED-OPERATION change produced by the v1.0–v1.2 remediation plan, so the
ledger can be audited against the current program. Nothing else moved.

## Corrections carried from V2 notes
- **C-1** fresh-mirror cloud run: exit=3 with extras bit; PARTIAL family.
- **C-2** orphan purge → `LAN_COMPLETE`/`CLOUD_COMPLETE` (no anomaly row).
- **C-3** cloud verifier granularity is COUNT-level (not per-file).
- **C-4** nightly-with-disabled-leg: the leg produces NO flow run at all
  (superseded below by P2-SCHED).

## Contract upgrades from the remediation plan
| ID(s) | Old expected | New expected | Source |
|---|---|---|---|
| CLOUD-06/07 (+ any exit-9) | typo bucket → `CLOUD_NO_CHANGES_COMPLETE` | fatal signals in rclone log → `CLOUD_FAILED` with error tail; clean log only → NO_CHANGES | P1-EXIT9 / R1 |
| LAN-06/LAN-15 | files_failed reported 0 (/NJS blindness) | positional Files-summary FAILED column; bit-3 floor ≥1 when summary absent; LAN-15 asserts ==2 | P1-COUNT A′ |
| WOL-08 | port-overflow behavior recorded verbatim | out-of-range port returns False (clamp guard) | P1-WOL |
| WOL-09 | unparseable MAC ValueError escapes (ANOMALY-RECORDED) | broad-but-loud catch warns and continues → PASS | P1-WOL |
| SCH-07/08 | slot wrapped the flow body | slot + watchdog lock live in `_backup_slot` per pipeline; flow body has no wrapper (deadlock-safe same-commit rule); direct callers serialize (SCH-16) | P2-CONC |
| SCH nightly family | disabled leg fired and COMPLETED empty | disabled legs never registered; stale deployments PAUSED at boot (`_reconcile_disabled_legs`) | P2-SCHED / R5 |
| LAN-03 | identity ambiguous across duplicate HealthError classes | single class (`core.health.HealthError`), canonical pytest.raises form | P1-EXC |
| log/error text | em-dashes allowed in machine-facing strings | U+2014/U+2013 forbidden in logger/raise literals (AST scanner CI test); HTML/docstring typography kept | P1-TEXT |
| DB pragmas | synchronous relied on DDL default | explicit NORMAL default + `maintenance.sqlite_synchronous: normal\|full` toggle; DB-18 readback contract | R4 |
| cloud duration | hard subprocess kill at timeout | optional `--max-duration` SOFT cutoff, auto = timeout−300s margin (`cloud.max_duration_seconds`; unset=auto, 0=off) | C-A |
| ledger rows | bare sids (RT vs branch collisions) | `<BRANCH_TAG>/<sid>` prefix on all scenario-file rows | P4-SID |

## New scenarios introduced by the plan
- **SCH-16** direct pipeline entry serializes on the global slot.
- **SYS-05** Machine-scope `CLOUDSDK_PYTHON` exists and bundled gcloud runs in a simulated fresh-boot env.
- **P0 guard suite** sandbox-outside-production guard (identity/mixed-case/junction/ancestor cases) for e2e helpers.

## Ledger conventions (V3)
- Rows are append-only; latest row per sid wins.
- Branch tags: A–J scenario batches, K = SYS batch (incl. SYS-05), RT = regression probes, unprefixed = pre-campaign or non-scenario probes.
