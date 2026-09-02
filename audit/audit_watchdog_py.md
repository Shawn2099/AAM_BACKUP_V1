# Code Audit - File 3 of 26: watchdog.py
Lines: 496 | Size: 23 KB | Audited: 2026-09-02

---

## Summary

watchdog.py is the Windows NSSM-managed daemon that monitors the Prefect API
health, manages service restarts, and protects active backups from disruption.
The logic, deferral counters, and circuit breaker design are solid.

HOWEVER: There is a CRITICAL syntax error introduced by commit bdcdc7e.
The try/except restructuring in main() broke the indentation of the
healthy/unhealthy branching, making the file UNPARSEABLE by Python.
The watchdog service CANNOT start on the current code.

---

## CRITICAL SYNTAX ERROR - IMMEDIATE FIX REQUIRED

File: watchdog.py | Lines: 338-489

The main loop if healthy / else (unhealthy) block has completely
broken indentation. Python raises IndentationError at line 339.

WHAT THE FILE LOOKS LIKE (broken):

    while True:
        try:
            healthy = _check_health()

            if healthy:        <- line 338, the if block has NO body
            if failures > 0:   <- line 339, separate if at wrong indentation
                logger.info(...)
            failures = 0
            ...
            time.sleep(...)
            continue

        # -- Unhealthy --    <- This is still inside the try block
        failures += 1         <- BUT at try-level indent, not inside any if
        ...

        except Exception as exc:   <- except is at wrong level (try closes above)
            ...

WHAT IT SHOULD LOOK LIKE (correct):

    while True:
        try:
            healthy = _check_health()

            if healthy:
                if failures > 0:        <- indented under if healthy
                    logger.info(...)
                failures = 0
                ...
                time.sleep(...)
                continue

            # -- Unhealthy --
            failures += 1
            ...

        except Exception as exc:        <- except matches try at same level
            logger.exception(...)
            time.sleep(...)

ROOT CAUSE:
The commit added the try/except wrapper around while True and restructured
indentation. The body of if healthy: (lines 339-369) was de-indented by
one level during the refactor, leaving if healthy: as an empty if with
the body floating at the same level as the else case. The except block
at line 489 is also at the wrong level relative to the 	ry at line 335.

VERIFIED WITH: python -c "import ast; ast.parse(open('watchdog.py').read())"
RESULT: IndentationError: expected an indented block after 'if' statement on line 338

IMPACT:
- The AamWatchdog service CANNOT START. Python will raise IndentationError
  before executing a single line of main().
- The Prefect server and backup agent are completely unmonitored.
- Any Prefect server hang or stop will NOT be detected or recovered.
- This is a silent total loss of the watchdog capability.

---

## try/except Structural Issues (same root cause)

Lines 335-491 - try/except/else body logic is mangled.

The unhealthy path (lines 372-488) and the except clause (line 489) are
all at wrong indentation levels. Once the if healthy: indentation is fixed,
the whole block needs a systematic re-indent to restore the correct structure:

  while True:
      try:
          healthy = _check_health()
          if healthy:
              [healthy body - all indented under if healthy]
              time.sleep(CHECK_INTERVAL_SECONDS)
              continue
          [unhealthy body - at try level, runs when healthy is False]
          failures += 1
          ...
      except Exception as exc:   <- matches try
          logger.exception(...)
          time.sleep(...)

---

## Findings (Non-Syntax Issues)

### HIGH - 1

HIGH-1: _start_allowed / service_start_log - Module-Level Mutable State (Lines 256-272)

service_start_log is a module-level dict. _start_allowed() mutates it
by appending the current time unconditionally before returning True,
which means counting the attempt happens even if _start_service() later
fails. A failed start still burns a slot in the circuit breaker window.

The circuit breaker window (3600s / 1 hour) means if sc start fails 3
times due to transient SCM errors (not a real crash loop), the watchdog
is silenced for an hour on a genuinely crashed service.

Fix: Only append to service_start_log AFTER _start_service() returns True.

---

### MEDIUM - 3

MEDIUM-1: _transfer_process_running - No Process Filtering by User/Session (Lines 123-139)

The function checks for any rclone.exe or robocopy.exe system-wide.
On a shared Windows server, another user or admin running rclone/robocopy
for a different purpose would cause the watchdog to defer indefinitely (up
to 8 hours), falsely treating the unrelated process as an active backup.

Fix: Check that the process is a child of the backup agent PID, or at
minimum verify the command-line arguments contain the backup destination paths.

MEDIUM-2: _resolve_paths - Silent Fall-Through on All Errors (Lines 85-98)

    except Exception:
        pass  # defaults already set at module level

If config.yaml is malformed, the watchdog silently uses hardcoded paths
(C:\BackupAgent\). In a deployment where the lock file is somewhere else,
the watchdog will never see it and will restart services during active backups.
Should at least log the exception at WARNING level.

MEDIUM-3: _check_health - No Distinction Between Network Error and 5xx (Lines 302-309)

    except Exception:
        return False

A 503/500 from the Prefect API (e.g., a dependency is broken but the
process is alive) returns the same False as a connection refused. This
means a hung-but-responsive API server gets restarted just as fast as a
completely dead one. Consider checking resp.status_code explicitly and
logging the specific failure mode.

---

### LOW - 2

LOW-1: BACKUP_LOCK_PATH / LOG_DIR as Module-Level Globals (Lines 81-82)

Global mutable state set by _resolve_paths() makes testing harder and
could cause subtle bugs if main() is ever called twice. Consider passing
resolved paths as parameters to the functions that need them.

LOW-2: _service_state Return Value '' on Timeout (Lines 206-215)

After both retry attempts for sc query timeout, the function returns ''.
The caller _service_is_running() returns False for '', which is correct.
But the main loop at line 450 checks if state in (START_PENDING, ...),
then if state != RUNNING - an empty string falls through to the second
check and triggers a _start_service call. This means a sc query timeout
is treated as the service being STOPPED, potentially triggering an
unnecessary start attempt. Should be checked explicitly.

---

## Verdict
| Severity | Count |
|----------|-------|
| CRITICAL |   1   |
| HIGH     |   1   |
| MEDIUM   |   3   |
| LOW      |   2   |
| Total    |   7   |

NOT PRODUCTION-READY. The watchdog daemon cannot start due to a Python
IndentationError introduced by the bdcdc7e commit's try/except refactor.
This must be fixed before deployment - the backup system currently has
ZERO watchdog protection.
