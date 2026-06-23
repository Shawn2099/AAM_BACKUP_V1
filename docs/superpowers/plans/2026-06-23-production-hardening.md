# Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the backup agent for safer unattended production use by fixing run-history resilience, timeout/config drift, and unsafe service install behavior.

**Architecture:** Keep the current single-host Windows service design and make surgical fixes in the reliability-critical paths. Prefer small, reversible changes that improve operational safety without restructuring the orchestration model or deployment topology.

**Tech Stack:** Python 3.12, Prefect 3, FastAPI, SQLite, pytest, Windows batch services via NSSM

---

### Task 1: Plan and Scope Lock

**Files:**
- Create: `docs/superpowers/plans/2026-06-23-production-hardening.md`
- Review: `core/backup_repository.py`
- Review: `core/cloud_reporter.py`
- Review: `flow.py`
- Review: `deploy/install_services.bat`

- [ ] **Step 1: Confirm the hardening scope**

Scope includes only:
- run-history failure visibility and persistence safety
- cloud diff timeout/config correctness
- maintenance DB tuning consistency
- installer behavior that can disrupt unrelated processes

Scope excludes:
- large architectural changes
- streaming redesign for manifest/diff memory use
- migration away from SQLite

- [ ] **Step 2: Confirm baseline validation commands**

Run:
```bash
./.venv_311/bin/pytest -q
python3 -m py_compile $(rg --files -g '*.py' -g '!**/.venv/**' -g '!**/.venv_311/**' -g '!**/.venv_test/**')
```

Expected:
- pytest passes
- py_compile exits 0

### Task 2: Harden Run-History Failure Handling

**Files:**
- Modify: `core/backup_repository.py`
- Modify: `flow.py`
- Test: `tests/test_backup_repository.py`
- Test: `tests/test_flow_helpers.py`

- [ ] **Step 1: Write the failing tests**

Add tests covering:
```python
def test_record_run_history_returns_false_on_insert_failure(...):
    ...

def test_record_run_history_returns_false_on_checkpoint_failure(...):
    ...

def test_record_run_logs_critical_when_history_not_persisted(...):
    ...
```

- [ ] **Step 2: Run targeted tests to verify the current gap**

Run:
```bash
./.venv_311/bin/pytest tests/test_backup_repository.py tests/test_flow_helpers.py -q
```

Expected:
- at least one new test fails because `_record_run` only warns and does not emit a stronger operational signal

- [ ] **Step 3: Implement minimal reliability improvement**

Change behavior so that:
```python
# core/backup_repository.py
# keep bool return contract, but log with enough severity/context

# flow.py
# when run history cannot be persisted, log a critical operational event
# including mode/run_id so operators can correlate the missing audit trail
```

Do not raise from `_record_run`; it runs in `finally` blocks and must not mask the original backup failure.

- [ ] **Step 4: Re-run targeted tests**

Run:
```bash
./.venv_311/bin/pytest tests/test_backup_repository.py tests/test_flow_helpers.py -q
```

Expected:
- PASS

### Task 3: Fix Timeout and Config Drift

**Files:**
- Modify: `core/cloud_reporter.py`
- Modify: `flow.py`
- Test: `tests/test_cloud_reporter.py`
- Test: `tests/test_flow_orchestration.py`

- [ ] **Step 1: Write the failing tests**

Add tests covering:
```python
def test_get_cloud_diff_uses_passed_timeout(...):
    ...

def test_backup_maintenance_uses_configured_sqlite_tuning(...):
    ...
```

- [ ] **Step 2: Run targeted tests to verify they fail**

Run:
```bash
./.venv_311/bin/pytest tests/test_cloud_reporter.py tests/test_flow_orchestration.py -q
```

Expected:
- timeout test fails because `get_cloud_diff()` ignores its `timeout` argument
- maintenance tuning test fails because `backup()` instantiates `ManifestDB` with defaults

- [ ] **Step 3: Implement the minimal fixes**

Apply:
```python
# core/cloud_reporter.py
result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

# flow.py
db = ManifestDB(
    config.paths.database_path,
    busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
    vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
)
```

- [ ] **Step 4: Re-run targeted tests**

Run:
```bash
./.venv_311/bin/pytest tests/test_cloud_reporter.py tests/test_flow_orchestration.py -q
```

Expected:
- PASS

### Task 4: Make Installer Service-Safe

**Files:**
- Modify: `deploy/install_services.bat`
- Test: `tests/test_install_services.py`

- [ ] **Step 1: Add coverage for install script safety**

Create:
```python
def test_install_services_does_not_kill_all_python_processes():
    script = Path("deploy/install_services.bat").read_text(encoding="utf-8")
    assert "taskkill /F /IM python.exe /T" not in script
    assert "taskkill /F /IM prefect.exe /T" not in script
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:
```bash
./.venv_311/bin/pytest tests/test_install_services.py -q
```

Expected:
- FAIL because the current script kills unrelated host processes

- [ ] **Step 3: Replace blanket process kills with service-local stop/remove logic**

Keep:
```bat
"%NSSM%" stop  %SVC_WATCHDOG% 2>nul
"%NSSM%" stop  %SVC_AGENT%   2>nul
"%NSSM%" stop  %SVC_SERVER%  2>nul
"%NSSM%" remove %SVC_WATCHDOG% confirm 2>nul
"%NSSM%" remove %SVC_AGENT%   confirm 2>nul
"%NSSM%" remove %SVC_SERVER%  confirm 2>nul
```

Remove:
```bat
taskkill /F /IM python.exe /T 2>nul
taskkill /F /IM prefect.exe /T 2>nul
```

- [ ] **Step 4: Re-run the targeted test**

Run:
```bash
./.venv_311/bin/pytest tests/test_install_services.py -q
```

Expected:
- PASS

### Task 5: Full Validation

**Files:**
- Test: `tests/test_backup_repository.py`
- Test: `tests/test_cloud_reporter.py`
- Test: `tests/test_flow_orchestration.py`
- Test: `tests/test_install_services.py`

- [ ] **Step 1: Run focused validation**

Run:
```bash
./.venv_311/bin/pytest tests/test_backup_repository.py tests/test_cloud_reporter.py tests/test_flow_orchestration.py tests/test_install_services.py -q
```

Expected:
- PASS

- [ ] **Step 2: Run the full suite**

Run:
```bash
./.venv_311/bin/pytest -q
```

Expected:
- PASS

- [ ] **Step 3: Run syntax validation**

Run:
```bash
python3 -m py_compile $(rg --files -g '*.py' -g '!**/.venv/**' -g '!**/.venv_311/**' -g '!**/.venv_test/**')
```

Expected:
- PASS

### Self-Review

- Spec coverage: all agreed production blockers are mapped to concrete tasks.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: file paths, function names, and commands match the current repo layout.

### Execution Handoff

Plan saved to `docs/superpowers/plans/2026-06-23-production-hardening.md`.

This request already asked for execution, so proceed inline in this session with the task order above.
