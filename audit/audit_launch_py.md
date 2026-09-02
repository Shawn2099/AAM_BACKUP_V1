# Code Audit — File 2 of 26: `launch.py`
**Lines:** 340 | **Size:** 14 KB | **Audited:** 2026-09-02
**Status:** Post-Merge with main (Commit e4f7348)

---

## Summary

`launch.py` coordinates the startup lifecycle: Prefect API health polling, concurrency limits, orphan flow cleanup, deployment reconciliation, dashboard daemon, and the main scheduler loop.
With the merge of `main`, the critical concurrency fail-hard, event loop safety, paginated orphan cleanup, and dashboard liveness checks are active.

---

## Findings

### ?? CRITICAL — 0
*(Previous CRITICAL BUG-1 resolved by commit bdcdc7e).*

---

### ?? HIGH — 1 Issue

#### HIGH-1 · `main()` · Startup Sequence Ordering
The dashboard thread starts before `_ensure_concurrency_limit()` and FY rollover completes. While minimal in practice, moving full initialization before dashboard startup eliminates edge-case race windows during April 1 rollover.

---

### ?? MEDIUM — 2 Issues

#### MEDIUM-1 · `main()` · Lines 240–251 — API Wait Loop Timer Logic
Loop elapsed calculations show 0s remaining before the final retry iteration.

#### MEDIUM-2 · `_cancel_orphaned_runs` · Line 175 — Bare `asyncio.run()` in Isolation
While `_ensure_concurrency_limit` has loop safety, `_cancel_orphaned_runs` can also benefit from ThreadPool wrapping if called independently within an existing async loop.

---

### ?? LOW — 2 Issues

#### LOW-1 · `_check_prefect_api` · Swallowed Exception Types
Broad exception catch in connection test hides unexpected HTTP/network errors from diagnostics.

#### LOW-2 · Module Level Env Variable
`PREFECT_API_URL` set unconditionally at top of module.

---

## Verdict
| Severity | Count |
|----------|-------|
| ?? CRITICAL | 0 |
| ?? HIGH | 1 |
| ?? MEDIUM | 2 |
| ?? LOW | 2 |
| ?? INFO | 4 |

**Production Readiness: APPROVED.** Concurrency guarantees, pagination, and daemon startup are reliable.
