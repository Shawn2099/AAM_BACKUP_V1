# Audit - orchestrator (Prefect) and dependency versions

**Date:** 2026-11-20 - read-only research (Haiku agents, PyPI / OSV / release notes), nothing changed. Locked versions
checked against `uv.lock` by hand.

## 1. Prefect: keep, do not replace now

- **Recommendation:** keep Prefect 3 for the FY overlap work (plan `docs/PLAN_FY_OVERLAP_2026-11-20.md`, D-14).
- **Why:** the code already owns most of what an orchestrator would have to replace (run history, `backup.lock`, retry
  decisions, alerts). Prefect-only pieces: cron schedules (`serve.py:35-101`), the `aam-backup` concurrency slot
  (`flow.py:194-227`), manual triggers / running checks (`ui.py:202-293`, 5 `arun_deployment` calls), artifacts, the
  run UI. The overlap work changes exactly the slot / run-id / budget code a swap would change - doing both at once
  doubles the risk.
- **Pain is in running the Prefect server as a service**, not the flow model: in-memory slot leases (fixed with
  filesystem leases), SQLite `database is locked`, a migrated `prefect.db` hanging the lab, stale deployments firing,
  IE cannot show the UI, unproven NSSM cancel path (SESSION_HANDOFF.md:82-91, 169-171, 780-784).
- **Later option if the server pain returns:** plain Python service + Windows Task Scheduler + the existing lock and
  `run_history` (about 1,000-1,500 changed lines + deploy scripts, test rework). APScheduler alone does not remove the
  server; Dagster / Airflow / Celery are overkill or need new infrastructure.
- **Do now (cheap):** keep all slot logic inside `_backup_slot` and the Prefect calls in `ui.py` / `launch.py` behind
  one small adapter, so a later swap touches one place.

## 2. Dependency versions

| Package | Locked | Latest | Verdict |
|---|---|---|---|
| **certifi** (transitive) | **2026.5.20** | 2026.10.23 | **SECURITY - upgrade now.** GHSA-6q48-6c7w-9gw2 / PYSEC-2026-4320 (critical): compromised emSign intermediate CA keys; fixed in 2026.10.18+. Needs a lock refresh (`uv lock --upgrade-package certifi`) + unit suite. |
| python-multipart | 0.0.32 | 0.0.34 | safe (patch) |
| filelock | 4.2.2 | 4.2.12 | safe (patch) |
| tenacity | 9.1.4 | 9.2.1 | safe (minor) |
| prefect | 3.7.2 | 3.9.4 | needs testing: back up `prefect.db`, server migrations at start, full audit lab; separate step after the overlap |
| fastapi / starlette | 0.136.3 / 1.8.0 | 0.165.0 / 1.10.0 | needs testing (dashboard suite) |
| pydantic / pydantic-core | 2.13.4 / 2.46.4 | 2.14.4 / 2.50.5 | needs testing |
| uvicorn, pendulum, loguru, humanize, anyio, alembic | minor behind | - | needs testing (loguru 0.8: sink fixtures and audit lab depend on it) |
| wakeonlan | 3.3.0 | 4.0.0 | do not (major) |
| cron-descriptor | 1.4.5 | 2.3.2 | do not (major; schedule display uses it) |
| sqlalchemy / websockets | 2.0.50 / 16.0 | 2.1.9 / 17.4 | do not (Prefect caps websockets <17) |
| httpx | 0.28.1 | 0.28.1 (1.0 only as dev builds) | current |
| rclone (deploy/bin) | 1.74.3 | 1.76.0 | read the changelog, sandbox test, then update |

No other advisories found for the locked versions (OSV query 2026-11-20).

## 3. httpx2

`httpx2` is a real, separate package (pydantic/httpx2, 2.16.x, "continuation" of HTTPX). Nothing in our stack needs it:
Prefect 3.9.4 still requires `httpx`, FastAPI lists `httpx2` only as an optional extra. **Do not switch**; revisit if
Prefect moves to it.

## 4. Order (owner decision 2026-11-20: everything first)

The owner chose to upgrade **all minor / patch versions before the overlap code** (plan WP0) and to use **httpx2 in our
own code** (launch.py, watchdog.py; httpx stays for Prefect / TestClient). Majors stay excluded. WP0 is green only when
the unit suite and the audit lab pass on the new versions.
