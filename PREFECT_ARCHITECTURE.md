# Prefect Server Architecture — Research & Decision

Date: 2026-05-29

---

## Current Architecture

```
launch.py (single process)
├── Prefect API server    (subprocess.Popen)
├── Dashboard UI          (daemon thread, uvicorn)
└── Backup scheduler      (main thread, prefect.serve())
```

**How `serve()` works:**
- `prefect.serve()` is a long-running process that:
  1. Registers deployments with the Prefect API
  2. Polls for scheduled work (cron triggers)
  3. Executes flow runs in-process (no separate worker needed)
  4. Manages its own concurrency via the `limit` parameter
- It's the simplest Prefect deployment model — no work pools, no workers, no Docker
- Recommended by Prefect for single-server setups

**Current behavior:**
- Prefect server starts → waits for API → creates concurrency limit → cancels orphans → serves deployments
- All 4 deployments (cloud, lan, weekly, monthly) run in the same process
- If Prefect server subprocess crashes, everything dies
- If `launch.py` is killed, Prefect server becomes orphaned (cleaned up by terminate/kill)

---

## Option A: Keep Current Architecture (All-in-One)

```
Task Scheduler → start.bat → launch.py
                                ├── Prefect server (subprocess)
                                ├── Dashboard (thread)
                                └── Scheduler (main thread)
```

**Pros:**
- Simplest setup — one batch file, one Task Scheduler entry
- Already working and tested
- No additional configuration needed
- `serve()` handles everything (scheduling + execution)

**Cons:**
- Prefect server crash kills everything
- No independent restart of components
- Prefect server startup adds ~30s delay to launch
- If `launch.py` is restarted, Prefect server is also restarted (loses in-memory state)

**When to use:** Development, testing, or single-server with reliable uptime.

---

## Option B: Separate Prefect Server (Two Task Scheduler Entries)

```
Task Scheduler #1 → start_server.bat → prefect server start
Task Scheduler #2 → start_app.bat    → launch.py (dashboard + scheduler only)
```

**Pros:**
- Prefect server runs independently — survives app restarts
- App restart doesn't restart Prefect server (no startup delay)
- Prefect server can be monitored/restarted independently
- Cleaner separation of concerns

**Cons:**
- Two Task Scheduler entries to manage
- Need to ensure Prefect server is ready before app starts (startup ordering)
- More configuration on the server

**When to use:** Production single-server where uptime matters.

---

## Option C: Windows Service via NSSM (Most Robust)

```
Windows Service #1 → NSSM → prefect server start
Task Scheduler    → start_app.bat → launch.py (dashboard + scheduler only)
```

**Pros:**
- Prefect server auto-starts on boot (before any user logs in)
- Windows Service Manager handles restart on failure
- Can be monitored via standard Windows tools
- Survives logoff/logon cycles

**Cons:**
- Requires installing NSSM (Non-Sucking Service Manager)
- More complex initial setup
- NSSM is a third-party tool

**When to use:** Production server that needs maximum uptime.

---

## Prefect's Own Recommendations

From the Prefect docs:

> **For self-hosted single-server setups:** `serve()` is sufficient. No need for work pools or workers.

> **Keep Workers Active:** Ensure Prefect workers are always running, whether as systemd services, Docker containers, or Kubernetes deployments.

> **Service Separation:** For optimal performance, run API servers and background services separately.

Key insight: **`serve()` is the correct model for this project.** It combines the scheduler and worker into one process, which is exactly what we need for a single-server backup automation. There's no benefit to adding work pools + workers — that's for distributed/multi-server setups.

---

## Recommendation: Option B

**Two separate processes managed by Task Scheduler:**

```
Task Scheduler Entry 1: "AAM Prefect Server"
  → At system startup, delay 10s
  → start_server.bat
  → prefect server start

Task Scheduler Entry 2: "AAM Backup App"
  → At system startup, delay 60s (after server is ready)
  → start_app.bat
  → launch.py (dashboard + scheduler only)
```

**Why Option B over A:**
- Prefect server survives app restarts (code updates, config changes)
- App restart is fast (~5s) without waiting for Prefect server (~30s)
- Each component can be restarted independently
- Simple to implement — just split `launch.py`

**Why Option B over C:**
- No additional tools (NSSM) to install
- Task Scheduler is built into Windows Server 2016
- Two entries is manageable for a single server
- NSSM is overkill for this use case

---

## Implementation Plan

### Changes needed:

1. **Create `start_server.bat`** — starts Prefect server
2. **Modify `launch.py`** — remove Prefect server subprocess, connect to existing server
3. **Modify `start.bat`** — rename to `start_app.bat`, remove server management
4. **Document** — update deployment instructions for two Task Scheduler entries

### `start_server.bat`:
```batch
@echo off
REM Prefect API Server — runs as separate Task Scheduler entry
REM Starts at system startup with 10s delay

cd /d "%~dp0"
set PATH=C:\Program Files\Python312\Scripts;%PATH%
set PREFECT_API_URL=http://127.0.0.1:4200/api
uv run prefect server start
```

### Modified `launch.py`:
```python
# Remove: subprocess.Popen(["prefect", "server", "start"])
# Remove: _wait_for_prefect_api() (server is already running)
# Keep: _ensure_concurrency_limit()
# Keep: _cancel_orphaned_runs()
# Keep: dashboard thread
# Keep: scheduler (main thread)
# Add: Prefect API health check with clear error if server not running
```

### `start_app.bat`:
```batch
@echo off
REM AAM Backup App — Dashboard + Scheduler
REM Starts at system startup with 60s delay (after Prefect server is ready)

cd /d "%~dp0"
set PATH=C:\Program Files\Python312\Scripts;%PATH%
set PREFECT_API_URL=http://127.0.0.1:4200/api
uv run python launch.py
```

---

## Windows Server 2016 Task Scheduler Setup

### Entry 1: Prefect Server
- Name: `AAM Prefect Server`
- Trigger: At system startup, delay 10 seconds
- Action: Start `start_server.bat`
- Run whether user is logged on or not
- Stop if it runs longer than: None (runs indefinitely)
- If the task fails, restart every: 1 minute, up to 3 times

### Entry 2: Backup App
- Name: `AAM Backup App`
- Trigger: At system startup, delay 60 seconds
- Action: Start `start_app.bat`
- Run whether user is logged on or not
- Stop if it runs longer than: None (runs indefinitely)
- If the task fails, restart every: 1 minute, up to 3 times

---

## What Does NOT Need to Change

- `flow.py` — still uses `serve()` pattern (correct)
- `serve.py` — still creates deployments (correct)
- `ui.py` — still connects via Prefect SDK (correct)
- `core/*` — no changes needed
- `models/config.py` — no changes needed
- All tests — still valid

---

## Decision

**Go with Option B.** It's the simplest improvement that gives real operational benefit — independent restart of Prefect server vs app — without adding complexity (no Docker, no NSSM, no work pools).
