# AAM Backup Automation (`v2-lean`) — Final Stack & Dependency Specification

> **Goal:** Zero-headache, client-server-ready backup appliance for Windows Server 2016+ (**Python `>=3.14,<3.15`**) with maximum reliability, minimal custom glue code, and modern, non-deprecated dependencies.

---

## 1. Architecture Summary (2 Peer Services + Watchdog)

Instead of 3 tightly coupled Prefect services (`AamPrefectServer` → `AamBackupAgent` + `AamWatchdog`), `v2-lean` runs **2 independent Windows services** managed by **Servy**, plus a lean health/overdue watchdog:

1. **`AamUI` (`ui.py` via `uvicorn`, `workers=1`)** — FastAPI dashboard over local HTTPS, reading `manifest.db` and enqueuing manual jobs into `huey.db`.
2. **`AamWorker` (`huey_consumer -k thread -w 2`)** — Executes scheduled (`@periodic_task`) and manual (`@task`) backup, report, rollover, and audit jobs.
3. **`AamWatchdog` (`watchdog.py`)** — Monitors `worker.heartbeat` (stale deadlock detection), runs overdue backup checks (`core/overdue.py`), checks disk/key readiness, and uses a `last_good_alerts` fallback if `config.toml` or `manifest.db` is broken.

---

## 2. Final Python Runtime Dependencies (`[project.dependencies]`)

| Package | Recommended Spec | Verified Latest | Role | Replaces / Notes |
|---|---|---|---|---|
| **huey** | `>=3.5.4` | `3.5.4` | Persistent task queue & cron scheduler (`SqliteHuey`) | Replaces `prefect`, `serve.py`, Prefect lifecycle in `launch.py`/`watchdog.py`, and Prefect log bridge in `core/logging.py`. |
| **fastapi** | `>=0.165.0` | `0.165.0` | Web dashboard API & UI | Upgrade from `0.136.3`. |
| **uvicorn** | `>=0.54.0` | `0.54.0` | ASGI server (`workers=1`) | Upgrade from `0.48.0`. |
| **pydantic** | `>=2.14.4` | `2.14.4` | Data & schema validation | Upgrade from `2.13.4`. |
| **pydantic-settings** | `>=2.16.3` | `2.16.3` | Typed config loading (`TomlConfigSettingsSource`) | Replaces manual config loading boilerplate in `models/config.py`. |
| **tomlkit** | `>=0.15.2` | `0.15.2` | Comment-preserving TOML edits | Replaces `ruamel.yaml`, `pyyaml`, and YAML round-trip helpers in `core/config_file.py`. *(Reads use stdlib `tomllib`.)* |
| **peewee** | `>=4.4.0` | `4.4+ / 4.5+` | Lightweight SQLite ORM + native `pwmigrate` | Replaces ~1,400+ lines of raw `sqlite3` DDL & manual `SCHEMA_VERSION` migrations in `core/manifest.py`. |
| **pwdlib[argon2]** | `>=0.4.2` | `0.4.2` | Password hashing (`Argon2id`) | Replaces custom PBKDF2-SHA256 in `core/admin_auth.py`. |
| **pyrate-limiter** | `>=4.0.0` | `v4.x` | In-memory / SQLite rate limiting (`MemoryListBucket`) | Replaces custom `_RATE_LIMITS` dict + lock in `ui.py` via a ~10-line FastAPI `Depends` helper (no `fastapi-limiter` needed). |
| **itsdangerous** | *(Starlette extra)* | Active | Signed cookie sessions (`SessionMiddleware`) | Replaces in-memory `_sessions` dict in `ui.py` so sessions survive `AamUI` restarts. |
| **jinja2** | *(FastAPI extra)* | Active | Dashboard HTML + Email HTML templating | Already installed; replaces manual HTML string concatenation in `core/email_compose.py`. |
| **fpdf2** | *(Latest PyPI)* | Active | PDF report generation (weekly/monthly) | Pure Python, zero external DLLs (unlike `weasyprint` which breaks in Nuitka on Windows). |
| **loguru** | `>=0.8.0` | `0.8.0+` | Structured file logging & rotation | Upgrade from `0.7.3` (fixes Windows clock-skew rotation edge cases). |
| **tenacity** | `>=9.2.1` | `9.2.1` | Inline micro-retries only | Upgrade from `>=8.5.0`; keep only for SMTP `4xx` (`core/notify.py`) and SQLite lock retries (`core/manifest.py`). |
| **filelock** | `>=4.2.2` | `4.2.2` | Cross-process OS file locking | Already past `4.2.1` CVE fixes; simplifies `core/process.py`. |
| **psutil** | `>=7.2.2` | `7.2.2` | Process inspection & tree termination | Keep as-is. |
| **python-multipart** | `>=0.0.32` | `0.0.32` | FastAPI form/upload parsing | Already past `0.0.30` DoS/traversal CVE fixes. |
| **cronsim** | `>=2.7` | `2.7` | Cron expression evaluation | **Must add explicitly** (was previously transitive via `prefect`); used in `core/budget.py`, `core/email_compose.py`, `core/overdue.py`, and Huey boot catch-up. |
| **cron-descriptor** | `>=1.4.5` | `1.4.5` | Human-readable cron labels for UI | Keep to avoid ~150+ lines of custom cron-to-English code. |
| **humanize** | `>=4.15.0` | `4.15.0` | Human-friendly sizes & durations | Keep. |
| **tzdata** | *(IANA tz db)* | Active | Windows timezone database for `zoneinfo` / `pendulum` | Required on Windows for IANA timezones (`Asia/Kolkata`). |
| **pendulum** | `>=3.3.0` | `3.3.x` | IST formatting & date math | Keep existing working formatting/parsing code; `3.3.0+` fixes Windows/Python 3.12+ `zoneinfo` overhead. |
| **pywin32** *(skip for Nuitka)* | *(Windows API)* | Active | — | **Not recommended** for Nuitka `--standalone` (DLL/`win32timezone` quirks); keep existing zero-dep `ctypes` DPAPI in `core/secrets.py`. |
| **httpx2** *(when needed)* | *(Latest PyPI)* | Active | Modern HTTP/2 client | Successor to `httpx`. Add when enabling the dead-man's switch provision (`Healthchecks.io`). |

### Removed Dependencies
* **`prefect`** — Replaced by `huey` (`SqliteHuey`).
* **`pyyaml` & `ruamel.yaml`** — Replaced by stdlib `tomllib` + `tomlkit`.
* **`wakeonlan`** *(optional removal)* — Can be replaced by ~6 lines of stdlib `socket` in `core/wol.py` (or kept as-is).

---

## 3. Dev / Test Dependencies (`[dependency-groups].dev`)

| Package | Recommended Spec | Notes |
|---|---|---|
| **pytest** | `>=9.1.1` | Test runner |
| **pytest-cov** | `>=7.1.0` | Coverage |
| **pytest-timeout** | `>=2.4.0` | Hang protection |
| **hypothesis** | `>=6.168.5` | Property-based tests |
| **time-machine** | `>=3.5.1` | Calendar/FY rollover clock simulation |
| **ruff** | `>=0.14.0` | Linter & formatter |
| **httpx** | `>=0.28.1` | Required by `fastapi.testclient.TestClient` |
| **nuitka** | *(Latest PyPI)* | Compiles Python → native Windows C/C++ `.exe` (`--standalone`) |

---

## 4. External Windows Server Tools & Packaging

| Tool | Version / Build | Role | Notes |
|---|---|---|---|
| **Servy** (`aelassas/servy`) | `v11.0` (self-contained) | Windows Service Wrapper | Replaces `nssm.exe` (2014). Supports Windows Server 2016+ without `.NET` pre-install; provides recursive process-tree kill, graceful `Ctrl+C`, size/date log rotation, PowerShell/CLI, and Manager GUI. |
| **Nuitka** + **Inno Setup** | Latest stable | Native `.exe` compiler + `.exe` installer | Compiles Python → native machine code (`--standalone` folder, **not** `--onefile`) to protect source IP and avoid PyInstaller AV false positives; **Inno Setup** wraps `.dist` + `rclone.exe` + `Servy` into a single `AAMBackupSetup.exe` wizard (and runs service install/uninstall silently). Sign both `.exe`s with a Code Signing Certificate. |
| **rclone** | `1.74.x` | Data transfer & verification | Keep native `subprocess` + `--use-json-log` (`core/tool_runner.py`). Do not replace with Python rclone wrappers. |
| **Local HTTPS (`Caddy`)** | `Caddy` (`tls internal`) | LAN HTTPS for Dashboard | **Chosen** for hands-off cert auto-renewal + HTTP→HTTPS redirect under Servy (enables `https_only=True` on `SessionMiddleware`). |

---

## 5. Non-Negotiable Operational Rules for Zero-Headache Deployment

1. **Huey SQLite isolation:** Keep `huey.db` separate from `manifest.db` (both in `paths.runtime_dir`, WAL mode, generous busy timeout). Run `huey_consumer` with `-k thread -w 2 --delay 0.5 --max-delay 30 --backoff 1.2` (never `-k process` on Windows).
2. **Global backup lock:** Keep `@huey.lock_task` + `backup.lock` (`filelock`) so scheduled and manual runs never overlap.
3. **Boot catch-up:** On `AamWorker` startup (`@huey.on_startup`), check `cronsim` vs `run_history` so power outages/reboots never silently skip a nightly backup.
4. **Watchdog resilience:**
   * Huey touches `worker.heartbeat` every 5 min; watchdog restarts `AamWorker` if stale > 15 min and no `backup.lock` is held.
   * Snapshot SMTP alert config to `last_good_alerts.json` on every valid load so corrupt `config.toml` or unreadable `manifest.db` still sends `CRITICAL` alerts.
5. **Dead-Man's Switch Provision (Inactive for Now):**
   * Include `healthcheck_url: str | None = None` in config + a Huey `@huey.signal(SIGNAL_EXECUTING, SIGNAL_COMPLETE, SIGNAL_ERROR)` listener that no-ops when `None`. Enabling `Healthchecks.io` later requires only setting the URL in `config.toml`.

---

## 6. Nuitka (`--standalone`) Packaging Rules (No Runtime Surprises)

Use **one** unified CLI entry point (`aam.exe ui` / `aam.exe worker` / `aam.exe watchdog`) compiled with Nuitka `--standalone` (never `--onefile`, which triggers temp-unpack AV heuristics) and packaged with **Inno Setup**:

1. **No string-based dynamic imports:**
   * Pass the imported `app` object to `uvicorn.run(app, ...)` (not `"ui:app"`).
   * Start Huey via Python (`Consumer(huey, ...).run()`) with task modules explicitly imported, instead of `huey_consumer` CLI string loading.
2. **Explicit Nuitka includes:**
   * `--include-package=argon2,argon2_cffi_bindings,pydantic,pydantic_core,tzdata,huey,peewee,tomlkit,fastapi`
   * `--include-module=sqlite3,uvicorn.logging,uvicorn.loops.auto,uvicorn.protocols.http.auto,uvicorn.protocols.websockets.auto,uvicorn.lifespan.on`
   * `--include-data-dir=templates=templates --include-data-dir=static=static`
3. **`pywin32` caution:** `pywin32` can hit frozen DLL/`win32timezone` quirks under Nuitka; keeping the existing zero-dep `ctypes` DPAPI in `core/secrets.py` (89 lines) avoids `pywin32` packaging issues altogether unless `win32serviceutil`/`win32job` is strictly needed.
4. **Code Signing:** Sign `aam.exe` and `AAMBackupSetup.exe` with `signtool.exe` so Windows SmartScreen and client AVs trust the binaries cleanly.

