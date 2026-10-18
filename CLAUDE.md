# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start here

Active work is the MVP hardening on branch `mvp-hardening`. Before doing anything, read:

1. `SESSION_HANDOFF.md` — current position, next steps, verified tool facts, gotchas (update it at the end of every session).
2. `MVP_HARDENING_PLAN.md` — the owner-approved plan: rules (§2), work packages (§7), config keys (§8), decisions DEC-1..8 (§11), checklist (§13). Do not re-plan.
3. `docs/PLAN_OPEN_TOPICS_2026-10-18.md` — the agreed plan + task checklist for the current work (tick tasks as they are committed).
4. `docs/ARCHITECTURE.md` — processes, flow step-by-step, decision points, file map, and §10 "gates" (every check each leg must pass).

**Worker agents** (asked to run tests/checks, not to build): follow `AGENT_TASKS.md` only. You never edit tracked files, and the trial box is read-only for you (worker agents only); your logs and reports go to `agent_runs/`.

`HANDOVER.md` and the many `*_REPORT*.md` / `AUDIT_*.md` files at the root are historical audit material, not current state.

## What this is

Nightly backup automation for an accounting firm on **Windows Server 2016**: `rclone sync` → GCS (cloud leg) and `robocopy /MIR` → NAS over SMB (LAN leg, with Wake-on-LAN and remote shutdown), plus FY rollover (April 1), weekly integrity audit, weekly/monthly email reports, and a FastAPI dashboard. Orchestrated by **Prefect 3** running as three NSSM services: `AamPrefectServer` (API :4200), `AamBackupAgent` (`launch.py` → dashboard thread + `prefect serve` of the deployments in `serve.py`), `AamWatchdog` (`watchdog.py`).

## Commands

The venv is at `.venv` (Python 3.12, managed by `uv`). Use the venv interpreter directly; `pyproject.toml` `addopts` is overridden because it adds `-v`.

```
# full unit suite (~3 min)
.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600

# single file / single test
.venv\Scripts\python.exe -m pytest tests/test_lan_sync.py -q -p no:cacheprovider -o addopts=""
.venv\Scripts\python.exe -m pytest "tests/test_flow_orchestration.py::TestBackupModeRouting::test_valid_modes_accepted" -q -p no:cacheprovider -o addopts=""

# audit lab: runs the REAL robocopy/rclone against a sandboxed rig (~20 min, run in background)
.venv\Scripts\python.exe -m pytest audit_lab --ignore=audit_lab/test_w7_deployed.py -q -s --timeout=1800 -p no:cacheprovider -o addopts=""
```

- Known intentional audit-lab red: `test_w16_schedule_collisions_across_year` (fixed by plan WP8). `audit_lab/test_w7_deployed.py` needs the deployed services and is excluded.
- While the audit lab runs, do not edit modules it imports: its Prefect child processes import fresh code.
- Dev tools (`pytest-timeout`, `pytest-cov`, `ruff`) are pinned in the `dev` dependency group: `.venv\Scripts\ruff.exe check .` (backlog of existing findings; not yet a gate). rclone for the audit lab is `deploy\bin\rclone.exe` (not on PATH).
- Production entry points: the NSSM services run `.venv\Scripts\python.exe launch.py` (agent), `.venv\Scripts\python.exe watchdog.py` and `-m prefect server start` directly (no `uv run` at boot, `AAM_CONFIG` set); flows via Prefect deployments. Install/ops scripts are in `deploy/`: `04_check_readiness.ps1 -ServiceAccount .\x` (runs `deploy/probe_service_account.py` as the account), then `06_install_services.ps1 -ServiceAccount .\x`.

## Architecture (big picture)

- **`flow.py`** holds every Prefect flow and task. `backup(mode)` runs `_run_cloud_pipeline` and/or `_run_lan_pipeline`; each pipeline runs inside `_backup_slot()` (Prefect global concurrency limit `aam-backup`=1 **plus** the `backup.lock` PID file the watchdog honours) and moves through phases `pre` → `sync` → `verify` (cloud only) → `post`, always writing one `run_history` row in `finally` via `_record_run`. A non-success terminal status must raise (`PartialRun`/`ExceptionGroup`) so Prefect never shows COMPLETED for a failed backup.
- **`core/`** holds the business logic, one concern per module (health, preflights, sync, verify/reporter, integrity audit, WoL, shutdown, rollover, manifest DB, reports). Outcome decisions live in a few pure functions: `cloud_sync.classify_rclone_exit` + log scan, `cloud_verify.decide_cloud_verify_result`, `lan_sync.decide_lan_result` (exit bitmask **and** completed job summary in the log), `integrity._run_rclone_check`.
- **Shared building blocks (plan §5)**; use these, do not re-implement:
  - `core/statuses.py`: the only place that defines what a status means (success set, category, label, description). The DB "last success" query, reports and dashboard all use it; never classify by string suffix.
  - `core/tool_runner.run_tool`: the only subprocess wrapper for external tools. It streams output to files, kills the whole process tree on timeout, never returns sentinel exit codes, and records to the run manifest.
  - `core/run_artefacts.py`: per-run full-log folders `<runtime>/run_logs/<day>/<run_id>/`, plus `purge_older_than()` as the single retention function. Pipelines pass the folder **path string** to tasks; tasks call `RunArtefacts.open(path)`.
  - `ManifestDB.from_config(config)`: the only way to open the SQLite DB in production code.
  - `core/notify.py`: the single alert path (records delivery in `notifications`, cooldown, dashboard link). Built in M1; replaces `report.send_failure_alert` in WP8.
  - `core/guards.py` (G1–G5), `core/approvals.py`, `core/rclone_errors.py`: M2/M3 modules; see the "built, not wired yet" table in `SESSION_HANDOFF.md`. `core/smb_ready.py` is already wired.
- **`models/config.py`**: pydantic models; every section inherits `StrictModel` (`extra="forbid"`). `CONFIG_PATH` is absolute (env `AAM_CONFIG`, else `<repo>/config.yaml`). Removed keys go in `DEPRECATED_KEYS` so they are dropped with a warning.
- **`ui.py`**: dashboard API; status text comes from `core.statuses` (`status_label`/`status_category`/`status_description`), and `static/js/dashboard.js` only renders it.
- Robocopy logs are written with `/UNILOG` (**UTF-16LE with BOM**) and read with `lan_sync._read_log_tail(..., encoding="utf-16")`; rclone logs are written with `--log-file` + `--use-json-log`.

## Project rules (owner, non-negotiable)

- Reliability over speed; no guesses. Ask the owner when an action could cause a wrong result or data loss. In code this means: fail closed, record `*_BLOCKED`, email "open the dashboard", and approve on the dashboard. Never auto-proceed.
- **Every tunable is a config key**, with its default and bounds in the pydantic model. Add a key in the same commit as the code that reads it; never put a literal at the call site.
- Reuse tools and locked libraries (rclone/robocopy flags, Prefect, stdlib, tenacity, psutil, cronsim, pendulum). New dependencies need owner approval.
- Test first: each fix gets a regression test that fails on the old code. Commit per sub-step, citing plan/finding IDs. End commit messages with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Accepted risks (do not work on): ransomware versus the LAN mirror; the GCS lifecycle keeping about 2 versions.
- **Production-trial box: full control** (owner, 2026-10-18): `ssh innoviztawinserver2016` (code `C:\AAMBackup`, runtime `C:\BackupAgent`). Deploying, restarting services, config changes and tests are allowed; tell the owner before anything that stops a backup or touches backup data.

## Testing conventions

- `tests/conftest.py` and `audit_lab/conftest.py` call `test_support.sandbox_config.install()` before any project import, so `CONFIG_PATH` resolves to a throwaway sandbox config. Tests must never use the real `config.yaml`.
- Autouse fixtures reset loguru sinks after each test and **fail any test that closes fd 0/1/2**. When mocking `mkstemp`, also patch `os.close`.
- `test_support/fake_tools.fake_run_tool(module, ...)` fakes `run_tool` and writes real-format logs (UTF-16 for robocopy, see `robocopy_log()`). `test_support/smtp_server.smtp_server()` is a real SMTP server for transport tests.
- Do not monkeypatch a Prefect `@task` with a plain function; patch `task.fn`, or patch the module attribute with a `MagicMock` whose `.with_options` returns the fake.

## Environment gotchas

- **Git Bash heredocs mangle backslashes** (`\n`, `\t`, `\\`) here, even with `<<'EOF'`. For code or text containing backslashes, use the Write/Edit tools, or a Python script file created with Write, and check diffs for stray `\n`.
- Remote PowerShell over SSH: use `;` instead of `&&`, set `[Console]::OutputEncoding=[Text.Encoding]::UTF8`, and since there is no scp, pipe scripts in: `cat x.py | ssh innoviztawinserver2016 'C:\AAMBackup\.venv\Scripts\python.exe -'`.
- `smtplib` raises `SMTPSenderRefused` for transient 4xx replies too; classify SMTP errors by reply code (`core.notify._is_permanent_smtp_error`).

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **AAM_BACKUP_V1** (26672 symbols, 64677 relationships, 413 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis. For unified PDG impact, add `mode: "pdg"` with optional `line: <N>` — it returns statement-level `affectedStatements` over CDG + REACHING_DEF and inter-procedural symbols in `interproceduralByDepth`/`byDepth`; no-layer/degraded PDG results are UNKNOWN-risk notes (`--pdg` layer). CLI equivalent: `node .gitnexus/run.cjs impact "symbolName" --direction upstream --mode pdg --line <N> --repo .`.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).
- For control/data dependence, `pdg_query({mode: "controls", target: "fileOrSymbol"})` answers "under what condition does X run?" (CDG, incl. guard clauses) and `pdg_query({mode: "flows", target, variable})` traces "where does variable Y flow?" (REACHING_DEF). `--pdg` layer.

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/AAM_BACKUP_V1/context` | Codebase overview, check index freshness |
| `gitnexus://repo/AAM_BACKUP_V1/clusters` | All functional areas |
| `gitnexus://repo/AAM_BACKUP_V1/processes` | All execution flows |
| `gitnexus://repo/AAM_BACKUP_V1/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
