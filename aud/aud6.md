Chunk 6 Audit: cross-cutting (reports/logging/hashing)
Functions analyzed
File:line	Function	Intent
report.py:20-62	_send_email	SMTP send with STARTTLS (587) or SSL (465), returns bool
report.py:65-99	send_failure_alert	Build failure email body + delegate to _send_email
report.py:102-160	generate_report_html	Build summary HTML from ManifestDB.get_runs_since()
report.py:163-181	send_summary_report	Call generate_report_html (or accept prebuilt), then _send_email
report.py:184-199	send_weekly_report / send_monthly_report	Thin wrappers → send_summary_report
logging.py:14-40	configure	loguru stderr + daily rotating file, 30-day retention
logging.py:46-94	configure_prefect_bridge	Forward loguru to Prefect run logger, idempotent
hashing.py:9-16	compute_md5	Streaming MD5 via hashlib.file_digest (Py 3.11+)
hashing.py:19-27	verify_checksum	Compare hex digest, reject PENDING_CHECKSUM
process.py:6-8	pid_alive → psutil.pid_exists	Cross-platform PID alive check
Cross-file duplications
None significant. Searched for smtplib, MIMEMultipart, sendmail, send_email across the entire codebase:
- core/report.py is the only module that does SMTP — _send_email is module-private (good) and every email entry point (send_failure_alert, send_summary_report, send_weekly_report, send_monthly_report, plus the manual-trigger endpoints in ui.py:387-458) routes through it. The Prefect flows in flow.py:544,565,640 also go through this single function. Single source of truth, no duplication.
- templates/dashboard.py:render_dashboard builds the UI shell (HTML5 doctype, dark CSS, JS, status cards, run-history table) — fundamentally different from report.py:generate_report_html which builds an email body (no CSS, no JS, simple <html><body>). The dashboard calls _serve_report() in ui.py:461-471 which delegates back to generate_report_html for the actual table data, so the formatting is shared, not duplicated. Correct separation.
Anti-patterns
core/report.py:40-62 — SMTP lifecycle, the connection-leak warning in graphify Community 73:
The actual code is defensible:
server: smtplib.SMTP | smtplib.SMTP_SSL | None = None
try:
    if config.smtp_port == 465:
        server = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30)
    else:
        server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
        server.starttls()
    server.login(...)
    server.sendmail(...)
    server.quit()
    ...
except Exception as e:
    if server is not None:
        try: server.quit()
        except Exception: pass
- If SMTP() constructor raises → server is None → no quit needed. ✓
- If starttls() raises → server was assigned → except block calls quit(). ✓
- If login() / sendmail() raises → same. ✓
- The test test_quits_on_sendmail_failure (tests/test_report.py:98-111) confirms quit() is called on sendmail failure.
Graphify's "SMTP connection leak" warning is a false positive based on the 5-step pattern alone. The defensive if server is not None: try/except server.quit() makes it leak-free. That said, the cleaner idiom is with smtplib.SMTP(...) as server: (context manager) — it removes the manual quit() and the server = None tracking entirely.
core/logging.py:25-30 — stderr sink missing enqueue=True:
logger.add(sys.stderr, format=LOG_FORMAT, level="INFO", colorize=True)
The file sink at line 32 has enqueue=True (good), but the stderr sink does not. On Windows NSSM / systemd journald / CI capture pipes, a slow stderr write can block the logging thread. Minor — usually fine, but inconsistent with the file sink right next to it.
core/report.py:14 — import humanize not declared in pyproject.toml dependencies (verified by reading the file): humanize is installed in the venv (4.15.0, likely a transitive dep) but should be a direct dependency since the code imports it directly. If a clean install drops the transitive, the agent crashes on first import.
core/report.py:34-38 — uses MIMEMultipart + MIMEText: works fine, but the modern stdlib equivalent is email.message.EmailMessage which auto-handles headers, content-type, and encoding. Not broken, just dated.
core/logging.py:92 — logger.opt(depth=1, exception=False).debug(...) inside a loguru sink: correctly uses depth=1 to skip the sink frame when loguru is logging the failure message, preventing infinite recursion. Correct, just worth flagging as non-obvious.
core/process.py — DEAD CODE
This 8-line module defines only pid_alive(pid) -> psutil.pid_exists(pid). The only callsite is tests/test_ui.py:11,121-127. No production code uses it. Confirmed by grep -rn "pid_alive" --include="*.py" | grep -v .venv:
- core/process.py (definition)
- tests/test_ui.py (test-only)
watchdog.py:71-80 implements its own _pid_is_alive via tasklist /FI "PID eq ..." (Windows-specific), and flow.py:610 writes a PID-stamped lock file but never reads it back from this module. The psutil dependency in pyproject.toml:19 is the only thing keeping this module alive.
Security
Item	Status	Note
Hardcoded credentials in core/	None	All SMTP creds come from config.notifications.
config.example.yaml:87 smtp_password: ""	OK (template empty)	Should document env-var substitution as the recommended path.
models/config.py:131 smtp_password: str = ""	OK	Stored as plain str in pydantic, but…
models/config.py:149 __repr__ masks password as '***'	Good	Defensive default.
tests/test_report.py:51,89,103,124 smtp_password="pass"	OK	Test-only literal.
SMTP creds in env vars?	Not supported	No ${VAR} expansion in AppConfig.from_yaml. Real deployments that want to keep passwords out of config.yaml have to mount the file with a secret manager — no in-app support.
DashboardConfig.api_key (config.example.yaml:76)	Same issue	Plaintext in config.yaml; masked in __repr__.
No plaintext passwords in source. Risk is config-file-on-disk exposure, mitigated by config.yaml being gitignored (per config.example.yaml:5).
Recommendations (read-only, not applied)
1. core/process.py:1-8 + tests/test_ui.py:121-128 — delete the module and the test. Zero production callsites. Drops psutil from pyproject.toml:19 (verify no other use first — I already grepped, no other use).
2. core/report.py:40-62 — convert _send_email to use smtplib.SMTP / SMTP_SSL as context managers (Py 3.x context manager support). Removes the server = None tracker, the manual quit() in both success and except branches, and the if server is not None: try/except server.quit() defensive block. Same logic, ~10 fewer lines, harder to leak.
3. pyproject.toml:9-23 — add humanize>=4.0 to dependencies. core/report.py:14 imports it directly.
4. core/logging.py:25-30 — add enqueue=True to the stderr sink for consistency with the file sink and to avoid head-of-line blocking under pipe capture.
5. models/config.py + config.example.yaml — add env-var interpolation in AppConfig.from_yaml (e.g. ${SMTP_PASSWORD}) so real deployments can keep passwords out of config.yaml. Existing __repr__ masking already handles the in-process side correctly.
6. core/report.py:34-38 — optional: migrate to email.message.EmailMessage. Pure code-style; no behavior change. Skip if you want to minimize churn.
7. Graphify Community 73 ("SMTP Connection Leak Warning") — suppress or merge into the function docstring once the context-manager refactor lands. Currently a false positive that will keep showing up in audits.
Tests verifying the analysis
- tests/test_report.py:30-135 — covers _send_email happy path (TLS+SSL), skip-when-unconfigured, and test_quits_on_sendmail_failure confirms the leak-prevention path.
- tests/test_logging.py:7-26 — test_is_idempotent confirms configure_prefect_bridge adds exactly one sink across multiple calls.
- tests/test_hashing.py:11-63 — covers known-content ("hello world" → 5eb63bbbe01eeed093cb22bb8f5acdc3 matches rclone lowercase hex), empty file, binary, Path object, PENDING_CHECKSUM short-circuit, and intentional case-sensitivity.
- tests/test_ui.py:121-128 — covers pid_alive mock, but this test is for a module with zero production callsites.
