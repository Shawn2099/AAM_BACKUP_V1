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
import time
from datetime import timedelta
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pendulum
from loguru import logger

from core import netinfo
from core.time_utils import now_iso

SENT = "SENT"
FAILED = "FAILED"
SUPPRESSED = "SUPPRESSED"
DISABLED = "DISABLED"

# Never silenced by notifications.send_on_failure (audit 2026-10-18, item 1):
# a decision is needed or protection is lost, and RECOVERED closes an alert
# the reader has already seen. Only routine FAILURE / WARNING emails obey it.
ALWAYS_SENT_KINDS = frozenset({"ACTION_REQUIRED", "RECOVERED", "INFO"})

# Who an email is for (owner 2026-10-22: two recipient lists). Every producer states its audience
# explicitly; nothing is routed by guessing from the kind.
AUDIENCE_ALERTS = "alerts"      # problems, action needed, warnings, resolved: notifications.recipients
AUDIENCE_REPORTS = "reports"    # weekly / monthly report, new-financial-year notice
AUDIENCE_BOTH = "both"          # the test email: every address of both lists, once
AUDIENCES = (AUDIENCE_ALERTS, AUDIENCE_REPORTS, AUDIENCE_BOTH)


@dataclass(frozen=True)
class Recipients:
    """The addresses one email goes to; `note` says when a list stood in for an empty one."""
    addresses: list[str]
    note: str | None = None


def resolve_recipients(notifications, audience: str) -> Recipients:
    """THE place that turns an audience into addresses. An empty list uses the other one (never a
    silent loss); both empty = no addresses, which `send_email` reports as a failed delivery.
    `both` is the case-insensitive de-duplicated union (an address in both lists gets one mail)."""
    if audience not in AUDIENCES:
        raise ValueError(f"unknown audience {audience!r} (expected one of {AUDIENCES})")
    alerts = list(notifications.recipients)
    reports = list(notifications.report_recipients)
    if not alerts and not reports:
        return Recipients([], "no recipients are configured (alerts list and reports list are empty)")
    if audience == AUDIENCE_BOTH:
        seen, union = set(), []
        for address in alerts + reports:
            if address.lower() not in seen:
                seen.add(address.lower())
                union.append(address)
        return Recipients(union)
    own, other, name, other_name = ((alerts, reports, "alerts", "reports")
                                    if audience == AUDIENCE_ALERTS
                                    else (reports, alerts, "reports", "alerts"))
    if own:
        return Recipients(own)
    return Recipients(other, f"the {name} list is empty - sent to the {other_name} list")

class RecipientsPartlyRefused(Exception):
    """The mail server accepted the message for some addresses and refused others (a typo in a list).
    The message WAS delivered to the accepted ones, but the refused address never gets it, so the
    delivery is reported as failed and names them - not retried (the refusal would repeat)."""

    def __init__(self, refused: dict):
        self.refused = refused
        listed = "; ".join(
            f"{addr}: {code} {msg.decode(errors='replace') if isinstance(msg, bytes) else msg}"
            for addr, (code, msg) in refused.items())
        super().__init__(f"the mail server refused: {listed}")


def _is_permanent_smtp_error(exc: BaseException) -> bool:
    """True when retrying cannot help (RFC 5321: 5xx = permanent).

    Classified by reply CODE, not exception type: smtplib raises
    SMTPSenderRefused / SMTPRecipientsRefused for transient 4xx replies
    too (e.g. "451 try again later"), which must be retried.
    """
    if isinstance(exc, (smtplib.SMTPNotSupportedError, RecipientsPartlyRefused)):
        return True  # e.g. STARTTLS / AUTH not offered by the server; a refused address stays refused
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
    artefacts_dir: str | None = None   # the run's full-log folder (support attachments)
    first_reported_at: str | None = None   # set by _notify on a reminder of an open incident
    audience: str = AUDIENCE_ALERTS    # which recipient list(s): alerts | reports | both
    cooldown_hours: float | None = None   # repeat window of THIS alert; None = alerts.repeat_cooldown_hours


@dataclass
class DeliveryResult:
    status: str
    error: str | None = None
    attempts: int = 0
    message_id: str | None = None
    rung: int | None = None       # index into attachment_rungs that was delivered (None = no ladder)
    recipients: list[str] | None = None   # the addresses it was addressed to (None = never resolved)


# ── SMTP transport ────────────────────────────────────────────────────────

def _smtp_security(notifications) -> str:
    mode = getattr(notifications, "smtp_security", "auto")
    if mode == "auto":
        return "ssl" if notifications.smtp_port == 465 else "starttls"
    return mode


@contextmanager
def _smtp_session(notifications):
    """A connected, TLS-secured, authenticated SMTP session; always quit on exit."""
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
        yield server
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass


def _advertised_size(server) -> int:
    """The server's EHLO `SIZE` limit in bytes; 0 = not advertised or "no limit" (RFC 1870)."""
    try:
        server.ehlo_or_helo_if_needed()     # smtplib only greets lazily, inside sendmail
        return int(server.esmtp_features.get("size", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _send_once(notifications, msgs: list[MIMEMultipart], first: int = 0,
               state: dict | None = None, *, recipients: list[str]) -> None:
    """Send the richest of msgs[first:] that fits the server's advertised SIZE (the poorest if
    none does; the server then has the last word). `state["rung"]` is set to the index tried
    BEFORE the data is sent, so a failure can say which rung it was."""
    with _smtp_session(notifications) as server:
        limit = _advertised_size(server)
        sizes = [len(m.as_string()) for m in msgs]
        pick = next((i for i in range(first, len(msgs)) if not limit or sizes[i] <= limit),
                    len(msgs) - 1)
        if state is not None:
            state["rung"] = pick
        refused = server.sendmail(notifications.sender, list(recipients), msgs[pick].as_string())
        if refused:
            raise RecipientsPartlyRefused(refused)


def _is_attachment_problem(exc: BaseException) -> bool:
    """A refusal that a smaller message can cure: 552 (too large), a content filter's 554, or a
    hang-up while the data was being sent (RFC 5321 s.3.8: a server may drop a too-big DATA)."""
    if isinstance(exc, (smtplib.SMTPServerDisconnected, ConnectionError)):
        return True
    return getattr(exc, "smtp_code", None) in (552, 554)


def email_config_problem(notifications) -> str | None:
    """Return why email cannot be sent with this config, or None if usable. Which list is used is
    not decided here: an empty list stands in for the other (`resolve_recipients`)."""
    if not notifications.smtp_host:
        return "notifications.smtp_host is empty"
    if not notifications.sender:
        return "notifications.sender is empty"
    if not notifications.recipients and not notifications.report_recipients:
        return "no recipients are configured (notifications.recipients and report_recipients are empty)"
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


def _attach(msg: MIMEMultipart, attachments: list[dict]) -> MIMEMultipart:
    for att in attachments:
        part = MIMEApplication(att["content"], Name=att["filename"])
        part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
        msg.attach(part)
    return msg


def send_email(notifications, subject: str, body_html: str,
               attachments: list[dict] | None = None, *, audience: str,
               body_text: str | None = None,
               headers: dict[str, str] | None = None,
               message_id: str | None = None,
               attachment_rungs: list[list[dict]] | None = None) -> DeliveryResult:
    """Send one email with retry (tenacity). Never raises.

    audience: alerts | reports | both - which recipient list(s) get it (`resolve_recipients`).
    attachments: [{"filename": str, "content": bytes}]
    attachment_rungs: the same lists from richest to poorest (the last may be empty). The richest
    rung that fits the server's advertised SIZE is sent; a refusal that a smaller message can cure
    (552, a content filter's 554, a hang-up mid-DATA) drops ONE rung and resends at once, so an
    attachment can never keep an alert from being delivered. `DeliveryResult.rung` says which.
    Every email carries Message-ID (Google requires it from every sender), Date,
    Auto-Submitted (RFC 3834: no out-of-office replies) and a plain-text part
    next to the HTML (audit 2026-10-18). `headers` adds e.g. In-Reply-To.
    """
    problem = email_config_problem(notifications)
    if problem:
        logger.error(f"Email not sent ({subject!r}): {problem}")
        return DeliveryResult(FAILED, problem, 0)
    try:
        target = resolve_recipients(notifications, audience)
    except ValueError as e:          # a programming error, but "never raises" holds for the transport
        logger.error(f"Email not sent ({subject!r}): {e}")
        return DeliveryResult(FAILED, str(e), 0)
    to = list(target.addresses)
    if target.note:
        logger.warning(f"Email {subject!r}: {target.note}")

    domain = notifications.sender.rpartition("@")[2] or "aam-backup"
    message_id = message_id or make_msgid(domain=domain)
    ladder = attachment_rungs is not None
    rungs = list(attachment_rungs) if ladder and attachment_rungs else [attachments or []]
    text = body_text if body_text is not None else html_to_text(body_html)

    def build(atts: list[dict]) -> MIMEMultipart:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = notifications.sender
        msg["To"] = ", ".join(target.addresses)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = message_id
        msg["Auto-Submitted"] = "auto-generated"
        for name, value in (headers or {}).items():
            msg[name] = value
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text, "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html", "utf-8"))
        msg.attach(alt)
        return _attach(msg, atts)

    msgs = [build(r) for r in rungs]

    from tenacity import (
        RetryError,
        Retrying,
        retry_if_exception,
        stop_after_attempt,
        wait_random_exponential,
    )

    attempts, first, state = 0, 0, {"rung": None}
    while True:
        state["rung"] = None
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(notifications.retry_attempts),
                wait=wait_random_exponential(multiplier=notifications.retry_base_seconds,
                                             max=notifications.retry_base_seconds * 8),
                retry=retry_if_exception(lambda e: not _is_permanent_smtp_error(e)
                                         and not _drops_a_rung(e, state, rungs)),
                reraise=True,
            ):
                with attempt:
                    attempts += 1
                    _send_once(notifications, msgs, first, state, recipients=target.addresses)
            break
        except (RetryError, Exception) as e:  # noqa: BLE001
            used = state["rung"]
            if used is not None and _drops_a_rung(e, state, rungs):
                logger.warning(f"Email {subject!r}: rung {used} refused ({type(e).__name__}: {e}); "
                               f"resending with less attached")
                first = used + 1
                continue
            kind = "permanent SMTP error, not retried" if _is_permanent_smtp_error(e)                 else f"failed after {attempts} attempt(s)"
            logger.error(f"Email {kind} {subject!r}: {e}")
            return DeliveryResult(FAILED, f"{type(e).__name__}: {e}", attempts, message_id,
                                  recipients=to)
    rung = state["rung"] if ladder else None
    logger.info(f"Email sent: {subject!r} (attempt {attempts}"
                + (f", attachment rung {rung}" if ladder else "") + ")")
    return DeliveryResult(SENT, None, attempts, message_id, rung, recipients=to)


def attachment_ladder(attachments: list[dict]) -> tuple[list[list[dict]], list[str]]:
    """(rungs, labels) richest to poorest for one alert: everything, the report without the logs
    .zip, then no attachment. Rungs that would repeat the one before are left out."""
    if not attachments:
        return [], []
    rungs, labels = [list(attachments)], ["full"]
    report_only = [a for a in attachments if not a["filename"].lower().endswith(".zip")]
    if report_only and len(report_only) < len(attachments):
        rungs.append(report_only)
        labels.append("report-only")
    rungs.append([])
    labels.append("none")
    return rungs, labels


def _drops_a_rung(exc: BaseException, state: dict, rungs: list[list[dict]]) -> bool:
    """True when `exc` happened on a rung that still has attachments AND a poorer rung exists."""
    used = state["rung"]
    return (used is not None and used + 1 < len(rungs) and bool(rungs[used])
            and _is_attachment_problem(exc))


# ── Alert rendering ───────────────────────────────────────────────────────

_SERVER_ONLY_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""}


_link_cache: dict = {}


def _nas_host(config) -> str | None:
    host = getattr(getattr(config, "wol", None), "server_ip", "") or ""
    if host:
        return host
    dest = getattr(getattr(config, "paths", None), "lan_destination", "") or ""
    if dest.startswith("\\\\"):
        return dest.lstrip("\\").split("\\", 1)[0] or None
    return None


def _detected_ip(config) -> str | None:
    """The office address of this server (plan D1), looked up at most every
    dashboard.link_cache_seconds."""
    from models.config import DashboardConfig, model_default
    ttl = getattr(config.dashboard, "link_cache_seconds", model_default(DashboardConfig, "link_cache_seconds"))
    hit = _link_cache.get("ip")
    if hit and ttl and time.monotonic() - hit[0] < ttl:
        return hit[1]
    ip = netinfo.office_ip(_nas_host(config))
    _link_cache["ip"] = (time.monotonic(), ip)
    return ip


def dashboard_urls(config) -> tuple[str, str | None]:
    """(link, alternative link) for emails (plan D2). A manual dashboard.public_url wins.
    Otherwise: the bind address when it is one office PCs can reach, else (0.0.0.0 / ::) the
    detected office address, else the server name. The alternative is the server name."""
    d = config.dashboard
    if getattr(d, "public_url", ""):
        return d.public_url, None
    host = getattr(d, "bind_address", "127.0.0.1")
    name = f"http://{socket.gethostname()}:{d.port}"
    if host in ("0.0.0.0", "::"):          # "all interfaces" is not an address to open
        ip = _detected_ip(config)
        if ip:
            return f"http://{ip}:{d.port}", name
        return name, None
    return f"http://{host}:{d.port}", (name if netinfo.usable(host) else None)


def dashboard_url(config) -> str:
    return dashboard_urls(config)[0]


def dashboard_link_problem(config) -> str | None:
    """Why the emails' dashboard link would not open on an office PC, or None
    (owner 2026-10-18: dashboard on the office network only). Plan D3: also a manual IP that is
    not an address of this server, and a bind address other than the detected office address."""
    url = dashboard_url(config)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"dashboard.public_url {url!r} is not a link (expected http://<server>:<port>)"
    if parts.hostname.lower() in _SERVER_ONLY_HOSTS:
        return (f"the dashboard link in emails is {url}, which opens only on the server "
                "itself - set dashboard.public_url to the address office PCs use "
                "(e.g. http://backup-server:8080)")
    d = config.dashboard
    detected = _detected_ip(config)
    if netinfo.usable(parts.hostname) and parts.hostname not in netinfo.all_ipv4():
        return (f"the dashboard link {url} uses {parts.hostname}, which is not an address of this "
                f"server (this server's office address is {detected or 'unknown'}) - correct "
                "dashboard.public_url")
    bind = getattr(d, "bind_address", "127.0.0.1")
    if bind not in ("0.0.0.0", "::") and netinfo.usable(bind) and detected and bind != detected:
        return (f"the dashboard listens only on {bind} but the office address of this "
                f"server is {detected} - office PCs cannot open it; set dashboard.bind_address to "
                "0.0.0.0 or the office address")
    return None


def render_alert(config, event: AlertEvent, db=None) -> tuple[str, str, list[dict]]:
    """Return (subject, html_body, attachments) for an event.

    The text is the plain-language email of core.email_compose; problem emails
    carry the support report + logs of core.support_bundle (audit 2026-10-18)."""
    from core.email_compose import compose
    from core.support_bundle import build

    subject, body, _text = compose(config, event, db)
    return subject, body, build(config, event, db)


# ── Public entry point ────────────────────────────────────────────────────

# D1: when building an alert fails (DB, incident lookup, compose, support bundle) a short plain notice is
# sent instead. No DB here, so the repeat cooldown is process-local: event key -> time.monotonic() of the
# last notice (a persistent condition re-notified every minute by the watchdog must not become a mail storm).
_FALLBACK_SENT: dict[str, float] = {}


def _cooldown_hours(config, event: AlertEvent) -> float:
    """How long a repeat of this alert is held back: its own window, else the global one."""
    return (event.cooldown_hours if event.cooldown_hours is not None
            else config.alerts.repeat_cooldown_hours)


def _fallback_alert(config, event: AlertEvent, error: Exception) -> DeliveryResult:
    """The short notice (D1): the event's own title and message, no DB, no compose, no attachments.
    Obeys notifications.send_on_failure like a normal alert. Never raises."""
    import html

    try:
        if not config.notifications.send_on_failure and event.kind not in ALWAYS_SENT_KINDS:
            return DeliveryResult(DISABLED, "notifications.send_on_failure is false "
                                            "(routine failure/warning emails only)")
        cooldown_h = _cooldown_hours(config, event)
        last = _FALLBACK_SENT.get(event.key)
        if last is not None and cooldown_h > 0 and time.monotonic() - last < cooldown_h * 3600:
            return DeliveryResult(SUPPRESSED, f"same short notice already sent (cooldown {cooldown_h}h)")
        subject = f"[{config.firm_name} Backup] {event.kind}: {event.title}"
        lines = [event.title, event.message or "",
                 "This is a short notice: the full alert could not be prepared "
                 f"({type(error).__name__}). The details are in the backup program's logs on the server."]
        body = "".join(f"<p>{html.escape(line)}</p>" for line in lines if line)
        result = send_email(config.notifications, subject, body, audience=event.audience,
                            headers={"X-AAM-Reference": event.run_id or event.key})
        if result.status == SENT:
            _FALLBACK_SENT[event.key] = time.monotonic()
        return result
    except Exception as e:  # noqa: BLE001 - the last resort must not raise
        logger.error(f"Short alert notice for {event.key} failed too: {e}")
        return DeliveryResult(FAILED, f"short notice failed: {e}")


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
        result = _fallback_alert(config, event, e)       # D1: the human is still told
        if db is not None:                                # ... and the outcome recorded when the DB works
            try:
                db.record_notification({
                    "event_key": event.key, "kind": event.kind, "mode": event.mode,
                    "subject": event.title, "status": result.status, "error": result.error,
                    "attempts": result.attempts, "created_at": now_iso(),
                    "fault_code": event.fault_code, "message_id": result.message_id,
                    "thread_id": None, "attachments_rung": None,
                    "audience": event.audience, "recipients": result.recipients})
            except Exception as rec_err:  # noqa: BLE001
                logger.error(f"Could not record the short notice for {event.key}: {rec_err}")
    finally:
        if own_db and db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    return result


def _incident_closed(event: AlertEvent, db, config, prior: dict) -> bool:
    """Has the problem behind `prior` (the incident's last SENT mail) ended - whether or not a RESOLVED
    mail was ever sent? A later SUCCESSFUL run (cloud / lan), a later VERIFIED audit, a healthy watchdog
    check, or - for alerts with no resolution signal of their own - `alerts.incident_quiet_days` without
    any row of this key (a SUPPRESSED / LIMITED repeat counts as the problem still occurring).
    Reminder limits count the mails of one incident, so an incident that never closed would silence the
    first mail of a NEW problem with the same key."""
    mode, since = event.mode, prior["created_at"]
    try:
        if mode in ("cloud", "lan"):
            return db.leg_success_after(mode, since)
        if mode == "audit":
            return db.audit_verified_after(since)
        if mode == "watchdog":
            healthy = db.watchdog_healthy_at()
            return bool(healthy) and _is_after(healthy, since)
        latest = db.last_notification(event.key) or prior
        quiet = timedelta(days=config.alerts.incident_quiet_days)
        return pendulum.now("UTC") - pendulum.parse(latest["created_at"]) > quiet
    except Exception as e:  # noqa: BLE001 - when unsure the incident stays open (the safe side for a count)
        logger.warning(f"Could not tell whether the {event.key} incident is over ({e}); treated as open")
        return False


def _is_after(a_iso: str, b_iso: str) -> bool:
    return pendulum.parse(a_iso).timestamp() > pendulum.parse(b_iso).timestamp()


def _incident(event: AlertEvent, db, config=None) -> tuple[dict | None, str | None]:
    """(the earlier SENT email of this same, unchanged incident or None,
    Message-ID of the incident's first email to reply to or None).

    Audit 2026-10-18 item 6: a RESOLVED email or a different fault code on
    the leg starts a new incident, so its first email is never suppressed. An incident also ends when
    the problem itself ended (`_incident_closed`), even if no RESOLVED mail was sent."""
    prior = db.last_notification(event.key, status=SENT)
    if event.kind == "RECOVERED":
        same = prior if prior and not db.sent_in_mode_after(
            event.mode, prior["id"], recovered=False) else None
        open_problem = db.last_open_incident(event.mode) or {}
        root = open_problem.get("thread_id") or open_problem.get("message_id")
        return same, root
    unchanged = (prior is not None and prior.get("fault_code") == event.fault_code
                 and not db.sent_in_mode_after(event.mode, prior["id"], recovered=True)
                 and not (config is not None and _incident_closed(event, db, config, prior)))
    same = prior if unchanged else None
    root = (same.get("thread_id") or same.get("message_id")) if same else None
    return same, root


def _notify(config, event: AlertEvent, db) -> DeliveryResult:
    if event.fault_code is None:
        from core.email_compose import effective_code
        event = replace(event, fault_code=effective_code(event))
    same, root = _incident(event, db, config)
    if same and event.kind != "RECOVERED":
        first = (db.notification_by_message_id(root) if root else None) or same
        event = replace(event, first_reported_at=first["created_at"])
    subject, body, attachments = render_alert(config, event, db)
    labels: list[str] = []

    if not config.notifications.send_on_failure and event.kind not in ALWAYS_SENT_KINDS:
        result = DeliveryResult(DISABLED, "notifications.send_on_failure is false "
                                          "(routine failure/warning emails only)")
    else:
        cooldown_h = _cooldown_hours(config, event)
        if same and cooldown_h > 0 and _within_hours(same["created_at"], cooldown_h):
            result = DeliveryResult(
                SUPPRESSED, f"same alert already sent at {same['created_at']} "
                            f"(cooldown {cooldown_h}h)")
        else:
            headers = {"X-AAM-Reference": event.run_id or event.key}
            if event.fault_code:
                headers["X-AAM-Fault-Code"] = event.fault_code
            if root:      # reminder / RESOLVED: same conversation as the first email
                headers["In-Reply-To"] = root
                headers["References"] = root
            rungs, labels = attachment_ladder(attachments)
            result = send_email(config.notifications, subject, body, audience=event.audience,
                                headers=headers, attachment_rungs=rungs or None)

    try:
        db.record_notification({
            "event_key": event.key, "kind": event.kind, "mode": event.mode,
            "subject": subject, "status": result.status, "error": result.error,
            "attempts": result.attempts, "created_at": now_iso(),
            "fault_code": event.fault_code, "message_id": result.message_id,
            "thread_id": (root or result.message_id) if result.status == SENT else None,
            "attachments_rung": (labels[result.rung] if labels and result.rung is not None
                                 else None),
            "audience": event.audience, "recipients": result.recipients,
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
