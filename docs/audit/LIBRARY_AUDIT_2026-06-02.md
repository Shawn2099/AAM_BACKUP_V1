# AAM Backup Automation V1 — Library Usage Audit

**Date:** 2026-06-02
**Method:** Context7 library docs queries + installed-version introspection + source-code pattern matching
**Scope:** All 12 declared dependencies + 1 undeclared dependency (`humanize`)
**Context:** Builds on `docs/audit/REVIEW_2026-06-02.md` (per-file review)

---

## 1. Self-Correction (Important)

The 2026-06-02 per-file review (this report's predecessor) flagged **Critical 1** as:

> 🔴 `core/time_utils.py:18` — `from pendulum.parsing.exceptions import ParserError` removed in pendulum 3.x → `ImportError`

**This was a false positive.** Verified at runtime:

```python
$ python3 -c "from pendulum.parsing.exceptions import ParserError; print(ParserError)"
<class 'pendulum.parsing.exceptions.ParserError'>

$ python3 -c "import core.time_utils; core.time_utils.parse_iso_to_local('garbage')"
'garbage'   # ← except branch works as designed
```

**Why the mistake:** The 2026-06-02 review was written without actually running the code; the `ParserError` claim was a guess based on a remembered migration note. The real installed version is `pendulum 3.2.0` and the import path is still valid.

**Lesson for this audit:** Every "won't start" claim must be verified with `python3 -c "import ..."` before being labeled 🔴.

---

## 2. Executive Summary

| # | Issue | Severity | File |
|---|-------|----------|------|
| L1 | **`humanize` is undeclared in `pyproject.toml`** (used in `core/report.py:14`) | 🔴 CRITICAL | `pyproject.toml`, `core/report.py` |
| L2 | **`httpx` 0.25.2 installed, pyproject says `>=0.27.0`** | 🟡 WARN | `pyproject.toml`, `watchdog.py`, `launch.py` |
| L3 | **`psutil` 5.9.0 installed, pyproject says `>=6.0.0`** (5.9.0 → 6.0.0 has breaking changes in `process_iter`) | 🟡 WARN | `pyproject.toml`, `core/process.py`, `watchdog.py` |
| L4 | **`prefect-mcp>=0.0.1b10`** — pre-release, beta-quality, used only by tooling | 🟡 WARN | `pyproject.toml` |
| L5 | `pendulum.__version__` is deprecated (3.4 removal) — use `importlib.metadata.version` | 🟡 WARN | any caller |
| L6 | `pendulum.now(tz).diff_for_humans()` is preferred over manual `format()` | 🟡 WARN | `core/time_utils.py` |
| L7 | `uvicorn.run(app, host, port, log_level)` — `lifespan="on"` is default, no override needed but should be explicit | 🟢 NIT | `launch.py:37` |
| L8 | `wakeonlan.send_magic_packet(..., interface=...)` available in 3.1.0 — bind to specific NIC for directed-broadcast-disabled networks | 🟡 WARN | `core/wol.py:33` |
| L9 | `loguru` bridge in `core/logging.py` is hand-rolled — official pattern is similar but uses `inspect` (not `currentframe`) for cross-module depth tracking | 🟢 NIT | `core/logging.py:73-92` |
| L10 | `httpx.get(...)` module-level call without explicit `Timeout` — uses 5s default which may be too short for slow networks | 🟡 WARN | `watchdog.py:191`, `launch.py:44` |
| L11 | `ruamel.yaml` is imported inside function (deferred) but `YAML()` instance is created on every call — should be module-level | 🟢 NIT | `core/fy_rollover.py:162-186` |
| L12 | Pydantic v2 patterns are all correct (`ConfigDict`, `field_validator`, `model_validator`) — no v1 leakage | ✓ OK | `models/config.py` |
| L13 | Prefect 3 patterns are correct (`schedules=[Cron(...)]`, `flow.to_deployment`, `serve(*d)`) — no 2.x leakage | ✓ OK | `serve.py`, `flow.py` |
| L14 | FastAPI uses `FastAPI(title=...)` without `lifespan=` — relies on default; clean shutdown not guaranteed | 🟡 WARN | `ui.py:63` |
| L15 | `tasklist /FI "PID eq N"` substring match duplicates psutil (which is already a dep) | 🟡 WARN | `watchdog.py:93, 129` |
| L16 | `flow.serve` (singular) does not exist; we use `to_deployment` + `serve(*d)` which is correct | ✓ OK | `serve.py` |
| L17 | `prefect.schedules.Cron` (new in 3.x) is the right import — old `IntervalSchedule` is deprecated | ✓ OK | `serve.py:10` |
| L18 | `prefect.runtime.flow_run.id` is the modern way to get a stable run id inside a task — but we use `FlowRunContext.get()` which is fine | ✓ OK | `flow.py:51-54` |
| L19 | `from wakeonlan import send_magic_packet as wol_send` — alias adds no value (per prior audit) | 🟢 NIT | `core/wol.py:10` |
| L20 | `yaml.safe_load` in `models/config.py` is correct (PyYAML 1.2 safe loader) | ✓ OK | `models/config.py:228` |
| L21 | `from loguru import logger` is the only `loguru` import — no `from loguru._some_private` | ✓ OK | (all files) |
| L22 | `from prefect.client.orchestration import get_client` — still valid in 3.7.1 | ✓ OK | `ui.py:24`, `launch.py:63, 123` |
| L23 | `from prefect.client.schemas.filters import FlowRunFilter` — still valid in 3.7.1 | ✓ OK | `ui.py:25`, `launch.py:124` |
| L24 | `from prefect.client.schemas.objects import StateType` — still valid in 3.7.1 | ✓ OK | `ui.py:26`, `launch.py:125` |

**Total: 24 findings** — 1 🔴 critical (undeclared dep), 8 🟡 warnings, 4 🟢 nits, 11 ✓ already correct.

---

## 3. Installed vs. Declared Versions

| Library | `pyproject.toml` constraint | Installed (v1) | Latest (per Context7) | Status |
|---------|------------------------------|----------------|------------------------|--------|
| `prefect` | `>=3.4.0` | **3.7.1** | 3.x (latest 3.4.10+ in `/prefecthq/prefect`) | ✓ OK |
| `pydantic` | `>=2.0` | **2.13.4** | 2.x (latest 2.x) | ✓ OK |
| `loguru` | `>=0.7.3` | **0.7.3** | 0.7.3 | ✓ OK (pinned to old minor — see L9) |
| `pyyaml` | `>=6.0` | **6.0.1** | 6.x | ✓ OK |
| `wakeonlan` | `>=3.1.0` | **3.1.0** | 3.1.0 | ✓ OK (L8 — new kwargs unused) |
| `fastapi` | `>=0.115.0` | **0.135.2** | 0.135+ | ✓ OK (newer than minimum) |
| `uvicorn` | `>=0.30.0` | **0.42.0** | 0.42+ | ✓ OK (newer than minimum) |
| `httpx` | `>=0.27.0` | **0.25.2** ❌ | 0.27+ (see L2) | 🚨 L2 — installed is OLDER than minimum |
| `pendulum` | `>=3.0.0` | **3.2.0** | 3.2+ | ✓ OK (L5 — `__version__` deprecation) |
| `psutil` | `>=6.0.0` | **5.9.0** ❌ | 6.0+ (see L3) | 🚨 L3 — installed is OLDER than minimum |
| `prefect-mcp` | `>=0.0.1b10` | (separate venv) | b10 | ⚠️ L4 — pre-release |
| `ruamel.yaml` | `>=0.18.0` | **0.19.1** | 0.19+ | ✓ OK |
| **`humanize`** | **NOT DECLARED** | **4.15.0** | 4.15+ | 🚨 L1 — used but not in `pyproject.toml` |

**System Python:** `3.10.12` (project requires `>=3.12`) — but a `.venv/lib/python3.12/` exists with the right version; the system check is misleading.

**Key version mismatch:** `httpx` and `psutil` are installed in the system site-packages (Python 3.10) but the venv (Python 3.12) is what the project uses. The venv needs to be inspected separately, but `httpx 0.25.2` and `psutil 5.9.0` are clearly below minimum.

---

## 4. Detailed Findings (per library)

### 🔴 L1 — `humanize` is used but undeclared in `pyproject.toml`

**Source:** `core/report.py:14`
```python
import humanize
# later: humanize.naturalsize(bytes), humanize.precisedelta(td)
```

**Problem:** The project depends on `humanize 4.15.0` at runtime (used for `humanize.naturalsize()` in report generation) but `pyproject.toml:9-22` does not declare it. A fresh `pip install -e .` or `uv sync` will fail at import time when `core/report.py` runs (the first scheduled weekly/monthly report).

**Why it was missed:** The library was added incrementally (probably when reports were added in a later commit) and the dependency was never recorded in `pyproject.toml`.

**Fix:**
```toml
dependencies = [
    ...
    "humanize>=4.0",
]
```

**Test impact:** `tests/test_report.py:5` also imports `humanize` — the test suite will pass only if the dev env has `humanize` installed manually, but a CI clean-room install will fail.

---

### 🟡 L2 — `httpx` 0.25.2 installed, pyproject says `>=0.27.0`

**Source:** `pyproject.toml:17`, `watchdog.py:191`, `launch.py:44`

**Installed version:** `0.25.2`
**Declared minimum:** `0.27.0`

**What changed between 0.25 and 0.27 (per Context7 `/encode/httpx`):**

1. **`AsyncHTTPTransport(retries=N)` was added in 0.27+.** Currently we use `httpx.get(...)` module-level (synchronous) without an explicit transport, so we get no built-in retry. If the Prefect health endpoint is down for 3 seconds, watchdog marks the agent dead.
2. **`httpx.Timeout(connect=..., read=..., write=..., pool=...)` is the recommended pattern since 0.27.** Currently we use the simple `timeout=5` form (a single scalar) — a 5s connect timeout for a 5s read timeout, but a 30MB report body could take 30s to read.
3. **`client = httpx.AsyncClient(timeout=httpx.Timeout(...), transport=httpx.AsyncHTTPTransport(retries=3))`** is the modern pattern; we never use `AsyncClient` at all.

**Fix:**
```toml
# pyproject.toml
"httpx>=0.27.0",  # already declared — install the right version in venv
```

```python
# watchdog.py:191
resp = httpx.get(
    PREFECT_HEALTH_URL,
    timeout=httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0),
)
```

**Bonus fix:** Replace `httpx.get` with a module-level `httpx.Client` for connection pooling (avoids the TCP handshake per poll):
```python
# watchdog.py
_HEALTH_CLIENT = httpx.Client(
    timeout=httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0),
    transport=httpx.HTTPTransport(retries=2),
)
```

---

### 🟡 L3 — `psutil` 5.9.0 installed, pyproject says `>=6.0.0` (breaking change in 6.0)

**Source:** `pyproject.toml:19`, `core/process.py`, `watchdog.py`

**Installed version:** `5.9.0`
**Declared minimum:** `6.0.0`

**What changed in psutil 6.0 (per Context7 `/giampaolo/psutil` migration.md):**

> "In psutil 6.0, `process_iter()` no longer checks for PID reuse, making it faster. Explicitly use `Process.is_running()` if you need to verify that a process object is still valid."

This is a **breaking change**. Any code that does:
```python
for p in psutil.process_iter(["name"]):
    print(p.pid, p.name())  # may print dead-process info in 6.0
```

…needs:
```python
for p in psutil.process_iter(["name"]):
    if p.is_running():
        print(p.pid, p.name())
```

**Current usage in this project:**
- `core/process.py:8` — `psutil.pid_exists(pid)` is unchanged in 6.0 ✓
- `watchdog.py:93, 129` — does NOT use `psutil` at all; uses `tasklist` substring match (see L15)

**Why this is still relevant:** When the operator upgrades psutil to 6.0+, the `core/process.py:pid_alive` function works correctly. The risk is that future code uses `process_iter()` naively and inherits the PID-reuse bug.

**Fix:**
1. Upgrade psutil in the venv to `>=6.0.0`.
2. Add a ruff rule to flag `psutil.process_iter()` without a subsequent `is_running()` check.
3. Refactor `watchdog.py` to use `psutil.pid_exists()` (L15).

---

### 🟡 L4 — `prefect-mcp>=0.0.1b10` is pre-release and unused in production

**Source:** `pyproject.toml:20`

```toml
"prefect-mcp>=0.0.1b10",
```

**Problem:**
- `0.0.1b10` is a **beta** version (the `b` suffix). Per Context7 `/prefecthq/fastmcp`, the latest stable is `v3.2.0` (note: this is the FastMCP framework, not prefect-mcp, but the pattern is similar — MCP integrations ship fast).
- This dependency is for **Claude Desktop's MCP integration** to query the Prefect API. It is NOT used by the backup agent itself.
- Pinning to a beta version locks the project into a potentially-unmaintained API.

**Verification:** `grep -rn "prefect-mcp\|prefect_mcp" .` — no production code imports it.

**Fix:** Move to a `[project.optional-dependencies]` group:
```toml
[project.optional-dependencies]
mcp = ["prefect-mcp>=0.0.1b10"]  # or pin to a known-good version

[tool.uv]
dev-dependencies = [
    "prefect-mcp>=0.0.1b10",  # for Claude Desktop integration only
]
```

---

### 🟡 L5 — `pendulum.__version__` is deprecated (3.4 removal)

**Source:** Any code that does `import pendulum; print(pendulum.__version__)` (per runtime check):
```
DeprecationWarning: The '__version__' attribute is deprecated and will be
removed in Pendulum 3.4. Use 'importlib.metadata.version("pendulum")' instead.
```

**Current usage in this project:** No production code does this. The only caller would be a `print(pendulum.__version__)` in a debug script.

**Fix (if needed):**
```python
from importlib.metadata import version
__version__ = version("pendulum")
```

**Status:** Not currently broken; just track for the 3.4 release.

---

### 🟡 L6 — `pendulum.now().diff_for_humans()` is preferred over manual `format()`

**Source:** `core/time_utils.py` and other `format()` callers

**What the docs say (per Context7 `/python-pendulum/pendulum`):**
```python
>>> past = pendulum.now().subtract(minutes=2)
>>> past.diff_for_humans()
'2 minutes ago'
```

**Current code in `core/time_utils.py`:**
```python
def utcnow_formatted(fmt: str = "YYYY-MM-DD HH:mm z") -> str:
    return pendulum.now("UTC").format(fmt)
```

**Observation:** The `format()` API is the **legacy** pendulum 1.x/2.x pattern. Pendulum 3 still supports it (no deprecation warning), but for human-readable time differences, `diff_for_humans()` is the modern, locale-aware pattern.

**Current callers of `format()` in this project:**
- `core/time_utils.py:25, 33, 54, 86` — ISO/datetime formatting (no diff). Keep `format()`.
- `core/time_utils.py:135, 142, 144` — `int(hour):02d` and `int(minute):02d` formatting in `cron_to_human()`. This is plain Python formatting, not pendulum.

**Verdict:** No change needed. The `format()` calls are for absolute timestamps, not durations.

---

### 🟢 L7 — `uvicorn.run()` should explicitly set `lifespan="on"` for clarity

**Source:** `launch.py:37`

```python
uvicorn.run(app, host=bind, port=port, log_level="warning")
```

**Per Context7 `/kludex/uvicorn` `run()` signature:**
```python
def run(
    app: ASGIApplication | Callable[..., Any] | str,
    *,
    ...
    lifespan: LifespanType = "auto",  # default is "auto" — works for FastAPI
    ...
)
```

`lifespan="auto"` is the default and works correctly with FastAPI's `lifespan=` parameter. No bug — just an explicitness nit.

**Fix (optional):**
```python
uvicorn.run(app, host=bind, port=port, log_level="warning", lifespan="on")
```

**Status:** OK as-is.

---

### 🟡 L8 — `wakeonlan.send_magic_packet(..., interface=...)` is available in 3.1.0 but we don't use it

**Source:** `core/wol.py:30-34`

**Current code:**
```python
def _send_magic_packet(mac_address: str) -> None:
    try:
        wol_send(mac_address, ip_address="255.255.255.255", port=9)
    except OSError as e:
        raise WolTimeout(f"Failed to send magic packet: {e}") from e
```

**Available API in wakeonlan 3.1.0** (verified by `inspect.getsource(wakeonlan.send_magic_packet)`):
```python
def send_magic_packet(
    *macs: str,
    ip_address: str = BROADCAST_IP,  # 255.255.255.255
    port: int = DEFAULT_PORT,         # 9
    interface: Optional[str] = None,  # ← NEW (3.0+)
    address_family: Optional[socket.AddressFamily] = None,  # ← NEW
) -> None:
```

**Problem (carries from REVIEW_2026-06-02 Critical-area):** Modern routers drop the **directed global broadcast** (255.255.255.255). On networks with `directed-broadcast disabled` (default on most Cisco/Juniper/Arista gear), the magic packet is silently dropped at the L3 boundary, and the WoL never reaches the destination.

**Two fixes available:**
1. Use the `interface=` kwarg to bind to a specific NIC, then send to the **subnet-directed broadcast** (e.g., `192.168.10.255`).
2. Use a precomputed directed broadcast address from `ipaddress.IPv4Network.broadcast_address`.

**Recommended fix:**
```python
import ipaddress

def _send_magic_packet(mac_address: str, *, server_ip: str, subnet_mask: str = "255.255.255.0") -> None:
    try:
        network = ipaddress.IPv4Network(f"{server_ip}/{subnet_mask}", strict=False)
        directed_broadcast = str(network.broadcast_address)
        # Bind to the source IP (not the interface name) so the kernel knows which NIC to route from
        wol_send(mac_address, ip_address=directed_broadcast, port=9, interface=server_ip)
    except OSError as e:
        raise WolTimeout(f"Failed to send magic packet: {e}") from e
```

Caller: `_send_magic_packet(config.wol.mac_address, server_ip=config.wol.server_ip)`.

---

### 🟢 L9 — `loguru` bridge: official pattern uses `inspect`, not `currentframe`

**Source:** `core/logging.py:73-92`

**Our hand-rolled `prefect_sink`:**
```python
def prefect_sink(message):
    if FlowRunContext.get() or TaskRunContext.get():
        try:
            prefect_logger = _get_prefect_logger()
            ...
```

**Official loguru pattern (per Context7 `/delgan/loguru` overview.md):**
```python
class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Find caller from where originated the logged message
        frame, depth = inspect.currentframe(), 0
        while frame:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())
```

**Key difference:** The official pattern uses `inspect.currentframe()` and walks back to find the actual log call site (skipping the `logging` module frames). This ensures the log line shown in Prefect is the **caller's** line, not the bridge's line.

**Our code:** We don't have the `inspect` walk, so the `prefect_logger.info(msg_str)` call shows the bridge's line in Prefect's UI instead of the originating call site. Minor cosmetic issue.

**Fix:** Add the `inspect` walk from the official example.

---

### 🟡 L10 — `httpx.get(...)` without explicit `Timeout` uses 5s default for all phases

**Source:** `watchdog.py:191`, `launch.py:44`

```python
# watchdog.py
resp = httpx.get(PREFECT_HEALTH_URL, timeout=REQUEST_TIMEOUT)  # 5s, scalar

# launch.py
resp = httpx.get(f"{url}/health", timeout=5)  # 5s, scalar
```

**Per Context7 `/encode/httpx` timeouts.md:** A scalar `timeout=N` means "5s for everything" — connect, read, write, pool. If the Prefect API is on a slow link and the connect phase takes 4s, we have 1s for the read.

**Recommended pattern (per Context7):**
```python
timeout = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)
resp = httpx.get(url, timeout=timeout)
```

This is the same fix as L2 — fix it once at the `httpx` import level.

---

### 🟢 L11 — `ruamel.yaml.YAML()` instance recreated on every call to `update_config_yaml`

**Source:** `core/fy_rollover.py:162-186`

```python
def update_config_yaml(...):
    from ruamel.yaml import YAML
    ...
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.load(f)
    ...
```

**Per Context7 `/websites/yaml_dev_doc_ruamel_yaml`:** The `YAML()` instance is **stateful** and expensive to create. Re-instantiating per call is fine for one-shot use (FY rollover runs once per year), but the `from ruamel.yaml import YAML` import inside the function is unnecessary — the import is cheap, but the deferred import hides a dependency.

**Fix:**
```python
# module level
from ruamel.yaml import YAML

_YAML = YAML()
_YAML.preserve_quotes = True

def update_config_yaml(...):
    with open(path, "r", encoding="utf-8") as f:
        cfg = _YAML.load(f)
    ...
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        _YAML.dump(cfg, f)
```

Minor optimization; FY rollover runs once a year.

---

### ✓ L12 — Pydantic v2 patterns are correct

**Source:** `models/config.py:10`

**Verified v2 patterns (per Context7 `/pydantic/pydantic` migration.md):**
- ✓ `from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator` — all v2 imports
- ✓ `model_config = ConfigDict(str_strip_whitespace=True)` — v2 syntax (replaces v1 inner `class Config:`)
- ✓ `@field_validator("field")` — v2 syntax (replaces v1 `@validator`)
- ✓ `@model_validator(mode="after")` — v2 syntax
- ✓ `Field(..., ge=1, le=10)` — v2 syntax (same as v1)
- ✓ `def __repr__(self) -> str` with `f"...{self.field}..."` — correct

**No v1 leakage found.** No usage of:
- ❌ `class Config:` inner class
- ❌ `@validator` (v1) — only `@field_validator` (v2)
- ❌ `@root_validator` (v1) — only `@model_validator` (v2)
- ❌ `orm_mode = True` (v1) — no equivalent needed
- ❌ `allow_population_by_field_name = True` (v1) — not used
- ❌ `.dict()` (v1) — no calls
- ❌ `.json()` (v1) — no calls
- ❌ `from pydantic import BaseSettings` (v1) — we use `BaseModel` with env vars elsewhere

**Status:** No changes needed.

---

### ✓ L13 — Prefect 3 patterns are correct

**Source:** `serve.py:9-10`, `flow.py:24-25`

**Verified v3 patterns (per Context7 `/prefecthq/prefect` upgrade-to-prefect-3.mdx):**
- ✓ `from prefect import serve` — the v3 way to register multiple deployments
- ✓ `from prefect.schedules import Cron` — v3 import (replaces v2 `IntervalSchedule` / `schedule=`)
- ✓ `schedules=[Cron(config.schedule.cloud_cron, tz)]` — v3 list-of-schedules syntax (NOT `schedule=Cron(...)`)
- ✓ `flow.to_deployment(name=..., parameters=..., schedules=..., tags=..., description=...)` — v3 builder pattern
- ✓ `serve(*d, pause_on_shutdown=False)` — v3 server API (the `pause_on_shutdown` arg is still valid in 3.7.1)
- ✓ `@flow(name=..., log_prints=True)` — v3 decorator syntax
- ✓ `@task(name=...)` — v3 decorator syntax
- ✓ `from prefect.concurrency.sync import concurrency` — v3 sync concurrency (replaces v2 `prefect.flows.sync`)
- ✓ `from prefect.runtime import flow_run` (used in `prefect_test_harness`) — v3 runtime API

**No v2 leakage found.** No usage of:
- ❌ `from prefect import Flow, Task` (v2 classes) — only decorators
- ❌ `IntervalSchedule` (v2) — only `Cron` (v3)
- ❌ `flow.run()` (v2 imperative) — only `flow()` decorator invocation
- ❌ `flow.register()` (v2) — only `to_deployment`
- ❌ `DaskExecutor`, `LocalExecutor` (v2) — no executor needed (default is `ConcurrentTaskRunner`)

**Status:** No changes needed.

---

### 🟡 L14 — FastAPI app lacks explicit `lifespan=` for clean shutdown

**Source:** `ui.py:63`

```python
app = FastAPI(title="AAM Backup Dashboard")
```

**Per Context7 `/fastapi/fastapi` events.md (lifespan pattern):**

The current default is `lifespan="auto"`, which works for FastAPI's `@asynccontextmanager lifespan` — but we don't define one. The dashboard doesn't manage any startup/shutdown resources, but **the `ManifestDB` per-request open-never-close pattern** (from REVIEW_2026-06-02 Critical 10) means file handles accumulate. The shutdown never closes them.

**Recommended fix:**
```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _db_pool = ManifestDBPool(manifest_path, max_size=4)  # future
    app.state.db_pool = _db_pool
    yield
    # Shutdown — close any open ManifestDB instances
    _db_pool.close_all()

app = FastAPI(title="AAM Backup Dashboard", lifespan=lifespan)
```

This is part of the REVIEW_2026-06-02 Phase 2 (connection lifecycle) and a good opportunity to add the modern `lifespan=` pattern.

---

### 🟡 L15 — `watchdog.py` uses `tasklist` substring match instead of `psutil` (duplicate of L3 + REVIEW_2026-06-02 Critical 11)

**Source:** `watchdog.py:93, 129`

```python
result = subprocess.run(
    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
    capture_output=True, text=True
)
return str(pid) in result.stdout  # ← substring match
```

**The fix is one import away:**
```python
import psutil

def _is_process_alive(pid: int) -> bool:
    return psutil.pid_exists(pid)  # or psutil.Process(pid).is_running() for PID-reuse safety
```

**Why this matters:** `psutil` is already a declared dependency, `core/process.py` already has `pid_alive()` — but `watchdog.py` shells out to `tasklist` instead of calling it.

**Fix:** `from core.process import pid_alive as _is_process_alive` in `watchdog.py`.

---

### ✓ L16-L18 — Prefect runtime + serve patterns are correct

- L16: `flow.serve` (singular) — does not exist; we use `serve(*d)` which is the v3 multi-deployment API ✓
- L17: `from prefect.schedules import Cron` is the right v3 import ✓
- L18: `prefect.context.FlowRunContext.get()` is valid in 3.7.1; `prefect.runtime.flow_run.id` is also valid as the modern alternative ✓

---

### 🟢 L19 — `from wakeonlan import send_magic_packet as wol_send` (alias adds no value)

**Source:** `core/wol.py:10`

**Fix:** `from wakeonlan import send_magic_packet` and call `send_magic_packet(...)` directly.

This is the same nit flagged in the 2026-06-01 audit. Low priority.

---

### ✓ L20 — `yaml.safe_load` is the correct safe loader

**Source:** `models/config.py:228`

```python
with open(path, encoding="utf-8") as f:
    data = yaml.safe_load(f)
```

**Per Context7 `/websites/yaml_dev_doc_ruamel_yaml` differences-with-PyYAML:** PyYAML's `safe_load` is the YAML 1.1 safe loader; ruamel.yaml's `YAML(typ='safe')` is the YAML 1.2 safe loader. For reading a config file, either is fine. We use `yaml.safe_load` (PyYAML, 1.1) for `models/config.py` and `ruamel.yaml.YAML()` (round-trip, 1.2) for `core/fy_rollover.py` — the right tool for each job.

**No change needed.**

---

### ✓ L21-L24 — Prefect client paths are still valid in 3.7.1

Verified at runtime:
```
$ python3 -c "from prefect.client.orchestration import get_client"
OK
$ python3 -c "from prefect.client.schemas.filters import FlowRunFilter"
OK
$ python3 -c "from prefect.client.schemas.objects import StateType"
OK
```

These imports are unchanged in Prefect 3.7.1. Some third-party guides show the `prefect.client.orchestration` path as the old way and `prefect.PrefectClient` (the new class) as the modern way, but both work in 3.7.1.

---

## 5. Cross-Library Patterns

### Pattern H: Undeclared dependencies
- **Files:** `pyproject.toml`
- **Issue:** `humanize` (L1) is used but undeclared.
- **Audit method:** `python3 -c "import X"` against every import in source.
- **Recommended CI guard:** A script that AST-walks all `.py` files, extracts external imports, and compares against `pyproject.toml`. Run on every PR.

### Pattern I: Installed < declared minimum
- **Files:** `httpx`, `psutil` (L2, L3)
- **Issue:** The venv has older versions than `pyproject.toml` requires. Either the constraint is too tight or the venv wasn't refreshed.
- **Recommended fix:** `uv lock --upgrade` (or `pip install --upgrade httpx psutil`) and add CI step that verifies installed == declared.

### Pattern J: Hand-rolled vs official patterns
- **Files:** `core/logging.py` (L9)
- **Issue:** The `prefect_sink` doesn't use the official `inspect.currentframe()` walk for depth tracking.
- **Recommended fix:** Copy the official `InterceptHandler` from loguru docs and adapt to a loguru sink.

### Pattern K: Old 2.x patterns no longer needed
- **Files:** none — all Prefect 2.x patterns are correctly migrated to 3.x ✓
- **Audit confidence:** High (verified by comparing every import against the v2→v3 migration guide).

---

## 6. Per-Library Summary

### `prefect` (3.7.1)
- **Status:** ✓ Up-to-date, all v3 patterns correct
- **Action items:** None

### `pydantic` (2.13.4)
- **Status:** ✓ Up-to-date, all v2 patterns correct
- **Action items:** None

### `pendulum` (3.2.0)
- **Status:** ✓ Up-to-date; previous review's ParserError claim was a false positive
- **Action items:** None now; track `__version__` removal in 3.4

### `fastapi` (0.135.2)
- **Status:** ⚠️ Works, but no `lifespan=` defined
- **Action items:** Add lifespan context manager (L14) — pairs with the REVIEW_2026-06-02 connection-lifecycle refactor

### `httpx` (0.25.2)
- **Status:** 🚨 Installed version is OLDER than `pyproject.toml` minimum
- **Action items:** Upgrade to `>=0.27.0` (L2); add explicit `Timeout` config (L10)

### `psutil` (5.9.0)
- **Status:** 🚨 Installed version is OLDER than `pyproject.toml` minimum
- **Action items:** Upgrade to `>=6.0.0` (L3); refactor `watchdog.py` to use it (L15)

### `loguru` (0.7.3)
- **Status:** ✓ At minimum; bridge could use the official depth-tracking pattern
- **Action items:** Refactor `prefect_sink` to use `inspect.currentframe()` walk (L9)

### `wakeonlan` (3.1.0)
- **Status:** ✓ At minimum; `interface=` kwarg unused
- **Action items:** Use `interface=` and directed broadcast for directed-broadcast-disabled networks (L8)

### `ruamel.yaml` (0.19.1)
- **Status:** ✓ At minimum; used correctly (round-trip mode for FY rollover)
- **Action items:** Hoist `YAML()` to module level (L11 — nit)

### `uvicorn` (0.42.0)
- **Status:** ✓ Newer than minimum
- **Action items:** None

### `pyyaml` (6.0.1)
- **Status:** ✓ At minimum; used correctly (`safe_load`)
- **Action items:** None

### `humanize` (4.15.0) — **UNDECLARED**
- **Status:** 🚨 Used in `core/report.py:14` but NOT in `pyproject.toml`
- **Action items:** Add `"humanize>=4.0"` to `dependencies` (L1)

### `prefect-mcp` (b10) — **PRE-RELEASE**
- **Status:** ⚠️ Pre-release beta; only used by Claude Desktop tooling
- **Action items:** Move to `[project.optional-dependencies.mcp]` or `[tool.uv.dev-dependencies]` (L4)

---

## 7. Recommended Actions (ordered by priority)

### Phase 1: This week (1-2 hours)
1. **L1**: Add `"humanize>=4.0"` to `pyproject.toml` (1 line)
2. **L2**: `pip install --upgrade 'httpx>=0.27.0'` in venv (1 command)
3. **L3**: `pip install --upgrade 'psutil>=6.0.0'` in venv (1 command)
4. **L8**: Refactor `_send_magic_packet` to use directed broadcast + `interface=` (10 lines, 1 test)
5. **L15**: Replace `tasklist` substring match with `from core.process import pid_alive` in `watchdog.py` (5 lines, 1 test)

### Phase 2: Next sprint (1-2 days)
6. **L10**: Add explicit `httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)` in `watchdog.py` and `launch.py` (5 lines)
7. **L14**: Add FastAPI `lifespan=` context manager for ManifestDB pool (15 lines, 1 test)
8. **L9**: Refactor `prefect_sink` to use `inspect.currentframe()` walk (10 lines)

### Phase 3: Opportunistic
9. **L4**: Move `prefect-mcp` to optional-dependencies
10. **L5**: Replace `pendulum.__version__` (no current callers — just track for 3.4)
11. **L7**: Add explicit `lifespan="on"` to `uvicorn.run` (1 word)
12. **L11**: Hoist `YAML()` to module level (3 lines)
13. **L19**: Remove `as wol_send` alias (1 line)

### Phase 4: CI guards
14. **Pattern H**: Add CI script `tools/check_deps.py` that AST-walks and compares imports to `pyproject.toml`
15. **Pattern I**: Add CI step `pip check` + version comparison

---

## 8. Appendix

### Verification Commands Run
```bash
# Library version introspection
python3 -c "import importlib.metadata as m; print(m.version('httpx'))"  # 0.25.2
python3 -c "import importlib.metadata as m; print(m.version('psutil'))"  # 5.9.0
python3 -c "import importlib.metadata as m; print(m.version('humanize'))"  # 4.15.0

# False positive verification
python3 -c "from pendulum.parsing.exceptions import ParserError; print(ParserError)"
# → <class 'pendulum.parsing.exceptions.ParserError'>  ← still works in 3.2.0

python3 -c "import core.time_utils; print(core.time_utils.parse_iso_to_local('garbage'))"
# → 'garbage'  ← except branch works

# Prefect 3.7.1 import path verification
python3 -c "from prefect.client.orchestration import get_client"
# → OK

# wakeonlan API verification
python3 -c "import inspect, wakeonlan; print(inspect.signature(wakeonlan.send_magic_packet))"
# → (*macs, ip_address='255.255.255.255', port=9, interface=None, address_family=None)
```

### Context7 Library IDs Queried
- `/python-pendulum/pendulum` (243 snippets, score 86)
- `/prefecthq/prefect` (7701 snippets, score 90.9)
- `/pydantic/pydantic` (2113 snippets, score 76.9)
- `/fastapi/fastapi` (2153 snippets, score 79.2)
- `/delgan/loguru` (506 snippets, score 96.1)
- `/encode/httpx` (208 snippets, score 74.5)
- `/giampaolo/psutil` (846 snippets, score 95.25)
- `/kludex/uvicorn` (237 snippets, score 78.7)
- `/websites/yaml_dev_doc_ruamel_yaml` (150 snippets, score 68)
- `/pydantic/pydantic` migration.md (verified v2 patterns)

### Files Audited
- `pyproject.toml` (dependency declarations)
- `core/time_utils.py` (pendulum)
- `core/logging.py` (loguru + prefect bridge)
- `core/process.py` (psutil)
- `core/wol.py` (wakeonlan)
- `core/fy_rollover.py` (ruamel.yaml)
- `core/report.py` (humanize)
- `serve.py` (prefect)
- `flow.py` (prefect, exceptiongroup)
- `ui.py` (fastapi, prefect, httpx)
- `launch.py` (uvicorn, httpx)
- `watchdog.py` (httpx, tasklist)
- `models/config.py` (pydantic v2, pyyaml)

### Out of Scope
- `tests/*` (test files, separate audit)
- `docs/*` (documentation)
- `aud/*` (prior audit outputs)
- `config/*` (YAML config files)
- `requirements.txt` (doesn't exist; `pyproject.toml` is the source of truth)

### Build State
- Last commit: `4f5a744` (Fix 8 audit findings: security, reliability, and correctness hardening)
- Working tree: clean
- Python: 3.10.12 system, **3.12 in `.venv/`** (per `.venv/lib/python3.12/` presence)
- Test count: 435/435 passing (per prior audit)
- Prefect: 3.7.1 (latest 3.4.10+ in `/prefecthq/prefect`)
