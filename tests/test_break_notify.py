"""Adversarial real-world scenarios for the alert path (core/notify.py).

Every scenario talks to a REAL local SMTP server (test_support.smtp_server) or a real dead / silent TCP
port; nothing reaches the network. Invariant under test: an alert failure never raises into the backup
flow, and every attempt is recorded in the notifications table with its outcome.
"""

from __future__ import annotations

import contextlib
import email
import email.header
import socket
import sqlite3
import threading
import time
from unittest import mock

import pendulum
import pytest

from core import alert_class as ac
from core import notify as nt
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support import ui_login
from test_support.smtp_server import smtp_server

SENDER = "backup@firm.example"
ALERTS = "ops@firm.example"
REPORTS = "reports@firm.example"


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(tmp_path, port=25, *, alerts=None, **notif) -> AppConfig:
    n = {"smtp_host": "127.0.0.1", "smtp_port": port, "sender": SENDER, "recipients": [ALERTS],
         "smtp_security": "none", "smtp_auth": False, "retry_attempts": 3,
         "retry_base_seconds": 0.01, "smtp_timeout_seconds": 1, "send_on_failure": True}
    n.update(notif)
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": "\\\\nas\\share\\FY26-27",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications=n, alerts={"repeat_cooldown_hours": 12, **(alerts or {})},
    )


def _event(key="backup:lan:LAN_FAILED", kind="FAILURE", mode="lan", title="LAN backup failed",
           message="robocopy exit 16", **kw) -> nt.AlertEvent:
    return nt.AlertEvent(key=key, kind=kind, mode=mode, title=title, message=message, **kw)


def _dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _silent_server():
    """Accepts TCP connections and never sends the SMTP greeting (a hung relay)."""
    lst = socket.socket()
    lst.bind(("127.0.0.1", 0))
    lst.listen(8)
    lst.settimeout(0.2)
    held, stop = [], threading.Event()

    def accept_loop():
        while not stop.is_set():
            try:
                conn, _ = lst.accept()
                held.append(conn)
            except OSError:
                continue

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        yield lst.getsockname()[1]
    finally:
        stop.set()
        t.join(2)
        for c in held:
            c.close()
        lst.close()


class FakeClock:
    def __init__(self):
        self.t = pendulum.now("UTC").replace(microsecond=0)

    def advance(self, hours: float) -> None:
        self.t = self.t.add(seconds=int(hours * 3600))

    def now(self, tz=None):
        return self.t

    def iso(self) -> str:
        return self.t.to_iso8601_string()


@contextlib.contextmanager
def fake_clock():
    """Freezes the clock the cooldown check and the recorded created_at both read."""
    clock = FakeClock()
    with mock.patch("pendulum.now", side_effect=clock.now), \
            mock.patch.object(nt, "now_iso", side_effect=clock.iso):
        yield clock


def _rows(db) -> list[dict]:
    return [dict(r) for r in db._get_conn().execute("select * from notifications order by id")]


def _parsed(raw: str):
    return email.message_from_string(raw)


def _body_text(raw: str) -> str:
    out = []
    for part in _parsed(raw).walk():
        if part.is_multipart():
            continue
        out.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))
    return "\n".join(out)


def _subject(raw: str) -> str:
    return str(email.header.make_header(email.header.decode_header(_parsed(raw)["Subject"])))


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _fresh_fallback_throttle():
    nt._FALLBACK_SENT.clear()
    yield
    nt._FALLBACK_SENT.clear()


# ── transport: server down, 4xx vs 5xx, refusals, timeouts ─────────────────

def test_01_smtp_server_down_is_retried_then_failed_and_recorded(tmp_path, db):
    cfg = _cfg(tmp_path, _dead_port(), retry_attempts=3)
    result = nt.notify(cfg, _event(), db=db)
    rows = _rows(db)
    assert result.status == nt.FAILED
    assert result.attempts == 3                      # no connection: a transient error, every try used
    assert len(rows) == 1 and rows[0]["status"] == nt.FAILED and rows[0]["attempts"] == 3
    assert rows[0]["error"]                          # the reason is recorded for the dashboard


# 4xx = transient (retried). 550 = permanent (one try). 552/554 are 5xx too, but for an alert WITH
# attachments they mean "the attachment is the problem": the ladder drops ONE rung and resends (B4).
@pytest.mark.parametrize("reply, attempts", [
    ("421 4.7.0 service busy", 3),
    ("450 4.2.0 mailbox busy", 3),
    ("451 4.3.0 try again later", 3),
    ("550 5.1.8 sender rejected", 1),
    ("552 5.3.4 message too big", 2),
    ("554 5.7.1 blocked by policy", 2),
])
def test_02_transient_4xx_is_retried_and_permanent_5xx_is_not(tmp_path, db, reply, attempts):
    with smtp_server(fail_with=reply) as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], retry_attempts=3)
        result = nt.notify(cfg, _event(), db=db)
    assert result.status == nt.FAILED
    assert result.attempts == attempts
    assert srv.mail_attempts == attempts
    assert _rows(db)[0]["attempts"] == attempts


def test_03_relay_recovers_on_second_try_and_the_mail_arrives_once(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], retry_attempts=3)
        real_send_once = nt._send_once
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            srv.fail_with = "421 busy" if calls["n"] == 1 else None
            return real_send_once(*args, **kwargs)

        with mock.patch.object(nt, "_send_once", side_effect=flaky):
            result = nt.notify(cfg, _event(), db=db)
    assert result.status == nt.SENT and result.attempts == 2
    assert len(srv.messages) == 1
    assert _rows(db)[0]["status"] == nt.SENT and _rows(db)[0]["attempts"] == 2


@pytest.mark.parametrize("recipients, refuse, delivered", [
    # a typo in one list: the accepted address still gets the alert, the refused one is named
    ([ALERTS, "typo@firm.example"], ("typo@firm.example",), 1),
    # every address refused: nothing sent, a permanent failure, one try
    ([ALERTS], (ALERTS,), 0),
])
def test_04_refused_recipients_are_named_not_retried_and_the_rest_still_get_the_alert(
        tmp_path, db, recipients, refuse, delivered):
    with smtp_server(refuse_rcpt=refuse) as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], retry_attempts=3, recipients=recipients)
        result = nt.notify(cfg, _event(), db=db)
    assert result.status == nt.FAILED
    assert refuse[0] in (result.error or "")
    assert result.attempts == 1                       # a refusal repeats: not retried
    assert len(srv.messages) == delivered
    assert all(refuse[0] not in r for m in srv.messages for r in m["rcpts"])
    assert _rows(db)[0]["status"] == nt.FAILED


def test_05_silent_relay_times_out_on_every_try_without_hanging(tmp_path, db):
    with _silent_server() as port:
        cfg = _cfg(tmp_path, port, retry_attempts=2, smtp_timeout_seconds=1)
        started = time.monotonic()
        result = nt.notify(cfg, _event(), db=db)
        elapsed = time.monotonic() - started
    assert result.status == nt.FAILED
    assert result.attempts == 2
    assert elapsed < 30, f"the alert path hung for {elapsed:.1f}s"
    assert _rows(db)[0]["status"] == nt.FAILED


@pytest.mark.parametrize("kind, server_kwargs, rung_text", [
    # the relay hangs up mid-DATA when the big file arrives (RFC 5321 3.8): resend without it
    ("hangup", {"hangup_over": 4000}, "<p>short text</p>"),
    # a content filter refuses a .zip with 554: resend without it, the alert is not lost
    ("content_filter", {"reject_attachments": "554 5.7.1 zip refused by content filter"}, "<p>text</p>"),
])
def test_06_attachment_problems_drop_one_rung_and_the_alert_still_arrives(
        tmp_path, kind, server_kwargs, rung_text):
    with smtp_server(**server_kwargs) as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        rungs = [[{"filename": "logs.zip", "content": b"Z" * 20000}], []]
        result = nt.send_email(cfg.notifications, "Backup failed", rung_text,
                               audience=nt.AUDIENCE_ALERTS, attachment_rungs=rungs)
    assert result.status == nt.SENT, (kind, result.error)
    assert result.rung == 1                           # the poorer rung (no attachment) went out
    assert len(srv.messages) == 1 and "Content-Disposition: attachment" not in srv.messages[0]["data"]


# ── size, unicode and header injection ─────────────────────────────────────

def test_07_huge_subject_does_not_raise_and_is_recorded(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        result = nt.notify(cfg, _event(title="B" * 20000), db=db)
    assert result.status in (nt.SENT, nt.FAILED)
    assert len(_rows(db)) == 1 and _rows(db)[0]["status"] == result.status


def test_08_oversized_alert_text_keeps_the_inline_body_bounded(tmp_path, db):
    """alerts.body_max_chars: 'Inline message length in the email body'. The full text belongs in the
    attachment, not repeated in the body. A 2 MB robocopy dump must not land in the HTML and text parts."""
    limit = 1000                                        # AlertsConfig.body_max_chars default
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        result = nt.notify(cfg, _event(message="robocopy line " * 150_000), db=db)
    assert result.status == nt.SENT
    # only the inline HTML / text parts: the full text is meant to sit in the support attachment
    inline = "\n".join((p.get_payload(decode=True) or b"").decode("utf-8", "replace")
                       for p in _parsed(srv.messages[0]["data"]).walk()
                       if not p.is_multipart() and p.get_filename() is None)
    assert inline.count("robocopy line") * len("robocopy line ") <= 20 * limit, (
        f"inline body carries {inline.count('robocopy line')} copies of the 2 MB message")


def test_09_body_larger_than_the_server_size_limit_fails_once_and_is_recorded(tmp_path):
    with smtp_server(size_limit=50_000) as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], retry_attempts=3)
        result = nt.send_email(cfg.notifications, "Huge", "<p>" + "x" * 200_000 + "</p>",
                               audience=nt.AUDIENCE_ALERTS)
    assert result.status == nt.FAILED and result.attempts == 1       # 552 is permanent
    assert srv.messages == []


def test_10_malayalam_and_symbols_in_subject_and_body_arrive_intact(tmp_path, db):
    title = "പണം അടയ്ക്കൽ: LAN backup failed — ₹1,00,000 café"
    message = "ഫയലുകൾ പകർത്താനായില്ല (robocopy exit 16) — നമസ്കാരം"
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        result = nt.notify(cfg, _event(title=title, message=message), db=db)
    assert result.status == nt.SENT, result.error
    raw = srv.messages[0]["data"]
    assert title in _subject(raw)
    assert message in _body_text(raw)


def test_11_crlf_in_subject_cannot_inject_a_bcc_or_extra_header(tmp_path, db):
    evil_title = "Backup\r\nBcc: attacker@evil.example\r\nX-Injected: yes"
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        result = nt.notify(cfg, _event(title=evil_title), db=db)   # must not raise
    assert result.status in (nt.SENT, nt.FAILED)
    for msg in srv.messages:
        headers = _parsed(msg["data"])
        assert headers.get_all("Bcc") is None
        assert headers.get_all("X-Injected") is None
        assert "attacker@evil.example" not in msg["rcpts"]


def test_12_crlf_in_a_recipient_cannot_smuggle_an_rcpt_command(tmp_path):
    """Config validation rejects such an address at load; this checks the transport when the value
    gets past validation (assignment after load, a future code path)."""
    with pytest.raises(Exception, match="alerts list"):
        _cfg(tmp_path, 25, recipients=["ops@firm.example\r\nRCPT TO:<evil@evil.example>"])
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        cfg.notifications.recipients = [f"{ALERTS}>\r\nRCPT TO:<evil@evil.example>"]
        result = nt.send_email(cfg.notifications, "x", "<p>x</p>", audience=nt.AUDIENCE_ALERTS)
    assert not any("evil" in r for m in srv.messages for r in m["rcpts"]), result


# ── recipients: empty lists, two lists, union ──────────────────────────────

def test_13_no_recipients_at_all_fails_without_touching_the_server(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], recipients=[], report_recipients=[])
        result = nt.notify(cfg, _event(), db=db)
    assert result.status == nt.FAILED and "no recipients" in result.error
    assert srv.mail_attempts == 0
    assert _rows(db)[0]["status"] == nt.FAILED


def test_14_empty_alerts_list_sends_to_the_reports_list_and_says_so(tmp_path):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], recipients=[], report_recipients=[REPORTS])
        result = nt.send_email(cfg.notifications, "Alert", "<p>x</p>", audience=nt.AUDIENCE_ALERTS)
    assert result.status == nt.SENT and result.recipients == [REPORTS]
    assert srv.messages[0]["rcpts"] and REPORTS in srv.messages[0]["rcpts"][0]


def test_15_alerts_and_reports_lists_are_kept_apart(tmp_path):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], recipients=[ALERTS], report_recipients=[REPORTS])
        a = nt.send_email(cfg.notifications, "Alert", "<p>a</p>", audience=nt.AUDIENCE_ALERTS)
        r = nt.send_email(cfg.notifications, "Weekly report", "<p>r</p>", audience=nt.AUDIENCE_REPORTS)
    assert a.recipients == [ALERTS] and r.recipients == [REPORTS]
    assert ALERTS in srv.messages[0]["rcpts"][0] and REPORTS not in srv.messages[0]["rcpts"][0]
    assert REPORTS in srv.messages[1]["rcpts"][0] and ALERTS not in srv.messages[1]["rcpts"][0]


def test_16_both_audience_mails_each_address_once_even_across_lists_and_case(tmp_path):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], recipients=["Ops@Firm.example", REPORTS],
                   report_recipients=["ops@firm.example", REPORTS.upper()])
        result = nt.send_email(cfg.notifications, "Test", "<p>t</p>", audience=nt.AUDIENCE_BOTH)
    assert result.status == nt.SENT
    assert len(srv.messages) == 1
    assert len(srv.messages[0]["rcpts"]) == 2


# ── configuration: mail not configured, or switched off ────────────────────

@pytest.mark.parametrize("notif, status, attempts, text", [
    ({"smtp_host": ""}, nt.FAILED, 0, "smtp_host"),                           # not configured
    ({"smtp_auth": True, "smtp_username": "", "smtp_password": ""},
     nt.FAILED, 0, "smtp_password"),                                          # auth without a password
    ({"send_on_failure": False}, nt.DISABLED, 0, "send_on_failure"),          # switched off
])
def test_17_mail_not_configured_or_off_never_connects_and_is_recorded(tmp_path, db, notif, status, attempts, text):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], **notif)
        result = nt.notify(cfg, _event(), db=db)
    assert result.status == status and text in (result.error or "")
    assert srv.mail_attempts == attempts
    assert _rows(db)[0]["status"] == status


# ── alert-class switches and dedup / cooldown ──────────────────────────────

ALL_SWITCHES_OFF = {"send_decision_needed": False, "send_overdue": False, "send_action_needed": False,
                    "send_resolved": False, "send_info": False}
CLASS_EVENTS = {
    ac.FAILURE: _event(),
    ac.DECISION: _event(key="blocked:lan", kind="ACTION_REQUIRED", title="Waiting for you"),
    ac.OVERDUE: _event(key="stale:lan", kind="ACTION_REQUIRED", title="Overdue"),
    ac.ACTION: _event(key="rollover:clock_behind", kind="ACTION_REQUIRED", mode="rollover", title="Act"),
    ac.INFO: _event(key="rollover:done:FY27-28", kind="INFO", mode="rollover", title="New year"),
    ac.SECURITY: _event(key="password:admin", kind="INFO", mode="watchdog", title="Password changed"),
    ac.TEST: _event(key="test:now", kind="INFO", mode="report", title="Test"),
}


def test_18_every_class_with_its_switch_off_is_disabled_while_security_and_test_always_go(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], send_on_failure=False, alerts=ALL_SWITCHES_OFF)
        outcomes = {cls: nt.notify(cfg, ev, db=db).status for cls, ev in CLASS_EVENTS.items()}
    assert {c: s for c, s in outcomes.items() if c not in (ac.SECURITY, ac.TEST)} == {
        c: nt.DISABLED for c in CLASS_EVENTS if c not in (ac.SECURITY, ac.TEST)}
    assert outcomes[ac.SECURITY] == nt.SENT and outcomes[ac.TEST] == nt.SENT
    assert len(srv.messages) == 2


def test_19_same_key_is_suppressed_inside_the_cooldown_other_keys_are_not(tmp_path, db):
    with smtp_server() as srv, fake_clock() as clock:
        cfg = _cfg(tmp_path, srv.server_address[1])
        first = nt.notify(cfg, _event(), db=db)
        clock.advance(11)
        second = nt.notify(cfg, _event(), db=db)                              # same key, inside 12 h
        other = nt.notify(cfg, _event(key="backup:cloud:CLOUD_FAILED"), db=db)  # different key
    assert (first.status, second.status, other.status) == (nt.SENT, nt.SUPPRESSED, nt.SENT)
    assert [r["status"] for r in _rows(db)] == [nt.SENT, nt.SUPPRESSED, nt.SENT]
    assert len(srv.messages) == 2


def test_20_cooldown_expires_on_the_fake_clock_and_the_repeat_is_threaded_to_the_first_mail(tmp_path, db):
    with smtp_server() as srv, fake_clock() as clock:
        cfg = _cfg(tmp_path, srv.server_address[1])
        first = nt.notify(cfg, _event(), db=db)
        clock.advance(13)
        again = nt.notify(cfg, _event(), db=db)
    assert (first.status, again.status) == (nt.SENT, nt.SENT)
    assert len(srv.messages) == 2
    assert _parsed(srv.messages[1]["data"])["In-Reply-To"] == first.message_id


def test_21_zero_cooldown_never_suppresses_and_a_per_alert_zero_overrides_the_global(tmp_path, db):
    with smtp_server() as srv, fake_clock():
        cfg = _cfg(tmp_path, srv.server_address[1], alerts={"repeat_cooldown_hours": 0})
        a = nt.notify(cfg, _event(), db=db)
        b = nt.notify(cfg, _event(), db=db)
    assert (a.status, b.status) == (nt.SENT, nt.SENT)

    with smtp_server() as srv2, fake_clock():
        cfg2 = _cfg(tmp_path, srv2.server_address[1])          # global window 12 h
        c = nt.notify(cfg2, _event(key="stale:lan", cooldown_hours=0.0), db=db)
        d = nt.notify(cfg2, _event(key="stale:lan", cooldown_hours=0.0), db=db)
    assert (c.status, d.status) == (nt.SENT, nt.SENT)


def test_22_fallback_notice_is_throttled_by_its_own_clock_and_expires(tmp_path):
    """Broken DB -> short notice; a repeat inside the window is suppressed, after it is sent again."""
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        mono = {"t": 1000.0}
        with mock.patch("core.manifest.ManifestDB.from_config", side_effect=RuntimeError("db locked")), \
                mock.patch.object(nt.time, "monotonic", side_effect=lambda: mono["t"]):
            first = nt.notify(cfg, _event())
            mono["t"] += 11 * 3600
            second = nt.notify(cfg, _event())
            mono["t"] += 2 * 3600
            third = nt.notify(cfg, _event())
    assert (first.status, second.status, third.status) == (nt.SENT, nt.SUPPRESSED, nt.SENT)
    assert len(srv.messages) == 2


# ── nothing raises into the backup flow; every outcome is recorded ─────────

@pytest.mark.parametrize("breakage", ["transport_raises", "config_is_none"])
def test_23_a_broken_alert_path_never_raises_into_the_backup(tmp_path, db, breakage):
    if breakage == "transport_raises":
        cfg = _cfg(tmp_path, 25, retry_attempts=2)
        with mock.patch.object(nt, "_send_once", side_effect=RuntimeError("socket exploded")):
            result = nt.notify(cfg, _event(), db=db)
        assert result.status == nt.FAILED and result.attempts == 2
    else:
        result = nt.notify(None, _event(), db=db)        # e.g. config failed to load in a watchdog loop
        assert result.status == nt.FAILED
    assert _rows(db)[-1]["status"] == nt.FAILED


def test_24_a_database_that_fails_to_record_does_not_hide_the_result(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        with mock.patch.object(ManifestDB, "record_notification",
                               side_effect=sqlite3.OperationalError("database is locked")):
            result = nt.notify(cfg, _event(), db=db)
    assert result.status == nt.SENT and len(srv.messages) == 1


def test_25_every_outcome_gets_its_own_row_in_order(tmp_path, db):
    with smtp_server(fail_with="550 no") as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        nt.notify(cfg, _event(), db=db)                                   # 550 -> FAILED
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        with fake_clock():
            nt.notify(cfg, _event(key="k:ok"), db=db)                    # SENT
            nt.notify(cfg, _event(key="k:ok"), db=db)                    # SUPPRESSED
        off = _cfg(tmp_path, srv.server_address[1], send_on_failure=False)
        nt.notify(off, _event(key="k:off"), db=db)                       # DISABLED
        nt.notify(_cfg(tmp_path, srv.server_address[1], recipients=[]), _event(key="k:none"), db=db)
    assert [r["status"] for r in _rows(db)] == [
        nt.FAILED, nt.SENT, nt.SUPPRESSED, nt.DISABLED, nt.FAILED]
