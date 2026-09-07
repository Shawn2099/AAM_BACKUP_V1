# Chaos Harness — Coverage Matrix

Sources: `TEST_MATRIX.md` phases 1–28 (master catalog), `CLOUD_PHASE_PLAN.md`
(P8–P12 + P22-R3 + P12/13/14/16/17), `00_spec_extracts.md` (P9/P10),
`BATCH_A_REPORT.md` (P9/P10 PASS), `BATCH_B_REPORT.md` (P11/P12 PASS),
`POST_F08_VALIDATION_REPORT.md` (P0–P7/P11/P13-part1/P25 PASS),
`POST_ADVERSARIAL_VALIDATION/FINAL_REPORT.md` (T1 INVALID, T2 PASS,
T3A INVALID, T3B PASS, T4 PASS+findings), Batch 1 (H1–H5).

Q1–Q5 do not exist as test IDs (Qwen was the executor role, not a test
series). The adversarial series is T1–T4.

| Test | Existing coverage | Batch 1 coverage | Remaining gap | Needs harness support? | Priority |
|---|---|---|---|---|---|
| P0–P7 (baseline, locks, worker death, service stop, pre-F-08) | PASS (pre-existing) | — | None | No | — |
| P8 C-DK-001 double-kill | Partial (main + sync-only OK; verify-only → F-08) | H2 covers verify-kill leg | main + sync-only + control matrix rerun | Yes (B2-2) | HIGH |
| P9 cloud fault matrix F1–F5 | PASS Batch A @c0d525b | — | Revalidation only | No | Low |
| P10 V2a/b/c cloud corruption | PASS Batch A | H3 (V2a shape) | V2b/c revalidation only | No | Low |
| P11 F1 diff mapping | PASS Batch B | — | None | No | — |
| P12 audit cloud leg | PASS Batch B | — | None | No | — |
| P13-part1 rclone arg probes | PASS | — | None | No | — |
| P13-part2 live LAN audit matrix | NOT EXECUTED | H4 (single-file divergence) | missing/extra/size/mtime classes | Yes (B2-3) | HIGH |
| P14 cloud audit matrix | NOT EXECUTED | Partial via P12+V2b pattern | deployment-level matrix | Partial (covered by B2-3 pattern; no new scenario) | Med |
| P15 benchmark | NOT EXECUTED | — | Relative-only numbers | No | Low |
| P16 sharded audit | NOT EXECUTED | — | Scope isolation / no false global VERIFIED | Yes (B2-4) | HIGH |
| P17 state model | NOT EXECUTED | — | Backup≠integrity, NOT_VERIFIED, history immutability | Yes (B2-5) | HIGH |
| P18 recovery | NOT EXECUTED | H4 T4-C pattern | None new | No | — |
| P19 lock+audit overlap | NOT EXECUTED | — | Concurrency semantics; nondeterministic | Deferred | Med |
| P20 watchdog matrix | NOT EXECUTED | — | Needs watchdog-fault review; service risk | Deferred | Med |
| P21 boot/recovery | NOT EXECUTED | — | Service manipulation risk | Deferred | Low |
| P22 xbackend concurrency | NOT EXECUTED (`ph22_xbackend.py` exists) | — | Nondeterministic overlap | Deferred | Med |
| P23 exhaustion | NOT EXECUTED | — | Long-horizon; needs Batch-2 primitives first | Deferred | Low |
| P24 endurance | NOT EXECUTED | — | 30+ runs; needs primitives first | Deferred | Low |
| P25 scheduling | PASS | — | None | No | — |
| P26 report/UI | NOT EXECUTED | — | Cosmetic truthfulness | Folded into B2-5 evidence | Low |
| P27/P28 prod integrity + convergence | Procedural per campaign | — | None (live-session duty) | No | — |
| T1 RC4–7 | INVALID (unreachable) | Preserved by decision | None (do not manufacture) | No | — |
| T2 Option-B | PASS | Covered | Revalidation only | No | — |
| T3A size-blind | INVALID by design | H3 documents | None | No | — |
| T3B verify-kill | PASS | H2 | None | No | — |
| T4 LAN audit | PASS + findings → remediation | H4 | Revalidation only | No | — |
| Phase 5 LAN network chaos | Gate DONE, scenarios pending | — | Share removal → PARTIAL(11) | Yes (B2-1 companion leg) | HIGH |
| Phase 6 T04/A1 kill matrix | NOT EXECUTED (HIGHEST PRIORITY) | H1 (single shape) | Multi-timing × mechanism matrix | Yes (B2-1) | HIGHEST |
| Phase 7 lineage/orphan | NOT EXECUTED | — | Complex, lower contract value per cost | Deferred | Low |
| H5 persistence fault | BLOCKED (blast radius) | Blocked | Unchanged | No (STEP 10) | Blocked |

## Batch 2 selection (exactly five)

- B2-1: T04/A1 kill-timing matrix + LAN network-chaos leg (phases 6+5)
- B2-2: C-DK-001 matrix completion — main + sync-only + control (phase 8; H2 is the verify-only leg)
- B2-3: Live LAN audit divergence-class matrix (phase 13-part2; extends H4)
- B2-4: Sharded audit scope isolation (phase 16)
- B2-5: Integrity state-model observability (phase 17 + P26 fold-in)

Deferred (not coverage-worthy yet): P15, P19–P24, phase 7, H5-blocked.
Invalid/by-design: T1, T3A. Adequate: P0–P12, P13-part1, P25, T2/T3B/T4, H1–H5 shapes.
