"""One alert path for every notification (plan §5.5, WP8; owner DEC-3).

`notify(config, event)` is the single way the system tells a human
something. It:

* sends the email through ONE SMTP transport (`send_email`), with retries
  via tenacity and every SMTP parameter taken from config;
* de-duplicates: the same `event.key` already SENT within
  `alerts.repeat_cooldown_hours` is SUPPRESSED (recorded, not sent);
* RECORDS every outcome (SENT / FAILED / SUPPRESSED / DISABLED) in the
  manifest `notifications` table, so the dashboard can show that alert
  delivery itself is failing — email is the only push channel;
* points the reader to the dashboard (`dashboard.public_url`) for anything
  that needs a decision — decisions are only taken on the dashboard.

It never raises: a broken alert path must not break a backup.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from urllib.parse import urlsplit

from loguru import logger

from core.time_utils import now_iso

SENT = "SENT"
FAILED = "FAILED"
SUPPRESSED = "SUPPRESSED"
DISABLED = "DISABLED"

# Never silenced by notifications.send_on_failure (audit 2026-10-18, item 1):
# a decision is needed or protection is lost, and RECOVERED closes an alert
# the reader has already seen. Only routine FAILURE / WARNING emails obey it.
ALWAYS_SENT_KINDS = frozenset({"ACTION_REQUIRED", "RECOVERED"})

def _is_permanent_smtp_error(exc: BaseException) -> bool:
    """True when retrying cannot help (RFC 5321: 5xx = permanent).

    Classified by reply CODE, not exception type: smtplib raises
    SMTPSenderRefused / SMTPRecipientsRefused for transient 4xx replies
    too (e.g. "451 try again later"), which must be retried.
    """
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return True  # e.g. STARTTLS / AUTH not offered by the server
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _msg in exc.recipients.values()]
        return bool(codes) and all(500 <= c < 600 for c in codes)
    code = getattr(exc, "smtp_code", None)
    return isinstance(code, int) and 500 <= code < 600


@dataclass
class AlertEvent:
    """One thing a human must be told about.

    key:        de-duplication key, e.g. "backup:lan:LAN_FAILED"
    kind:       FAILURE | ACTION_REQUIRED | RECOVERED | WARNING
    mode:       cloud | lan | rollover | audit | watchdog | report | ...
    """

    key: str
    kind: str
    mode: str
    title: str
    message: str
    status: str | None = None
    exit_code: int | None = None
    details: dict = field(default_factory=dict)
    occurred_at: str = field(default_factory=now_iso)
    attachment_text: str | None = None
    fault_code: str | None = None      # core.faults code (docs/EMAIL_CONTENT_SPEC.md §4)
    run_id: str | None = None          # the run this is about: support reference
    resolved_code: str | None = None   # RECOVERED: the code that was fixed


@dataclass
class DeliveryResult:
    status: str
    error: str | None = None
    attempts: int = 0
    message_id: str | None = None


# ── SMTP transport ────────────────────────────────────────────────────────

def _smtp_security(notifications) -> str:
    mode = getattr(notifications, "smtp_security", "auto")
    if mode == "auto":
        return "ssl" if notifications.smtp_port == 465 else "starttls"
    return mode


def _send_once(notifications, msg: MIMEMultipart) -> None:
    security = _smtp_security(notifications)
    timeout = notifications.smtp_timeout_seconds
    if security == "ssl":
        server = smtplib.SMTP_SSL(notifications.smtp_host, notifications.smtp_port,
                                  timeout=timeout, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(notifications.smtp_host, notifications.smtp_port, timeout=timeout)
    try:
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
        if notifications.smtp_auth:
            server.login(notifications.smtp_username, notifications.smtp_password)
        server.sendmail(notifications.sender, list(notifications.recipients), msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass


def email_config_problem(notifications) -> str | None:
    """Return why email cannot be sent with this config, or None if usable."""
    if not notifications.smtp_host:
        return "notifications.smtp_host is empty"
    if not notifications.sender:
        return "notifications.sender is empty"
    if not notifications.recipients:
        return "notifications.recipients is empty"
    if notifications.smtp_auth and not (notifications.smtp_username and notifications.smtp_password):
        return "notifications.smtp_auth is true but smtp_username/smtp_password are empty"
    return None


class _TextExtractor(HTMLParser):
    """Plain-text rendering of our own email HTML (stdlib only)."""

    _BREAKS = {"br", "p", "div", "h1", "h2", "h3", "h4", "tr", "pre", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._href: str | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        elif tag in self._BREAKS:
            self.out.append("\n")
        elif tag == "td":
            self.out.append("  ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href:
            self.out.append(f" ({self._href})")
            self._href = None
        elif tag in self._BREAKS and tag != "br":
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(body_html: str) -> str:
    parser = _TextExtractor()
    parser.feed(body_html)
    lines = [ln.rstrip() for ln in "".join(parser.out).splitlines()]
    text, blank = [], False
    for ln in lines:                       # collapse runs of empty lines
        if ln.strip() or not blank:
            text.append(ln)
        blank = not ln.strip()
    return "\n".join(text).strip()


def send_email(notifications, subject: str, body_html: str,
               attachments: list[dict] | None = None, *, body_text: str | None = None,
               headers: dict[str, str] | None = None,
               message_id: str | None = None) -> DeliveryResult:
    """Send one email with retry (tenacity). Never raises.

    attachments: [{"filename": str, "content": bytes}]
    Every email carries Message-ID (Google requires it from every sender), Date,
    Auto-Submitted (RFC 3834: no out-of-office replies) and a plain-text part
    next to the HTML (audit 2026-10-18). `headers` adds e.g. In-Reply-To.
    """
    problem = email_config_problem(notifications)
    if problem:
        logger.error(f"Email not sent ({subject!r}): {problem}")
        return DeliveryResult(FAILED, problem, 0)

    domain = notifications.sender.rpartition("@")[2] or "aam-backup"
    message_id = message_id or make_msgid(domain=domain)
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = notifications.sender
    msg["To"] = ", ".join(notifications.recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id
    msg["Auto-Submitted"] = "auto-generated"
    for name, value in (headers or {}).items():
        msg[name] = value
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_text if body_text is not None else html_to_text(body_html),
                        "plain", "utf-8"))
    alt.attach(MIMEText(body_html, "html", "utf-8"))
    msg.attach(alt)
    for att in attachments or []:
        part = MIMEApplication(att["content"], Name=att["filename"])
        part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
        msg.attach(part)

    from tenacity import (
        RetryError,
        Retrying,
        retry_if_exception,
        stop_after_attempt,
        wait_random_exponential,
    )

    attempts = 0
    try:
        for attempt in Retrying(
            stop=stop_after_attempt(notifications.retry_attempts),
            wait=wait_random_exponential(multiplier=notifications.retry_base_seconds,
                                         max=notifications.retry_base_seconds * 8),
            retry=retry_if_exception(lambda e: not _is_permanent_smtp_error(e)),
            reraise=True,
        ):
            with attempt:
                attempts = attempt.retry_state.attempt_number
                _send_once(notifications, msg)
    except (RetryError, Exception) as e:  # noqa: BLE001
        kind = "permanent SMTP error, not retried" if _is_permanent_smtp_error(e) \
            else f"failed after {attempts} attempt(s)"
        logger.error(f"Email {kind} {subject!r}: {e}")
        return DeliveryResult(FAILED, f"{type(e).__name__}: {e}", attempts, message_id)
    logger.info(f"Email sent: {subject!r} (attempt {attempts})")
    return DeliveryResult(SENT, None, attempts, message_id)


# ── Alert rendering ───────────────────────────────────────────────────────

_SERVER_ONLY_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""}


def dashboard_url(config) -> str:
    d = config.dashboard
    if getattr(d, "public_url", ""):
        return d.public_url
    host = d.bind_address
    if host in ("0.0.0.0", "::"):          # "all interfaces" is not an address to open
        host = socket.gethostname()
    return f"http://{host}:{d.port}"


def dashboard_link_problem(config) -> str | None:
    """Why the emails' dashboard link would not open on an office PC, or None
    (owner 2026-10-18: dashboard on the office network only)."""
    url = dashboard_url(config)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"dashboard.public_url {url!r} is not a link (expected http://<server>:<port>)"
    if parts.hostname.lower() in _SERVER_ONLY_HOSTS:
        return (f"the dashboard link in emails is {url}, which opens only on the server "
                "itself - set dashboard.public_url to the address office PCs use "
                "(e.g. http://backup-server:8080)")
    return None


def render_alert(config, event: AlertEvent, db=None) -> tuple[str, str, list[dict]]:
    """Return (subject, html_body, attachments) for an event.

    The text is the plain-language email of core.email_compose (audit
    2026-10-18); a long message is cut in the body and attached in full."""
    from core.email_compose import compose

    subject, body, _text = compose(config, event, db)
    limit = config.alerts.body_max_chars
    message = event.message or ""
    attachments: list[dict] = []
    full_text = event.attachment_text if event.attachment_text is not None else message
    if full_text and (len(message) > limit or event.attachment_text is not None):
        cap = config.alerts.attachment_max_kb * 1024
        data = full_text.encode("utf-8")
        if len(data) > cap:
            data = data[-cap:]
        attachments.append({"filename": "details.txt", "content": data})
    return subject, body, attachments


# ── Public entry point ────────────────────────────────────────────────────

def notify(config, event: AlertEvent, db=None) -> DeliveryResult:
    """Send (or suppress) one alert and record the outcome. Never raises."""
    own_db = db is None
    try:
        if db is None:
            from core.manifest import ManifestDB
            db = ManifestDB.from_config(config)
        result = _notify(config, event, db)
    except Exception as e:  # noqa: BLE001 - alerting must never break a backup
        logger.error(f"Alert pipeline error for {event.key}: {e}")
        result = DeliveryResult(FAILED, f"alert pipeline error: {e}")
    finally:
        if own_db and db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    return result


def _notify(config, event: AlertEvent, db) -> DeliveryResult:
    subject, body, attachments = render_alert(config, event, db)

    if not config.notifications.send_on_failure and event.kind not in ALWAYS_SENT_KINDS:
        result = DeliveryResult(DISABLED, "notifications.send_on_failure is false "
                                          "(routine failure/warning emails only)")
    else:
        cooldown_h = config.alerts.repeat_cooldown_hours
        last = db.last_notification(event.key, status=SENT) if cooldown_h > 0 else None
        if last and _within_hours(last["created_at"], cooldown_h):
            result = DeliveryResult(
                SUPPRESSED, f"same alert already sent at {last['created_at']} "
                            f"(cooldown {cooldown_h}h)")
        else:
            headers = {"X-AAM-Reference": event.run_id or event.key}
            if event.fault_code:
                headers["X-AAM-Fault-Code"] = event.fault_code
            result = send_email(config.notifications, subject, body, attachments,
                                headers=headers)

    try:
        db.record_notification({
            "event_key": event.key, "kind": event.kind, "mode": event.mode,
            "subject": subject, "status": result.status, "error": result.error,
            "attempts": result.attempts, "created_at": now_iso(),
        })
    except Exception as e:  # noqa: BLE001
        logger.error(f"Could not record notification outcome for {event.key}: {e}")
    log = logger.info if result.status in (SENT, SUPPRESSED) else logger.warning
    log(f"Alert {event.key}: {result.status}" + (f" ({result.error})" if result.error else ""))
    return result


def _within_hours(iso_ts: str, hours: float) -> bool:
    import pendulum

    try:
        then = pendulum.parse(iso_ts)
    except Exception:  # noqa: BLE001
        return False
    return (pendulum.now("UTC") - then).total_seconds() < hours * 3600
