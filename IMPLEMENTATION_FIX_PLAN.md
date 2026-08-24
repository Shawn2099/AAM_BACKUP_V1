# AAM Backup — Remediation Implementation Plan
**Version:** 1.0 · **Date:** 2026-08-24 · **Status:** DRAFT — awaiting operator approval
**Source evidence:** `docs/SCENARIO_TEST_REPORT.md` (148 scenarios executed), `CODE_AUDIT.md`,
this session's live incidents. **Zero program changes are made until this document is approved.**

---

## Guiding rules (apply to every fix)

| Rule | Meaning |
|---|---|
| R1 Evidence-first | Every fix starts from a ledger row / file:line cited here. No speculative fixes. |
| R2 RED→GREEN | Write/extend a failing test from the existing scenario suite BEFORE the code change. |
| R3 Smallest correct diff | Prefer deletion or stdlib over new machinery. No new dependencies unless named below. |
| R4 One concern per commit | Commit per item ID; full scenario suite must stay green after each. |
| R5 No behavior drift | Catalog contracts stay intact except where a catalog CORRECTION is explicitly listed (C-x items). |
| R6 Rollback | Each item is independently revertible (`git revert <sha>`); DB/GCS/data steps have their own backup step. |

---

## PHASE 0 — Incident containment, forensics, recovery (DATA FIRST)
**Severity: CRITICAL · No code changes in this phase.**

### 0.1 Confirmed contamination of production source folder  `P0-DATA`
Evidence: `C:\BackupData\FY26-27` currently contains ONLY `E2E_TEST_FY\`
(locked_document.txt, test_large.bin 5 MB, test_normal.txt, SubFolder\nested.txt,
.AAM_TARGET_MOUNTED — all created 08-23 **13:25**). GCS `FY26-27` mirrors these 5 objects;
tonight's 22:00 nightly "successfully" copied them (files_copied=5).

Root cause identified (writer): `tests/test_e2e_real_hardware.py::_get_test_paths()` lines 84-103:
```python
source_root = Path(config.paths.source_drive)   # PRODUCTION folder
test_source = source_root / "E2E_TEST_FY"       # sandbox INSIDE production tree
```
Every e2e test (test_1..test_6) then writes fixtures into the production tree and
LAN-syncs them onward. Original client files that lived in `FY26-27` root are no
longer present there.

Tasks:
- [ ] 0.1a Forensics (read-only): grep repo history/tests for the pre-incident file
      inventory recorded in `CODE_AUDIT.md`; search all local drives + Recycle Bin
      for those filenames/sizes; check Prefect flow-run records around 08-23 13:25
      to enumerate every writer.
- [ ] 0.1b Classify: real data loss vs demo-placeholder-only (config.yaml calls the
      path PLACEHOLDER). Operator signs off on the classification.
- [ ] 0.1c Recovery attempt for any confirmed real files (disk search / older copies).
      If unrecoverable → written loss declaration, signed by operator.
- [ ] 0.1d Restoration: rebuild `C:\BackupData\FY26-27` to the agreed known-good set,
      then ONE supervised `rclone sync` to bring GCS `FY26-27` back in line.
      Delete stray `gs://…/FY25-26/closing_ledger.xlsx` left by the FY-08 first attempt.
- [ ] 0.1e Permanent guard (code, small): `_get_test_paths()` moves sandboxes to a
      sibling directory (same convention as `tests/e2e_helpers.py`:
      `<parent-of-prod>\E2E_TEST_SOURCE` style) **plus** a hard assertion:
      `assert prod_root not in test_root.parents and test_root != prod_root`.
      Same guard added to `tests/e2e_helpers.py` clean/create helpers.
      RED test: unit test that instantiates the helper against a config whose
      source_drive is a sentinel dir and asserts created paths are outside it.

Exit criteria: prod source + GCS mirror restored to operator-signed state; guard test green;
e2e suite re-run green with sandboxes outside production tree.

---

## PHASE 1 — Correctness foundations (small, high-leverage code fixes)

### 1.1 Unify HealthError hierarchy  `P1-EXC` (HIGH)
Evidence: `core/health.py:14` and `core/lan_preflight.py:15` both define
`class HealthError`. Consequence seen live: name rebinding/mismatch made LAN‑03's
except-matching fragile; callers catching one variant silently miss the other.
Fix (best practice — single exception per domain): delete the duplicate class in
`lan_preflight.py`, import from `core.health`. Update the two internal raise sites.
RED: new unit test `test_healtherror_is_single_class` asserting
`core.health.HealthError is core.lan_preflight.HealthError`.
Regression sweep: `grep -rn "lan_preflight import HealthError"` → only canonical import remains.

### 1.2 Exit-9 silent-success trap (classifier)  `P1-EXIT9` (CRITICAL)
Evidence: CLOUD‑06/CLOUD‑07 rows — nonexistent bucket → exit 9 mapped to
`CLOUD_NO_CHANGES_COMPLETE` although stderr carries fatal errors.
Fix (no reinvention — use data rclone already emits): `run_cloud_sync` already
captures rclone's JSON log stream. Track `max_log_level` while reading stderr;
classification becomes:
```
exit 9 AND max_level <= INFO  -> CLOUD_NO_CHANGES_COMPLETE
exit 9 AND any ERROR/CRITICAL -> CLOUD_FAILED (error tail kept)
```
RED: extend CLOUD‑06 matrix case (c) to assert `CLOUD_FAILED` for typo-bucket.
Catalog correction C-1 recorded (catalog expected PARTIAL+anomaly for orphans —
already superseded; now fully aligned).

### 1.3 Robocopy failed-file counter blindness (/NJS)  `P1-COUNT` (HIGH)
Evidence: LAN‑06/LAN‑15 rows — real copy failures report `files_failed=0`;
`** FAILED:` markers never reach the tail because `/NJS` suppresses the summary.
Fix (choose ONE at approval — default A):
- **A (preferred): drop `/NJS`, keep `/NJH`.** Summary section returns; extend
  `count_failed_lines()` to also read `Files : ... Failed N` summary line.
  Standard robocopy parsing, no heuristics on body text.
- B: keep flags; parse body `ERROR <code>` line pairs. More brittle across locales.
RED: LAN‑15 assertion upgraded to `files_failed == 2`.
Note: dropping /NJS enlarges captured log size (~few KB) — acceptable; log rotation
already configured.

### 1.4 Watchdog/WoL narrow exception scopes  `P1-WOL` (MEDIUM)
Evidence: WOL‑08 (`OverflowError` escapes `_smb_port_open`),
WOL‑09 (`ValueError` escapes `_send_magic_packet` warn-and-continue net).
Fix: widen catches to `(OSError, ValueError, OverflowError)` at exactly the two
documented sites; docstrings updated to match reality.
RED: WOL‑08/09 scenarios flip ANOMALY-RECORDED → PASS with same probes.

### 1.5 Machine-facing string hygiene  `P1-TEXT` (LOW)
Evidence: four U+2014 em-dash sightings in log/error strings
(report.py:120, cloud_verify error, flow PARTIAL msg, health clock msg).
Fix: replace em-dashes with ASCII `-` **only in log messages and error strings**
(UI HTML keeps typography). Add a CI-greppable unit test scanning
`core/*.py` logger/error literals for `\u2014|\u2013` (RED first).

---

## PHASE 2 — Orchestration honesty (scheduler/deployments)

### 2.1 Don't schedule disabled legs  `P2-SCHED` (HIGH)
Evidence: last night `backup-lan` fired 21:00, flow COMPLETED while doing NOTHING
(`lan.enabled=false`) — zero DB trace. Misleading success family.
Fix (smallest correct): in `serve._deployments()` filter deployment creation on the
matching enabled flag (`lan.enabled`, `cloud.enabled`). Weekly/monthly/rollover
always registered. Flow-level guard stays as defense-in-depth.
RED: SCH‑01 extension — with a disabled-leg config clone, `_deployments()`
returns no backup-lan deployment.
Docs: DEPLOYMENT_GUIDE §scheduler gains one sentence.

### 2.2 Concurrency slot coverage for direct pipeline entry  `P2-CONC` (OPTIONAL — needs decision)
Today the slot wraps the `backup` flow; pipelines called by tests/scripts bypass it.
Proposed (only if approved): move `concurrency("aam-backup", occupy=1, …)` from the
flow body into the top of `_run_lan_pipeline`/`_run_cloud_pipeline` (single place both
production flow and any caller pass through). Lock acquisition moves with it unchanged.
Risk note: pipeline-level slot changes re-entrancy semantics — LAN-19-style scenarios
rerun to confirm no deadlock (slot is per-global-limit, flows still serialize).

---

## PHASE 3 — Deployment prerequisite (environment, not code)

### 3.1 `CLOUDSDK_PYTHON`  `P3-GCP` (HIGH ops)
Evidence: REP/FY‑10 live failure — bundled gcloud.cmd needs Python; without the env
var every ARCHIVE transition silently no-ops.
Fix: `deploy/07_configure_gcloud.ps1` (new, idempotent):
detect venv python → `[Environment]::SetEnvironmentVariable('CLOUDSDK_PYTHON', py, 'Machine')`;
guide §archive gains the same instruction. Verify via FY‑10 rerun on a box WITHOUT
manual env (simulate by clearing process var in the scenario setup).
RED: FY‑10 currently passes only because the test sets the var; add SYS‑05 scenario
asserting machine-scope variable exists post-install-script.

---

## PHASE 4 — Test-suite hygiene (enables trustworthy future runs)

### 4.1 Ledger sid collision  `P4-SID` — RT‑01 and Branch‑A both write rows tagged `LAN-03`.
Fix: scenario_support.record_op prefixes sids from scenario files with their branch tag
(`A/LAN-03`) OR RT files switch to `RT/LAN-03`. Purely additive.
### 4.2 Remove debug scaffolding left in Branch A LAN‑03 (marker/tb dumps), keep the
stray-import removal (that was a REAL test-bug fix — see P0/P4 notes).
### 4.3 Append-tooling rule codified: single builder script writes whole files;
append-mode only. (Already followed manually since Batch 21.)
### 4.4 Catalog corrections folded into `SCENARIO_CATALOG_V3.md`:
C-1 fresh-mirror exit=3 (+extras bit); C-2 orphan purge → COMPLETE (no anomaly);
C-3 verifier granularity (count-level, not per-file); C-4 nightly-with-disabled-leg
skips leg (post 2.1 deployments unregister instead).

---

## Execution order & gates

```
APPROVAL ──► PHASE 0 (data forensics/recovery, no code)
              │  gate G0: operator signs restoration manifest
              ▼
            PHASE 1 (1.1 → 1.2 → 1.3(A|B) → 1.4 → 1.5)   [full suite green per item]
              │  gate G1
              ▼
            PHASE 2 (2.1; 2.2 only if approved)
              │  gate G2
              ▼
            PHASE 3 (deploy script + guide)
              │  gate G3
              ▼
            PHASE 4 (hygiene + catalog v3)
              │  FINAL: full catalog re-run (expect ≥ previous 146-pass baseline,
              │         with upgraded assertions from 1.2/1.3/1.4 now stricter)
              ▼
            30-day nightly observation note appended to ledger
```

## Effort estimate

| Phase | Items | Est. effort | Risk |
|---|---|---|---|
| 0 | 5 tasks | 0.5–1 day (forensics-bound) | Low (read-only until restore step) |
| 1 | 5 fixes | ~4–6 h coding + suite runs | Low-Med (classifier/parser touches) |
| 2 | 1 + 1 optional | 1–2 h | Med only for 2.2 |
| 3 | 1 script + docs | 1 h | None |
| 4 | hygiene + docs | 1–2 h | None |

## Open decisions required from operator (blocking items marked ⛔)

1. ⛔ Approve this plan / pick starting phase.
2. ⛔ P1-COUNT remedy: **A** drop `/NJS` (recommended) or **B** parse ERROR body lines.
3. ⛔ P2-CONC: move concurrency slot into pipelines — yes/no.
4. ⛔ Phase 0.1b: if forensics concludes the pre-incident `FY26-27` content was
   demo-placeholder only, confirm we may recreate a representative sample set
   instead of hunting originals further.

---

# v1.1 PRE-MORTEM REVISIONS (reliability/stability first, performance second)

Per operator request, every item was stress-tested for second-order failures.
Revised decisions below supersede v1.0 where they conflict.

## P0-DATA � hardened
Risks identified:
 a) Path guard bypassed by case differences / junctions / symlinks.
 b) Restoration sync racing concurrent test or nightly activity.
 c) GCS mutations irreversible -> restoration mistakes become permanent.
 d) Original-file recovery fishing without an authoritative inventory.
Hardened design:
 1. Guard helper assert_sandbox_safe(path) in tests/e2e_helpers.py:
    compares Path(x).resolve().casefold() BOTH directions
    (test-inside-prod AND prod-inside-test); used by _get_test_paths()
    AND every e2e create/clean helper. RED unit test w/ sentinel dirs,
    mixed-case + junction cases.
 2. All GCS restore operations performed while holding the production
    concurrency slot (or verified-quiet window) and preceded by a full
    bucket listing snapshot saved to docs/phase0_gcs_before.json.
 3. Restore uses EXPLICIT object operations (deletefile per known-bad key,
    upload per agreed sample file) - never a blanket purge - so every step
    is reversible from the snapshots.
 4. Recovery confidence gated by CODE_AUDIT.md inventory: if the doc does
    not enumerate original filenames/sizes, recovery confidence is LOW and
    we stop after disk+VSS checks (vssadmin list shadows; bucket versioning
    check) instead of open-ended hunting.

## P1-EXC � upgraded severity CRITICAL + re-export strategy
New finding while reviewing: flow.py imports pre_backup_health but NEVER
imports/HealthError-catches around pipeline calls. Today that is survivable
(F2 records failure) but it means exception IDENTITY was never load-bearing -
which is exactly why the duplicate went unnoticed. After unification, add a
regression test: simulate dry-run raise through _run_lan_pipeline's except
path and assert it is recognized as HealthError.
Strategy: core.health keeps THE class; lan_preflight does
`from core.health import HealthError` (re-export). Zero call-site churn;
any external importer of either path keeps working.

## P1-COUNT � decision REVISED to A-prime (positional summary parse)
Stability analysis:
 - Plain A (parse English word FAILED) breaks on localized Windows.
 - B (body ERROR pairing) brittle across robocopy versions.
A-prime (chosen): remove /NJS (keep /NJH removed too so stats block prints),
parse the 'Files:' stats ROW positionally - columns are fixed by robocopy
(Total Copied Skipped Mismatch FAILED Extras) regardless of locale labels;
take index 4. Floor with exit-bitmask: bit3 set => at least 1.
RED: LAN-15 asserts files_failed == 2; LAN-06 asserts >= 1.
Side effects logged: summary adds ~1-2KB per run log (rotation exists);
catalog v3 captures new expected values.

## P1-WOL � revised to broad-but-loud catches
Tuple-enumeration ages badly. Final: catch Exception at exactly the two inner
sites, LOG type+args, continue rounds / return False.
KeyboardInterrupt/SystemExit still propagate (Exception excludes them).
_plus_ explicit port clamp guard in _smb_port_open
(0<=port<=65535 else return False) - WOL-08 then expects False, not raise.

## P1-TEXT � scanner scoped to AST string literals
Raw-line scan false-positives on docstrings (full of em-dashes by design).
Final: ast.walk over core/*.py collecting Constant-str nodes passed to
logger.* calls or used in raised exceptions; flag U+2014/U+2013 there.
Module/class/function DOCSTRINGS excluded. RED scanner test first.

## P2-SCHED � EXPANDED: stale deployments must be deleted
Critical gap found: filtering registration is NOT enough. Prefect's server-
side scheduler keeps firing previously-registered deployments even when a
later serve() omits them. Final design (declarative desired-state):
 1. Build desired set from enabled flags (existing plan).
 2. After serve()-registration, ensure-absent step deletes any
    'aam-backup/backup-lan' deployment when lan disabled (and vice versa)
    via client.delete_deployment_by_name - idempotent, logged.
RED scenario: disabled-leg clone -> registration routine -> live server
asserts deployment absent (and stays absent across a second boot).
Doc note: toggling enabled requires agent restart (deployment registration
happens at boot) - added to guide.

## P2-CONC � approved-shape revision (was optional)
Moving the slot DOWN is correct ONLY IF the flow-level slot is removed in the
same commit - otherwise global limit=1 self-deadlocks up to 3600s.
Final shape:
 1. DELETE with-concurrency block from flow.backup().
 2. ADD identical slot at top of each pipeline, wait timeout sourced from
    config (new key maintenance.concurrency_wait_seconds, default 3600)
    so tests can pass a small value instead of hanging.
 3. NEW scenario SCH-16: two threads calling pipelines directly -> second
    waits (serialization now covers manual/direct entry, closing tonight's
    observed gap where tests bypassed the slot).
Risk accepted: direct-callers (tests) may queue behind a real nightly.

## P3-GCP � add service-restart maintenance step
Machine-scope env vars are only inherited by services AFTER restart.
Script gains final step: restart AamBackupAgent/AamWatchdog/AamPrefectServer
(or schedule with operator). SYS-05 scenario asserts Machine-scope value.

## PHASE 4 additions
 - SYS-05 scenario (P3 verification).
 - record_op sid prefixing (P4-SID) implementation detail: prefix comes from
   a module-level BRANCH_TAG constant per scen file (A..J/K) - zero ambiguity
   vs RT rows.
 - LAN-03 restored to canonical pytest.raises form once 1.1 lands (identity
   guaranteed), probes removed.

## UPDATED OPEN DECISIONS (blocking)
 D1 Approve v1.1 revisions wholesale (recommended) or item-by-item.
 D2 P1-COUNT: confirm A-prime positional parse (stability-first choice).
 D3 P2-SCHED destructive step: authorize deletion of stale disabled-leg
    deployments on the LIVE server during rollout window.
 D4 P0 restore preference once forensics reports: exact-original recovery
    attempt vs agreed-sample-set rebuild.

---

# v1.2 RESEARCH ADDENDUM (internet deep-scan, 2026-08-24)
Sources consulted: rclone.org/docs (exit codes 0-10, --error-on-no-transfer,
--max-duration/--cutoff-mode/--timeout), rclone forum maintainer threads,
Starlette/FastAPI session-security references incl. OWASP mapping
(FASTAPI-SESS-001/002, FASTAPI-CSRF-001), sqlite.org + durability analyses
(WAL x synchronous matrix), installed-client capability probe
(pause_deployment/resume_deployment available; delete_deployment by id only;
paused field present). Findings mapped to plan items:

## R1  P1-EXIT9  -> CONFIRMED + hardened
rclone docs officially warn --error-on-no-transfer "turns a usually non-fatal
error into a potentially fatal one - check your scripts". Exit 9 documented as
"successful, but no files transferred". Our cross-check design is exactly the
recommended care. HARDENING: rclone known quirk - some FATAL errors are logged
OUTSIDE the JSON logger (github issue #6038, log.Fatalf paths). Therefore the
classifier must treat ANY non-JSON-parseable line containing 'Failed to' /
'NOTICE: Failed' as an error signal too, not just level>=ERROR JSON fields.

## R2  NEW ADDITION C-A: in-rclone duration cap
Docs: --max-duration exits code 10 when reached; --timeout (IO idle, default
5m) breaks stalled transfers. Forum threads confirm interplay quirks with
--retries (retry loop can restart deadline) -> if adopted, ALSO pass explicit
--retries-sleep so a retry cannot reset the clock silently.
Proposal: cloud_sync gains optional config keys max_duration_seconds (default:
subprocess_timeout_seconds minus 300s margin) and passes
--max-duration + --cutoff-mode SOFT. Watchdog zombie cap remains outer layer.
Stability win: transfers self-terminate INSIDE rclone (graceful .partial
handling) instead of being hard-killed by subprocess timeout.

## R3  Sessions -> KEEP server-side store (research-backed)
Starlette SessionMiddleware requires itsdangerous (NOT installed) and stores
session data in SIGNED COOKIES readable by clients - violating the opaque-token
guidance (FASTAPI-SESS-002) we already follow with random token_hex(32) +
server-side dict. Adopted hardening from same references:
 - secure=True flag auto-enabled when dashboard served over TLS (new config
   key dashboard.tls_enabled, default false until reverse-proxy/TLS added).
 - CSRF posture documented: state-changing endpoints REQUIRE X-API-Key header
   (browsers never send it cross-site) - cookie-only auth will be REMOVED for
   POST routes in P2 rollout (small ui.py change + UI-03/04 test updates).

## R4  SQLite durability -> explicit NORMAL + optional FULL toggle
Research consensus (sqlite.org WAL page + durability analyses): WAL +
synchronous=NORMAL = no corruption risk, app-crash safe, possible loss of the
LAST commit(s) on power cut; FULL adds per-commit WAL fsync (durability).
Decision: ManifestDB._get_conn sets PRAGMA synchronous=NORMAL explicitly
(today it relies on defaults), plus new maintenance key
sqlite_synchronous: normal|full (default normal; operator may choose full).
NEW scenario DB-18 asserts both pragmas read back as configured.

## R5  P2-SCHED revision: PAUSE beats DELETE (installed client supports it)
Client probe: pause_deployment/resume_deployment exist AND Deployment.paused
field exists in this prefect version. Deleting destroys schedule/history;
pausing is reversible and stops server-side scheduling - strictly safer.
REVISED P2-SCHED: disabled leg -> pause_deployment(name=...); re-enabling ->
resume_deployment(). Deletion removed from plan. RED scenario updated to
assert paused=true after boot with lan.enabled=false.

## R6  Robocopy counter -> A-prime stands
No superior standard found; positional stats-row parsing is locale-independent
(column ORDER fixed by robocopy). Added safeguard: parse fails closed -
if summary block absent/unparseable, fall back to bit3 floor (>0 when bit3)
instead of 0.

## R7  NEW ADDITION C-B: SKIPPED legs become visible rows
Tonight LAN nightly COMPLETED-with-nothing left zero trace. After 2.1 pause
fix this disappears for DISABLED legs, but runtime skips (e.g., health-gate
refusals reaching flow-level) should still record a row:
insert_run(status='LAN_SKIPPED'|'CLOUD_SKIPPED', exit_code=0) inside the
pipelines' skip branches so reports/dashboard show WHY nothing ran.
UI-11 assertion extended (recent_runs may include Skipped display).

## R8  NEW ADDITION C-C: machine-readable ops log
loguru natively supports serialized sinks. Add a SECOND production sink
(logs/ops.jsonl, serialize=True, rotation same policy) alongside human log.
Kills every grep-trap finding permanently (em-dash family) and enables future
alerting on _etype-style structured fields.

## R9  NEW ADDITION C-D: manifest.db nightly safety copy
After purge/VACUUM task: sqlite backup API copy to
manifest_YYYYMMDD.db.keep N=7 (stdlib sqlite3.Connection.backup). Protects
run_history/file_entries against the exact contamination-class incidents this
campaign surfaced. NEW scenario DB-19 verifies rotation count.

## R10  Backlog (explicitly NOT scheduled now)
 - Secrets (smtp_password/api_key) at rest via Windows DPAPI - hardening only.
 - slowapi/limits library rate limiting - current tested implementation kept.
 - watchfiles event-driven config reload - TTL polling kept (zero deps).

Sources: rclone.org/docs#exit-codes|--error-on-no-transfer|--max-duration|
--cutoff-mode|--timeout ; forum.rclone.org threads 27738/14938/22723 ;
starlette.dev/middleware#SessionMiddleware ; OWASP FastAPI security reference
(FASTAPI-SESS/CSRF items) ; sqlite.org/pragma + WAL durability analyses.
