"""Audit 2026-10-18 (docs/EMAIL_CONTENT_SPEC.md §3, §6): every email carries
a valid Message-ID and Date (Google requires a Message-ID from every sender),
Auto-Submitted (no out-of-office replies), and a plain-text part next to the
HTML. Checked on the wire with the real SMTP test server."""

import email
import email.utils

from core import notify as nt
from models.config import NotificationConfig
from test_support.smtp_server import smtp_server


def _notif(port) -> NotificationConfig:
    return NotificationConfig(smtp_host="127.0.0.1", smtp_port=port, sender="backup@firm.example",
                              recipients=["ops@firm.example"], smtp_security="none",
                              smtp_auth=False, retry_attempts=1, retry_base_seconds=0.01,
                              smtp_timeout_seconds=5)


def _send(**kw):
    with smtp_server() as srv:
        result = nt.send_email(_notif(srv.server_address[1]), "Subject", "<p>Hello <b>you</b></p>", **kw)
    assert result.status == nt.SENT
    return result, email.message_from_string(srv.messages[0]["data"])


def test_message_id_date_and_auto_submitted_headers():
    result, msg = _send()
    mid = msg["Message-ID"]
    assert mid and mid.startswith("<") and mid.endswith("@firm.example>")
    assert result.message_id == mid
    assert email.utils.parsedate_to_datetime(msg["Date"]) is not None
    assert msg["Auto-Submitted"] == "auto-generated"
    for header in ("Message-ID", "Date", "From", "To", "Subject"):
        assert len(msg.get_all(header)) == 1          # single-instance headers once


def test_each_email_gets_its_own_message_id():
    first, _ = _send()
    second, _ = _send()
    assert first.message_id != second.message_id


def test_plain_text_part_next_to_html():
    _, msg = _send()
    alt = [p for p in msg.walk() if p.get_content_type() == "multipart/alternative"]
    assert len(alt) == 1
    types = [p.get_content_type() for p in alt[0].get_payload()]
    assert types == ["text/plain", "text/html"]          # html last = preferred
    text = alt[0].get_payload()[0].get_payload(decode=True).decode("utf-8")
    assert "Hello you" in text and "<b>" not in text


def test_given_plain_text_and_extra_headers_are_used():
    _, msg = _send(body_text="Plain words only.",
                   headers={"X-AAM-Fault-Code": "AAM-NAS-01", "In-Reply-To": "<a@firm.example>"},
                   message_id="<fixed-1@firm.example>")
    alt = [p for p in msg.walk() if p.get_content_type() == "text/plain"][0]
    assert alt.get_payload(decode=True).decode("utf-8") == "Plain words only."
    assert msg["X-AAM-Fault-Code"] == "AAM-NAS-01"
    assert msg["In-Reply-To"] == "<a@firm.example>"
    assert msg["Message-ID"] == "<fixed-1@firm.example>"


def test_html_to_text_keeps_links_and_lines():
    text = nt.html_to_text("<h2>Title</h2><p>Line one<br>line two</p>"
                           "<p><a href='http://backup-server:8080'>Open</a></p>"
                           "<table><tr><td>Firm</td><td>AAM &amp; Co</td></tr></table>")
    assert "Title" in text and "Line one\nline two" in text
    assert "Open (http://backup-server:8080)" in text
    assert "AAM & Co" in text
