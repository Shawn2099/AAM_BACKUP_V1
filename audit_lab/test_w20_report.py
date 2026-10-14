"""WAVE 20 — coverage-driven tests for core/report.py.

The gap map ranked core/report.py 77%: `send_failure_alert`'s HTML body +
attachment path (lines 124-168) had NEVER been executed by any prior wave,
and the SMTP retry/permanent-error branches were untested. This wave closes
all of them, plus the status-display variants of generate_report_html and the
CSV-injection guard.

Safety: smtplib and time.sleep are faked — no network, no real delays.
"""
from __future__ import annotations

import smtplib
import time
import types

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(120)


class FakeDB:
    """ManifestDB stand-in: only the two methods report.py uses."""

    def __init__(self, runs=(), audits=None):
        self._runs, self._audits = list(runs), audits or {}

    def get_runs_since(self, days):
        return self._runs

    def latest_audit(self, leg):
        v = self._audits.get(leg)
        if isinstance(v, Exception):
            raise v
        return v


def _run(**kw):
    base = {"started_at": "2026-10-13T10:00:00", "ended_at": "2026-10-13T10:01:00",
            "mode": "cloud", "status": "CLOUD_COMPLETE", "files_copied": 3,
            "files_failed": 0, "bytes_copied": 1024, "duration_seconds": 61.0,
            "exit_code": 0, "error_message": "", "extended_metrics": "{}"}
    base.update(kw)
    return base


class _FakeSMTP:
    """Scriptable SMTP server: records the envelope, raises on demand.

    NB: recording attributes must NOT share a name with a method (instance
    attributes shadow methods - that once made `server.login` resolve to None).
    """

    def __init__(self, behaviour=None):
        self.sent = None
        self.tls_started = False
        self.logged_in = None
        self.quit_called = False
        self._behaviour = behaviour or []

    def starttls(self, **kw):
        self.tls_started = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def sendmail(self, sender, recipients, msg):
        self.sent = (sender, recipients, msg)
        b = self._behaviour.pop(0) if self._behaviour else None
        if isinstance(b, Exception):
            raise b

    def quit(self):
        self.quit_called = True


def _cfg(sandbox, **over):
    cfg = L.build_config(sandbox, source=sandbox / "src", lan_dest=str(sandbox / "nas"),
                         runtime=sandbox / "runtime")
    n = cfg.notifications
    n.smtp_host = "smtp.example.test"
    n.smtp_port = 587
    n.smtp_username = "u"
    n.smtp_password = "p"
    n.sender = "backup@example.test"
    n.recipients = ["ops@example.test"]
    n.send_on_failure = True
    for k, v in over.items():
        setattr(n, k, v)
    return cfg, n


# ── _send_email_with_attachments ──────────────────────────────────────────────

def test_w20_email_not_configured(sandbox, monkeypatch):
    from core import report as rep

    _, n = _cfg(sandbox)
    n.smtp_host = ""
    assert rep._send_email_with_attachments(n, "s", "b") is False

    _, n = _cfg(sandbox)
    n.recipients = []
    assert rep._send_email_with_attachments(n, "s", "b") is False

    _, n = _cfg(sandbox)
    n.smtp_username = ""
    assert rep._send_email_with_attachments(n, "s", "b") is False   # no creds


def test_w20_email_starttls_success(sandbox, monkeypatch):
    from core import report as rep

    _, n = _cfg(sandbox)
    made = _FakeSMTP()
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout=None, **kw: made)
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("wrong port path"))
    ok = rep._send_email_with_attachments(
        n, "Subject", "<html>b</html>",
        attachments=[{"filename": "failure_details.txt", "content": b"boom"}])
    assert ok is True and made.tls_started is True and made.quit_called is True
    sender, recipients, msg = made.sent
    assert made.logged_in == ("u", "p")
    sender, recipients, msg = made.sent
    assert sender == "backup@example.test" and recipients == ["ops@example.test"]
    assert "failure_details.txt" in msg          # the MIME attachment is attached
    import email
    html_parts = [part.get_payload(decode=True).decode("utf-8")
                  for part in email.message_from_string(msg).walk()
                  if part.get_content_type() == "text/html"]
    assert html_parts == ["<html>b</html>"]     # UTF-8 body (base64 on the wire)


def test_w20_email_ssl_port_465(sandbox, monkeypatch):
    from core import report as rep

    _, n = _cfg(sandbox, smtp_port=465)
    made = _FakeSMTP()
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda host, port, timeout=None, **kw: made)
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("should use SSL"))
    assert rep._send_email_with_attachments(n, "S", "b") is True
    assert made.tls_started is False                  # implicit TLS, no starttls
    assert made.logged_in == ("u", "p")


def test_w20_email_permanent_error_no_retry(sandbox, monkeypatch):
    from core import report as rep

    _, n = _cfg(sandbox)
    made = _FakeSMTP(behaviour=[smtplib.SMTPAuthenticationError(535, b"bad creds")])
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout=None, **kw: made)
    monkeypatch.setattr(time, "sleep", lambda s: pytest.fail("must not retry"))
    assert rep._send_email_with_attachments(n, "S", "b") is False

    # WP8 fix: 421 is a TRANSIENT reply (smtplib still raises SenderRefused
    # for it) - it is retried and the email is delivered, not dropped.
    made = _FakeSMTP(behaviour=[smtplib.SMTPSenderRefused(421, b"try later", "sender")])
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout=None, **kw: made)
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    assert rep._send_email_with_attachments(n, "S", "b") is True
    assert len(slept) == 1


def test_w20_email_transient_retries_then_succeeds(sandbox, monkeypatch):
    from core import report as rep

    _, n = _cfg(sandbox)
    made = _FakeSMTP(behaviour=[OSError("connection reset")])
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout=None, **kw: made)
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    assert rep._send_email_with_attachments(n, "S", "b") is True   # 2nd attempt ok
    assert len(slept) == 1                                          # one backoff


def test_w20_email_transient_exhausts(sandbox, monkeypatch):
    from core import report as rep

    _, n = _cfg(sandbox)
    made = _FakeSMTP(behaviour=[OSError("x")] * 3)
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout=None, **kw: made)
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    assert rep._send_email_with_attachments(n, "S", "b") is False
    assert len(slept) == 2                       # 3 attempts, 2 backoffs
    L.record("W20.smtp_retry_policy",
             "SMTP policy (core.notify): 5xx never retries, 4xx/transient errors back "
             "off and give up after notifications.retry_attempts",
             "auth 535->no-retry False | 421->retried->True | transient x1->True on "
             "attempt 2 | transient x3->False with 2 backoffs",
             "OK - the retry policy behaves exactly as documented")


# ── send_failure_alert ────────────────────────────────────────────────────────

def _capture_alert(rep, monkeypatch):
    seen = {}

    def fake_send(config, subject, body, attachments):
        seen["subject"] = subject
        seen["body"] = body
        seen["attachments"] = attachments
        return True

    monkeypatch.setattr(rep, "_send_email_with_attachments", fake_send)
    return seen


def test_w20_failure_alert_disabled(sandbox, monkeypatch):
    from core import report as rep

    cfg, n = _cfg(sandbox, send_on_failure=False)
    monkeypatch.setattr(rep, "_send_email_with_attachments",
                        lambda *a, **k: pytest.fail("must not send"))
    assert rep.send_failure_alert(n, "Firm", "boom", {"mode": "cloud"}) is False


def test_w20_failure_alert_long_error_attaches(sandbox, monkeypatch):
    from core import report as rep

    cfg, n = _cfg(sandbox)
    seen = _capture_alert(rep, monkeypatch)
    long_err = "E" * 2500
    ok = rep.send_failure_alert(n, "Firm & Co", long_err,
                                {"mode": "lan", "status": "LAN_PARTIAL",
                                 "exit_code": 11},
                                timestamp="2026-10-13T10:00:00")
    assert ok is True
    assert seen["subject"] == "Backup Failure Alert - Firm & Co (LAN)"
    body = seen["body"]
    assert "Firm &amp; Co" in body                # html-escaped
    assert "TRUNCATED - SEE ATTACHMENT" in body
    assert "LAN_PARTIAL" in body and ">11<" in body
    assert "2026-10-13 10:00:00" in body          # the Time row
    att = seen["attachments"]
    assert len(att) == 1 and att[0]["filename"] == "failure_details.txt"
    assert att[0]["content"] == long_err.encode("utf-8")   # full error, untruncated


def test_w20_failure_alert_short_error_no_attachment(sandbox, monkeypatch):
    from core import report as rep

    cfg, n = _cfg(sandbox)
    seen = _capture_alert(rep, monkeypatch)
    rep.send_failure_alert(n, "Firm", "short boom", {"mode": "cloud"})   # no timestamp
    assert seen["attachments"] == []
    assert "short boom" in seen["body"] and "TRUNCATED" not in seen["body"]
    assert "<p><strong>Time:</strong>" not in seen["body"]
    assert seen["subject"] == "Backup Failure Alert - Firm (CLOUD)"

    # a missing mode reads UNKNOWN (never crashes, never lies)
    rep.send_failure_alert(n, "Firm", "e", {"status": "X"})
    assert "UNKNOWN" in seen["subject"]
    assert "UNKNOWN" in seen["body"]            # the Pipeline cell


def test_w20_failure_alert_zero_exit_code(sandbox, monkeypatch):
    from core import report as rep

    cfg, n = _cfg(sandbox)
    seen = _capture_alert(rep, monkeypatch)
    rep.send_failure_alert(n, "Firm", "err", {"mode": "cloud", "status": "X",
                                              "exit_code": 0})
    assert ">0<" in seen["body"]                  # exit 0 renders as 0, not "-"


# ── generate_report_html ──────────────────────────────────────────────────────

def test_w20_report_html_empty(sandbox):
    from core import report as rep

    assert rep.generate_report_html(FakeDB([]), "Firm", 7, "Weekly") == ""


def test_w20_report_html_status_variants(sandbox):
    from core import report as rep

    runs = [
        _run(status="CLOUD_COMPLETE"),
        _run(status="LAN_NO_CHANGES_COMPLETE"),
        _run(status="LAN_PARTIAL"),
        _run(status="CLOUD_FAILED"),
        _run(status="LAN_SKIPPED"),
        _run(status="SUCCESS"),
        _run(status="WEIRD_STATUS"),
        _run(error_message="x" * 250),            # truncated inline
    ]
    audits = {"cloud": {"status": "VERIFICATION_FAILED",
                        "ended_at": "2026-10-13T09:00:00",
                        "detail": "mismatch"},
              "lan": RuntimeError("audit db gone")}   # -> NOT VERIFIED
    html = rep.generate_report_html(FakeDB(runs, audits), "Firm", 7, "Weekly")
    for marker in ("No Changes", "Completed", "Partial", "Failed", "Skipped",
                   "WEIRD_STATUS", "Success Rate", "NOT VERIFIED",
                   "VERIFICATION_FAILED", "x" * 97 + "..."):
        assert marker in html, marker
    # is_email=True adds the CSV-attachment notice
    html_email = rep.generate_report_html(FakeDB(runs), "Firm", 7, "Weekly",
                                          is_email=True)
    assert "A complete CSV with full error logs is attached" in html_email
    # is_email=False -> no CSV notice
    html2 = rep.generate_report_html(FakeDB(runs), "Firm", 7, "Weekly")
    assert "complete CSV with full error logs" not in html2


def test_w20_report_html_row_limit(sandbox):
    from core import report as rep

    runs = [_run(run_id=f"r{i}") for i in range(25)]
    html = rep.generate_report_html(FakeDB(runs), "Firm", 30, "Monthly")
    assert "Showing 10 most recent backups (out of 25 total)" in html


def test_w20_report_html_no_audits(sandbox):
    from core import report as rep

    html = rep.generate_report_html(FakeDB([_run()]), "Firm", 7, "Weekly")
    assert html.count("No independent audit has run yet.") == 2   # cloud + lan


# ── _csv_safe / CSV generation ────────────────────────────────────────────────

def test_w20_csv_safe():
    from core.report import _csv_safe, _generate_csv_data

    assert _csv_safe(None) == ""
    assert _csv_safe("plain") == "plain"
    assert _csv_safe("123") == "123"
    for bad in ("=SUM(A1)", "+1", "-2", "@cmd", "\tTAB", "\rCR"):
        assert _csv_safe(bad) == "'" + bad, bad
    data = _generate_csv_data([_run(error_message="=cmd()", extended_metrics='{"a":1}')])
    text = data.decode("utf-8")
    assert "'=cmd()" in text                       # neutralised in the CSV too
    assert "Started At" in text and "Extended Metrics" in text
    L.record("W20.csv_injection_guard",
             "G5: cells starting = + - @ TAB CR are quote-prefixed so Excel "
             "never evaluates free-text error/metric columns",
             "=SUM/+1/-2/@cmd/TAB/CR -> quoted; None->''; numbers untouched",
             "OK - the CSV injection guard holds")


# ── send_summary_report / weekly / monthly ────────────────────────────────────

def test_w20_summary_report_no_runs(sandbox, monkeypatch):
    from core import report as rep

    cfg, n = _cfg(sandbox)
    monkeypatch.setattr(rep, "_send_email_with_attachments",
                        lambda *a, **k: pytest.fail("must not send"))
    assert rep.send_summary_report(FakeDB([]), n, "Firm", 7, "Weekly") is False


def test_w20_summary_report_regenerates_when_body_falsy(sandbox, monkeypatch):
    from core import report as rep

    cfg, n = _cfg(sandbox)
    seen = {}
    monkeypatch.setattr(rep, "_send_email_with_attachments",
                        lambda c, s, b, a: seen.update(subject=s, body=b) or True)
    # body_html=None (default) -> generated from the DB
    ok = rep.send_summary_report(FakeDB([_run()]), n, "Firm", 7, "Weekly")
    assert ok is True and "Backup Report" in seen["body"]


def test_w20_summary_report_happy(sandbox, monkeypatch):
    from core import report as rep

    cfg, n = _cfg(sandbox)
    seen = {}

    def fake_send(config, subject, body, attachments):
        seen["subject"], seen["attachments"] = subject, attachments
        return True

    monkeypatch.setattr(rep, "_send_email_with_attachments", fake_send)
    ok = rep.send_summary_report(FakeDB([_run()]), n, "Firm", 7, "Weekly")
    assert ok is True
    assert seen["subject"] == "Backup Weekly Report - Firm"
    att = seen["attachments"]
    assert att[0]["filename"] == "Firm_Weekly_Report.csv"
    assert att[0]["content"].startswith(b"Started At")


def test_w20_summary_report_uses_given_body(sandbox, monkeypatch):
    from core import report as rep

    cfg, n = _cfg(sandbox)
    seen = {}
    monkeypatch.setattr(rep, "_send_email_with_attachments",
                        lambda c, s, b, a: seen.update(subject=s, body=b) or True)
    rep.send_summary_report(FakeDB([_run()]), n, "Firm", 30, "Monthly",
                            body_html="<html>GIVEN</html>")
    assert seen["body"] == "<html>GIVEN</html>"    # no regeneration
    assert seen["subject"] == "Backup Monthly Report - Firm"


def test_w20_weekly_monthly_delegate(sandbox, monkeypatch):
    from core import report as rep

    cfg, n = _cfg(sandbox)
    calls = []

    def fake_summary(db, config, firm, days, period, body_html=None):
        calls.append((days, period, body_html))
        return True

    monkeypatch.setattr(rep, "send_summary_report", fake_summary)
    assert rep.send_weekly_report(FakeDB([]), n, "Firm") is True
    assert rep.send_monthly_report(FakeDB([]), n, "Firm", body_html="X") is True
    assert calls == [(7, "Weekly", None), (30, "Monthly", "X")]
