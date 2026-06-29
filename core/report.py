"""Reports — failure alerts, weekly/monthly summaries via email and HTML.

Reads from ManifestDB run_history. Zero knowledge of backup internals.
generate_report_html() is shared between email delivery and UI download.
"""

import csv
import html
import io
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import humanize
from loguru import logger

from core.manifest import ManifestDB
from core.time_utils import now_formatted
from models.config import NotificationConfig


def _send_email_with_attachments(
    config: NotificationConfig,
    subject: str,
    body_html: str,
    attachments: list[dict] = None,
) -> bool:
    """Send email via SMTP with optional attachments. Returns True on success."""
    if not all([config.smtp_host, config.sender, config.recipients]):
        logger.warning("Email not configured — skipping")
        return False

    if not config.smtp_username or not config.smtp_password:
        logger.warning("SMTP credentials not set — skipping")
        return False

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = config.sender
    msg["To"] = ", ".join(config.recipients)

    # Attach the HTML body
    msg_alt = MIMEMultipart("alternative")
    msg_alt.attach(MIMEText(body_html, "html"))
    msg.attach(msg_alt)

    # Attach the files
    if attachments:
        for att in attachments:
            part = MIMEApplication(att["content"], Name=att["filename"])
            part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
            msg.attach(part)

    server: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    try:
        if config.smtp_port == 465:
            server = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
            server.starttls()

        server.login(config.smtp_username, config.smtp_password)
        server.sendmail(config.sender, config.recipients, msg.as_string())

        logger.info(f"Email sent: {subject}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email '{subject}': {e}")
        return False

    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


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
        logger.info("send_on_failure disabled — skipping alert")
        return False

    mode = (run_data.get("mode") or "unknown").upper()
    subject = f"Backup Failure Alert — {firm_name} ({mode})"

    ts_display = f"<p><strong>Time:</strong> {html.escape(timestamp[:19].replace('T', ' '))}</p>" if timestamp else ""
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
            r.get("error_message", ""),
            r.get("extended_metrics", "")
        ])
        
    return output.getvalue().encode("utf-8")


def generate_report_html(
    db: ManifestDB,
    firm_name: str,
    days: int,
    period: str,
    is_email: bool = False
) -> str:
    """Generate an HTML report string for the given time period.

    Returns "" if no runs found in the period.
    Usable by both email delivery and UI download endpoints.
    """
    runs = db.get_runs_since(days)

    if not runs:
        return ""

    total = len(runs)
    successes = sum(1 for r in runs if (str(r.get("status", "")).endswith("_COMPLETE") and "NO_CHANGES" not in str(r.get("status", ""))) or r.get("status") == "SUCCESS")
    no_changes = sum(1 for r in runs if "NO_CHANGES" in str(r.get("status", "")))
    partials = sum(1 for r in runs if str(r.get("status", "")).endswith("_PARTIAL"))
    skipped = sum(1 for r in runs if str(r.get("status", "")).endswith("_SKIPPED"))
    failures = total - successes - no_changes - partials - skipped

    total_files = sum(r.get("files_copied") or 0 for r in runs)
    total_bytes = sum(r.get("bytes_copied") or 0 for r in runs)

    success_rate = ((successes + no_changes) / total * 100) if total > 0 else 0

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

    # Limit to 10 rows in the email body to guarantee we stay under the 102KB limit
    rows = ""
    row_count = 0
    for r in runs[:10]:
        start = (r.get("started_at") or "-")[:19].replace("T", " ")
        mode = (r.get("mode") or "unknown").upper()
        status = _status_display(r.get("status") or "")
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

    now = now_formatted("YYYY-MM-DD HH:mm z")
    
    csv_notice = ""
    if is_email:
        csv_notice = '<p style="color:#059669;font-weight:600;font-size:0.9rem;margin:16px 0;">A complete CSV with full error logs is attached to this email.</p>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;color:#1f2937;margin:0;padding:0">
<div style="max-width:800px;margin:0 auto;padding:24px">
<h2 style="color:#1e3a5f;margin:0 0 8px 0">{html.escape(period)} Backup Report — {html.escape(firm_name)}</h2>
<p style="color:#6b7280;margin:0 0 24px 0">Period: Last {days} days (generated {now})</p>

{csv_notice}

<h3 style="color:#374151;margin:0 0 8px 0">Summary</h3>
<table style="width:100%;border-collapse:collapse;margin:0 0 24px 0">
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb;width:180px">Total Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{total}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Successful Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{successes}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">No Changes (Up-to-date)</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{no_changes}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Partial Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{partials}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Skipped Runs</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{skipped}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Failed Backups</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{failures}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600">Success Rate</td><td style="padding:6px 12px;border:1px solid #e5e7eb;font-weight:600">{success_rate:.1f}%</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Files Backed Up</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{total_files:,}</td></tr>
<tr><td style="padding:6px 12px;border:1px solid #e5e7eb;background:#f9fafb">Data Transferred</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{total_bytes:,} ({humanize.naturalsize(total_bytes, binary=True)})</td></tr>
</table>

<h3 style="color:#374151;margin:0 0 8px 0">Recent Backups (Last 10)</h3>
<table style="width:100%;border-collapse:collapse;margin:0 0 8px 0">
<tr><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Started</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Pipeline</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Status</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Files</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Duration</th><th style="text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;font-size:0.85rem">Error</th></tr>
{rows}
</table>
<p style="color:#6b7280;font-size:0.85rem;margin:0">Showing {row_count} most recent backups (out of {total} total)</p>
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
    """Send aggregated summary report via email with CSV attachment.

    Returns True if email sent, False if skipped or failed.
    """
    runs = db.get_runs_since(days)
    if not runs:
        logger.info(f"No runs found in last {days} days — skipping {period} report")
        return False
        
    body = body_html or generate_report_html(db, firm_name, days, period, is_email=True)
    if not body:
        return False

    # Generate CSV with all runs for attachment
    csv_bytes = _generate_csv_data(runs)
    csv_filename = f"{firm_name.replace(' ', '_')}_{period}_Report.csv"

    attachments = [{
        "filename": csv_filename,
        "content": csv_bytes
    }]

    subject = f"Backup {period} Report — {firm_name}"
    return _send_email_with_attachments(config, subject, body, attachments)


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

