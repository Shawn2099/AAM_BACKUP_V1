# Chaos Harness Coverage Reconciliation (Batch 1 + Batch 2 vs complete catalog)

Read-only analysis. No code, deploy, or destructive action taken.
"43/43 harness tests PASS" = harness validation ONLY, not product chaos
validation. A dry-run is never counted as live coverage below.

Evidence authorities: `POST_REMEDIATION_CHAOS_AUDIT.md` (P0–P8 verdicts),
`BATCH_A/B_REPORT.md` (P9–P12 @c0d525b), `POST_F08_VALIDATION_REPORT.md`,
adversarial `FINAL_REPORT.md` + T-determinations (T1–T4 @7bf54fde),
`TEST_MATRIX.md` (phases 1–28 definitions; its "pending" flags are
STALE — superseded by the audit doc above).

## Master matrix

Test/Phase| Objective| Historical live evidence| Batch 1| Batch 2| Live execution status| Remediation-sensitive?| Contract coverage| Final classification| Remaining gap
P0 baseline| convergence | PASS (audit §P0) | — | — | valid live | No | 14 | 🟢 | none
P1 pytest suite| no new failures | PASS 1454P/3F-pre/40E-env | — | — | valid live | No | 12 | 🟢 | none
P2 locks L1–L12| live lock never deleted | PASS (audit §P2) | — | — | valid live | No (untouched) | 10 | 🟢 | none
P3 worker death| CRASHED never COMPLETE | PASS (audit §P3) | — | — | valid live | No | 1,5,10,12 | 🟢 | none
P4 service stop| boot-heal, no false COMPLETE | PASS (audit §P4) | — | — | valid live | No | 1,5,12,14 | 🟢 | none
P5 network chaos| outage → truthful PARTIAL | PASS (gate + scenarios) | — | B2-1 leg | valid live | No | 1,4,5 | 🟢 | re-run cheap, not required
P6 kill matrix 26/26| no killed run COMPLETE | PASS 26/26 (audit §P6) | H1 shape | B2-1 matrix | valid live | No (lan_sync change is comment-only) | 1,2,5,9,11,12 | 🟢 | B2-1 is regression, not first coverage
P7 lineage/orphan| no false row under overlap | PASS (audit §P7) | — | — | valid live | No | 1,5,10 | 🟢 | none
P8-main / sync-only| gate fails truthful | OK rows 119/120 | H2 pattern | B2-2 | valid live | Message text only (F-T3-2) | 1,3,5,11 | 🟢 (verdict) / 🟠 (message wording) | re-prove messages live
P8-verify-only (F-08)| killed verify must fail | FAIL→fixed@c0d525b→T3B PASS live | H2 | B2-2 | valid live | Message text only | 1,3,11 | 🟢 (verdict) / 🟠 (message) | same as above
P9 F1–F5| bounded truthful FAILED | PASS Batch A @c0d525b | — | — | valid live | No (fail pre-verify; gate text untouched there) | 1,5,10,12 | 🟢 | none
P10 V2a/b/c| blind daily + detecting audit | PASS Batch A | H3 (V2a) | — | valid live | V2b/c row detail+counters (F-T4-2/3) | 8,9,14 | 🟡 (V2a 🟢; V2b/c 🟠 message/counters) | light revalidation
P11 F1 mapping| gate truth table | PASS Batch B | — | — | valid live | No (decision unchanged) | 1,4,5 | 🟢 | none
P12 cloud audit| clean VERIFIED, read-only | PASS Batch B (8/8) | — | — | valid live | Row detail/counters (F-T4-2/3) | 6,7,9 | 🟠 | light revalidation
P13-part1 arg probes| exit-code contract | DONE (9 probes) | — | — | valid live | No | 2,3 | 🟢 | none
P13-part2 live LAN matrix| per-class verdicts | NOT EXECUTED | H4 (1 class) | B2-3 | harness-ready | YES (target of F-T4-1/2/3) | 1,6,7,9,13,14 | 🔵 | live run required
P14 cloud matrix| deployment-level classes | NOT EXECUTED | pattern only | B2-3 pattern | harness-ready | YES (same code) | 6,9,13 | 🔵 | live run required (lower pri than LAN)
P15 benchmark| relative numbers | NOT EXECUTED | — | — | none | No | — | 🔴 | low value; defer
P16 sharding| no false global VERIFIED | NOT EXECUTED | — | B2-4 | harness-ready | YES (audit code) | 4,6 | 🔵 | live run required
P17 state model| separation surfacing | NOT EXECUTED | — | B2-5 | harness-ready | YES (row shapes) | 6,7,13 | 🔵 | observational run required
P18 recovery| heal→VERIFIED | NOT EXECUTED | H4 T4-C pattern | B2-3 pattern | harness-ready | YES | 14 | 🔵 | folds into B2-3/T4-C live
P19 lock×audit| overlap semantics | NOT EXECUTED | — | — | none | Partial | 10 | 🔴 | needs semantics review; defer
P20 watchdog| no live-lock kill | NOT EXECUTED | — | — | none | No | 10 | 🔴 | needs watchdog-fault review; defer
P21 boot| boot-heal | NOT EXECUTED | — | — | none | No | 5,14 | 🔴 | service risk; defer
P22 xbackend| serialization | NOT EXECUTED (`ph22_xbackend.py` exists) | — | — | none | No | 10 | 🔴 | nondeterministic; defer
P23/P24 exhaustion/endurance| boundedness | NOT EXECUTED | — | — | none | No | 10,14 | 🔴 | needs Batch-1/2 primitives first; defer
P25 scheduling| no accidental audit | PASS 9/9 | — | — | valid live | No | 13 | 🟢 | none
P26 report/UI| truthful surfacing | NOT EXECUTED | — | B2-5 fold-in | harness-ready | YES | 13 | 🔵 | folds into B2-5
P27/P28 prod/convergence| procedural | per-campaign | — | — | live-duty | — | 16 | 🟡 | performed each live session
T1 RC4–7| live mismatch COMPLETE | INVALID (unreachable) | decision preserved | — | cannot execute | No | 2 | ⚫ | do not manufacture
T2 Option-B| default false | PASS live @7bf54fde | — | — | valid live | No (policy untouched) | 5,10,12 | 🟢 | cheap re-run in campaign
T3A size-blind| documents blindness | INVALID by design | H3 | — | by design | No | 8 | ⚫ | not a failure
T3B verify-kill| no unverified entries | PASS live | H2 | B2-2 | valid live | Message text (F-T3-2) | 1,3,11,15 | 🟢/🟠 | re-prove message
T4 LAN audit| detect+classify+counters | PASS + findings → remediation | H4 | B2-3 | superseded by remediation | YES (all F-T4-x) | 1,6,7,9,13,14 | 🟠 | full T4-A/B/C revalidation required
H1 robocopy kill| harness shape | dry-run only | H1 | B2-1 | NOT live via harness | No | 1,2,5 | 🔵 | live via B2-1
H2 verify kill| harness shape | dry-run only | H2 | B2-2 | NOT live via harness | Message text | 1,3,11 | 🔵 | live via B2-2/T3B
H3 same-size cloud| harness shape | dry-run only | H3 | — | NOT live via harness | No | 8 | 🔵 | live optional (design doc)
H4 divergence| harness shape | dry-run only | H4 | B2-3 | NOT live via harness | YES | 1,6,7,9 | 🔵 | live via T4
H5 persistence| BLOCKED | — | blocked | — | cannot execute | — | 15 | ⚫ | blast-radius review first

## Batch 1/2: what dry-run proves and does NOT prove

Each H/B2 scenario CAN exercise the listed real entrypoints via chaos
deployments + existing tools, with contract assertions on observed
state. Proven today: target resolution, safety rejection, evidence
packaging, fault engage/restore mechanics on scratch, registry
integrity. NOT proven: any live application behavior — no flow was
triggered, no fault engaged a live run, no verdict observed. The 43/43
result validates the instrument, not the product.

## Business-contract matrix

Rule| Tests covering it| Live proof?| Harness proof?| Gap
1 no false COMPLETE| P3/P4/P6/P8/T3B/T4 | yes (P6 26/26, T3B) | B2-1/B2-2 ready | remediation messages only
2 exit≠integrity proof| P6/P13-part1/T1-analysis | yes | B2-1 | none new
3 verify exit≠completion| F-08/T3B | yes live | H2/B2-2 | message re-proof
4 COMPLETE needs evidence| P11/T3B/gate suites | yes | B2-2 | none new
5 no silent promotion| P3/P4/P6/P8 | yes | B2-1/B2-2 | none new
6 backup≠integrity| P12/P17-shape/T4 | partial (P17 missing) | B2-5 | B2-5 live
7 audit never rewrites| P12/T4 | yes | B2-3/B2-5 | T4 revalidation (remediation)
8 daily resource-conscious| T3A/P10-V2a | yes by design | H3 | none
9 weekly independent RO| P12/T4 | yes | B2-3 | T4 revalidation
10 locks/concurrency| P2/P7 | yes | — | P19/P22 deferred (review first)
11 C-DK-001/T04-A1| P6/P8/T3B | yes | B2-1/B2-2 | message re-proof only
12 Prefect reflects outcome| P3/P4/P6/P8/T2/T3B | yes | all scenarios | none new
13 reports agree| P25/P12-partial | partial (P26 missing) | B2-5 | B2-5 live
14 recovery correctness| P4/T4-C pattern | partial (P18 missing) | B2-3/T4-C | fold into live T4-C
15 persistence failures| H5 only | no | none (blocked) | BLOCKED — sole red rule
16 prod untouched| every campaign | procedural | safety guards | ongoing duty

## Redundancy

- Unique: P6 matrix, P8-verify-only, T3B (no-entry proof), T4 (audit
  classification), B2-4 (scopes), B2-5 (state surfacing), H5 (if unblocked).
- Overlapping: H1⊂P6/B2-1; H2⊂T3B/B2-2; H3⊂P10-V2a; H4⊂T4/B2-3;
  P8-main⊂B2-2; P18⊂T4-C/B2-3; P26⊂B2-5.
- Redundant (keep as regression, never expand): P0/P1/P11/P13-part1/P25
  unit-equivalents; B2-2 control leg after first pass.
- Regression-only: remediation unit suites, F-08 suites, gate suites.
- Safety-only: prod checkpoints P27, safety tests.

## Remediation-sensitive (must rerun vs immutable commit)

T4-A/B/C (all F-T4-x + detail/counter formats), P12 + P10-V2b/c (row
detail/counters), T3B + P8 message wording (F-T3-2; verdict logic
already proven — assert text only). Historical evidence remains valid
for: P0–P7, P9, P10-V2a, P11, P13-part1, P25, T2, P8 verdicts, T3B
verdict logic, all unit suites. Do NOT rerun those for remediation
reasons (cheap opportunistic re-runs allowed, not required).

## NEXT LIVE CAMPAIGN (minimum)

1. T4-A canary-only VERIFIED (remediation target; needs commit; Qwen yes)
2. T4-B single-file divergence → content-mismatch + path + counters (needs commit; Qwen yes)
3. T4-C restore → successor VERIFIED + history intact (needs commit; Qwen yes)
4. B2-3 remaining classes (missing/extra/size/mtime) piggybacked on T4 rig time (needs commit; Qwen yes)
5. B2-4 shard clean/dirty pair (needs commit; Qwen yes)
6. B2-5 state-model queries after 1–5 (read-only; Qwen yes)
7. T3B message-accuracy re-proof (needs commit; Qwen yes; verdict already proven)
8. T2 default-false re-proof (needs commit; Qwen yes; cheap)
9. P12 + P10-V2b/c row-shape re-proof (needs commit; Qwen optional)
H5 stays BLOCKED. P14 follows B2-3 pattern only if rig time remains.

## Batch-3 decision: B — stop harness development and begin live execution

Coverage, not preference: every high-value contract gap is either
historically proven (P0–P12, T2/T3B), harness-ready with dry-run proof
(B2-1–B2-5, H1–H4), blocked (H5), invalid (T1/T3A), or deferred for
safety review (P19–P22). New code now would only duplicate live-proven
paths. The binding constraint is the immutable remediation commit +
live rig time, not tooling. Build nothing until the campaign above
reports; then target only empirically observed gaps.

## Totals

TOTAL CATALOG TESTS: 47 (28 phases + T1–T4 + H1–H5 + B2-1–B2-5 counted once; overlapping shapes noted, not double-counted as gaps)
VALID LIVE COVERAGE: 22 (P0–P7, P9–P12, P13-part1, P25, T2, T3B-verdict, P8-verdicts)
HARNESS-READY / NOT LIVE: 9 (H1–H4, B2-1–B2-5 minus overlaps → B2-1, B2-2, B2-3, B2-4, B2-5, H1–H4 shapes)
PARTIAL: 4 (P10-V2b/c, P12, T3B-message, P8-message)
REMEDIATION REVALIDATION REQUIRED: 7 (T4-A/B/C, P12, P10-V2b/c, T3B-msg, P8-msg)
NOT COVERED: 6 (P15, P19, P20, P21, P22, P23/P24)
BLOCKED / INVALID: 4 (H5, T1, T3A + P26-standalone folded)

BUSINESS-CONTRACT RULES: 16
FULLY COVERED: 11 (1,2,3,4,5,8,9,10,11,12,16 — live-historical)
PARTIALLY COVERED: 4 (6,7,13,14 — need B2-5/T4/P18 live legs)
NOT COVERED: 1 (15 — H5 blocked)

RECOMMENDED NEXT ACTION: B — stop harness development; commit remediation; execute the 9-item live campaign above.

Why: the instrument is proven (43/43 dry-run), the catalog is mapped,
and all remaining high-value unknowns require live application
behavior under the remediation commit — no further harness code can
answer them. Building Batch 3 now would optimize code volume over
coverage while the actual product verdicts wait on rig time.
