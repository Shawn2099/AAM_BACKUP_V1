"""Two recipient lists - recording and warnings (plan A2, schema v5).

* notifications rows carry the audience and the addresses actually used;
* a delivery failure is cleared only by a later SENT mail of the SAME audience (a delivered report must
  not hide a rejected alert address) - `both` clears either;
* the dashboard says so when nobody would receive any email (red) or when the alerts list is empty
  and alerts go to the reports list (amber). An empty reports list is a normal setup: no banner item."""
from __future__ import annotations

import json
import sqlite3

import pytest

from core import notify as nt
from core.attention import AMBER, RED, compute_attention
from core.manifest import SCHEMA_VERSION, ManifestDB
from core.time_utils import now_iso
from models.config import AppConfig
from test_support import ui_login
from test_support.smtp_server import smtp_server

ALERTS = ["ops@firm.example"]
REPORTS = ["boss@firm.example"]


def _cfg(tmp_path, port=25, **notif) -> AppConfig:
    n = {"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@firm.example",
         "recipients": ALERTS, "report_recipients": REPORTS, "smtp_security": "none",
         "smtp_auth": False, "retry_attempts": 1, "retry_base_seconds": 0.01}
    n.update(notif)
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path), "lan_destination": "\\\\nas\\share\\FY26-27",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications=n)


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _row(db, status, audience, *, n=1, key="k"):
    for _ in range(n):
        db.record_notification({"event_key": key, "kind": "FAILURE", "mode": "cloud", "subject": "s",
                                "status": status, "created_at": now_iso(), "audience": audience})


# ── schema v5 ────────────────────────────────────────────────────────────────

def test_schema_is_v5_and_a_v4_database_is_upgraded_in_place(tmp_path):
    assert SCHEMA_VERSION == 5
    path = str(tmp_path / "old.db")
    first = ManifestDB(path)
    first.record_notification({"event_key": "old", "kind": "FAILURE", "mode": "lan", "subject": "s",
                               "status": "SENT", "created_at": now_iso()})
    first.close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE notifications DROP COLUMN audience")
    raw.execute("ALTER TABLE notifications DROP COLUMN recipients")
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()

    upgraded = ManifestDB(path)
    try:
        cols = {r[1] for r in upgraded._get_conn().execute("PRAGMA table_info(notifications)")}
        version = upgraded._get_conn().execute("PRAGMA user_version").fetchone()[0]
        old = upgraded.last_notification("old")
    finally:
        upgraded.close()
    assert {"audience", "recipients"} <= cols and version == 5
    assert old["status"] == "SENT" and old["audience"] is None      # old rows keep working


# ── what notify() records ────────────────────────────────────────────────────

def test_notify_records_the_audience_and_the_addresses_used(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], recipients=[])
        ev = nt.AlertEvent(key="k", kind="FAILURE", mode="cloud", title="t", message="m")
        result = nt.notify(cfg, ev, db=db)
    assert result.status == nt.SENT and result.recipients == REPORTS
    row = db.last_notification("k")
    assert row["audience"] == "alerts"                      # what it was FOR
    assert json.loads(row["recipients"]) == REPORTS         # where it really went (fallback)


def test_a_failed_delivery_still_records_audience_and_recipients(tmp_path, db):
    cfg = _cfg(tmp_path, 1)                                 # nothing listens
    ev = nt.AlertEvent(key="k2", kind="INFO", mode="test", title="t", message="m", audience="both")
    nt.notify(cfg, ev, db=db)
    row = db.last_notification("k2")
    assert row["status"] == nt.FAILED and row["audience"] == "both"
    assert json.loads(row["recipients"]) == ALERTS + REPORTS


# ── failures are cleared per audience ────────────────────────────────────────

def _failed_ids(db):
    return [r["audience"] for r in db.notification_failures_since(7)]


def test_a_sent_report_does_not_clear_an_alert_delivery_failure(db):
    _row(db, "FAILED", "alerts")
    _row(db, "SENT", "reports")
    assert _failed_ids(db) == ["alerts"]


def test_a_later_sent_alert_clears_an_alert_failure(db):
    _row(db, "FAILED", "alerts")
    _row(db, "SENT", "alerts")
    assert _failed_ids(db) == []


def test_a_sent_both_mail_clears_failures_of_either_audience(db):
    _row(db, "FAILED", "alerts")
    _row(db, "FAILED", "reports")
    _row(db, "SENT", "both")
    assert _failed_ids(db) == []


def test_old_rows_without_an_audience_count_as_alerts(db):
    _row(db, "FAILED", None)
    _row(db, "SENT", "reports")
    assert _failed_ids(db) == [None]
    _row(db, "SENT", "alerts")
    assert _failed_ids(db) == []


def test_an_older_sent_mail_clears_nothing(db):
    _row(db, "SENT", "alerts")
    _row(db, "FAILED", "alerts")
    assert _failed_ids(db) == ["alerts"]


# ── dashboard attention ──────────────────────────────────────────────────────

def _codes(db, cfg, level=None):
    return {i["code"] for i in compute_attention(db, cfg) if level is None or i["level"] == level}


def test_nobody_would_receive_any_email_is_red(tmp_path, db):
    cfg = _cfg(tmp_path, recipients=[], report_recipients=[])
    assert "recipients_none" in _codes(db, cfg, RED)


def test_an_empty_alerts_list_is_amber_and_says_where_alerts_go(tmp_path, db):
    cfg = _cfg(tmp_path, recipients=[])
    items = [i for i in compute_attention(db, cfg) if i["code"] == "alerts_list_empty"]
    assert items and items[0]["level"] == AMBER
    assert "reports list" in items[0]["message"]


def test_an_empty_reports_list_is_a_normal_setup_and_shows_nothing(tmp_path, db):
    cfg = _cfg(tmp_path, report_recipients=[])
    assert not ({"recipients_none", "alerts_list_empty"} & _codes(db, cfg))


def test_two_lists_set_shows_nothing(tmp_path, db):
    assert not ({"recipients_none", "alerts_list_empty"} & _codes(db, _cfg(tmp_path)))
