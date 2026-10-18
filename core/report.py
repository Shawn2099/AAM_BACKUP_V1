"""Reports — failure alerts, weekly/monthly summaries via email and HTML.

Reads from ManifestDB run_history. Zero knowledge of backup internals.
generate_report_html() is shared between email delivery and UI download.
"""

import csv
import html
import io
import json
import re

import humanize
import pendulum
from loguru import logger

from core import faults, statuses
from core.email_compose import LEG_LABEL
from core.manifest import ManifestDB
from core.notify import SENT, send_email
from core.time_utils import IST, format_ist, now_formatted
from models.config import NotificationConfig




def _send_email_with_attachments(
    config: NotificationConfig,
    subject: str,
    body_html: str,
    attachments: list[dict] = None,
) -> bool:
    """Send one email through the SINGLE SMTP transport (core.notify.send_email,
    plan WP8): retries via tenacity, 5xx permanent / 4xx retried (the SMTP
    library raises SenderRefused for transient 4xx replies too), optional auth,
    configurable security and timeout. True only when delivered."""
    result = send_email(config, subject, body_html, attachments or [])
    return result.status == SENT


def send_failure_alert(
    config: NotificationConfig,
    firm_name: str,
    error_message: str,
    run_data: dict,
    timestamp: str = "",
) -> bool:
    """Send immediate email on backup failure.
    
    Attaches the full error log as a text file if the error is extremely long.
    """
    if not config.send_on_failure:
        logger.info("send_on_failure disabled - skipping alert")
        return False

    mode = (run_data.get("mode") or "unknown").upper()
    subject = f"Backup Failure Alert - {firm_name} ({mode})"

    ts_display = f"<p><strong>Time:</strong> {html.escape(format_ist(timestamp))}</p>" if timestamp else ""
    status_code = html.escape(str(run_data.get("status") or ""))
    exit_code = run_data.get("exit_code")
    exit_code_display = html.escape(str(exit_code)) if exit_code is not None else "-"

    # Truncate the inline error for a clean email UI, attach the rest
    attachments = []
    body_error_limit = 1000
    if len(error_message) > body_error_limit:
        error_display = html.escape(error_message[:body_error_limit]) + "\n\n... [TRUNCATED - SEE ATTACHMENT FOR FULL ERROR]"
        attachments.append({
            "filename": "failure_details.txt",
            "content": error_message.encode("utf-8")
        })
    else:
        error_display = html.escape(error_message)

    body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;color:#1f2937;margin:0;padding:0">
<div style="max-width:600px;margin:0 auto;padding:24px">
<h2 style="color:#dc2626;margin:0 0 16px 0">Backup Failure Alert \u2014 {html.escape(firm_name)}</h2>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600;width:120px">Firm</td>
<td style="padding:8px 12px;border:1px solid #e5e7eb">{html.escape(firm_name)}</td></tr>
<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600">Pipeline</td>
<td style="padding:8px 12px;border:1px solid #e5e7eb">{html.escape(mode)}</td></tr>
<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600">Status</td>
<td style="padding:8px 12px;border:1px solid #e5e7eb">{status_code or '-'}</td></tr>
<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600">Exit Code</td>
<td style="padding:8px 12px;border:1px solid #e5e7eb">{exit_code_display}</td></tr>
<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600">Error</td>
<td style="padding:8px 12px;border:1px solid #e5e7eb;color:#dc2626;white-space:pre-wrap;font-family:monospace;font-size:13px">{error_display}</td></tr>
</table>
{ts_display}
<p style="color:#6b7280;font-size:13px;margin-top:24px">Review the server logs or attached details for more info.</p>
</div>
</body>
</html>"""

    return _send_email_with_attachments(config, subject, body, attachments)


# G5: cells that begin with one of these are interpreted as formulas/commands
# by spreadsheet apps when the CSV is opened in Excel (CSV injection / DDE).
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Neutralize CSV formula injection for text cells.

    Error messages and metric JSON are free text that ends up in a CSV opened
    in Excel by accountants. A cell starting with =, +, -, @ (or tab/CR) is
    evaluated as a formula/DDE by Excel; prepending a single quote forces
    literal-text treatment in Excel and LibreOffice without changing what the
    user reads (the quote renders as the cell's text format marker, not the
    value). Numeric columns are never passed through this helper.
    """
    text = "" if value is None else str(value)
    if text.startswith(_CSV_INJECTION_PREFIXES):
        return "'" + text
    return text


def _generate_csv_data(runs: list[dict]) -> bytes:
    """Generate a CSV payload containing the full history and un-truncated errors."""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write Headers
    writer.writerow([
        "Started At", "Ended At", "Mode", "Status", 
        "Files Copied", "Files Failed", "Bytes Copied", 
        "Duration (sec)", "Exit Code", "Error Message", "Extended Metrics"
    ])
    
    # Write Rows
    for r in runs:
        writer.writerow([
            r.get("started_at", ""),
            r.get("ended_at", ""),
            r.get("mode", ""),
            r.get("status", ""),
            r.get("files_copied", 0),
            r.get("files_failed", 0),
            r.get("bytes_copied", 0),
            r.get("duration_seconds", 0),
            r.get("exit_code", ""),
            # G5: free-text columns neutralized against CSV injection
            _csv_safe(r.get("error_message", "")),
            _csv_safe(r.get("extended_metrics", ""))
        ])
        
    return output.getvalue().encode("utf-8")


def period_runs(db: ManifestDB, days: int, period: str, now=None):
    """(runs, label) of a report period (audit 2026-10-18 item 5).

    Monthly = the previous calendar month (the report is sent on the 1st);
    otherwise the last `days` days, labelled "12-18 Oct 2026"."""
    now = now or pendulum.now(IST)
    if period == "Monthly":
        end = now.start_of("month")
        start = end.subtract(months=1)
        # get_runs_since counts back from the real clock, so size it from there
        span = (pendulum.now(IST) - start).days + 1
        runs = [r for r in db.get_runs_since(span) if start <= _when(r) < end]
        return runs, start.format("MMMM YYYY")
    start, last = now.subtract(days=days), now.subtract(days=1)
    label = (f"{start.format('D')}-{last.format('D MMM YYYY')}" if start.month == last.month
             else f"{start.format('D MMM')} - {last.format('D MMM YYYY')}")
    return db.get_runs_since(days), label


def _when(run: dict):
    try:
        return pendulum.parse(str(run.get("started_at")), tz=IST)
    except Exception:  # noqa: BLE001 - an unreadable time sorts first, never crashes
        return pendulum.datetime(1970, 1, 1, tz=IST)


_OK = (statuses.CATEGORY_SUCCESS, statuses.CATEGORY_NO_CHANGES)


def _fault(run: dict) -> tuple[str | None, str]:
    try:
        code = (json.loads(run.get("extended_metrics") or "{}") or {}).get("fault_code")
    except (ValueError, AttributeError):
        code = None
    if code in faults.CATALOG:
        return code, faults.CATALOG[code].title
    return None, statuses.label(run.get("status"))


def leg_summaries(db, runs: list[dict], legs) -> list[dict]:
    """Per leg: runs OK, last good copy, problems (fault code, resolved?)."""
    out = []
    for leg in legs:
        leg_runs = sorted((r for r in runs if r.get("mode") == leg), key=_when)
        finished = [r for r in leg_runs
                    if statuses.category(r.get("status")) != statuses.CATEGORY_RUNNING]
        ok_times = [_when(r) for r in finished if statuses.category(r.get("status")) in _OK]
        problems = []
        for r in finished:
            if statuses.category(r.get("status")) in _OK:
                continue
            code, title = _fault(r)
            problems.append({"when": format_ist(r.get("started_at"), "D MMM, h:mm A"),
                             "code": code, "title": title,
                             "resolved": any(t > _when(r) for t in ok_times)})
        try:
            last = db.last_successful_run(leg)
        except Exception:  # noqa: BLE001 - a partial DB (tests) has no history query
            last = None
        last_when = (last.get("ended_at") or last.get("started_at")) if last else None
        out.append({"leg": leg, "label": LEG_LABEL.get(leg, leg.upper()),
                    "total": len(finished), "ok": len(ok_times), "problems": problems,
                    "last_good": format_ist(last_when, "D MMM YYYY, h:mm A") if last_when
                    else "none yet"})
    return out


def _open_decisions(db) -> int:
    try:
        return len(db.open_approvals())
    except Exception:  # noqa: BLE001
        return 0


def report_headline(db, summaries: list[dict]) -> tuple[str, bool]:
    """(one line for the subject and the top, whether a person must act)."""
    for s in summaries:
        if s["total"] == 0:
            return f"action needed: {s['label']} - no backup ran", True
    for s in summaries:
        open_problems = [p for p in s["problems"] if not p["resolved"]]
        if open_problems:
            return f"action needed: {s['label']} - {open_problems[-1]['title']}", True
    for s in summaries:
        try:
            audit = db.latest_audit(s["leg"])
        except Exception:  # noqa: BLE001
            audit = None
        if isinstance(audit, dict) and audit.get("status") == "VERIFICATION_FAILED":
            return f"action needed: {s['label']} - safety check found differences", True
    if _open_decisions(db):
        return "action needed: a backup is waiting for your decision", True
    fixed = sum(len(s["problems"]) for s in summaries)
    if fixed:
        return f"backups OK - {fixed} problem(s) this period, all resolved", False
    return "all backups OK", False


def _summary_html(db, summaries: list[dict]) -> str:
    headline, act = report_headline(db, summaries)
    cell = "padding:6px 12px;border:1px solid #e5e7eb;vertical-align:top"
    rows = ""
    for s in summaries:
        if s["total"] == 0:
            detail = "No backup ran in this period."
        else:
            items = [f"{html.escape(p['when'])}: {html.escape(p['title'])}"
                     + (f" ({p['code']})" if p["code"] else "")
                     + (" - resolved" if p["resolved"] else " - <b>not resolved</b>")
                     for p in s["problems"]]
            detail = "<br>".join(items) or "No problems."
        rows += (f"<tr><th style='{cell};text-align:left;background:#f9fafb'>"
                 f"{html.escape(s['label'])}</th><td style='{cell}'>{s['ok']} of {s['total']} "
                 f"runs OK<br><span style='color:#6b7280'>Last good copy: "
                 f"{html.escape(s['last_good'])}</span></td><td style='{cell}'>{detail}</td></tr>")
    decisions = _open_decisions(db)
    if decisions == 1:
        open_items = "1 backup waiting for your decision on the dashboard"
    elif decisions:
        open_items = f"{decisions} backups waiting for your decision on the dashboard"
    else:
        open_items = "none"
    todo = ("Open the dashboard and look at the items marked 'not resolved'; forward this "
            "report to support if you are unsure." if act else "Nothing.")
    colour = "#b91c1c" if act else "#047857"
    return (f"<div style='background:{colour};color:#ffffff;padding:10px 14px;border-radius:6px;"
            f"font-weight:600;margin:0 0 16px 0'>"
            f"{html.escape(headline[0].upper() + headline[1:])}</div>"
            f"<table style='width:100%;border-collapse:collapse;margin:0 0 12px 0'>{rows}</table>"
            f"<p><b>Open items:</b> {html.escape(open_items)}<br><b>What to do:</b> "
            f"{html.escape(todo)}</p>")


def generate_report_html(
    db: ManifestDB,
    firm_name: str,
    days: int,
    period: str,
    is_email: bool = False,
    legs=None,
    now=None,
) -> str:
    """Generate an HTML report string for the given time period.

    Returns "" if no runs found in the period.
    Usable by both email delivery and UI download endpoints. `legs` = the
    enabled legs (a leg with no run at all is shown as such); default: the
    legs that appear in the runs.
    """
    runs, label = period_runs(db, days, period, now)

    if not runs:
        return ""
    legs = tuple(legs) if legs is not None else _legs_in(runs)
    summary_html = _summary_html(db, leg_summaries(db, runs, legs))

    total = len(runs)
    cats = [statuses.category(r.get("status")) for r in runs]
    successes = cats.count(statuses.CATEGORY_SUCCESS)
    no_changes = cats.count(statuses.CATEGORY_NO_CHANGES)
    partials = cats.count(statuses.CATEGORY_PARTIAL)
    skipped = cats.count(statuses.CATEGORY_SKIPPED)
    blocked = cats.count(statuses.CATEGORY_BLOCKED)
    running = cats.count(statuses.CATEGORY_RUNNING)
    # Everything not positively classified counts as a failure (fail loud):
    # failed + unknown statuses.
    failures = total - successes - no_changes - partials - skipped - blocked - running

    total_files = sum(r.get("files_copied") or 0 for r in runs)
    total_bytes = sum(r.get("bytes_copied") or 0 for r in runs)

    success_rate = ((successes + no_changes) / total * 100) if total > 0 else 0

    # every run of the period (audit 2026-10-18: "last 10" hid part of a week)
    rows = ""
    row_count = 0
    for r in runs:
        start = format_ist(r.get("started_at"))
        mode = (r.get("mode") or "unknown").upper()
        status = statuses.label(r.get("status"))
        files = r.get("files_copied") or 0
        dur = f"{r.get('duration_seconds', 0):.0f}s" if r.get("duration_seconds") else "-"
        
        # Heavy truncation for the inline email (CSV will have full error)
        err = r.get("error_message", "")
        if err and len(err) > 100:
            err_display = html.escape(err[:97]) + "..."
        else:
            err_display = html.escape(err) if err else "-"
            
        rows += f"<tr><td>{html.escape(start)}</td><td>{html.escape(mode)}</td><td>{html.escape(status)}</td><td>{files}</td><td>{html.escape(dur)}</td><td>{err_display}</td></tr>"
        row_count += 1

    generated = now_formatted("YYYY-MM-DD HH:mm z")

    integrity_rows = ""
    for leg in ("cloud", "lan"):
        try:
            audit = db.latest_audit(leg)
        except Exception:
            audit = None
        if not audit:
            status_text, checked, detail = "NOT VERIFIED", "-", "No independent audit has run yet."
        else:
            status_text = audit.get("status", "UNKNOWN")
            if status_text == "AUDIT_INCOMPLETE":
                # WP7: "could not run" (network/TLS/DNS/process) is never divergence
                status_text = "AUDIT COULD NOT RUN"
            checked = format_ist(audit.get("ended_at") or audit.get("started_at"))
            detail = html.escape(str(audit.get("detail") or "-"))
        integrity_rows += (
            f"<tr><td style=\"padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb;width:180px\">"
            f"{leg.upper()} Integrity</td><td style=\"padding:6px 12px;border:1px solid #e5e7eb\">"
            f"{html.escape(status_text)} (checked: {html.escape(str(checked))})<br>"
            f"<span style=\"color:#6b7280;font-size:0.85rem\">{detail}</span></td></tr>"
        )

    csv_notice = ""
    if is_email:
        csv_notice = '<p style="color:#059669;font-weight:600;font-size:0.9rem;margin:16px 0;">A complete CSV with full error logs is attached to this email.</p>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;color:#1f2937;margin:0;padding:0">
<div style="max-width:800px;margin:0 auto;padding:24px">
<h2 style="color:#1e3a5f;margin:0 0 8px 0">{html.escape(period)} Backup Report — {html.escape(firm_name)}</h2>
<p style="color:#6b7280;margin:0 0 16px 0">Period: {html.escape(label)} (generated {generated})</p>

{summary_html}

{csv_notice}

<h3 style="color:#374151;margin:0 0 8px 0">Summary</h3>
<table style="width:100%;border-collapse:collapse;margin:0 0 24px 0">
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb;width:180px">Total Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{total}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Successful Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{successes}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">No Changes (Up-to-date)</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{no_changes}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Partial Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{partials}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Skipped Runs</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{skipped}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Blocked (approval needed)</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{blocked}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">In Progress</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{running}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Failed Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{failures}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600">Success Rate</td><td style="padding:6px 12px;border:1px solid #e5e7eb;font-weight:600">{success_rate:.1f}%</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Files Backed Up</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{total_files:,}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Data Transferred</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{total_bytes:,} ({humanize.naturalsize(total_bytes, binary=True)})</td></tr>
</table>

<h3 style="color:#374151;margin:0 0 8px 0">Integrity Verification (independent weekly audit)</h3>
<p style="color:#6b7280;font-size:0.85rem;margin:0 0 8px 0">A COMPLETE backup means the transfer finished normally — it does not imply independent content verification. Integrity below reflects the latest read-only audit only.</p>
<table style="width:100%;border-collapse:collapse;margin:0 0 24px 0">
{integrity_rows}
</table>

<h3 style="color:#374151;margin:0 0 8px 0">Recent Backups (all of the period)</h3>
<table style="width:100%;border-collapse:collapse;margin:0 0 8px 0">
<tr><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Started</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Pipeline</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Status</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Files</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Duration</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Error</th></tr>
{rows}
</table>
<p style="color:#6b7280;font-size:0.85rem;margin:0">Showing all {row_count} backups of the period</p>
</div>
</body>
</html>"""


def send_summary_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
    days: int,
    period: str,
    body_html: str = None,
    legs=None,
) -> bool:
    """Send aggregated summary report via email with CSV attachment.

    Returns True if email sent, False if skipped or failed.
    """
    email = build_summary_email(db, firm_name, days, period, body_html=body_html, legs=legs)
    if email is None:
        logger.info(f"No runs found in last {days} days - skipping {period} report")
        return False
    subject, body, attachments = email
    return _send_email_with_attachments(config, subject, body, attachments)


def build_summary_email(db: ManifestDB, firm_name: str, days: int, period: str,
                        body_html: str = None, legs=None,
                        now=None) -> tuple[str, str, list[dict]] | None:
    """(subject, html body, [CSV attachment]) for a summary report, or None
    when the period has no runs. The subject says the outcome."""
    runs, label = period_runs(db, days, period, now)
    if not runs:
        return None
    body = body_html or generate_report_html(db, firm_name, days, period, is_email=True,
                                             legs=legs, now=now)
    if not body:
        return None
    legs = tuple(legs) if legs is not None else _legs_in(runs)
    headline, _ = report_headline(db, leg_summaries(db, runs, legs))
    safe_firm = re.sub(r'[^a-zA-Z0-9_\-]', '_', firm_name)
    attachments = [{"filename": f"{safe_firm}_{period}_Report.csv",
                    "content": _generate_csv_data(runs)}]
    return (f"[{firm_name} Backup] {period} backup report {label} - {headline}", body,
            attachments)


def _legs_in(runs: list[dict]) -> tuple[str, ...]:
    return tuple(leg for leg in ("cloud", "lan") if any(r.get("mode") == leg for r in runs))


def send_weekly_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
    body_html: str = None,
    legs=None,
) -> bool:
    return send_summary_report(db, config, firm_name, 7, "Weekly", body_html=body_html,
                               legs=legs)


def send_monthly_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
    body_html: str = None,
    legs=None,
) -> bool:
    return send_summary_report(db, config, firm_name, 30, "Monthly", body_html=body_html,
                               legs=legs)

