# Code Audit - File 4 of 26: serve.py
Lines: 96 | Size: 3.5 KB | Audited: 2026-09-02

---

## Summary

serve.py is a thin deployment registration module. It reads config.yaml,
builds Prefect Cron deployment objects for each enabled leg, and hands
them to Prefect's serve() or to launch.py's serve() call.

The file is syntactically clean and structurally correct. No CRITICAL or
HIGH bugs found. A few medium and low issues are noted below.

---

## Findings

### CRITICAL - 0 | HIGH - 0

---

### MEDIUM - 2

MEDIUM-1: No Cron Expression Validation Before Passing to Prefect (Lines 40, 51, 61, 71, 84)

    schedules=[Cron(config.schedule.cloud_cron, tz)],

The cron strings and timezone are read from config.yaml and passed
directly to Prefect's Cron object. If an operator writes a malformed
cron string (e.g. "0 18 * *" - missing field) or an invalid timezone
(e.g. "IST" instead of "Asia/Kolkata"), Prefect will raise an exception
at serve() time, crashing launch.py's main thread silently.

There is no validation, try/except, or helpful error message at the point
of construction. The error surfaces deep in Prefect internals with an
unhelpful traceback.

Fix: Validate cron expressions and timezone strings in models/config.py's
ScheduleConfig model using field_validator before they reach serve.py.

MEDIUM-2: _deployments() Loads Config at Call Time, Not at Import (Line 30)

    config = load_config(CONFIG_PATH)

load_config() is called inside _deployments() (deferred - which is correct
per the comment on line 22). However, if config.yaml is temporarily
unavailable or locked at the moment deployments() is called from
launch.py's main thread, the entire launch process fails at that point
with no retry logic.

For a system that starts as a Windows service after boot, config.yaml
might be on a network drive or have a transient filesystem access race.
Fix: Wrap load_config in a short retry loop with a clear error message,
or ensure config.yaml is always on a local drive (document this constraint).

---

### LOW - 2

LOW-1: Public/Private Function Indirection Is Unnecessary (Lines 16-18)

    def deployments():
        return _deployments()

deployments() is a one-liner wrapper for _deployments(). The docstring says
"Public entry point" but both functions are in the same module scope. The
indirection adds no value (no access control, no caching, no transformation).
Can be collapsed into a single deployments() function.

LOW-2: __main__ Block Registers Deployments Without Enabled Guard (Lines 93-95)

    if __name__ == "__main__":
        d = _deployments()
        serve(*d, pause_on_shutdown=False)

When serve.py is run directly (python serve.py), _deployments() filters
by enabled flags correctly. However, if both lan and cloud are disabled in
config.yaml, d will contain only the 3 always-present deployments (weekly,
monthly, rollover). serve() with a minimal tuple is valid, but there is no
informational print to warn the operator that backup deployments are absent.
This can cause silent confusion during manual testing.

---

## INFO

- Line 10: rom prefect.schedules import Cron - correct import for Prefect 3.
- Line 90: Returns tuple() which is what Prefect serve(*deployments) expects -
  correct use of unpacking.
- Concurrency tags on backup deployments (lines 41, 52) are correct - matches
  the tag-based concurrency limit created in launch.py.

---

## Verdict
| Severity | Count |
|----------|-------|
| CRITICAL |   0   |
| HIGH     |   0   |
| MEDIUM   |   2   |
| LOW      |   2   |
| Total    |   4   |

PRODUCTION READY with minor caveats. The cron/timezone validation gap
(MEDIUM-1) is the most important item to address before an operator
changes schedule config in production.
