"""Incidents (docs/EMAIL_CONTENT_SPEC.md §6, audit 2026-10-18 item 6).

A repeat is suppressed only while the problem is unchanged: a RESOLVED email
or a different fault code on the same leg starts a new incident, whose first
email always goes out. Reminders and the RESOLVED email reply to the
incident's first email (In-Reply-To / References), so mail programs group them."""

import email as em
import sqlite3

import pytest

from core import notify as nt
from core.manifest import SCHEMA_VERSION, ManifestDB
from models.config import AppConfig
from test_support.smtp_server import smtp_server

UNC = "\\\\nas\\backup\\FY26-27"


def _cfg(tmp_path, port, cooldown=12) -> AppConfig:
    return AppConfig(
        paths={"source_drive": str(tmp_path), "lan_destination": UNC,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"auth_enabled": False, "public_url": "http://backup-server:8080"},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@firm.example",
                       "recipients": ["o@firm.example"], "smtp_security": "none",
                       "smtp_auth": False, "retry_attempts": 1, "retry_base_seconds": 0.01},
        alerts={"repeat_cooldown_hours": cooldown},
    )


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _fail(code="AAM-NAS-01"):
    return nt.AlertEvent(key="backup:lan:LAN_FAILED", kind="FAILURE", mode="lan", title="t",
                         message="m", status="LAN_FAILED", fault_code=code, run_id="r")


def _resolved():
    return nt.AlertEvent(key="recovered:lan", kind="RECOVERED", mode="lan", title="t",
                         message="m", status="RECOVERED", resolved_code="AAM-NAS-01")


def _msgs(srv):
    return [em.message_from_string(m["data"]) for m in srv.messages]


def test_unchanged_problem_is_suppressed_a_new_cause_is_not(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        assert nt.notify(cfg, _fail(), db=db).status == nt.SENT
        assert nt.notify(cfg, _fail(), db=db).status == nt.SUPPRESSED
        assert nt.notify(cfg, _fail("AAM-NAS-03"), db=db).status == nt.SENT   # new cause
    assert len(srv.messages) == 2


def test_failure_after_a_recovery_is_never_suppressed(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        assert nt.notify(cfg, _fail(), db=db).status == nt.SENT
        assert nt.notify(cfg, _resolved(), db=db).status == nt.SENT
        # failed again within the cooldown: the reader's last email said "resolved"
        assert nt.notify(cfg, _fail(), db=db).status == nt.SENT
        assert nt.notify(cfg, _resolved(), db=db).status == nt.SENT
    assert len(srv.messages) == 4


def test_reminders_and_resolved_reply_to_the_first_email(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], cooldown=0)     # 0 = every repeat is sent
        nt.notify(cfg, _fail(), db=db)
        nt.notify(cfg, _fail(), db=db)
        nt.notify(cfg, _resolved(), db=db)
        nt.notify(cfg, _fail("AAM-NAS-03"), db=db)
    first, reminder, resolved, new = _msgs(srv)
    root = first["Message-ID"]
    assert first["In-Reply-To"] is None
    assert reminder["In-Reply-To"] == root and root in reminder["References"]
    assert reminder["Subject"] == first["Subject"]
    assert resolved["In-Reply-To"] == root and root in resolved["References"]
    assert new["In-Reply-To"] is None                     # a new cause = a new incident
    body = "\n".join((p.get_payload(decode=True) or b"").decode("utf-8", "replace")
                     for p in reminder.walk() if p.get_content_type() == "text/plain")
    assert "Reminder" in body and "first reported" in body


def test_delivery_record_keeps_code_and_ids(tmp_path, db):
    with smtp_server() as srv:
        nt.notify(_cfg(tmp_path, srv.server_address[1]), _fail(), db=db)
    row = db.last_notification("backup:lan:LAN_FAILED")
    msg = _msgs(srv)[0]
    assert row["fault_code"] == "AAM-NAS-01"
    assert row["message_id"] == msg["Message-ID"] == row["thread_id"]


def test_v2_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL,
            kind TEXT NOT NULL, mode TEXT, subject TEXT, status TEXT NOT NULL, error TEXT,
            attempts INTEGER DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO notifications (event_key, kind, status, created_at)
            VALUES ('k', 'FAILURE', 'SENT', '2026-10-15T21:00:00+05:30');
        PRAGMA user_version = 2;
    """)
    conn.close()
    db = ManifestDB(path)
    try:
        row = db.last_notification("k")
        assert row["status"] == "SENT" and row["fault_code"] is None and row["message_id"] is None
        assert db._get_conn().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 3
    finally:
        db.close()
