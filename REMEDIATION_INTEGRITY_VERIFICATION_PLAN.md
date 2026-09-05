# AAM Backup Agent — Integrity Verification Remediation Plan (Session 6B)

Planning only. No source, test, config, deployment, or production changes made.
Repository audited at `5fd209a` (working tree clean). Audit record:
`C:\ChaosTest\evidence\CHAOS_AUDIT_FINDINGS.md` (§10.4–§10.17, §11.1–§11.17).
P2 cancellation is OUT of scope (unsupported API edge case, documented §25);
unexpected process termination/crash remains IN scope as an attack vector.

## 1. Executive Summary

COMPLETE is declared from process exit codes, not destination evidence.
Two reproduced HIGH false-success classes (T04/A1 LAN kill → `LAN_COMPLETE`;
C-DK-001 cloud double-kill → `CLOUD_COMPLETE` + `verified=True` over 0.035%
of source) share one root: no integrity gate between transfer and manifest
success. Minimum fix preserving architecture: (a) cloud evidence gate over
already-collected diff/size data; (b) new LAN source↔dest reconciliation gate
+ targeted hash verification of affected files on suspicion; (c) hash-aware
cloud verification via existing rclone (read-only); (d) truthful terminal
states (PARTIAL/FAILED/VERIFY_FAILED never COMPLETED); (e) weekly read-only
full audit as a separate schedule. No schema/lock/Prefect-topology/engine
changes. No 400 GB post-backup hashing. No new dependencies.

## 2. Current Architecture

| Stage | Location | Behavior |
|---|---|---|
| Cloud sync | `flow.py:cloud_sync_task` (~L190–206) → `core/cloud_sync.py:run_cloud_sync` L169–281, `classify_rclone_exit` L53–81 | `subprocess.run(rclone sync…, timeout=21600)`; raises on CLOUD_FAILED (L204–205) |
| Cloud verify+report | `flow.py:cloud_verify_and_report_task` L210–267 → `core/cloud_verify.py` L23–99; `core/cloud_reporter.py` L34–244 | `rclone check --one-way --size-only` → `verified=(rc==0)` (`cloud_verify.py` L71); same task collects size/manifest/diff |
| F1 gate | `flow.py` L560–582 | boolean-only; diff/size ignored; labels swapped (L564–565) |
| LAN sync | `flow.py:lan_sync_task` L359–370 → `core/lan_sync.py:run_lan_sync` L255–380, `classify_exit_code` L123–164 | `subprocess.run(robocopy /MIR /Z /ZB /XJ /MT:4 /R:3 /W:10 /V /TS /FP /NJH /NDL /NP /LOG:tmp, timeout=14400)`; stdout/stderr DEVNULL (L296–301); log temp file deleted in `finally` (L375–380); raises on LAN_FAILED via task (L368–369) |
| LAN snapshots | `flow.py` L326–356 → `core/lan_manifest.py` L13–115 | DEST-only walks, DEST-vs-DEST diff (metric, never gate) |
| Run row | `flow.py:_record_run` L797–847 → `ManifestDB.insert_run` (upsert) | written in `finally` after gates; `last_successful_run` = `LIKE '%_COMPLETE'` (`manifest.py` L523) |
| Tail | `flow.py:backup` L946–1038 | exception → generic alert + ExceptionGroup → FAILED; clean return → COMPLETED |

## 3. Confirmed Findings

T04/A1/A1-matrix (HIGH): kills at 4.5–99.56% → exits 0/1 → `LAN_COMPLETE` +
COMPLETED, silent, NAS shut down. C-DK-001 (HIGH): double-kill → `CLOUD_COMPLETE`,
`verified=True`, `files_copied=0`, silent; diff (+4627) correct but unused.
C-V2-MED-001 (MEDIUM): `--size-only --one-way` blind to same-size content,
mtime, extras. C-F1INV-002 (LOW): swapped alert labels, fixture codifies it.
PARTIAL → COMPLETED (both legs; cloud-PARTIAL+verified also alert-silent).
Session-5 "LAN_FAILED → COMPLETED" claim REFUTED against repo@5fd209a
(task-level raises exist); locked in as regression test, no fix needed.

## 4. Root Cause

Common: terminal-status decision is a function of transfer exit codes
(+ one killable verify boolean), never of destination evidence.
Backend-specific: robocopy exits carry no kill information; rclone verify
boolean is killable; `--size-only` is content-blind; F1 consumes boolean
only. Downstream (rows, alerts, reports, UI) is correct-by-construction
given a truthful `status` — so the fix belongs entirely at the decision.

## 5. Correctness Invariant

```
COMPLETE = transfer completed + no unresolved transfer errors
         + required independent integrity verification passed
         + no verification uncertainty
         ⇒ manifest success may be recorded
```

COMPLETE is never `exit_code < 8`. Any uncertainty fails closed to
`*_VERIFY_FAILED` (or `*_FAILED`), alerted, Prefect FAILED.

## 6. Proposed Verification Architecture

```
NORMAL LAN:  robocopy → exit/counter triage → source↔dest reconcile
             (path+size+mtime±2s) → clean? COMPLETE : §11 abnormal path
ABNORMAL:    SUSPECT (never trust exit) → affected-file set (§10)
             → targeted SHA-256 verify → mismatch? VERIFY_FAILED
             (next scheduled incremental run heals; no in-run re-transfer)
CLOUD:       rclone sync → hash-aware check + diff/size evidence gate
             → VERIFY_FAILED on any gap/uncertainty
PERIODIC:    separate weekly read-only full audit → report only, never
             modifies data or rewrites COMPLETE
```

## 7. LAN Verification Design

- CURRENT: no source comparison; DEST-vs-DEST diff is metric-only.
- PROBLEM: nothing observes whether dest matches source.
- OPTIONS: (A) parse robocopy /LOG per-file lines; (B) independent
  source+dest walks with tolerant compare; (C) A+B.
- RECOMMENDATION: B as the gate; A rejected as gate (killed-run logs are
  truncated — precisely when trust matters most) but log tail is RETAINED
  on gate failure for forensics (today it is deleted unparsed on rc 0–3).
- REASON: filesystem truth is independent of the suspect process; O(n)
  stat-only; reuses proven `lan_manifest.py` mechanics.
- "Matching" definition: same relative path (existing `PureWindowsPath`
  normalization), `size` equal, `|mtime_src − mtime_dst| ≤ 2 s`.
  Tolerance rationale: NTFS (~100 ns) vs SMB/NAS rounding + NAS clock
  skew; epoch floats both sides → no TZ issue. Extras: report-only
  (/MIR converges; rc 4–7 PARTIAL path preserved). Walk failure modes:
  root failure → raise (existing H2); any subtree-error count > 0 on the
  POST walk, or post-walk None (G14), → VERIFY_FAILED (fail-closed;
  reverses current skip-and-record). Source-mutation mid-transfer →
  mismatch → VERIFY_FAILED (correct direction; next run converges).
  Reparse points/symlinks: /XJ already excludes junctions; hidden/system/
  long-path/Unicode handled by existing walk (no filter changes).
- IMPACT: one extra local source stat-walk (minutes); no transfer change.

## 8. Cloud Verification Design

- CURRENT: `check --one-way --size-only` (`cloud_verify.py` L45–57);
  diff via `check --combined --size-only` (`cloud_reporter.py` L163–173).
- PROBLEM: content-blind; gate ignores diff.
- OPTIONS: (A) keep `--size-only` + evidence gate; (B) drop `--size-only`
  every run (full hash compare); (C) A + bounded rotating hash sample.
- RECOMMENDATION: every-run gate = (A) evidence conjunction
  (`verified AND diff.added/modified empty AND not _partial AND
  manifest_error None AND size clean`) — kills C-DK-001 with zero new I/O.
  (B) rejected as every-run: GCS hashes are stored (free) but the SOURCE
  side must be re-read (~400 GB HDD ≈ 1–2 h) — full hash-every-run is
  adopted only for the weekly audit (§14), where the cost is scheduled.
  (C) rotating sample is the separate enhancement, not first remediation.
  Remove `--size-only` from the diff `--combined` invocation so gate
  evidence and diff share one comparison basis; keep `--one-way`
  (mirror policy; extras converge next sync).
- REASON: hash availability confirmed by backend type (GCS stores
  MD5/CRC32C; rclone `check` default is size+hash) — but source-read cost
  dominates on HDD, so full-hash belongs in the weekly window, not the
  daily path. Verification stays READ-ONLY (no `sync`, no `--delete`).
- IMPACT: zero new I/O on daily runs; weekly audit cost scheduled (§15).

## 9. Robocopy Result Handling

- CURRENT: launch `lan_sync.py` L296 (`subprocess.run`, DEVNULL, /LOG file);
  rc 0–3 log never parsed, deleted (L375–380); counters (`failed_file_count`,
  `count_failed_lines`) only on FAILED/bit3 branches.
- PROBLEM: exit code trusted; per-file evidence (`/V /TS /FP` verbose lines
  already emitted) discarded exactly when needed for forensics.
- OPTIONS: (A) deeper log parsing as gate; (B) independent reconcile (§7);
  (C) B + retain log on failure.
- RECOMMENDATION: C-minus-A: no new log parsing for decisions; change
  `finally` (L375–380) to retain the /LOG file when the integrity gate
  fails and record its path in `error_msg`. No command-flag changes.
- REASON: smallest change; kill-truncated logs can't be a correctness
  source, but are valuable forensics. Termination-state coverage via
  `subprocess.run` is sufficient (timeout → kill → `-1` → LAN_FAILED,
  already loud); no Popen/monitoring threads (out-of-scope pattern).

## 10. Changed/Affected File Identification

- CURRENT: available sources are DEST before/after walks, manifest rows,
  and the (discarded) robocopy log.
- PROBLEM: no source-grounded affected set.
- RECOMMENDATION: affected set = `{missing} ∪ {modified}` from the
  §7 source-vs-post comparison — computed from existing walk outputs,
  no full-dataset rescans beyond the two stat walks, no command change.
  It doubles as the targeted-verification input (§12) and the alert
  content (counts + first-N paths).
- IMPACT: O(n) in-memory set ops on dicts already built.

## 11. Abnormal-Termination Handling

Triggers (any): timeout/−1, rc≥16, gate mismatch, walk errors,
`_partial` evidence, suspicious rc 4–7 with unresolved anomaly.
Behavior: status SUSPECT → NEVER COMPLETE → identify affected set (§10)
→ targeted SHA-256 verify of affected files (source vs dest reads,
bounded by affected count) → any mismatch/unreadable → `LAN_VERIFY_FAILED`
+ alert + raise (no NAS shutdown). NO in-run re-transfer: robocopy has no
safe file-list mode and a 400 GB re-mirror inside the run risks the
schedule; the next scheduled incremental run heals (proven behavior).
Matches NORMAL/CLOUD asymmetry honestly: cloud heals via next `sync`;
LAN heals via next `/MIR`.

## 12. Content Verification Strategy

| Level | Guarantee | Same-size detection | Cost (dual-core/HDD, ~400 GB PDFs/XLS) |
|---|---|---|---|
| A. size+mtime±2s (every LAN run) | kill/truncation/missing (A1a caught via unset/mismatched mtime+size) | NO (V2 residual) | stat-only, minutes; no content I/O |
| B. A + targeted SHA-256 of affected set (abnormal runs) | deterministic for suspect files | YES for affected files | bounded by suspect count (KB–GB, sequential) |
| C. full-dataset hash (weekly audit only) | full deterministic | YES | ~1–1.5 h sequential re-read, scheduled |

Minimum robust model: normal = A; abnormal = A + B; periodic = C.
Rotating per-run sampling beyond B is the deferred enhancement.

## 13. Hash Algorithm Decision

- CURRENT: no content hashing anywhere (stdlib `hashlib` available, no new deps).
- OPTIONS: `hashlib.sha256` vs BLAKE3 (new dependency) vs rclone hashes (cloud only).
- RECOMMENDATION: SHA-256 via stdlib for LAN targeted/audit hashing; rclone-native hashes for cloud.
- REASON: bottleneck on this hardware is sequential HDD I/O (~80–120 MB/s),
  not hash throughput — BLAKE3's CPU advantage is unobservable behind disk;
  adding a native dependency for no measured gain violates the minimal-change
  rule. Chunked streaming reads (e.g. 1–8 MB) keep RAM flat on DDR3.
  Adopt BLAKE3 only on measured evidence of CPU-boundedness (unlikely).
- IMPACT: zero dependency/maintenance cost.

## 14. Rclone Role (justified)

1. Cloud transfer engine — keep (existing).
2. Cloud every-run verification — keep `check`, evidence-gated (§8), read-only.
3. Periodic full cloud audit — YES: hash-aware `rclone check` (no `--size-only`),
   `--checkers 1`, read-only, separate Prefect schedule (weekly low-load
   window), report-only (own audit artifact + discrepancy alert; never
   rewrites backup COMPLETE). Benchmark on representative data first; if the
   window is exceeded, shard by prefix across weeks (batched/incremental).
4. Targeted LAN verification — NO: native Python (`os.walk`/`os.stat`/
   `hashlib`) is one mechanism, zero extra processes (no added kill surface),
   and avoids rclone local→SMB path/hash quirks.
5. Periodic LAN audit — native Python full walk + full SHA-256 stream,
   same schedule family as (3), same report-only contract.

## 15. Resource/Performance Model (dual-core Xeon, HDD, DDR3, ~400 GB)

- Normal LAN run delta: +1 local source stat-walk (minutes, sequential,
  negligible vs multi-GB SMB transfer) + in-memory compare. No new network.
- Abnormal LAN run delta: + bounded affected-file hash reads (sequential).
- Cloud daily delta: zero new I/O (evidence already fetched).
- Weekly audit: cloud `check` hash-aware ≈ listings + source re-read if
  hashes uncached (~1–2 h worst case, `--checkers 1` to protect the box);
  LAN native full walk+hash ≈ 1–1.5 h sequential. Scheduled off-peak,
  single worker, no concurrency increase (HDD seeks punish parallelism).
  Benchmark gates adoption; fallback is prefix-sharded batches.

## 16. Manifest/State Semantics

- `*_COMPLETE` rows are written only from the post-gate success path;
  gates raise before `return`, so `finally→_record_run` persists the
  failure status. New `LAN_VERIFY_FAILED` needs no schema change (text
  status; `LIKE '%_COMPLETE'` in `last_successful_run` already excludes
  it, as do reports/UI PARTIAL/FAILED branches).
- File-rows-before-gate ordering is KEPT (rows describe live dest truth;
  run row is authoritative; self-healing via prune).
- Prefect COMPLETED ⟺ row `*_COMPLETE`/`*_NO_CHANGES_COMPLETE` (Change 4
  terminal policy: PARTIAL raises via dedicated `PartialRun`, generic
  alert suppressed for it to avoid double-alert; FY rollover unaffected —
  calls `run_lan_sync` directly).

## 17. Exact Files to Modify

1. `flow.py` — F1 evidence conjunction (L560–582) + `verify_liveness` in
   `extended_metrics` (L620–628; pass through `exit_code` in
   `cloud_verify_and_report_task` return L261–266); LAN source snapshot +
   gate after `lan_record_task` (L704–726) + G14 fail-closed (L727–733);
   F1 label swap (L564–565); terminal policy blocks (cloud pre-post,
   LAN pre-return L766) + `PartialRun` + tail generic-alert skip.
2. `core/lan_manifest.py` — add `walk_lan_source` (mirror of
   `walk_lan_destination` L13–77) + tolerant `compare_source_dest`
   (missing/modified, mtime ±2 s; extras reported, not gated).
3. `core/lan_sync.py` — retain /LOG file on gate failure (change
   `finally` L375–380; path plumbed via result dict).
4. `core/cloud_reporter.py` — drop `--size-only` from diff `check`
   (L163–173 basis alignment).
5. New small module `core/integrity.py` — affected-set hash verifier
   (streaming SHA-256) + audit-runner shared by LAN/cloud periodic jobs.
   Justification: single home for verify primitives; alternatives
   (scattering into flow/tasks) duplicate failure semantics.
6. `serve.py`/`launch.py` — register weekly read-only audit deployment(s)
   (schedule + `--checkers 1` config surface in `models/config.py` only
   if a new timeout/tuning knob proves necessary).

## 18. Exact Functions to Modify

`flow._run_cloud_pipeline` (gate, metrics, policy); `flow._run_lan_pipeline`
(snapshots, gate, G14, F3 interplay, policy); `flow.cloud_verify_and_report_task`
(exit_code passthrough); `flow.backup` (PartialRun alert skip);
`lan_manifest.walk_lan_destination` (untouched — mirrored, not changed);
`lan_sync.run_lan_sync` (log retention); `cloud_reporter.get_cloud_diff`
(flag alignment); new `integrity.verify_files` / `integrity.audit_dataset`.

## 19. Files NOT to Modify

`core/process.py` (lock), `core/manifest.py` schema, transfer command
builders/flags (except log-retention plumbing), Prefect topology besides
audit schedule registration, `watchdog.py`, `core/report.py`
classification, `config.yaml`/deployments/schedules (except new audit
entry), production (`C:\AAMBackup`, `C:\BackupAgent`). Cancellation
machinery: nothing (out of scope).

## 20. Required Tests

- F1 fixture correction (`tests/test_flow_status_semantics.py` L65–81).
- New: cloud gate (added/modified/_partial/manifest_error/size_error →
  VERIFY_FAILED; clean → COMPLETE); LAN gate (missing/modified →
  VERIFY_FAILED + no shutdown; ≤2 s skew → COMPLETE; after=None →
  VERIFY_FAILED); tolerance unit tests; targeted-hash verifier tests;
  `PartialRun` single-alert + FAILED tests; hard-fail lock-in tests.
- Audit job tests: read-only (no dest mtime/content change), discrepancy
  report content, runtime/resource logging, sharding correctness.

## 21. Existing Chaos Regression Matrix

RT-1…RT-12 preserved (§11.15). New integrity tests supplement, never
replace: RT-1 kill matrix, RT-2 double-kill, RT-3 verify-kill control,
RT-4 same-size (sample/audit detection or declared residual), RT-5
replaced by documented unsupported-cancel note (no implementation),
RT-6/7 crash windows, RT-8 labels, RT-9 live-lock, RT-10 PARTIAL console,
RT-11 hard LAN fail, RT-12 cloud label truthfulness.

## 22. Post-Implementation Reattack Plan

Rerun in `C:\ChaosTest` only: T04/A1 matrix (taskkill+WMI @ ~5/16/54/99.5%);
C-DK-001 verbatim; single verify-kill control; v4b same-size mutation;
forced rc≥16; corrupt-key cloud. Required: NO false COMPLETE, alerts fire,
no shutdown on failed LAN. Expectations must not be edited to pass —
invariant (§5) governs. First weekly-audit benchmark on representative
data decides full-vs-sharded cadence.

## 23. Rollback Plan

Per-change `git revert` (audit job → sample/verifier → policy → LAN gate
→ cloud gate → labels). No migrations; new-status rows remain valid under
old code. Production redeploy of prior `C:\AAMBackup` build only after
validation; no runtime-state cleanup required.

## 24. Acceptance Criteria

1. No termination attack yields `*_COMPLETE` or COMPLETED; all alert.
2. COMPLETED ⟺ `*_COMPLETE`/`*_NO_CHANGES_COMPLETE` row.
3. F1 labels directionally correct.
4. Same-size corruption detected by hash-aware paths (cloud check without
   `--size-only` where scheduled; LAN targeted/audit hashing) or explicitly
   declared residual with detection owner (weekly audit).
5. Weekly audit modifies zero bytes (verified by pre/post inventory).
6. Full suite + new tests green.

## 25. Risks

NAS timestamp/clock behavior diverging beyond 2 s (fail-closed → noisy
VERIFY_FAILED; tunable, alert explains); source-mutation races (fail loud,
heal next run); weekly audit overrunning its window (benchmark → shard);
PARTIAL→FAILED console/alert pattern change (intended, needs operator
acknowledgment); P2 retained as documented unsupported edge.

## 26. Unresolved Questions

1. Weekly audit window approval (day/time, low-load confirmation) and
   full-vs-sharded decision pending benchmark numbers.
2. Hash-sample scope if later adopted (cap in GB/files per run).
3. NAS timestamp granularity validation on the actual share (confirm 2 s).
4. PARTIAL→FAILED operator acknowledgment (console semantics change).

## 27. Final Recommended Implementation Order

1. F1 labels + fixture → 2. cloud evidence gate + liveness metric →
2. LAN source-walk gate + G14 fail-closed + log retention → 4. terminal
   policy (`PartialRun`) → 5. `core/integrity.py` targeted verifier +
   abnormal path → 6. cloud diff flag alignment → 7. weekly audit job +
   benchmark (full vs sharded) → 8. gate/policy/targeted tests →
3. chaos reattack (§22) → 10. production validation + deploy.
