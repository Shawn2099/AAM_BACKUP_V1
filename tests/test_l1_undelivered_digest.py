"""opus-decision L1(b): ONE digest mail for alerts that could not be delivered, sent once mail works again.

Live-stack finding: while SMTP was down alerts were recorded FAILED and never made up for (a problem that
started and ended inside the outage, the 1 Jan activation INFO mail). The watchdog loop calls
`notify.send_undelivered_digest`: one mail, bounded by `alerts.undelivered_digest_max_age_hours` and spaced by
`alerts.undelivered_digest_retry_minutes`; never a per-alert resend."""
from __future__ import annotations

from unittest import mock

import pendulum
import pytest
from pydantic import ValidationError

from core import notify as nt
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import AlertsConfig, AppConfig
from test_support.smtp_server import smtp_server

T0 = pendulum.datetime(2026, 12, 30, 10, 0, tz=IST)


@pytest.fixture
def clock():
    state = {"now": T0}

    def now(tz=None):
        return state["now"]

    with mock.patch("pendulum.now", side_effect=now),             mock.patch.object(nt, "now_iso", side_effect=lambda: state["now"].isoformat()):
        yield state


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _cfg(tmp_path, port, **alerts) -> AppConfig:
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": "\\\\nas\\share\\FY26-27",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@x.example",
                       "recipients": ["o@x.example"], "smtp_security": "none", "smtp_auth": False,
                       "retry_attempts": 1, "retry_base_seconds": 0.01, "smtp_timeout_seconds": 1},
        alerts=alerts,
    )


def _failed(db, key, hours_ago, now, kind="FAILURE", subject=None, status="FAILED", audience="alerts"):
    db.record_notification({
        "event_key": key, "kind": kind, "mode": "lan", "subject": subject or f"subject of {key}",
        "status": status, "error": "smtp down", "attempts": 1,
        "created_at": now.subtract(hours=hours_ago).isoformat(), "audience": audience})


def _text(msg) -> str:
    """Subject + every text part of one captured mail, decoded."""
    import email
    import email.header
    m = email.message_from_string(msg["data"])
    subject = str(email.header.make_header(email.header.decode_header(m["Subject"])))
    parts = [p.get_payload(decode=True).decode("utf-8") for p in m.walk() if p.get_content_type() == "text/plain"]
    return subject + "\n" + "\n".join(parts)


def _digests(db):
    return [dict(r) for r in db._get_conn().execute(
        "select * from notifications where event_key = 'undelivered:digest' order by id")]


# ── keys ──────────────────────────────────────────────────────────────────────

def test_keys_defaults_and_bounds():
    a = AlertsConfig()
    assert a.undelivered_digest_max_age_hours == 72 and a.undelivered_digest_retry_minutes == 30
    AlertsConfig(undelivered_digest_max_age_hours=0)
    AlertsConfig(undelivered_digest_max_age_hours=336)
    AlertsConfig(undelivered_digest_retry_minutes=5)
    AlertsConfig(undelivered_digest_retry_minutes=1440)
    for bad in ({"undelivered_digest_max_age_hours": -1}, {"undelivered_digest_max_age_hours": 337},
                {"undelivered_digest_retry_minutes": 4}, {"undelivered_digest_retry_minutes": 1441}):
        with pytest.raises(ValidationError):
            AlertsConfig(**bad)


# ── the digest ────────────────────────────────────────────────────────────────

def test_three_lost_alerts_then_smtp_back_gives_exactly_one_digest_then_none(tmp_path, db, clock):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        _failed(db, "backup:lan:A", 5, T0, subject="LAN failed (A)")
        _failed(db, "backup:cloud:B", 4, T0, subject="Cloud failed (B)")
        _failed(db, "activation:info", 3, T0, kind="INFO", subject="FY27-28 activated (E2E-MAIL-1)")
        r = nt.send_undelivered_digest(cfg, db)
        assert r is not None and r.status == nt.SENT
        assert len(srv.messages) == 1
        data = _text(srv.messages[0])
        assert "LAN failed (A)" in data and "Cloud failed (B)" in data and "FY27-28 activated (E2E-MAIL-1)" in data
        assert "3 alerts could not be delivered" in data
        # second watchdog tick, even much later: nothing new, no mail
        clock["now"] = T0.add(hours=2)
        assert nt.send_undelivered_digest(cfg, db) is None
        assert len(srv.messages) == 1


def test_smtp_still_down_records_failed_digest_rows_spaced_by_the_retry_interval(tmp_path, db, clock):
    with smtp_server(fail_with="421 busy") as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], undelivered_digest_retry_minutes=30)
        _failed(db, "backup:lan:A", 2, T0)
        r1 = nt.send_undelivered_digest(cfg, db)
        assert r1.status == nt.FAILED
        clock["now"] = T0.add(minutes=10)
        assert nt.send_undelivered_digest(cfg, db) is None            # too soon: no attempt at all
        attempts = srv.mail_attempts
        clock["now"] = T0.add(minutes=31)
        r2 = nt.send_undelivered_digest(cfg, db)
        assert r2.status == nt.FAILED
        assert srv.mail_attempts == attempts + 1
        assert [d["status"] for d in _digests(db)] == [nt.FAILED, nt.FAILED]
        assert srv.messages == []
        # SMTP back: the same lost alert is still in the next digest (the watermark moves only on SENT)
        srv.fail_with = None
        clock["now"] = T0.add(minutes=70)
        r3 = nt.send_undelivered_digest(cfg, db)
        assert r3.status == nt.SENT and len(srv.messages) == 1


def test_rows_older_than_the_max_age_are_never_resent(tmp_path, db, clock):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], undelivered_digest_max_age_hours=72)
        _failed(db, "backup:lan:OLD", 80, T0, subject="too old")
        assert nt.send_undelivered_digest(cfg, db) is None
        _failed(db, "backup:lan:NEW", 70, T0, subject="young enough")
        r = nt.send_undelivered_digest(cfg, db)
        assert r.status == nt.SENT
        data = _text(srv.messages[0])
        assert "young enough" in data and "too old" not in data


def test_max_age_zero_switches_the_digest_off(tmp_path, db, clock):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], undelivered_digest_max_age_hours=0)
        _failed(db, "backup:lan:A", 1, T0)
        assert nt.send_undelivered_digest(cfg, db) is None
        assert srv.mail_attempts == 0 and _digests(db) == []


def test_an_alert_whose_key_was_later_sent_is_not_listed(tmp_path, db, clock):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        _failed(db, "backup:lan:A", 5, T0, subject="A lost then re-sent")
        _failed(db, "backup:lan:A", 4, T0, status="SENT")
        _failed(db, "backup:cloud:B", 3, T0, subject="B still lost")
        r = nt.send_undelivered_digest(cfg, db)
        assert r.status == nt.SENT
        data = _text(srv.messages[0])
        assert "B still lost" in data and "A lost then re-sent" not in data
        assert "1 alert could not be delivered" in data


def test_report_and_recovered_rows_are_not_part_of_the_digest(tmp_path, db, clock):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        _failed(db, "report:weekly", 5, T0, kind="REPORT", audience="reports")
        _failed(db, "recovered:lan", 5, T0, kind="RECOVERED")
        assert nt.send_undelivered_digest(cfg, db) is None
        assert srv.mail_attempts == 0


def test_end_to_end_outage_failure_row_then_digest(tmp_path, db):
    """SMTP down -> notify records FAILED -> SMTP up -> digest names it -> a second call sends nothing."""
    with smtp_server(fail_with="421 busy") as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        down = nt.notify(cfg, nt.AlertEvent(key="backup:lan:LAN_FAILED", kind="FAILURE", mode="lan",
                                            title="LAN backup failed", message="robocopy exit 16"), db=db)
        assert down.status == nt.FAILED
        srv.fail_with = None
        first = nt.send_undelivered_digest(cfg, db)
        assert first is not None and first.status == nt.SENT
        assert len(srv.messages) == 1
        assert nt.send_undelivered_digest(cfg, db) is None
        assert len(srv.messages) == 1


def test_the_watchdog_hook_calls_the_digest_with_config_and_db(tmp_path, monkeypatch):
    import watchdog
    from models import config as mc

    cfg = _cfg(tmp_path, 1)
    seen = []
    monkeypatch.setattr(mc, "load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(nt, "send_undelivered_digest", lambda c, d: seen.append((c, type(d).__name__)))
    watchdog._send_undelivered_digest()
    assert seen == [(cfg, "ManifestDB")]
