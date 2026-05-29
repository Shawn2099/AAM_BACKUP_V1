# Prefect 3.x Usage Audit

Reviewed against Prefect 3.7.2 docs (`prefect>=3.4.0` declared in `pyproject.toml`).

Files audited: `flow.py`, `serve.py`, `launch.py`, `ui.py`, `core/logging.py`

---

## Correct Usage (No Changes Needed)

- **`@flow` / `@task` decorators** — Correct with `name=`, `log_prints=True`
- **`to_deployment()` + `serve()`** in `serve.py` — Correct Prefect 3.x pattern
- **`Cron()` from `prefect.schedules`** — Correct import and usage
- **`get_client()` async context manager** in `ui.py` — Correct
- **`FlowRunFilter` / `StateType`** for querying flow runs — Correct
- **`run_deployment()`** for programmatic triggers — Correct
- **`FlowRunContext.get()` / `TaskRunContext.get()`** — Correct context detection
- **`get_run_logger()`** — Correct
- **`prefect server start`** as subprocess — Correct approach

---

## Reinvented (Should Use Built-in Prefect Features)

### 1. Manual retry loops — Prefect has native `retries` + `retry_delay_seconds`

**File:** `flow.py` — `cloud_backup_task` and `lan_backup_task`

Both tasks implement hand-rolled `for attempt in range(max_attempts)` loops with
`time.sleep(retry_delay)`. Prefect handles this natively via decorator parameters.

**Current (reinvented):** ~30 lines of manual retry logic per task

```python
@task(name="cloud-backup")
def cloud_backup_task(config):
    for attempt in range(max_attempts):
        try:
            ...
        except Exception:
            time.sleep(retry_delay)
```

**Correct:** Let Prefect handle it

```python
@task(name="cloud-backup", retries=2, retry_delay_seconds=300)
def cloud_backup_task(config):
    ...  # just the work, no retry loop
```

Prefect also supports exponential backoff:

```python
from prefect.tasks import exponential_backoff

@task(retries=2, retry_delay_seconds=exponential_backoff(backoff_factor=2))
```

And per-attempt retry delays as a list:

```python
@task(retries=3, retry_delay_seconds=[60, 180, 300])
```

**Config values to preserve:** `config.cloud.max_attempts`, `config.cloud.retry_delay_seconds`,
`config.lan.max_attempts`, `config.lan.retry_delay_seconds` should map to the `retries` and
`retry_delay_seconds` decorator params (pass via `with_options` if config-driven values are needed).

---

### 2. Manual file-based locking — Prefect has concurrency limits

**File:** `flow.py` — `acquire_global_backup_lock()` / `release_global_backup_lock()` (~80 lines)

Reinvents Prefect's built-in concurrency limits. Prefect provides tag-based concurrency limits
that enforce "only N concurrent runs" natively.

**Create limits once:**

```bash
prefect concurrency-limits create backup-cloud 1
prefect concurrency-limits create backup-lan 1
```

**Or use programmatically in tasks:**

```python
from prefect.concurrency.sync import concurrency

@task(name="cloud-backup", tags=["cloud"])
def cloud_backup_task(config):
    with concurrency("backup-cloud", occupy=1):
        ...  # only one cloud backup runs at a time
```

This replaces the entire `acquire_global_backup_lock` / `release_global_backup_lock` /
`release_global_backup_lock` pattern including stale PID detection and timeout logic.

**Note:** The current lock also prevents different modes from running simultaneously
(enforced sequential: cloud then LAN). If that's intentional, use a single concurrency
limit `backup-global` with limit=1. If cloud and LAN should be allowed to run in parallel
but not duplicate, use separate limits per mode.

---

### 3. Manual failure alerts — Prefect has `on_failure` state change hooks

**File:** `flow.py` — manual `send_failure_alert()` in each task's `finally` block

Prefect provides `on_failure` hooks that fire automatically when a flow or task enters
a Failed state, no `try/finally` needed.

```python
def _alert_on_failure(task_or_flow, run, state):
    """Called automatically by Prefect when the run fails."""
    send_failure_alert(...)

@task(name="cloud-backup", retries=2, retry_delay_seconds=300, on_failure=[_alert_on_failure])
def cloud_backup_task(config):
    ...
```

Or at the flow level to catch any failure:

```python
@flow(name="aam-backup", on_failure=[_alert_on_failure])
def backup(config_path, mode):
    ...
```

Hooks receive `(flow_or_task, run, state)` and have access to the run context.

---

## Correct But Could Be Simplified

### 4. Loguru-to-Prefect bridge

**File:** `core/logging.py` — `configure_prefect_bridge()` (~30 lines)

Creates a custom Loguru sink that detects Prefect context and forwards messages. This works
but adds complexity. Since tasks run under Prefect, `get_run_logger()` can be used directly.

**Alternative:** Use `get_run_logger()` directly in tasks instead of forwarding through Loguru:

```python
from prefect.logging import get_run_logger

@task(name="cloud-backup", retries=2, retry_delay_seconds=300)
def cloud_backup_task(config):
    logger = get_run_logger()
    logger.info("Starting cloud backup")  # appears in Prefect UI automatically
```

**Trade-off:** The bridge is useful if Loguru is the single logger across all `core/` modules
that don't import Prefect. Keeping it is fine; removing it simplifies the logging path.

---

### 5. `ExceptionGroup` for multi-mode errors

**File:** `flow.py` — `backup()` flow collects exceptions into `ExceptionGroup`

With native task retries + `on_failure` hooks, each task handles its own errors. The flow
can rely on Prefect state tracking instead of manually aggregating exceptions.

---

### 6. `launch.py` server readiness — polls with `sleep(30)`

**File:** `launch.py` line 100 — `time.sleep(30)` to wait for Prefect API

Instead of a flat 30-second sleep, poll the Prefect health endpoint:

```python
import httpx

def _wait_for_prefect_api(url="http://127.0.0.1:4200/api", timeout=60):
    for _ in range(timeout):
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return True
        except httpx.ConnectError:
            pass
        time.sleep(1)
    return False
```

---

## Summary

| Area | Status | Recommendation | Lines Saved |
|---|---|---|---|
| `@flow` / `@task` / `serve` | **Correct** | No change needed | — |
| `Cron` / `to_deployment` | **Correct** | No change needed | — |
| `get_client` / `FlowRunFilter` | **Correct** | No change needed | — |
| `run_deployment` | **Correct** | No change needed | — |
| Manual retry loops | **FIXED** | Prefect `retries` + `retry_delay_seconds` via `with_options()` | ~60 |
| File-based locking | **FIXED** | Prefect `concurrency("aam-backup", occupy=1)` | ~80 |
| Manual failure alerts | **FIXED** | Prefect `on_failure` hooks | ~20 |
| Loguru bridge | **Kept** | Useful for core/ modules | — |
| Server readiness | **FIXED** | Health endpoint polling | ~5 |
