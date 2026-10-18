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
from core.report_period import ReportPeriod
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


BACKUP_LEGS = ("cloud", "lan")


def resolve_period(days: int, period: str, now=None, report_period=None) -> ReportPeriod:
    """The period a report covers. Callers that know it (the scheduled flows: the previous
    calendar week/month of the run's scheduled time) pass `report_period`; the legacy
    (days, name) form means the previous calendar month for "Monthly", else the last `days`."""
    if report_period is not None:
        return report_period
    now = now or pendulum.now(IST)
    if period == "Monthly":
        return ReportPeriod.previous_month(now)
    return ReportPeriod.last_days(days, now)


def period_runs(db: ManifestDB, days: int, period: str, now=None, report_period=None):
    """(runs, label) of a report period (audit 2026-10-18 item 5; plan C)."""
    rp = resolve_period(days, period, now, report_period)
    return db.get_runs_between(rp.start, rp.end), rp.label


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
                             "resolved": any(t > _when(r) for t in ok_times),
                             "_at": _when(r)})
        try:
            last = db.last_successful_run(leg)
        except Exception:  # noqa: BLE001 - a partial DB (tests) has no history query
            last = None
        # P3: a failure is resolved by ANY later good run, also one after the period ends
        last_good_start = _when(last) if isinstance(last, dict) else None
        if last_good_start is not None:
            for p in problems:
                p["resolved"] = p["resolved"] or last_good_start > p["_at"]
        for p in problems:
            del p["_at"]
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


def _audit_row(db, leg: str) -> dict:
    try:
        audit = db.latest_audit(leg)
    except Exception:  # noqa: BLE001 - a partial DB (tests) has no audit query
        audit = None
    if not isinstance(audit, dict) or not audit:
        return {"leg": leg, "status": "NOT VERIFIED", "checked": "-",
                "detail": "No independent audit has run yet."}
    status_text = audit.get("status", "UNKNOWN")
    if status_text == "AUDIT_INCOMPLETE":
        # WP7: "could not run" (network/TLS/DNS/process) is never divergence
        status_text = "AUDIT COULD NOT RUN"
    return {"leg": leg, "status": status_text,
            "checked": format_ist(audit.get("ended_at") or audit.get("started_at")),
            "detail": str(audit.get("detail") or "-")}


def build_report_model(db: ManifestDB, report_period: ReportPeriod, legs=None) -> dict:
    """Everything a report says, computed ONCE (plan C3). Every renderer (email, download, CSV,
    subject) reads this and nothing else, so they cannot disagree.

    runs      backup runs (cloud / lan) that STARTED in the period; rollover and audit rows are
              not backups and are left out (P4)
    per_leg   files / bytes copied per leg, never added together across legs (P4)
    summaries per leg: runs OK, last good copy, problems; "resolved" looks at the whole history (P3)
    audits    latest independent audit of each ENABLED leg only (P5)"""
    runs = [r for r in db.get_runs_between(report_period.start, report_period.end)
            if r.get("mode") in BACKUP_LEGS]
    legs = tuple(legs) if legs is not None else BACKUP_LEGS
    summaries = leg_summaries(db, runs, legs)
    headline, act = report_headline(db, summaries)

    cats = [statuses.category(r.get("status")) for r in runs]
    total = len(runs)
    successes = cats.count(statuses.CATEGORY_SUCCESS)
    no_changes = cats.count(statuses.CATEGORY_NO_CHANGES)
    partials = cats.count(statuses.CATEGORY_PARTIAL)
    skipped = cats.count(statuses.CATEGORY_SKIPPED)
    blocked = cats.count(statuses.CATEGORY_BLOCKED)
    running = cats.count(statuses.CATEGORY_RUNNING)
    totals = {"total": total, "successes": successes, "no_changes": no_changes,
              "partials": partials, "skipped": skipped, "blocked": blocked, "running": running,
              # everything not positively classified counts as a failure (fail loud)
              "failures": total - successes - no_changes - partials - skipped - blocked - running,
              "success_rate": ((successes + no_changes) / total * 100) if total else 0}
    per_leg = {leg: {"files": sum(r.get("files_copied") or 0 for r in runs if r.get("mode") == leg),
                     "bytes": sum(r.get("bytes_copied") or 0 for r in runs if r.get("mode") == leg)}
               for leg in legs}
    return {"period": report_period, "legs": legs, "runs": runs, "totals": totals,
            "per_leg": per_leg, "summaries": summaries, "headline": headline, "act": act,
            "audits": [_audit_row(db, leg) for leg in legs],
            "open_decisions": _open_decisions(db)}


_TD = "padding:6px 12px;border:1px solid #e5e7eb"
_TH = ("text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;"
       "font-size:0.85rem")


def _summary_html(model: dict) -> str:
    cell = "padding:6px 12px;border:1px solid #e5e7eb;vertical-align:top"
    rows = ""
    for s in model["summaries"]:
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
    decisions = model["open_decisions"]
    if decisions == 1:
        open_items = "1 backup waiting for your decision on the dashboard"
    elif decisions:
        open_items = f"{decisions} backups waiting for your decision on the dashboard"
    else:
        open_items = "none"
    act = model["act"]
    todo = ("Open the dashboard and look at the items marked 'not resolved'; forward this "
            "report to support if you are unsure." if act else "Nothing.")
    colour = "#b91c1c" if act else "#047857"
    headline = model["headline"]
    return (f"<div style='background:{colour};color:#ffffff;padding:10px 14px;border-radius:6px;"
            f"font-weight:600;margin:0 0 16px 0'>"
            f"{html.escape(headline[0].upper() + headline[1:])}</div>"
            f"<table style='width:100%;border-collapse:collapse;margin:0 0 12px 0'>{rows}</table>"
            f"<p><b>Open items:</b> {html.escape(open_items)}<br><b>What to do:</b> "
            f"{html.escape(todo)}</p>")


def _stat_row(label: str, value, bold: bool = False) -> str:
    weight = ";font-weight:600" if bold else ""
    return (f"<tr><td style=\"{_TD};background:#f9fafb;width:180px{weight}\">{html.escape(label)}"
            f"</td><td style=\"{_TD}{weight}\">{value}</td></tr>\n")


def render_report_html(model: dict, firm_name: str, is_email: bool = False, name: str = "") -> str:
    """HTML of a report (the email body and the dashboard download), from the model only."""
    rp, t = model["period"], model["totals"]
    summary_html = _summary_html(model)
    title = f"{name or rp.kind} Backup Report"

    rows = ""
    for r in model["runs"]:                       # every run of the period, not a "last 10"
        dur = f"{r.get('duration_seconds', 0):.0f}s" if r.get("duration_seconds") else "-"
        err = r.get("error_message", "")
        # heavy truncation for the inline email (the CSV has the full error)
        err_display = (html.escape(err[:97]) + "..." if err and len(err) > 100
                       else html.escape(err) if err else "-")
        rows += (f"<tr><td>{html.escape(format_ist(r.get('started_at')))}</td>"
                 f"<td>{html.escape((r.get('mode') or 'unknown').upper())}</td>"
                 f"<td>{html.escape(statuses.label(r.get('status')))}</td>"
                 f"<td>{r.get('files_copied') or 0}</td><td>{html.escape(dur)}</td>"
                 f"<td>{err_display}</td></tr>")

    if t["total"] == 0:
        stats = (f"<p style=\"{_TD}\">No backups in {html.escape(rp.label)}.</p>")
    else:
        data = "".join(
            _stat_row(f"Files copied ({LEG_LABEL.get(leg, leg.upper())})", f"{v['files']:,}")
            + _stat_row(f"Data transferred ({LEG_LABEL.get(leg, leg.upper())})",
                        f"{v['bytes']:,} ({humanize.naturalsize(v['bytes'], binary=True)})")
            for leg, v in model["per_leg"].items())
        stats = ("<table style=\"width:100%;border-collapse:collapse;margin:0 0 24px 0\">\n"
                 + _stat_row("Total Backups", t["total"])
                 + _stat_row("Successful Backups", t["successes"])
                 + _stat_row("No Changes (Up-to-date)", t["no_changes"])
                 + _stat_row("Partial Backups", t["partials"])
                 + _stat_row("Skipped Runs", t["skipped"])
                 + _stat_row("Blocked (approval needed)", t["blocked"])
                 + _stat_row("In Progress", t["running"])
                 + _stat_row("Failed Backups", t["failures"])
                 + _stat_row("Success Rate", f"{t['success_rate']:.1f}%", bold=True)
                 + data + "</table>")

    integrity_rows = "".join(
        f"<tr><td style=\"{_TD};background:#f9fafb;width:180px\">{a['leg'].upper()} Integrity"
        f"</td><td style=\"{_TD}\">{html.escape(a['status'])} (checked: "
        f"{html.escape(str(a['checked']))})<br><span style=\"color:#6b7280;font-size:0.85rem\">"
        f"{html.escape(a['detail'])}</span></td></tr>" for a in model["audits"])

    csv_notice = ""
    if is_email:
        csv_notice = ('<p style="color:#059669;font-weight:600;font-size:0.9rem;margin:16px 0;">'
                      'A complete CSV with full error logs is attached to this email.</p>')
    generated = now_formatted("YYYY-MM-DD HH:mm z")
    head = "".join(f"<th style=\"{_TH}\">{h}</th>"
                   for h in ("Started", "Pipeline", "Status", "Files", "Duration", "Error"))
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;color:#1f2937;margin:0;padding:0">
<div style="max-width:800px;margin:0 auto;padding:24px">
<h2 style="color:#1e3a5f;margin:0 0 8px 0">{html.escape(title)} — {html.escape(firm_name)}</h2>
<p style="color:#6b7280;margin:0 0 16px 0">Period: {html.escape(rp.label)} (generated {generated})</p>

{summary_html}

{csv_notice}

<h3 style="color:#374151;margin:0 0 8px 0">Summary</h3>
{stats}

<h3 style="color:#374151;margin:0 0 8px 0">Integrity Verification (independent weekly audit)</h3>
<p style="color:#6b7280;font-size:0.85rem;margin:0 0 8px 0">A COMPLETE backup means the transfer finished normally — it does not imply independent content verification. Integrity below reflects the latest read-only audit only.</p>
<table style="width:100%;border-collapse:collapse;margin:0 0 24px 0">
{integrity_rows}
</table>

<h3 style="color:#374151;margin:0 0 8px 0">Recent Backups (all of the period)</h3>
<table style="width:100%;border-collapse:collapse;margin:0 0 8px 0">
<tr>{head}</tr>
{rows}
</table>
<p style="color:#6b7280;font-size:0.85rem;margin:0">Showing all {len(model['runs'])} backups of the period</p>
</div>
</body>
</html>"""


def generate_report_html(
    db: ManifestDB,
    firm_name: str,
    days: int,
    period: str,
    is_email: bool = False,
    legs=None,
    now=None,
    report_period: ReportPeriod | None = None,
) -> str:
    """HTML report for a period (plan C). An empty period still gives a report that says so
    (P1). `legs` = the enabled legs; default: the legs that appear in the runs."""
    rp = resolve_period(days, period, now, report_period)
    return render_report_html(build_report_model(db, rp, legs), firm_name, is_email, name=period)


def send_summary_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
    days: int,
    period: str,
    body_html: str = None,
    legs=None,
    report_period: ReportPeriod | None = None,
) -> bool:
    """Send aggregated summary report via email with CSV attachment.

    Returns True if email sent, False if it failed."""
    subject, body, attachments = build_summary_email(db, firm_name, days, period,
                                                     body_html=body_html, legs=legs,
                                                     report_period=report_period)
    return _send_email_with_attachments(config, subject, body, attachments)


def build_summary_email(db: ManifestDB, firm_name: str, days: int, period: str,
                        body_html: str = None, legs=None, now=None,
                        report_period: ReportPeriod | None = None
                        ) -> tuple[str, str, list[dict]]:
    """(subject, html body, [CSV attachment]) for a summary report. A period with no runs is
    still reported ("action needed: ... no backup ran", P1). The subject says the outcome."""
    rp = resolve_period(days, period, now, report_period)
    model = build_report_model(db, rp, legs)
    body = body_html or render_report_html(model, firm_name, is_email=True, name=period)
    safe_firm = re.sub(r'[^a-zA-Z0-9_\-]', '_', firm_name)
    attachments = [{"filename": f"{safe_firm}_{period}_Report.csv",
                    "content": _generate_csv_data(model["runs"])}]
    return (f"[{firm_name} Backup] {period} backup report {rp.label} - {model['headline']}",
            body, attachments)


def _legs_in(runs: list[dict]) -> tuple[str, ...]:
    return tuple(leg for leg in ("cloud", "lan") if any(r.get("mode") == leg for r in runs))


def send_weekly_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
    body_html: str = None,
    legs=None,
    report_period: ReportPeriod | None = None,
) -> bool:
    return send_summary_report(db, config, firm_name, 7, "Weekly", body_html=body_html,
                               legs=legs, report_period=report_period)


def send_monthly_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
    body_html: str = None,
    legs=None,
    report_period: ReportPeriod | None = None,
) -> bool:
    return send_summary_report(db, config, firm_name, 30, "Monthly", body_html=body_html,
                               legs=legs, report_period=report_period)

