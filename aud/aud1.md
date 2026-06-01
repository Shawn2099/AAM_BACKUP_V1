Now I have a comprehensive understanding. Let me produce the final audit report.
Chunk 1 Audit: flow.py + ui.py
Functions analyzed
flow.py (22 functions, 666 lines)
Function	Summary	Correct?
_stable_run_id (L47)	Generates stable run_id: {flow_run.id}-{mode} if Prefect context, else uuid4-{mode}	Yes, but the fallback is a bare except Exception swallowing everything. Not symmetric with cloud/lan retries — it's only stable per-pipeline, not across pipelines
health_check_task (L64)	Logs and delegates to pre_backup_health()	Yes — but pre_backup_health signature is now (source_path, mode, gcs_key_path) while tests assume the old (config, mode); the positional unpacking config.paths.source_drive works but is fragile **[FALSE — no tests exist that call pre_backup_health; grep across tests/ returned zero matches]**
cloud_preflight_task (L75)	Calls run_cloud_dry_run, raises on not ok	Yes
cloud_sync_task (L93)	Calls run_cloud_sync, raises on CLOUD_FAILED	Yes
cloud_verify_and_report_task (L116)	Opens temp_rclone_config context, runs verify + size + manifest + diff	Yes — but no try/except; if verify_cloud_integrity raises, the temp_rclone_config context manager still cleans up via finally
cloud_record_task (L157)	Calls record_sync_results(db, "cloud", manifest, removed), logs count, closes DB	Yes — sync_result parameter is accepted but never read; should be removed or used
wol_check_task (L174)	No-op when WoL disabled, else calls ensure_server_online	Yes
lan_preflight_task (L184)	run_lan_dry_run, raises on failure	Yes
lan_snapshot_before_task (L197)	Returns snapshot_to_dict(walk_lan_destination(...))	Yes
lan_snapshot_after_task (L206)	Same as before but for "after"	Yes — function is near-identical to lan_snapshot_before_task; could be one helper parametrised by label
lan_sync_task (L216)	run_lan_sync, raises on LAN_FAILED	Yes
lan_record_task (L230)	Computes diff_snapshots then record_sync_results, but does not return the diff	Yes, but wasteful — caller _run_lan_pipeline re-runs diff_snapshots at L467
lan_shutdown_task (L250)	Skips if either flag off, else shutdown_server in try/except	Yes
cloud_publish_artifact_task (L263)	Builds Markdown and calls create_markdown_artifact	Yes — but see duplication with lan_publish_artifact_task
lan_publish_artifact_task (L296)	Same shape as cloud version	Yes — but structurally a copy-paste of cloud's
_run_cloud_pipeline (L334)	Orchestrates: health → preflight → sync → verify → record → diff → artifact	Yes — but contains inline entry-normalization (Path/Size/ModTime → path/size/mtime) that duplicates core/backup_repository.py:30-37; also re-iterates manifest instead of getting differential from cloud_record_task
_run_lan_pipeline (L436)	Orchestrates: health → WoL → preflight → before → sync → after → record → diff → artifact → shutdown	Yes — calls diff_snapshots twice (once inside lan_record_task, once at L467); lan_shutdown_task is intentionally inside try-block per L484-487 comment, but if lan_record_task raises the server is not shut down
_record_run (L504)	Computes ended_at, duration, calls record_run_history, closes DB	Yes — but duration uses time.time() - pendulum.parse(started_at).timestamp(), duplicating date-parsing logic that belongs in core/time_utils
weekly_report_flow (L531)	Loads config, configures logging, calls send_weekly_report	Yes — but from core.report import send_weekly_report is a function-local import (L544), unnecessary indirection since the module is already imported in report.py itself
monthly_report_flow (L551)	Same as weekly for month	Yes — same function-local import smell at L564
backup (L575)	Top-level @flow: validates mode, configures logging, acquires backup.lock file, runs pipelines under concurrency()	Mostly correct — see "lock file" and "ExceptionGroup" issues below
ui.py (29 functions, 664 lines)
Function	Summary	Correct?
_check_rate_limit (L44)	Sliding-window per-key, evicts expired entries	Yes — only call site uses f"trigger:{ip}" and f"report:{ip}" namespace prefixes, but the function takes a pre-namespaced key, so caller is responsible for uniqueness
_create_session (L71)	Generates token, stores with created_at, calls cleanup	Yes
_cleanup_expired_sessions (L78)	Iterates sessions, deletes expired	Yes — but duplicates the expiry check that _validate_session also does (L93). Two slightly different code paths for the same predicate
_validate_session (L87)	Returns True/False, deletes expired inline	Yes — duplicates the > _SESSION_TTL.total_seconds() predicate (L82 vs L93)
_get_api_key (L99)	Returns configured key or empty string	Yes
_auth_enabled (L104)	Returns config flag	Yes
_check_api_key_header (L108)	Constant-time compare via hmac.compare_digest	Yes
_cfg (L120)	Lazy module-level singleton	Yes — but get_db (L130) does the same thing; two singletons for two related resources; could be unified
get_db (L130)	Lazy module-level singleton DB	Yes — note _render_dashboard does db = get_db() if db_path.exists() else None so the singleton is only used when DB exists
_is_running (L140)	One-liner wrapper: return await _prefect_has_active_run(pipeline)	Yes but redundant — wrapper adds zero value, just call _prefect_has_active_run directly
_prefect_has_active_run (L145)	Queries Prefect for RUNNING/PENDING flow runs, checks tags and parameters["mode"]	Yes — silently swallows Exception and returns False (L166-168); if Prefect is down, every trigger returns "not running" and starts a duplicate. The logger.error is right but the return value is fail-open
_run_in_background (L174)	Re-checks active runs, then await run_deployment(name=f"aam-backup/backup-{pipeline}")	Yes — hardcodes the aam-backup/ deployment prefix, which is the flow name. If flow is renamed this silently breaks
login_page (L196)	Renders HTML form via f-string concatenation	Yes — but the entire HTML is built by string concatenation, error-prone (must escape error); current error param is hard-coded to "Invalid+API+key" by caller, so escaping is implicit
login_submit (L226)	Verifies key, creates session, sets cookie	Yes — passes str(api_key) to compare_digest (good) but the else branch returns RedirectResponse("/login?error=Invalid+API+key", 303) without going through the rate limiter (_check_rate_limit is not applied on login!)
logout (L243)	Redirects to /login, deletes cookie	Yes
_require_auth (L249)	Checks session OR API-key header; HTML→303, API→401	Yes — but the 303 for missing session is a redirect, not a 401; an API client following a 303 may be confused. Also: when Accept is */* (e.g. curl with no header), neither branch may match and 401 wins — correct
dashboard (L266)	Auth + render	Yes
status (L272)	Returns JSON with last run, FY prefix, health, manifest stats	Yes — calls Path(cfg.paths.database_path).exists() to early-return 503; this is a race with the get_db singleton that always opens the DB. If file is deleted between the check and the get_db() call, the call will hit sqlite3.OperationalError
health (L324)	Unauthenticated {"status": "healthy"}	Yes — but source_drive could be D:\ on Windows which Path.exists() handles; the except Exception at L334 swallows everything and returns healthy, hiding real errors
trigger_cloud (L339) / trigger_lan (L351)	Rate limit, active check, schedule background task	Yes — but the two functions are near-identical (copy-paste with "cloud" vs "lan"). Could be _trigger(pipeline: str)
report_weekly (L366) / report_monthly (L376)	Rate limit, call _serve_report(7, "Weekly") / _serve_report(30, "Monthly")	Yes — same copy-paste smell
trigger_weekly_email (L388) / trigger_monthly_email (L425)	Build HTML, send via SMTP, return status JSON	Yes — same copy-paste; both call generate_report_html then send_*_report with body_html=...
_serve_report (L461)	Build HTML doc, return as Response with Content-Disposition: attachment	Yes — hardcodes 5 days/30 days via caller; magic number pendulum.now().format("YYYY-MM-DD") in filename; re-imports generate_report_html from core.report on every call
_get_last_success (L511)	db.last_run(mode), returns ended_at if status ends with _COMPLETE	Yes — calls DB; could share with _last_run_summary (L519) which also calls db.last_run(mode)
_last_run_summary (L519)	Dict-shape of last run for dashboard	Yes — duplicates DB call from _get_last_success
_get_health (L534)	shutil.disk_usage on source drive	Yes — fails-open: any exception → {"error": "unavailable"}
_render_dashboard (L548)	Builds 20+ dashboard vars, calls render_dashboard(...)	Mostly correct — see god-function note below; L627 finally: pass  # singleton — do not close is dead structure
Cross-file duplications found
Duplication	Locations	Severity
Entry-key normalization (Path/Size/ModTime → path/size/mtime)	core/backup_repository.py:30-37 AND flow.py:380-382 (inside _run_cloud_pipeline)	HIGH — re-iterates the same manifest, both call sites must stay in sync. Fix: extract normalize_entry(e: dict) -> dict into core/backup_repository.py and import it
diff_snapshots called twice in LAN pipeline	flow.py:232 (inside lan_record_task) AND flow.py:467 (inside _run_lan_pipeline)	MEDIUM — lan_record_task does not return the diff, so the caller recomputes it. Fix: have lan_record_task return diff and use the return value in _run_lan_pipeline
_prefect_has_active_run ↔ flow.py:618 concurrency	ui.py:140-168 + flow.py:618 concurrency("aam-backup", occupy=1)	LOW — both enforce "one backup at a time" but via different mechanisms. The UI check is fail-open (returns False on API error) while the concurrency limit is server-side and authoritative. Document or remove the UI check
HTML string concatenation patterns	ui.py:198-222 (login page) AND ui.py:480-498 (report wrapper)	LOW — both build raw HTML via f-strings; error_html is not HTML-escaped at L197; report wrapper uses html.escape() for firm_name but renders html_body unescaped (correct because html_body already escapes its own data)
Two singletons for related resources	ui.py:120-125 (_cfg) AND ui.py:130-134 (get_db)	LOW — two separate lazy singletons; could be a single class instance
Trigger endpoint copy-paste	ui.py:339-347 (cloud) and ui.py:351-359 (lan)	MEDIUM — trigger_cloud/trigger_lan are 9-line near-identical functions. Fix: _trigger(pipeline, request, background_tasks)
Report endpoint copy-paste	ui.py:365-372 and ui.py:375-382	LOW — same pattern; same fix
Email-trigger endpoint copy-paste	ui.py:387-421 and ui.py:424-458	MEDIUM — 35-line copy-paste; only differs in function names and the days/period literal. Fix: single _trigger_email_report(days, period, request)
Status-classification table	ui.py:611-620 (Python) AND templates/dashboard.py:194-219 (JS)	INHERENT — same logic, different language. Acceptable
_get_last_success + _last_run_summary both call db.last_run(mode)	ui.py:511-516 and ui.py:519-531	LOW — two queries when one dict could feed both. Same db.last_run(mode) round-trip
Anti-patterns / best-practices violations
flow.py
- L47-56 _stable_run_id — except Exception swallows everything including KeyboardInterrupt (Python Exception is OK for catching bugs but here it's catching legitimate RuntimeError from FlowRunContext.get(); consider narrowing to the expected exception). Also: lazy import from prefect.context import FlowRunContext inside the function — this is for testability but the import could live at the top of the file (Prefect is a hard dependency)
- L117-153 cloud_verify_and_report_task — 36-line task that does verify + size + manifest + diff all in one function. Borderline god-task; could be split into separate tasks
- L157-166 cloud_record_task — accepts sync_result: dict parameter that is never read (only verify_data is used). Dead parameter
- L184-193 / L216-226 — lan_preflight_task and lan_sync_task follow identical pattern (call function, raise on not ok / LAN_FAILED). Could share a _raise_on_status_error(result, prefix) helper
- L197-202 / L206-212 — lan_snapshot_before_task and lan_snapshot_after_task are identical except for the log message label. Combine into a single task with a label: str parameter
- L263-292 / L296-327 cloud_publish_artifact_task / lan_publish_artifact_task — 30+ lines of nearly identical Markdown construction. The only difference is the mode-specific sections. Fix: one _publish_artifact(mode, payload) function with a mode switch
- L334-430 _run_cloud_pipeline — 96 lines, the longest function in flow.py. Mixes orchestration, manual normalization (L378-405), differential computation, JSON serialization, and Prefect artifact publishing. Split into helper(s) for differential calc and JSON building
- L378-405 — manual entry normalization re-implements core/backup_repository.py:30-37. The mtime-comparison fallback (L394-405) uses three branches (numeric / parse-ISO / string-equality) when a single pendulum.parse(...).timestamp() would do
- L399-400 pendulum.parse(str(mtime)).timestamp() — manual ISO-parsing. This logic should live in core/time_utils.py as parse_iso_to_unix_timestamp() for consistency
- L410-414 / L472-477 — extended_metrics = json.dumps({...}) — fine, but the resulting string is then stored in a TEXT column; if anyone later wants to query it they need to JSON-parse in SQL. Acceptable, but worth documenting
- L462-470 _run_lan_pipeline — calls diff_snapshots again (L467) after lan_record_task already computed it. Pure waste of O(n) CPU
- L484-488 — lan_shutdown_task is inside the try block, with a comment justifying why it's not in else. This means if lan_record_task raises, the server is not shut down. The current behaviour is intentional (don't shut down if data is in inconsistent state) but a more robust design would put shutdown in finally with a flag set only on success
- L510 _record_run — duration = time.time() - pendulum.parse(started_at).timestamp() mixes time.time() (stdlib) with pendulum. Should be pendulum.now("UTC").timestamp() - pendulum.parse(started_at).timestamp() for timezone consistency
- L530-547 / L550-567 weekly_report_flow / monthly_report_flow — function-local imports from core.report import send_weekly_report (L544) and send_monthly_report (L564) are unnecessary; the module is already imported in core/report.py itself. Move to top
- L574-666 backup (the @flow) — 92 lines; borderline god-flow but reasonable for an entry point
- L604-613 watchdog lock — hardcodes the lock file name as "backup.lock". The matching path in watchdog.py:51 is Path(r"C:\BackupAgent\backup.lock") — the C:\BackupAgent segment is implicit. If the database parent dir changes, the watchdog lock will mismatch. Fragile cross-file contract — should be a shared constant in a config module
- L610 _lock_path.write_text(str(os.getpid())) — non-atomic write; two processes starting simultaneously could race. Comment at L607-609 acknowledges this but doesn't fix it. Should use filelock (already a project philosophy via the watchdog) or write to a tempfile + os.replace()
- L623 / L631 _run_cloud_pipeline(..., _stable_run_id("cloud"), utcnow_iso()) — utcnow_iso() is called inside the argument list and as the started_at of the parent flow, but _run_cloud_pipeline re-derives ended_at via its own utcnow_iso() inside _record_run (L509). This means the parent's started_at is just before the orchestration begins, and the actual pipeline's recorded start time is later. Minor, but could surprise readers
- L648 raise ExceptionGroup(...) — well-formed; ExceptionGroup is the backport from exceptiongroup (per pyproject.toml)
- L662-666 lock release — try: _lock_path.unlink(missing_ok=True) is correct, but if unlink(missing_ok=True) itself raises on Windows (e.g. file locked by antivirus), the except OSError: pass covers it. Good
- L665 except OSError: pass — swallows OSError silently. Acceptable for a cleanup path, but the comment is missing; consider logger.debug(...) for diagnosability
- No logging for concurrency acquisition failure — if concurrency("aam-backup", ...) waits 3600s and then times out, no log line explains the wait
- L13-17 imports — json, os, time, uuid all used. Clean
- No type annotations on most public functions — only _stable_run_id has a return type; the rest are Any-typed
ui.py
- L36-41 module-level globals — _RATE_LIMITS, _RATE_LOCK, _RATE_WINDOW, _RATE_MAX_*. Mixing module-level constants and runtime state. _RATE_WINDOW (300s), _RATE_MAX_TRIGGER (5), _RATE_MAX_LOGIN (10) are magic numbers — should be Final constants at module top with a comment
- L78-84 _cleanup_expired_sessions — duplicates the expiry check at L82 vs the one at L93 in _validate_session. Different code paths for the same predicate
- L120-125 _cfg / L130-134 get_db — two singletons for related resources. Could be one class AppState instance
- L140-142 _is_running — pure wrapper around _prefect_has_active_run. Adds no value, indirection for testing only
- L145-168 _prefect_has_active_run — except Exception at L166 returns False (fail-open). This means if Prefect is down, every trigger thinks no backup is running and starts a new one. Should at least return True (assume running) or use the concurrency limit as the authoritative gate. Currently a duplicate-run risk on Prefect outage
- L166 logger.error(f"Failed to query Prefect API: {e}") — the return False is the wrong fail-mode. Log and return True (conservatively assume running) is safer
- L186 run_deployment(name=f"aam-backup/backup-{pipeline}") — hardcodes the aam-backup/ deployment prefix. If the flow name in flow.py is renamed, this breaks silently (Prefect returns a ValueError that gets caught at L188, logged, and the trigger is lost)
- L196-222 login_page — 27-line raw HTML f-string concatenation. error param is interpolated without html.escape (currently safe because the only caller passes the static string "Invalid+API+key", but fragile)
- L226-239 login_submit — does not apply rate limiting. The login endpoint has no _check_rate_limit call. An attacker can brute-force the API key at line speed. The _RATE_MAX_LOGIN = 10 constant (L40) is defined but never used — dead config
- L249-262 _require_auth — returns 303 (redirect) for browsers and 401 for APIs. The 303 distinction is by Accept header sniffing; Accept: */* defaults to 401 — reasonable
- L275 if not Path(cfg.paths.database_path).exists() — race with get_db() on next line
- L324-335 health — except Exception returns {"status": "healthy"}. The "healthy" response is unconditional, which means broken Prefect integration or DB corruption would still report healthy. Fail-open for monitoring is questionable
- L339-359 trigger_cloud / trigger_lan — copy-paste; combine
- L365-382 report_weekly / report_monthly — copy-paste; combine
- L387-458 trigger_weekly_email / trigger_monthly_email — 35-line copy-paste. Only the function name, days count, and period label differ. Combine into one parameterised function
- L398-400 / L435-437 — function-local imports from core.report import send_weekly_report, generate_report_html (and the monthly variant). Move to top of file
- L461-505 _serve_report — re-imports generate_report_html on every call (L470). Move import to top
- L500 pendulum.now().format("YYYY-MM-DD") — should use utcnow_formatted("YYYY-MM-DD") from core/time_utils per project convention
- L511-516 / L519-531 — _get_last_success and _last_run_summary both call db.last_run(mode). Two DB round-trips when one dict would do
- L534-543 _get_health — except Exception: return {"error": "unavailable"} — fail-open. Same pattern as health endpoint
- L548-663 _render_dashboard — 115 lines, the largest function in ui.py. Mixes DB queries, Prefect API calls, HTML construction (history rows inline), template rendering. The inline for r in runs: loop building <tr> rows (L609-626) duplicates the same loop in templates/dashboard.py:194-219 (JS-side)
- L627-628 finally: pass  # singleton — do not close — try/finally with no body. This entire block is structurally if db: <code> and the try/finally is dead
- L630-639 flash messages — hardcoded dict of 4 messages. Acceptable but a 5th would warrant constants
- No type annotations on public FastAPI handlers — only login_submit, dashboard, status, trigger_*, report_* are not annotated. FastAPI uses these for OpenAPI generation; missing -> Response means the generated schema is incomplete
- L579, L584, L625 [:60] magic number — error message truncation at 60 chars. Same constant in 3 places. Should be _ERROR_TRUNCATE_LEN = 60
- L36-41 _RATE_MAX_LOGIN = 10 is dead — defined but never referenced (login endpoint doesn't rate-limit)
Dead code / unused imports / unused parameters
Item	Location	Status
_RATE_MAX_LOGIN	ui.py:40	Dead — defined but never used; _check_rate_limit is not called from login_submit
sync_result: dict parameter in cloud_record_task	flow.py:157	Unused — accepted but never read inside the function body
try/finally: pass block	ui.py:627-628	Dead structure — pass only
lan_publish_artifact_task _check_*_APIs checks	n/a	n/a
ExceptionGroup from backport	flow.py:21	Used at L648 — fine
Exception as e: pass in weekly_report_flow / monthly_report_flow	flow.py:537, 557	Acceptable — bridge setup is best-effort
Specific recommendations
File:line	Recommendation
flow.py:30-37 (in core/backup_repository.py) and flow.py:378-405	Extract normalize_entry(e: dict) -> dict into core/backup_repository.py; call it from both sites
flow.py:232 + flow.py:467	Have lan_record_task return the diff dict; use it in _run_lan_pipeline instead of recomputing
flow.py:230-246	lan_record_task should also compute the files_copied/bytes_copied summary (currently only _run_lan_pipeline does it via the wasted re-diff) and return both the diff and the counts
flow.py:197-212	Collapse lan_snapshot_before_task / lan_snapshot_after_task into one task with a label: str parameter
flow.py:263-327	Replace cloud_publish_artifact_task + lan_publish_artifact_task with one _publish_artifact(mode: str, payload: dict)
flow.py:394-400	Add parse_iso_to_unix_timestamp(iso_str: str) -> float to core/time_utils.py and use it here
flow.py:510	duration = pendulum.now("UTC").timestamp() - pendulum.parse(started_at).timestamp() for consistency
flow.py:544, 564	Move from core.report import send_weekly_report / send_monthly_report to top-of-file imports
flow.py:604	Extract BACKUP_LOCK_PATH constant shared with watchdog.py:51 (e.g. a core/lock_paths.py module)
flow.py:610	Use filelock (already a project pattern) or atomic write-then-rename for the lock file
flow.py:484-488	Either accept the current behaviour (no shutdown on record failure) and document it, or move lan_shutdown_task to finally with a shut_down = True flag set only on success
ui.py:40	_RATE_MAX_LOGIN = 10 is defined but never used — either apply it to login_submit (L226) or delete it. Currently this is a real security gap (login has no rate limit)
ui.py:140-142	Inline _is_running into its single call sites, or remove the wrapper
ui.py:166-168	On Prefect API error, return True (conservatively assume running) instead of False to prevent duplicate-trigger races
ui.py:186	Extract the deployment name f"aam-backup/backup-{pipeline}" to a constant or read from config; failing silently on rename is a footgun
ui.py:196-222	Wrap error in html.escape(...) (defensive)
ui.py:226-239	Add _check_rate_limit(client_ip, _RATE_MAX_LOGIN) to login_submit (currently missing — security gap)
ui.py:339-359	Combine trigger_cloud / trigger_lan into one _trigger_pipeline(pipeline, request, background_tasks)
ui.py:365-382	Combine report_weekly / report_monthly into one _report_endpoint(days, period, request)
ui.py:387-458	Combine trigger_weekly_email / trigger_monthly_email into one — the only difference is the days and period literal
ui.py:470	Move from core.report import generate_report_html to top-of-file
ui.py:500	filename = ...pendulum.now().format("YYYY-MM-DD") → use utcnow_formatted("YYYY-MM-DD") from core/time_utils
ui.py:511-531	_get_last_success + _last_run_summary should share one db.last_run(mode) call
ui.py:548-663	_render_dashboard is 115 lines; the inline history-row construction (L609-626) duplicates the JS in templates/dashboard.py. Move history-row rendering to a small helper and consider letting the JS handle the rows entirely (it already builds them at L192-225 of the template)
ui.py:627-628	Delete try/finally: pass — the if db: already gates the code
ui.py:579, L584, L625	Extract _ERROR_TRUNCATE_LEN = 60 constant (3 sites)
flow.py:13	import json is fine; flow.py:14 import os is fine. No stale imports
ui.py:13-22	All imports are used; no cleanup needed
Both files	Add return type annotations to all @task, @flow, and FastAPI handler functions (currently 0/22 in flow.py, 0/19 in ui.py handlers — undermines OpenAPI generation and mypy)
Note on backup.lock: The two-file contract between flow.py:604 and watchdog.py:51 is fragile. flow.py derives the path from config.paths.database_path's parent, while watchdog.py hardcodes C:\BackupAgent\backup.lock. The comment at watchdog.py:50 says they "must match" — but if the database path is ever reconfigured, the watchdog will look at the wrong location and bypass the lock entirely. Extract BACKUP_LOCK_PATH = Path(r"C:\BackupAgent\backup.lock") into a shared constant (e.g. core/paths.py) and have both files import it.
