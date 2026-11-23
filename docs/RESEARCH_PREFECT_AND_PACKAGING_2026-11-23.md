# Research: Prefect replacement, packaging, and language port (2026-11-23)

Read-only research. No tracked file was edited, no commit, no tests, no services touched.
Scope: three questions from the owner. Facts are cited [S#] (external, with access date) or [R] (repo file/line, measured here). Anything from memory is marked **UNVERIFIED**. Effort figures are estimates, not measurements.

## 0. Premise checks

- **FY-overlap activation is 1 Jan 2027, not "hours away".** Today is 2026-11-23, so it is about 39 days away. The plan says the first flow run on or after 1 Jan (Fri 1 Jan 2027, 02:01 cloud) performs the activation [R docs/PLAN_FY_OVERLAP_2026-11-20.md lines 13, 23, 79]. This changes the "replace before release" option: there is time to decide, but no time to replace and soak-test before the activation.
- **Prefect 3.9.4 (the pinned version) was released 2026-11-19**, four days before this report [S1]. `pyproject.toml` pins `prefect==3.9.4`; `uv.lock` agrees [R].

---

# QUESTION A: Is replacing Prefect worth it?

## A1. Coupling measured in the code

Counts are from `git ls-files` on the tracked tree (worktree copies under `.claude/` excluded).

| Measure | Value |
|---|---|
| Product Python (excl. tests, audit_lab, chaos_harness, test_support, tools) | 82 files, 26,682 lines |
| Product files that import `prefect` directly | 7 files, 29 import lines |
| Total lines in those 7 files | 8,423 (flow.py 3748, ui.py 1946, models/config.py 1411, tools/fy_overlap_rehearsal.py 605, launch.py 461, serve.py 136, core/logging.py 116) |
| Lines in flow.py that mention prefect/concurrency/@task/@flow/with_options | 76 |
| Prefect-specific lines (estimate from grep, not an exact count) | roughly 400-500 across flow, launch, ui, serve, logging, config |
| Test functions (`def test_`) in `tests/` | 3,447 (288 files) |
| Test functions in `audit_lab/` (real robocopy/rclone/Prefect rig) | 266 (48 files) |
| Test or lab files that import or mention Prefect | 64 of 336 |
| `core/` modules that import Prefect | 0 (only `core/logging.py` has a Prefect bridge; the rest mention Prefect in comments or service names) |

Good news for a replacement: `core/` (business logic) is Prefect-free. The coupling sits in the orchestration edge: `flow.py`, `serve.py`, `launch.py`, `ui.py`, and the logging bridge.

### Prefect API surface the product uses

- **Decorators and runtime:** `@flow` (5 flows: backup, integrity-audit, weekly-report, monthly-report, rollover-check), `@task` (19 tasks), `task.with_options(retries=..., retry_delay_seconds=...)` on preflight and verify tasks only (sync is `retries=0` by design; sync retries are internal) [R flow.py].
- **Concurrency:** `prefect.concurrency.sync.concurrency("aam-backup", occupy=1, timeout_seconds=...)` as the backup slot [R flow.py 222-233]. `launch.py` upserts a global limit and a tag limit both named `aam-backup` at boot [R launch.py 80-110].
- **Scheduling:** `serve.py` builds `Cron(...)` schedules and `to_deployment(...)` for 5 deployments; schedules are passed to `prefect.serve()`. `models/config.py` imports `prefect.server.schemas.schedules.CronSchedule` to validate cron values at config-load time [R serve.py, models/config.py 1232-1250].
- **Dashboard (ui.py):** `get_client`, `read_flow_runs` (filtered by RUNNING/PENDING/SCHEDULED), `arun_deployment(timeout=0)` for manual triggers, `wait_for_flow_run` for the bounded wait, `read_deployment_by_name` (served-deployment check). Fail-closed: if Prefect does not answer, busy checks return "busy" [R ui.py 223-330, 412, 872-880, 1378-1427].
- **Launcher (launch.py):** `read_deployment_schedules`, `delete_deployment_schedule`, `pause_deployment`, `resume_deployment` (reconciles disabled legs), `set_flow_run_state(Cancelled)` for orphaned PENDING/RUNNING runs at boot, `upsert_global_concurrency_limit_by_name` [R launch.py 194-342].
- **Other:** `create_markdown_artifact` (two summary artifacts), `get_run_logger` and `FlowRunContext` (loguru bridge), `PREFECT_API_URL` and `PREFECT_HOME` env, `PREFECT_TEST_MODE=1` switch in the tests.
- **Watchdog:** talks to Prefect only over HTTP `GET /api/health` (httpx), and stops/starts the `AamPrefectServer` service [R watchdog.py 731-748].
- **Tools:** `tools/fy_overlap_rehearsal.py` reads the slot via the Prefect client; `tools/server_health/remote/db_health.py` opens `prefect.db` read-only to report failed runs and overdue schedules.

### What the product really relies on

| Feature | Used? | Where it matters |
|---|---|---|
| Cron scheduling (per-leg, reports, rollover, audit) | Yes | serve.py; schedule validation in config |
| Manual trigger from dashboard | Yes | ui.py `arun_deployment` |
| Global slot (limit 1) | Yes, critical | flow.py `_backup_slot`; a slot leak caused a real outage (see A3) |
| Run state queries (busy, active, orphan cancel) | Yes | ui.py, launch.py |
| Task retries | Small (preflight/verify only) | flow.py `with_options` |
| Prefect UI as run history | Little | The dashboard's history is the app's own `run_history` in the manifest DB [R SESSION_HANDOFF.md, core/manifest.py] |
| Artifacts (markdown summaries in the Prefect UI) | Cosmetic | flow.py 1482-1549 |
| Workers, work pools, blocks, automations, cloud | No | Not referenced |
| Results persistence, caching, event feeds | No | Not referenced |

Conclusion: the product uses Prefect as a **cron-plus-trigger-plus-one-slot server with state queries**. It does not use Prefect's distinctive features.

## A2. Repo gotchas and operational costs (from the docs)

1. **Extra service and boot order.** `AamPrefectServer` runs `-m prefect server start` on port 4200; `AamBackupAgent` depends on it; the watchdog monitors it [R deploy/06_install_services.ps1 278-346]. Measured stop chain 0.3 s / 1.05 s and restart "API 200 after about 4 s" in one lab session [R docs/RELEASE_BREAK_SESSION_2026-11-22_PART6.md line 139].
2. **Lease loss on server restart: a real outage.** A cloud run held the `aam-backup` slot when the services restarted on 20 Oct. The default lease storage is in-memory, so the new server did not know the holder, and every run waited 3600 s and ended `*_SLOT_TIMEOUT` ("no backup since 20 Oct"). Fixed by `PREFECT_SERVER_CONCURRENCY_LEASE_STORAGE=...filesystem` (commit 479d6b7), and the slot had to be reset by hand through the Prefect API [R SESSION_HANDOFF.md 136-150].
3. **`sqlite3.OperationalError: database is locked`.** A few per day since July, mostly in Prefect's own `mark_deployments_ready`; once "error during state validation" (2026-09-01). Prefect already sets `busy_timeout=60s`; the owner accepted it for the MVP, and the fix named is Postgres, which is "a new dependency, owner decision" [R SESSION_HANDOFF.md 531, 837-843]. The community reports the same pattern and advises Postgres [S15, community archive, not authoritative].
4. **Migrations at server start.** 3.7.2 to 3.9.4 runs alembic migrations on the server DB at start. There is no way back without a copy of `<PREFECT_HOME>/prefect.db` plus `-wal` and `-shm` taken with services stopped. "No script does it yet" [R SESSION_HANDOFF.md 49]. The FY-overlap rollback procedure is "more than git reset" for this reason [R SESSION_HANDOFF.md 46].
5. **Test isolation.** The local lab hangs unless `PREFECT_HOME` is a clean directory (the PC's 1.8 GB `~/.prefect/prefect.db` was migrated by another Prefect version) [R CLAUDE.md gotchas, SESSION_HANDOFF.md 226-228]. Every lab file needs its own clean `PREFECT_HOME` [R RELEASE_BREAK_SESSION_2026-11-22_PART4/5].
6. **Test-mode switch.** Unit tests set `PREFECT_TEST_MODE=1` and the slot branch skips the occupancy API, so the slot is tested only in the lab [R flow.py 222-228, tests/conftest.py 7].
7. **Footprint.** `.venv` is 241 MB; the `prefect` package alone is 50 MB. `prefect` 3.9.4 declares about 50 direct dependencies (among them docker, apprise, asyncpg, opentelemetry, pydocket, graphviz, dateparser, alembic, sqlalchemy, uvicorn, cryptography). The lock has 128 packages in total [R .venv, uv.lock]. Server RAM and CPU were **not measured**.
8. **Hidden dependency.** The app imports `cronsim` in `core/budget.py`, `core/overdue.py`, `core/email_compose.py` and `tools/fy_overlap_rehearsal.py`, but `cronsim` is **not declared** in `pyproject.toml`. It arrives only through `pydocket`, which comes from Prefect [R uv.lock 1855-1866; pyproject.toml]. Removing Prefect would require adding `cronsim` as a direct dependency, which needs owner approval per CLAUDE.md. Note this even if Prefect stays.
9. **Windows-specific Prefect problems on record:** the Prefect cancel only sets a flag in the main thread on Windows (the repo's own `core/tool_runner.py` 114 notes it); the repo says robocopy must be killed by process tree, not by Prefect.
10. **Analytics** must be off (`PREFECT_SERVER_ANALYTICS_ENABLED=False`) [R deploy/06 line 318].

## A3. Windows Server 2016 and wheels

- Prefect's PyPI page lists no OS classifiers and requires Python `>=3.11,<3.16` [S1].
- The current Python 3.12 Windows docs say 3.12 supports Windows 8.1 and newer [S2]. PEP 11 ties support to Microsoft's extended-support windows [S3]. Server 2016 is therefore inside the supported set for 3.12 (derived from S2/S3; the page does not name Server 2016).
- Server 2016 extended support ends January 2027 **[UNVERIFIED, from memory: 12 Jan 2027]**. The project's own trial box is Server 2016 and the stack ran there [R SESSION_HANDOFF.md 254].
- Wheels: the project's own check found every runtime dependency has a Windows wheel and no compiler is needed [R RELEASE_BREAK_SESSION_2026-11-22_PART6.md line 98]. This was checked by the project, not by an outside source.
- Real-machine validation on Server 2016/2019 is still listed as **not verified** for the release [R RELEASE_BREAK_SESSION_2026-11-22.md section 7].

## A4. Alternatives (scheduling and orchestration on one Windows machine)

Effort is an estimate for a replacement that keeps today's behaviour (slot, busy checks, manual trigger, cron, cancel orphans) and passes the existing tests in adapted form.

| Option | Maturity / status | Windows service fit | Footprint | Server 2016 | What must be rewritten | Effort (days) | What is lost |
|---|---|---|---|---|---|---|---|
| **Keep Prefect 3.9.4** | Apache-2.0 [S1]; new release 2026-11-19 [S1]; widely used but a large app | Works now (NSSM) | Extra service, DB, ~50 deps | Installed and running on the trial box [R] | Nothing | 0 | n/a |
| **APScheduler 3.x** | 3.x line is the stable one; 4.0 is still **pre-release** and its README says "do NOT use this release in production" [S10] | In-process, fits the agent | Small | Pure Python **UNVERIFIED** | Scheduler, trigger endpoint, slot (lock file replaces Prefect limit), busy check | 5-8 | Prefect UI; Prefect run states |
| **Windows Task Scheduler (schtasks) calling a CLI** | Built into Windows, decades old **UNVERIFIED** on release history | Best native fit; runs as the service account; survives reboots | Zero extra service | Yes (standard feature) **UNVERIFIED** | New CLI per leg (`backup.py --leg cloud --fy ...`); dashboard triggers via `schtasks /run`; busy check via `backup.lock` + manifest; cron values mapped to daily/weekly triggers (arbitrary cron needs translation) | 6-9 | Dashboard's live "running" view becomes lock-file based; approval/resume flows need re-wiring; Prefect UI |
| **Custom scheduler: cronsim + SQLite table + lock file** | cronsim already in the tree (via pydocket); the app already uses it in 4 modules | Good, it is one process | Small | Yes | ~400-600 lines of new scheduler, tests for missed runs, DST, overlap, restart | 8-12 | Anything not rebuilt; more own code to test |
| **Huey** | Mature task queue; SQLite storage and periodic tasks; default timezone UTC; periodic tasks need one designated consumer [S11] | Works, but needs an extra consumer process; the SQLite backend needs peewee [S11] | Medium (new dep) | Pure Python, probably fine **UNVERIFIED** | Task wiring, timezone handling (app is local-time), one consumer | 6-10 | Prefect UI; built-in concurrency limit (would be lock file) |
| **Dramatiq / Celery** | Mature | Need a broker (Redis/RabbitMQ) **UNVERIFIED** for the broker requirement | Broker = a second service | Celery on Windows is limited **UNVERIFIED** | Broker install, worker model | 10+ | Simplicity; same problem as Prefect plus a broker |
| **Dagster / Airflow** | Mature, large | Not a good fit for a single Windows box **UNVERIFIED** (Airflow production on Windows is not supported **UNVERIFIED**) | Large (webserver, scheduler, DB) | Doubtful | Everything | 20+ | Simplicity |
| **Temporal** | Mature; the dev server runs on Windows with SQLite; production needs MySQL, PostgreSQL or Cassandra [S8, S9] | Dev only on SQLite | Heavy for production | Unclear | Everything | 20+ | Simplicity; needs a production database server |
| **kardianos/service (Go)** | zlib licence [S16]; releases not verified in this pass | Install/start/stop as a Windows service | Single binary | Yes **UNVERIFIED** | Only relevant with a Go port (Question C) | n/a here | n/a |

## A5. Recommendation (Question A)

**Verdict: do not replace before the release or the 1 Jan 2027 activation. Replace later, only if the measured problems recur, and then migrate behind an adapter. Do not replace wholesale now.**

Reasons:
- The failures the docs record are **infrastructure** (the SQLite lock inside Prefect's own services, a lease that lived only in server memory, a migration step). Two of the three have fixes or accepted risk already. Scheduling itself has no recorded failure.
- The slot, busy check and cancel-orphans logic was just repaired after a real outage. A replacement would reopen exactly that area during the FY-overlap window, with about 39 days and 3,447 unit tests plus a lab rig built around Prefect (64 test files). The owner's rule is "reliability over speed".
- The replacement work (6-12 days plus soak) would not finish with a safe margin before 1 Jan 2027.
- What Prefect costs is real but modest: one more service and one more SQLite file. The Prefect-free `core/` means the cost of leaving it later is limited.

**Later (after the first season):** if the SQLite lock or lease problems cost a night again, the lowest-risk target is **Windows Task Scheduler calling a CLI**, with `backup.lock` as the slot and the manifest DB as history. It removes a service and the Prefect DB, adds no dependency, and runs on Windows's own scheduler. Alternative: keep Prefect and move its server DB to PostgreSQL, but that is a new dependency and an owner decision.

**Do not schedule the replacement inside** 1 Apr (rollover), 1 Jan (activation), or the monthly/weekly report windows.

### Phased, low-risk migration (if "later" is chosen)

- **P1 (about 2 days): adapter first, no behaviour change.** New module, for example `core/scheduler_port.py`, with: `register_schedules(config)`, `trigger(leg, fy, approval_id)`, `active_runs()`, `slot_state()`, `cancel_orphans()`, `publish_summary(...)`. Only the Prefect backend implements it. `flow.py`, `serve.py`, `launch.py`, `ui.py`, `watchdog.py` call the port only. Existing tests must pass unchanged. This step alone removes the scattered Prefect imports and is worth doing even if Prefect stays.
- **P2 (about 3 days): second backend** `schtasks + CLI`, selected by a new config key `scheduler.backend` (default `prefect`), with its bounds in the pydantic model (house rule: every tunable is a config key). Tests written first, each failing on the old behaviour.
- **P3 (about 2 weeks elapsed): shadow on the trial box** with the new backend in a non-production schedule, owner approval before anything that stops a backup (CLAUDE.md "tell the owner").
- **P4 (about 1 day plus watch): cutover in a quiet window**, rollback = set `scheduler.backend: prefect`. Keep `prefect.db` archived, not deleted.

Total estimate: 6-10 days of work plus the elapsed soak.

---

# QUESTION B: Packaging (no source, no Python/uv on the client box)

## B1. Options compared

| Approach | Output | Protection (decompilation) | Size | Startup | Prefect fit | Known issues | License |
|---|---|---|---|---|---|---|---|
| **PyInstaller onedir** | Folder with exe + DLLs + archive | **Weak.** Bytecode in the archive; standard extractors recover it (tool names from memory, **UNVERIFIED**) | Large (estimate 150-300 MB with Prefect, **UNVERIFIED**, from the 241 MB venv) | Fast | Needs hidden-import work (see B2) | Hooks needed for entry points and data files | GPL-2.0 with an exception that allows building commercial products; bootloader embedding allowed [S4]; changes to PyInstaller's own source fall under GPL [S4] |
| **PyInstaller onefile** | Single exe that unpacks to temp at each start | Same as onedir | Same | **Slower** (unpacks each launch; **UNVERIFIED** magnitude) | Same | **Antivirus false positives reported** on onefile and sometimes on onedir; fix is vendor reports and code signing [S5] | Same |
| **Nuitka standalone** | Native exe built via C compiler | **Stronger.** No bytecode; much harder to recover than PyInstaller (general consensus **UNVERIFIED** in numbers) | Large | Fast | Harder: compiles dynamic code paths; Prefect's dynamic imports need `--include-package` work | Build time; compiler toolchain needed; Windows Service plugin is a Commercial feature per a third-party summary [S6/S7 context] | Commercial tooling is not distributable; **compiled output can be distributed without restriction** [S6]. Commercial price shown as €250/yr on one page and €400 on another (prices changed; check) [S7]. Free edition licence name not confirmed in this pass **UNVERIFIED** |
| **cx_Freeze** | Folder or MSI | Weak (like PyInstaller) | Medium | Fast | Unknown **UNVERIFIED** | Less used for complex apps | MIT **UNVERIFIED** |
| **PyOxidizer** | Embedded Python exe | Medium | Medium | Fast | Unknown | Project activity **UNVERIFIED**; check before use | MPL-2.0 **UNVERIFIED** |
| **Briefcase** | MSI (default in stable docs) or app folder or zip [S12] | Weak (it packages Python) | Medium | Fast | Unknown | MSI build needs WiX and .NET Framework 3.5 enabled; per-user install by default, per-machine with a flag [S12] | BSD **UNVERIFIED** |
| **zipapp / shiv** | `.pyz` file | **None** (source is readable) | Small | Needs Python on the box | Yes | Needs Python installed, so fails the requirement | n/a |
| **Cython compile of modules** | `.pyd` extensions | Medium-weak; the code is still in a binary that can be disassembled | Medium | Fast | Needs compiling every module; Prefect is not compiled | Build complexity | Apache-2.0 **UNVERIFIED** |
| **.pyc-only distribution** | `.pyc` files | Weak. Bytecode is version-locked to 3.12 and decompilers lag new versions **UNVERIFIED** | Small | Needs Python 3.12 on the box | Yes | Still needs Python on the box, so fails the requirement | n/a |
| **PyArmor** | Obfuscated scripts plus a runtime library | Medium. Its docs say the obfuscation is irreversible; stronger modes cost speed [S17]. Practical bypass difficulty **UNVERIFIED** | Medium | Some overhead | Needs careful handling of entry points | Runtime library is native code; vendor is a dependency | Commercial licence for some features **UNVERIFIED** |
| **Docker** | Container | n/a | n/a | n/a | n/a | Not for Windows services on this box | n/a |

Note: `zipapp`, `.pyc` and Docker do not meet the requirement (no Python on the client box, or not a Windows service). They are listed for completeness.

## B2. Frozen Prefect and the Windows-service path

- **Dynamic imports and entry points.** Prefect discovers components through entry points and dynamic imports. PyInstaller's static analysis misses `importlib`/`__import__`-based imports, so they must be declared as hidden imports [S13]. A third-party analysis notes that hooks for an entry script are never read, and that a spawned child process's dynamic imports are not in the frozen report [S14]. The repo's `serve`, `to_deployment` and `arun_deployment` paths are exactly these cases.
- **Subprocess server.** The NSSM service today runs `-m prefect server start`. A frozen build has no `python -m`; it needs a subcommand dispatcher in the exe (`aam.exe prefect-server ...`). This is a design change to the install scripts and to the watchdog's restart path **[derived from R deploy/06 line 284]**.
- **Server assets.** The Prefect server needs its UI assets and alembic migration scripts as data files. Their exact collection list was **not verified**; it must be tested on the frozen build.
- **Practical verdict:** freezing Prefect is the single largest risk of the packaging work, larger than the packaging itself. An unverified guess is 3-5 days of hook work and an open-ended test surface.
- Prefect's own community answers were not a reliable source for frozen behaviour in this pass; no official Prefect-on-PyInstaller guidance was found.

## B3. What protection each really gives

- PyInstaller and Briefcase: the code can be recovered from the package with low effort. Treat it as **no protection against a determined reader**, only against casual browsing.
- PyArmor: raises the cost. Its docs claim irreversibility [S17], but the repo has no independent evaluation. Treat as **medium, unquantified**.
- Nuitka: compiled C is substantially harder to recover than bytecode. It is still not secret; it is a cost barrier.
- **The real protection for a client is contractual and licence-based.** Source held in a private repo, delivery under a licence that forbids reverse engineering, and deployment by the vendor. Technical obfuscation adds only a cost barrier.

## B4. Other points

- **Code signing:** an Authenticode certificate is needed to reduce SmartScreen and antivirus prompts. Cost and EV requirements **UNVERIFIED** (from memory). Signing reduces but does not remove false positives [S5].
- **Antivirus:** the 2021 developer advice in S5 is to report false positives to each vendor, sign the exe, and expect to repeat reports per release. Onedir is lower risk than onefile. Defender on Server 2016 behaves as on Windows 10 **UNVERIFIED**; this needs a real test.
- **Startup:** onefile unpacks to a temp folder on each start; onedir does not. Magnitudes **UNVERIFIED**; measure on the trial box.
- **NSSM with an exe:** NSSM can run any executable; the install scripts change from `.venv\Scripts\python.exe launch.py` to `aam.exe agent`. Works in principle; test the stop/restart chain (the watchdog depends on it) **[R deploy/06, watchdog.py]**.
- **Updates on the client box:** replace the onedir folder, keep the data folder and the DBs. Schema migrations must run through the new binary. Rollback = previous folder. The FY-overlap rollback procedure already documents this kind of step [R SESSION_HANDOFF.md 46].
- **Debugging without source:** tracebacks show module paths but not source. Keep a private map from each build to the exact commit and the source. Nuitka can produce debug symbols for the C layer; PyInstaller has no equivalent **UNVERIFIED**.
- **Licences of bundled tools:** rclone (MIT **UNVERIFIED**) and NSSM (public domain **UNVERIFIED**) must be shipped with their licence files. Prefect is Apache-2.0 and needs its NOTICE/licence text in the bundle [S1]. PyInstaller's exception covers bundling, not modifications [S4].

## B5. Recommendation (Question B)

**Approach for a first release: PyInstaller onedir** (not onefile), built on a private build machine from a pinned lock file, with:
- `aam.exe` plus subcommands (`agent`, `watchdog`, `prefect-server`, `flow`), replacing the `python -m` calls in the install scripts;
- `rclone.exe` and `nssm.exe` bundled with their licence files;
- Authenticode code signing;
- hidden imports and data files for Prefect declared in a spec file and tested on the frozen build;
- delivered as a zip with an install script; source kept private.

Effort: **6-10 days** (packaging and hooks 3-5, install-script and service changes 1-2, Server 2016 and Defender testing 2-3), plus the frozen-Prefect risk above (open-ended).

**Nuitka** is the stronger protection but should wait: it needs a compiler toolchain, the Prefect compile path is untested, and the Windows Service plugin is a commercial feature (check price and terms first).

**Timing:** start the **legal/contract route now** (licence text and delivery terms cost nothing in engineering and protect the source most). Do the **packaging after the first season**, unless the client's contract requires a binary earlier. Packaging during the 1 Jan activation or the April rollover is the wrong time.

**Legal and practical alternative to hiding source:** a licence (proprietary, no reverse engineering, no redistribution) plus a service agreement in which the vendor operates the box. Source stays in a private repository. This is the only route that gives real protection; the technical routes only raise cost.

---

# QUESTION C: Port to Go, Rust or .NET

## C1. Size and test surface

- Product Python: 26,682 lines in 82 files (excl. tests and tools). Tools: 1,695 lines. Tests: 3,447 test functions and 266 lab test functions.
- The port is not only the code: the 3,447 tests and the lab rig encode incident-driven behaviour (for example the slot-lease repair and the robocopy/rclone exit-code logic). A port that passes "the same tests" would need them rewritten, and they would still only cover what they cover.
- **Effort estimate (UNVERIFIED, heuristic):** the business logic is roughly 20-30k lines in C# or Go, and the test port is comparable in work. Estimate 4-6 person-months for a behaviour-equivalent port with the break-it testing the repo has been doing (weeks of it).

## C2. Windows-native alternatives

- **.NET Worker Service** running as a Windows service: native to Windows, single runtime, Microsoft support **UNVERIFIED** details. Gains: no Python runtime; compiled. Loses: the Python libraries (rclone and robocopy wrappers are fine, but pydantic config, loguru, FastAPI dashboard and Prefect-free scheduling would need new equivalents).
- **Go + kardianos/service**: Zlib licence [S16]; installs, starts and stops Go programs as Windows services. Single binary, no runtime, mature enough for services **UNVERIFIED** release detail. Gains: single exe. Loses: same rewrite cost.
- **Rust**: possible, but the largest learning and rewrite cost of the three for this kind of service **UNVERIFIED**.

## C3. Verdict (Question C)

**Never, for now.** The port buys a single exe and compiled code (useful for C2 protection), but the same protection is available for much less effort through B5 (PyInstaller onedir plus contract). It costs 4-6 person-months and puts the verified behaviour at risk. Revisit only if (a) the client requires no Python and no packaging is accepted, (b) the product is being rebuilt for another reason, or (c) the scope is frozen after a stable season and the team wants a single native exe for maintenance.

---

# SOURCES

- [S1] PyPI, prefect 3.9.4 (fetched 2026-11-23): release date 2026-11-19; Requires-Python `<3.16,>=3.11`; licence Apache-2.0; no OS classifiers. https://pypi.org/project/prefect/3.9.4/
- [S2] Python 3.12 docs, "Using Python on Windows" (search summary, accessed 2026-11-23): supports Windows 8.1 and newer. https://docs.python.org/3.12/using/windows.html
- [S3] PEP 11, CPython platform support (accessed 2026-11-23). https://peps.python.org/0011
- [S4] PyInstaller licence page (search summary, accessed 2026-11-23): GPL-2.0 with an exception for commercial products; modifications under GPL. https://pyinstaller.org/en/latest/license.html
- [S5] Python Discourse, "Pyinstaller - false positive?" (2021 thread, accessed 2026-11-23): AV false positives, onefile and onedir; report to vendors, sign the exe. https://discuss.python.org/t/pyinstaller-false-positive/43171
- [S6] Nuitka, Commercial licence (fetched 2026-11-23): compiled output may be distributed without restriction; commercial tooling not distributable; contributions relicensed Apache-2.0. https://nuitka.net/doc/commercial-license.html
- [S7] Nuitka, purchase page (search summary, accessed 2026-11-23): tiers shown as €250/yr and €400/yr on different pages; check current prices. https://nuitka.net/commercial/purchase.html
- [S8] Temporal docs, dev server (search summary, accessed 2026-11-23): SQLite is for development and testing only. https://docs.temporal.io/develop/run-a-development-server
- [S9] Temporal docs, self-hosted guide (search summary, accessed 2026-11-23): production uses MySQL, PostgreSQL or Cassandra. https://docs.temporal.io/self-hosted-guide
- [S10] APScheduler README (search summary, accessed 2026-11-23): 4.0 is a pre-release, "do NOT use this release in production". https://raw.githubusercontent.com/agronholm/apscheduler/HEAD/README.rst
- [S11] Huey guide (search summary, accessed 2026-11-23): SQLite storage, periodic tasks, UTC default, one consumer for periodic tasks. https://huey.readthedocs.io/en/latest/guide.html
- [S12] Briefcase, Windows reference (search summary, accessed 2026-11-23): MSI default in stable docs; per-user by default; WiX; .NET Framework 3.5. https://briefcase.beeware.org/en/stable/reference/platforms/windows/
- [S13] PyInstaller, common issues and pitfalls (accessed 2026-11-23): hidden imports for dynamic imports. https://pyinstaller.org/en/latest/common-issues-and-pitfalls.html
- [S14] hookfix, PyPI (search summary, accessed 2026-11-23): hooks for the entry script are not read; child-process dynamic imports are not analysed. https://pypi.org/project/hookfix
- [S15] Prefect community archive (search summary, accessed 2026-11-23): SQLite "database is locked" reports; answers partly AI-generated, treat as leads only. https://linen.prefect.io/archive
- [S16] libraries.io, kardianos/service (search summary, accessed 2026-11-23): Zlib licence; Windows service install/start/stop. https://libraries.io/go/github.com%2Fkardianos%2Fservice
- [S17] PyArmor docs, latest (search summary, accessed 2026-11-23): obfuscation described as irreversible; stronger modes trade speed. https://pyarmor.readthedocs.io/en/latest/index.html
- [R] Repo files cited inline: flow.py, serve.py, launch.py, ui.py, watchdog.py, models/config.py, core/logging.py, deploy/06_install_services.ps1, pyproject.toml, uv.lock, SESSION_HANDOFF.md, CLAUDE.md, docs/RELEASE_BREAK_SESSION_2026-11-22*.md, docs/PLAN_FY_OVERLAP_2026-11-20.md.

Not verified in this pass: Server 2016 extended-support end date; Windows Service plugin pricing; exact PyInstaller/Nuitka/PyArmor bypass difficulty; Prefect server RAM/CPU; release histories and star counts for APScheduler, Huey, Dramatiq, Celery, Dagster, Airflow; .NET Worker Service details; rclone and NSSM licence text. Every item in these groups is marked UNVERIFIED where it appears.

---

# DECISION SUMMARY (one page, for the owner)

**Context:** the FY-overlap activation is **1 Jan 2027 (about 39 days away)**. Prefect 3.9.4 was released 2026-11-19. The product's core logic is Prefect-free; Prefect is the scheduler, trigger, slot and state layer.

**A. Replace Prefect?**
- **Decision: Later, not now. Keep Prefect through the release and the first season.**
- Why: the recorded failures are infrastructure (SQLite lock, lease lost on server restart, migration risk), two are fixed or accepted, and no scheduling failure is recorded. Replacing now reopens the slot and busy logic that was just repaired, inside the FY-overlap window.
- If later: adapter module first (no behaviour change), then Windows Task Scheduler + CLI behind a config switch, shadow run on the trial box, cutover in a quiet window.
- Effort: about 6-10 days of work plus about 2 weeks of shadow running.
- Top 3 risks: (1) slot and busy-check regression during cutover; (2) silent loss of the "no backup running" guarantee; (3) cutover that coincides with a backup or rollover.

**B. Package the product (no source, no Python on the client box)?**
- **Decision: Now, the contract and licence route (no engineering). Packaging later, after the first season.**
- Approach when packaging: PyInstaller onedir (not onefile), signed, with rclone.exe and nssm.exe bundled with licences; source kept private.
- Why: PyInstaller gives weak protection; the real protection is contractual. Nuitka gives more protection but needs a commercial feature for Windows services and an untested Prefect build.
- Effort: 6-10 days for packaging, plus open-ended risk from freezing Prefect.
- Top 3 risks: (1) Prefect does not run frozen (entry points, hidden imports, server subprocess); (2) antivirus false positives block the install on the client box; (3) update and rollback path becomes harder to debug without source.

**C. Port to Go, Rust or .NET?**
- **Decision: Never, for now.**
- Why: about 27k lines plus 3,447 tests and a lab rig, with incident-driven behaviour; 4-6 person-months; buys a single exe that packaging (B) gives for far less.
- Revisit only if the product is rebuilt for another reason, or the scope freezes and a native single exe is wanted.
- Top 3 risks: (1) behaviour drift from undocumented incident fixes; (2) months of testing with no gain in backup results; (3) a second codebase to maintain during the transition.

**Action items for the owner (no code changed today):**
1. Confirm the 1 Jan 2027 activation date stands (the plan is the authority).
2. Decide on the contract/licence route for the source.
3. Decide whether `cronsim` becomes a direct dependency (needed whether or not Prefect stays; new dependency, owner approval).
4. Decide whether to schedule the adapter refactor (P1) after the release; it removes scattered Prefect imports at low risk.
