"""Reports — failure alerts, weekly/monthly summaries via email and HTML.

Reads from ManifestDB run_history. Zero knowledge of backup internals.
generate_report_html() is shared between email delivery and UI download.
"""

import html
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from loguru import logger

import humanize
from core.manifest import ManifestDB
from core.time_utils import now_formatted
from models.config import NotificationConfig


def _send_email(
    config: NotificationConfig,
    subject: str,
    body_html: str,
) -> bool:
    """Send email via SMTP. Returns True on success."""
    if not all([config.smtp_host, config.sender, config.recipients]):
        logger.warning("Email not configured — skipping")
        return False

    if not config.smtp_username or not config.smtp_password:
        logger.warning("SMTP credentials not set — skipping")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.sender
    msg["To"] = ", ".join(config.recipients)
    msg.attach(MIMEText(body_html, "html"))

    server: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    try:
        if config.smtp_port == 465:
            server = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
            server.starttls()

        server.login(config.smtp_username, config.smtp_password)
        server.sendmail(config.sender, config.recipients, msg.as_string())
        server.quit()

        logger.info(f"Email sent: {subject}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email '{subject}': {e}")
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
        return False


def send_failure_alert(
    config: NotificationConfig,
    firm_name: str,
    error_message: str,
    run_data: dict,
    timestamp: str = "",
) -> bool:
    """Send immediate email on backup failure.

    Args:
        config: Notification configuration.
        firm_name: Firm name for subject/body.
        error_message: Error description.
        run_data: Dict with mode, status, exit_code.
        timestamp: ISO timestamp of the failure (optional).

    Returns:
        True if email sent.
    """
    if not config.send_on_failure:
        logger.info("send_on_failure disabled — skipping alert")
        return False

    mode = (run_data.get("mode") or "unknown").upper()

    subject = f"Backup Failure Alert — {firm_name} ({mode})"

    ts_display = ""
    if timestamp:
        ts_display = f"<p><strong>Time:</strong> {html.escape(timestamp[:19].replace('T', ' '))}</p>"

    status_code = html.escape(str(run_data.get("status") or ""))
    exit_code = run_data.get("exit_code")
    exit_code_display = html.escape(str(exit_code)) if exit_code is not None else "-"

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
<td style="padding:8px 12px;border:1px solid #e5e7eb;color:#dc2626">{html.escape(error_message)}</td></tr>
</table>
{ts_display}
<p style="color:#6b7280;font-size:13px;margin-top:24px">Review the server logs at the configured log directory for more details.</p>
</div>
</body>
</html>"""

    return _send_email(config, subject, body)


def generate_report_html(
    db: ManifestDB,
    firm_name: str,
    days: int,
    period: str,
) -> str:
    """Generate an HTML report string for the given time period.

    Returns "" if no runs found in the period.
    Usable by both email delivery and UI download endpoints.
    """
    runs = db.get_runs_since(days)

    if not runs:
        return ""

    total = len(runs)
    successes = sum(1 for r in runs if str(r.get("status", "")).endswith("_COMPLETE") or r.get("status") == "SUCCESS")
    partials = sum(1 for r in runs if str(r.get("status", "")).endswith("_PARTIAL"))
    skipped = sum(1 for r in runs if str(r.get("status", "")).endswith("_SKIPPED"))
    failures = total - successes - partials - skipped

    total_files = sum(r.get("files_copied") or 0 for r in runs)
    total_bytes = sum(r.get("bytes_copied") or 0 for r in runs)

    success_rate = (successes / total * 100) if total > 0 else 0

    def _status_display(status: str) -> str:
        if status in ("CLOUD_NO_CHANGES_COMPLETE", "LAN_NO_CHANGES_COMPLETE"):
            return "No Changes"
        if status.endswith("_COMPLETE") or status == "SUCCESS":
            return "Completed"
        if "_PARTIAL" in status:
            return "Partial"
        if "_FAILED" in status:
            return "Failed"
        if "_SKIPPED" in status:
            return "Skipped"
        return status

    rows = ""
    row_count = 0
    for r in runs:
        start = (r.get("started_at") or "-")[:19].replace("T", " ")
        mode = (r.get("mode") or "unknown").upper()
        status = _status_display(r.get("status") or "")
        files = r.get("files_copied") or 0
        dur = f"{r.get('duration_seconds', 0):.0f}s" if r.get("duration_seconds") else "-"
        err = r.get("error_message", "")
        err_display = html.escape(err[:80]) if err else "-"
        rows += f"<tr><td>{html.escape(start)}</td><td>{html.escape(mode)}</td><td>{html.escape(status)}</td><td>{files}</td><td>{html.escape(dur)}</td><td>{err_display}</td></tr>"
        row_count += 1

    now = now_formatted("YYYY-MM-DD HH:mm z")

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;color:#1f2937;margin:0;padding:0">
<div style="max-width:800px;margin:0 auto;padding:24px">
<h2 style="color:#1e3a5f;margin:0 0 8px 0">{html.escape(period)} Backup Report — {html.escape(firm_name)}</h2>
<p style="color:#6b7280;margin:0 0 24px 0">Period: Last {days} days (generated {now})</p>

<h3 style="color:#374151;margin:0 0 8px 0">Summary</h3>
<table style="width:100%;border-collapse:collapse;margin:0 0 24px 0">
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb;width:180px">Total Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{total}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Successful Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{successes}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Partial Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{partials}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Skipped Runs</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{skipped}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Failed Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{failures}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Success Rate</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{success_rate:.1f}%</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Files Backed Up</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{total_files:,}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Data Transferred</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{total_bytes:,} ({humanize.naturalsize(total_bytes, binary=True)})</td></tr>
</table>

<h3 style="color:#374151;margin:0 0 8px 0">Recent Backups</h3>
<table style="width:100%;border-collapse:collapse;margin:0 0 8px 0">
<tr><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Started</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Pipeline</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Status</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Files</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Duration</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Error</th></tr>
{rows}
</table>
<p style="color:#6b7280;font-size:0.85rem;margin:0">All {row_count} backups shown</p>
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
) -> bool:
    """Send aggregated summary report via email.

    Returns True if email sent, False if skipped or failed.
    """
    body = body_html or generate_report_html(db, firm_name, days, period)
    if not body:
        logger.info(f"No runs found in last {days} days — skipping {period} report")
        return False

    subject = f"Backup {period} Report — {firm_name}"
    return _send_email(config, subject, body)


def send_weekly_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
    body_html: str = None,
) -> bool:
    return send_summary_report(db, config, firm_name, 7, "Weekly", body_html=body_html)


def send_monthly_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
    body_html: str = None,
) -> bool:
    return send_summary_report(db, config, firm_name, 30, "Monthly", body_html=body_html)
