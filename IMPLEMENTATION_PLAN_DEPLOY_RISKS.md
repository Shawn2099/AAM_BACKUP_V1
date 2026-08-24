# AAM Backup — Deployment Risk Remediation Plan
**Version:** 1.1 · **Date:** 2026-08-24 · **Status:** DRAFT — awaiting operator approval
**v1.1 changelog (senior-dev review + research):** Phase 2 rewritten — sqlite3 legacy
transaction control does NOT cover DDL, so the migration needs explicit
`BEGIN IMMEDIATE`; migration-failure tests redesigned (sqlite3 C-type cannot be
monkeypatched — real lock-contention fixture instead). Phase 4 response contract
revised after reading `static/js/dashboard.js` (2-second polling; truthy-string
would freeze badges at "Running"). Phase 1 gained documented limitation: forced
restart targets `AamPrefectServer`, which does not kill a zombie rclone owned by
`AamBackupAgent`. Phase 3 gained optional `--max-delete` defense-in-depth
(rclone docs confirm sync deletes destination files; none passed today).
**Source evidence:** `CODE_AUDIT.md` (H3, H4, M8), `docs/audit/REVIEW_2026-06-02.md` (Critical 6),
this session's code verification sweep + GitNexus impact analysis.
**Zero program changes are made until this document is approved.**

---

## Guiding rules (inherited from IMPLEMENTATION_FIX_PLAN.md)

| Rule | Meaning |
|---|---|
| R1 Evidence-first | Every fix cites file:line verified in this session's sweep. |
| R2 RED→GREEN | Failing test written BEFORE the code change. |
| R3 Smallest correct diff | Prefer deletion/stdlib over new machinery. No new dependencies. |
| R4 One concern per commit | Commit per item ID; full suite stays green after each. |
| R5 No behavior drift | Pipeline contracts intact except where explicitly listed. |
| R6 Rollback | Each item independently revertible via `git revert <sha>`. |

## Impact-analysis warnings (GitNexus, 2026-08-24)

| Target symbol | Blast radius | Risk |
|---|---|---|
| `ManifestDB._get_conn` | 18 direct callers, 12 execution flows (backups, reports, dashboard) | **CRITICAL** |
| `ui._prefect_has_active_run` | guards `trigger_cloud`, `trigger_lan`, `/status` | **HIGH** |
| `watchdog.main` | `__main__` only | LOW |
| `health.check_source_drive` | both pipelines' health gate | LOW |

CRITICAL/HIGH items are mitigated by narrow in-function diffs and full-suite runs;
they are NOT reasons to skip the fixes — they are reasons to keep the diffs small.

---

## PHASE 1 — H4: watchdog deferral logic can kill a legitimate backup
**Priority: 1 (only finding that actively destroys running work).**

### Evidence & root cause (verified)
`watchdog.py:321` declares ONE shared counter `deferrals = 0` used by two branches
with different caps:
- Transfer branch (`:372-398`) caps at `MAX_TRANSFER_DEFERRALS = 240` (~8 h).
- Lock branch (`:400-426`) caps at `MAX_DEFERRALS = 15` (~30 min).

Failure sequence: during a Prefect API outage, a real multi-hour backup accrues
~90 transfer deferrals. The transfer then ends but the flow still holds the lock
(between rclone calls / post-processing). Control reaches the lock branch, which
sees `deferrals >= MAX_DEFERRALS` **immediately** → force-unlinks the LIVE lock
(`:414-415`) and restarts the agent mid-backup, skipping its intended 30-minute
grace entirely.

Second defect: after the transfer branch forces at `:386-389`, execution falls
through into `if lock_held:` using the STALE pre-unlink value → one wasted
120 s cycle before the restart actually happens (next iteration re-reads the
deleted lock).

### Review notes (v1.1 — verified against watchdog.py + test suite)
- **Feasibility confirmed:** `tests/test_watchdog.py` already drives `main()` with
  patched `time.sleep` / `httpx.get` / `_is_backup_running` /
  `_transfer_process_running` (see `test_deferral_active_transfer`) — RED tests
  use these exact fixtures with `side_effect` sequences.
- **Documented limitation (pre-existing, inherited):** the forced-restart path
  stops `WATCHED_SERVICE = "AamPrefectServer"` (watchdog.py:70). A zombie
  rclone/robocopy is a child of `AamBackupAgent`, so the 8 h transfer-cap force
  does NOT kill the zombie — it restarts orchestration around it. Out of scope to
  change the target service here; the force path must LOG this explicitly
  ("zombie process belongs to AamBackupAgent — manual kill may be required").
- **False-positive source:** `_transfer_process_running()` (watchdog.py:123-139)
  matches ANY rclone.exe/robocopy.exe system-wide via psutil. An operator's
  manual copy defers restarts up to 8 h. Accepted semantics; note in runbook.

### Fix design
Two changes, ~15 lines inside `main()` only:

1. **Split counters** — `transfer_deferrals` and `lock_deferrals`, each compared
   against its own cap. Healthy-reset at `:330` resets BOTH.
2. **Kill the fall-through** — change `if lock_held:` to `elif lock_held:` so the
   transfer branch cannot leak into lock logic on stale state. After either
   branch force-unlinks, proceed directly to the restart section below (no extra
   sleep cycle): re-read service state immediately instead of waiting one interval.

```python
failures = 0
transfer_deferrals = 0   # H4: independent counters ...
lock_deferrals = 0       # ... per signal (240-cycle vs 15-cycle caps)

...
if transferring:
    transfer_deferrals += 1
    if transfer_deferrals >= MAX_TRANSFER_DEFERRALS:
        logger.error("... zombie rclone/robocopy. Forcing restart.")
        with contextlib.suppress(OSError):
            BACKUP_LOCK_PATH.unlink(missing_ok=True)
        transfer_deferrals = 0
        # H4: no fall-through — go straight to restart logic below
    else:
        logger.warning(...); time.sleep(BACKUP_WAIT_INTERVAL); continue
elif lock_held:                      # H4: elif, not if
    lock_deferrals += 1
    if lock_deferrals >= MAX_DEFERRALS:
        logger.error("... stale lock. Forcing restart.")
        with contextlib.suppress(OSError):
            BACKUP_LOCK_PATH.unlink(missing_ok=True)
        lock_deferrals = 0
    else:
        logger.warning(...); time.sleep(BACKUP_WAIT_INTERVAL); continue
```

### Tasks
- [ ] 1a RED: extend watchdog test suite — simulate sequence
      [transferring ×(MAX_TRANSFER_DEFERRALS−1) → transferring ends, lock held ×1]
      and assert NO force-unlink occurs until `lock_deferrals >= MAX_DEFERRALS`
      (old code force-deletes on the first lock cycle).
- [ ] 1b RED: assert that when the transfer cap fires, the restart decision happens
      in the SAME iteration as the unlink (no 120 s dead cycle).
- [ ] 1c Apply the diff above; GREEN both tests; full suite green.
- [ ] 1d Manual soak (optional but recommended): stop Prefect server, start a fake
      long-running process matching `_transfer_process_running()`, confirm log shows
      independent counts and clean single-restart recovery.

**Exit criteria:** both new tests green; existing watchdog tests green; log lines
show `(deferral n/MAX_TRANSFER_DEFERRALS)` and `(deferral n/MAX_DEFERRALS)` tracked
separately.

---

## PHASE 2 — Critical 6 (manifest migration swallow) → silent run-history loss
**Priority: 2 (upgrade hazard; bites on the FIRST schema/version change on a live DB).**

### Evidence & root cause (verified)
`core/manifest.py:131-138`: the `extended_metrics` migration runs
`PRAGMA table_info` + `ALTER TABLE` inside `try/except Exception:
logger.error(...)`. On failure the connection is STILL returned and cached —
every later `insert_run` (`:433-437`, names `extended_metrics` in the INSERT)
raises `sqlite3.OperationalError: no such column`, which `record_run_history`
(`core/backup_repository.py:93-95`) converts to `return False`.
Net effect: **all run history silently stops being recorded** after a failed
migration; reports/dashboard show stale-but-plausible data. Same pattern at
`:121-122` (dedup step) logs at DEBUG — invisible.

Research (SQLite docs + migration practice):
- `ALTER TABLE ADD COLUMN` is metadata-only — instant regardless of row count,
  so retrying is cheap.
- Canonical versioning is `PRAGMA user_version` (32-bit int in the DB header;
  read costs nothing; survives without any table).
- Migration steps must be idempotent AND transactional; a failed step must roll
  back AND surface, never half-apply.
- Transient `SQLITE_BUSY` is the realistic failure mode here (two services open
  the DB simultaneously at boot) — busy_timeout already mitigates; add bounded
  retries before giving up.

### Fix design (narrow diff inside `_get_conn`) — CORRECTED v1.1
**v1.0 defect caught in review:** Python's sqlite3 (default
`LEGACY_TRANSACTION_CONTROL`, Python 3.12) only opens implicit transactions for
INSERT/UPDATE/DELETE/REPLACE — **never for DDL**. A bare
`conn.execute("ALTER TABLE ...")` runs in SQLite autocommit and commits
instantly; a subsequent `rollback()` cannot undo it, so v1.0's atomicity claim
was false. Confirmed against docs.python.org/3/library/sqlite3.html and
cpython#83638. The migration MUST open an explicit transaction:

```python
# Safe schema migration for extended_metrics (Critical-6: LOUD failure)
migrated = False
for attempt in range(3):                        # transient SQLITE_BUSY backoff
    try:
        columns = [r['name'] for r in conn.execute(
            "PRAGMA table_info(run_history)").fetchall()]
        if 'extended_metrics' not in columns:
            conn.execute("BEGIN IMMEDIATE")     # explicit: DDL gets NO implicit tx
            conn.execute("ALTER TABLE run_history ADD COLUMN extended_metrics TEXT")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")  # header write IS journaled
            conn.commit()                       # ALTER + stamp commit atomically
        else:
            # steady state: ensure version stamped once (idempotent)
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
        migrated = True
        break
    except sqlite3.OperationalError as e:
        conn.rollback()
        if attempt == 2 or 'locked' not in str(e).lower():
            conn.close(); self._conn = None
            raise ManifestSchemaError(
                f"run_history schema migration failed after {attempt+1} attempt(s): {e}"
            ) from e                            # FAIL STARTUP — NSSM restarts; breaker logs CRITICAL
        time.sleep(1.0 * (attempt + 1))
```

- New exception `ManifestSchemaError(RuntimeError)` exported from `core.manifest`;
  `import time` added to manifest.py (not currently imported).
- `SCHEMA_VERSION = 1` constant defined at module top; fresh DDL path stamps it
  AFTER `executescript` (executescript auto-commits pending transactions — do not
  mix the stamp into it).
- **Deliberate contract change (18 call sites — CRITICAL blast radius, accepted):**
  `_get_conn` may now raise at open time.
  - `record_run_history` catches broadly → returns False + error log (unchanged).
  - Flow tasks → Prefect task failure → visibly red run (intended).
  - `ui.get_db()` singleton assignment fails per-request → FastAPI 500s on
    dashboard until DB is repaired; NOT cached on failure, self-heals when DB is
    fixed (verified ui.py:165-170 — no swallow around construction).
  - Loud startup loop beats silent run-history loss.
- **Lock-hold note:** retries sleep while holding `self._lock` (all callers
  acquire it before `_get_conn`), blocking dashboard threads ≤ ~3 s during
  contention. Accepted and documented; do not release/re-acquire mid-migration.

### Tasks
- [ ] 2a RED (**redesigned**): sqlite3.Connection is a C extension type — its
      methods CANNOT be monkeypatched. Instead create REAL contention: open a
      second raw connection, `BEGIN EXCLUSIVE`, then construct ManifestDB with
      `busy_timeout_ms=50` → assert either retry-success after exclusive lock is
      released within the window or ManifestSchemaError on permanent contention,
      and that `self._conn` is left unclosed-None (not cached broken).
- [ ] 2b RED: pre-existing old-schema DB (create table without column via raw
      sqlite3) → ManifestDB migrates it; assert `extended_metrics` present AND
      `PRAGMA user_version == 1`; reopen → no second ALTER issued (fast path).
- [ ] 2c RED: fresh DB → DDL + user_version=1 stamped; insert_run with
      extended_metrics works end-to-end.
- [ ] 2d Apply diff; GREEN all three; full suite green (34 test-file references
      flagged by impact analysis must pass unchanged).

**Exit criteria:** failed migration can no longer produce a "working" DB object;
transient lock recovers automatically; `user_version` present on all DBs after
first open; ALTER+stamp proven atomic under injected kill (manual test: Ctrl-C
between BEGIN and COMMIT leaves column absent AND version 0).

- [ ] 2e (same commit, observability only): promote the dedup-swallow at
      manifest.py:121-122 from `logger.debug` to `logger.warning`.

---

## PHASE 3 — M8: empty-source-drive contradiction (needs operator decision)
**Priority: 3. Blocks first-run deployments onto a blank drive; otherwise latent.**

### Evidence & root cause (verified)
- `core/health.py:39-40` — `check_source_drive` returns False:
  `"Source drive appears empty"`.
- `core/cloud_preflight.py:72-75` — Probe A treats StopIteration (empty dir) as valid:
  `# Empty source drive - valid on first use. rclone sync handles it.`

In the orchestrated pipeline, `health_check_task` (`flow.py:123-133`) runs BEFORE
preflight, so fail-closed wins and every backup aborts while the source is empty.
Standalone preflight runs (deploy scripts) take the lenient path — two different
contracts for the same condition.

### Why fail-closed is the CORRECT default (research note)
Both sync engines mirror deletions: robocopy `/MIR` and `rclone sync` propagate
source-empty to the destination. An empty source is far more often
"drive unmounted / wrong drive letter / mount point hollow" than "genuinely
nothing yet". Fail-open here is how mirror-jobs wipe NASes/buckets industry-wide.
So the audit framing ("contradiction") resolves as: **health.py is right; the
preflight comment documents a hazard, not a feature.**

### Fix design (operator picks one)
**Option A — unify fail-closed (recommended, smallest diff):**
1. Improve the health message to be actionable:
   `"Source drive appears empty: E:\... — if this is a genuinely new FY folder, drop a canary file or set health.allow_empty_source=true"` (health.py:40).
2. Align preflight Probe A: replace the lenient comment/pass with a warning log +
   explicit note that the pipeline health gate will reject it unless overridden
   (keeps standalone-preflight diagnostics honest without changing its return).
3. Add config escape hatch `health.allow_empty_source: bool = False`
   (models/config.py HealthConfig + validator). Default False preserves today's
   safe behavior.
4. **Placement correction (v1.1):** `health_check_task` (flow.py:123-133) runs
   BEFORE either preflight computes destination inventory, so dest-awareness
   CANNOT live inside `check_source_drive`. The override decision lives in the
   PREFLIGHTS, which already know destination state:
   - cloud: Probe B's `--max-depth 0` listing (`cloud_preflight.py:92`) → empty
     prefix = legitimate first run;
   - LAN: walk result / canary check in `lan_preflight`.
   Contract when `allow_empty_source=True`: source empty + destination inventory
   empty ⇒ proceed; source empty + destination has files ⇒ hard abort with the
   anti-wipe message, **regardless of the flag**.

**Research validation (v1.1):** rclone's official sync docs state the
destination is updated to match source "including deleting files if necessary";
the rclone forum documents repeated real-world wipes from exactly this
empty-source scenario. This project passes NO delete cap today:
`build_rclone_sync_command` (core/cloud_sync.py) contains no `--max-delete`,
and the LAN leg is robocopy `/MIR` by design. The health gate is currently the
ONLY protection against mirror-wipe.

5. **Optional defense-in-depth (operator decision, separate commit):** pass
   `--max-delete N` to rclone sync (config `cloud.max_delete_count`, default 0 =
   refuse ALL deletions is too strict for FY rollovers where files legitimately
   move; suggested default: disabled `-1`, documented example value e.g. 5000).
   Robocopy has no equivalent flag — LAN relies on the health gate alone; record
   this asymmetry in DEPLOYMENT_GUIDE.md.

**Option B — status quo + docs:** keep both behaviors, document that empty source
always blocks scheduled backups. Zero code. Viable only if first-run-empty will
never occur in practice.

Tasks (Option A):
- [ ] 3a Operator decision recorded here: ______ (A or B).
- [ ] 3b RED: `test_health.py` — empty source + `allow_empty_source=False` → HealthError
      with new message; `=True` + dest-empty → OK; `=True` + dest-has-files → HealthError
      mentioning mirror risk.
- [ ] 3c Apply; update `config.example.yaml`; GREEN; full suite green.

**Exit criteria:** one documented contract; anti-wipe guard tested; example config updated.

---

## PHASE 4 — H3: dashboard active-run check (typed filter + fail-closed triggers)
**Priority: 4 (annoyance/duplicate-run window, not data risk).**

### Evidence & root cause (verified)
`ui.py:190-206` `_prefect_has_active_run()`:
1. Builds the filter as a raw nested dict `state={"type": {"any_": [...]}}`
   (`:193-195`) while `launch.py:137-142` correctly uses
   `FlowRunFilterState`/`FlowRunFilterStateType`. Works only via Pydantic
   coercion; brittle across Prefect upgrades.
2. `except Exception: return False` (`:204-206`) — any API hiccup answers
   "nothing running", disabling the duplicate-trigger guard on BOTH trigger
   endpoints (fail-open).
3. `limit=20` (`:196`) — >20 queued runs can hide an active one.

Prefect 3 reference (docs.prefect.io + src/prefect/client/schemas/filters.py)
confirms canonical form:
`FlowRunFilter(state=FlowRunFilterState(type=FlowRunFilterStateType(any_=[...])))`.

### Fix design
1. Typed filter mirroring launch.py exactly (import
   `FlowRunFilterState, FlowRunFilterStateType` next to the existing
   `FlowRunFilter` import at ui.py:27).
2. **Tri-state contract — REVISED v1.1 after reading `static/js/dashboard.js`:**
   The dashboard polls `/status` every 2 seconds (dashboard.js:182) and derives
   badges via `const isCloudRunning = data.cloud.running; badge = isCloudRunning ?
   'Running' : 'Idle'` (:108-109). A string `"unknown"` in `running` is TRUTHY in
   JS → badge would freeze at "Running" during outages. Therefore:
   - `/status` keeps `cloud.running` / `lan.running` as STRICT booleans
     (`False` when Prefect is unreachable — display-only degradation, zero JS
     changes required);
   - ADD sibling field `"run_state": "running" | "idle" | "unknown"` per mode
     for future consumers/tests; current JS ignores it.
   - Trigger endpoints: `None` ⇒
     `HTTPException(503, "Prefect API unavailable - cannot verify active runs; trigger refused")`
     (**fail-CLOSED**: worst case is a refused manual trigger during an outage,
     not a duplicate queued backup; concurrency slot still serializes anything
     that slips through). Consistent with G15's existing 500-on-failure path.
3. `limit=200` with comment (nightly tool; >200 queued PENDING/RUNNING runs is
   operationally impossible; removes the hide-an-active-run window cheaply).
   Load note: `/status` already issues TWO read_flow_runs per poll at 2 s
   cadence (~86k queries/day against localhost) — limit raise is negligible for
   the local server. Response-caching is noted as future work, out of scope (R3).
   Tag-based server-side filtering also deferred — requires deployment tags.

### Tasks
- [ ] 4a RED: mock `get_client.read_flow_runs` raising `httpx.ConnectError` →
      POST `/trigger/cloud` returns 503 (today: queues a duplicate run).
      GET `/status` still returns 200 with `cloud.running == false` AND
      `cloud.run_state == "unknown"` (booleans preserved — JS-safe).
- [ ] 4b RED: assert filter instance passed to client is
      `FlowRunFilter` with typed `state.type.any_` (guards against dict regression);
      assert `limit == 200`.
- [ ] 4c Apply diff; update existing `_prefect_has_active_run` mocks in
      `tests/test_ui*.py` for tri-state (`False` cases unchanged, add `None`
      cases); GREEN; full suite green.

**Exit criteria:** no code path answers "nothing running" from an exception;
both trigger endpoints fail closed with 503; `/status` degrades without
changing the JSON types the dashboard already consumes.

---

## Rollout order & verification

| Order | Item | Revert | Post-fix verification |
|---|---|---|---|
| 1 | H4 watchdog counters | `git revert` | watchdog unit tests + optional soak |
| 2 | Critical-6 migration | `git revert` | manifest suites + simulated locked-DB test |
| 3 | M8 (after operator decision) | `git revert` | health/preflight suites |
| 4 | H3 typed filter | `git revert` | UI suites |

Every phase: run FULL pytest suite (baseline 1442 passed / 53 hardware-skipped as
of 2026-08-23) + `detect_changes()` before each commit (repo rule). Services
restart required for items 1 and 4 (`07_restart_services.bat`); item 2 takes
effect on next service restart; item 3 needs `config.example.yaml` refresh on
the target host if Option A chosen.

## Explicitly deferred (logged, not forgotten)

| Finding | Reason deferred |
|---|---|
| Criticals 8/9 (memory-scale listings) | Capacity tradeoff, documented; revisit if bucket >2M files |
| M9 rollover ordering | Only fires April 1; schedule dedicated session before FY end |
| H7/H11 dead code (`mark_*_synced`, `sync_result` param) | Cosmetic; batch into next hygiene pass |
| M1-M5, M11 mediums | Hygiene batch; none block deployment |
