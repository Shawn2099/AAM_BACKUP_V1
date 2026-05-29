"""Reports — failure alerts, weekly/monthly summaries via email.

Reads from ManifestDB run_history. Zero knowledge of backup internals.
"""

import html
import smtplib
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from loguru import logger

from core.manifest import ManifestDB
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
) -> bool:
    """Send immediate email on backup failure.

    Args:
        config: Notification configuration.
        firm_name: Firm name for subject/body.
        error_message: Error description.
        run_data: Dict with mode, run_id, status, exit_code.

    Returns:
        True if email sent.
    """
    if not config.send_on_failure:
        logger.info("send_on_failure disabled — skipping alert")
        return False

    mode = (run_data.get("mode") or "unknown").upper()
    run_id = str(run_data.get("run_id") or "unknown")

    subject = f"Backup FAILED — {firm_name} ({mode})"
    body = f"""<html><body>
<h2 style="color: red;">Backup Failure — {html.escape(firm_name)}</h2>
<table>
  <tr><td><strong>Mode:</strong></td><td>{html.escape(mode)}</td></tr>
  <tr><td><strong>Run ID:</strong></td><td>{html.escape(run_id)}</td></tr>
  <tr><td><strong>Error:</strong></td><td>{html.escape(error_message)}</td></tr>
</table>
</body></html>"""

    return _send_email(config, subject, body)


def send_summary_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
    days: int,
    period: str,
) -> bool:
    """Send aggregated summary report for a time period.

    Args:
        db: ManifestDB instance.
        config: Notification configuration.
        firm_name: Firm name.
        days: Number of days to aggregate.
        period: Label like "Weekly" or "Monthly".

    Returns:
        True if email sent.
    """
    runs = db.get_runs_since(days)

    if not runs:
        logger.info(f"No runs found in last {days} days — skipping {period} report")
        return False

    total = len(runs)
    _success = {"LAN_COMPLETE", "CLOUD_COMPLETE"}
    _failure = {"LAN_FAILED", "CLOUD_FAILED"}
    _partial = {"LAN_PARTIAL", "CLOUD_PARTIAL"}
    successes = sum(1 for r in runs if r["status"] in _success)
    failures = sum(1 for r in runs if r["status"] in _failure)
    partials = sum(1 for r in runs if r["status"] in _partial)

    total_files = sum(r["files_copied"] or 0 for r in runs)
    total_bytes = sum(r["bytes_copied"] or 0 for r in runs)

    success_rate = (successes / total * 100) if total > 0 else 0

    rows = ""
    for r in runs[:10]:  # Show latest 10
        start = r["started_at"][:19] if r["started_at"] else "?"
        mode = r["mode"].upper()
        status = r["status"]
        files = r["files_copied"] or 0
        rows += f"<tr><td>{html.escape(start)}</td><td>{html.escape(mode)}</td><td>{html.escape(status)}</td><td>{files}</td></tr>"

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"Backup {period} Report — {firm_name}"
    body = f"""<html><body>
<h2>{html.escape(period)} Backup Report — {html.escape(firm_name)}</h2>
<p><strong>Period:</strong> Last {days} days (generated {now})</p>

<h3>Summary</h3>
<table>
  <tr><td>Total runs</td><td>{total}</td></tr>
  <tr><td>Successful</td><td>{successes}</td></tr>
  <tr><td>Partial</td><td>{partials}</td></tr>
  <tr><td>Failed</td><td>{failures}</td></tr>
  <tr><td>Success rate</td><td>{success_rate:.1f}%</td></tr>
  <tr><td>Files copied</td><td>{total_files:,}</td></tr>
  <tr><td>Bytes copied</td><td>{total_bytes:,} ({_human_bytes(total_bytes)})</td></tr>
</table>

<h3>Recent Runs</h3>
<table border="1" cellpadding="4" cellspacing="0">
  <tr><th>Started</th><th>Mode</th><th>Status</th><th>Files</th></tr>
  {rows}
</table>
</body></html>"""

    return _send_email(config, subject, body)


def send_weekly_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
) -> bool:
    return send_summary_report(db, config, firm_name, 7, "Weekly")


def send_monthly_report(
    db: ManifestDB,
    config: NotificationConfig,
    firm_name: str,
) -> bool:
    return send_summary_report(db, config, firm_name, 30, "Monthly")


def _human_bytes(n: int) -> str:
    val: float = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024:
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} PB"
