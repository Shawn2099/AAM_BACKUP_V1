"""opus-decision L2: a lost or renamed manifest.db that was silently replaced is reported loudly (not blocked).

Detection: the DB's `created_at` meta is newer (by day, IST) than the oldest run-log day folder under
`config.run_logs_dir` - those folders are written only by runs, which also write the DB. A genuine fresh
install has no day folder, a DB from before the marker has no `created_at`: no alarm. One ACTION_REQUIRED mail
(`db:recreated`) and a red attention item until an admin acknowledges it (meta `db_recreated_ack`)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pendulum
import pytest
from fastapi.testclient import TestClient

import ui
from core import db_identity, notify as nt
from core.attention import RED, compute_attention
from core.manifest import ManifestDB
from core.time_utils import IST

NOW = pendulum.datetime(2026, 11, 23, 12, 0, tz=IST)


@pytest.fixture(autouse=True)
def _frozen():
    with pendulum.travel_to(NOW, freeze=True):
        yield


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(run_logs_dir=tmp_path / "rt" / "run_logs", firm_name="AAM")


@pytest.fixture
def mails(monkeypatch):
    sent = []

    def fake_notify(config, event, db=None):
        sent.append(event)
        db.record_notification({"event_key": event.key, "kind": event.kind, "mode": event.mode,
                                "subject": event.title, "status": nt.SENT, "created_at": NOW.isoformat(),
                                "audience": "alerts"})
        return nt.DeliveryResult(nt.SENT)
    monkeypatch.setattr(nt, "notify", fake_notify)
    return sent


def _day(cfg, day: str):
    (Path(cfg.run_logs_dir) / day / f"{day}_0200_lan_abc").mkdir(parents=True)


def _created(db, iso: str | None):
    if iso is None:
        db._get_conn().execute("DELETE FROM db_meta WHERE key = 'created_at'")
        db._get_conn().commit()
    else:
        db.set_meta_created_at(iso)


def test_old_run_logs_and_a_fresh_db_alarm_once_with_one_mail(db, cfg, mails):
    _day(cfg, "2026-10-01")
    _created(db, NOW.isoformat())
    assert db_identity.check_at_startup(cfg, db) is True
    assert [e.key for e in mails] == ["db:recreated"] and mails[0].kind == "ACTION_REQUIRED"
    assert "2026-10-01" in mails[0].message and "restore" in mails[0].message
    assert db_identity.pending(db) is not None
    # a second boot: no second mail
    assert db_identity.check_at_startup(cfg, db) is False
    assert len(mails) == 1 and db_identity.pending(db) is not None


def test_a_genuine_fresh_install_is_quiet(db, cfg, mails):
    _created(db, NOW.isoformat())            # no run-log folders at all (the folder does not even exist)
    assert db_identity.check_at_startup(cfg, db) is False
    assert mails == [] and db_identity.pending(db) is None


def test_a_database_without_the_marker_is_treated_as_old(db, cfg, mails):
    _day(cfg, "2026-10-01")
    _created(db, None)
    assert db_identity.check_at_startup(cfg, db) is False
    assert mails == [] and db_identity.pending(db) is None


def test_a_database_older_than_the_run_logs_is_quiet(db, cfg, mails):
    _day(cfg, "2026-11-20")
    _created(db, NOW.subtract(days=60).isoformat())
    assert db_identity.check_at_startup(cfg, db) is False and mails == []


def test_first_run_folder_on_the_day_the_database_was_created_is_quiet(db, cfg, mails):
    _day(cfg, NOW.format("YYYY-MM-DD"))
    _created(db, NOW.isoformat())
    assert db_identity.check_at_startup(cfg, db) is False and mails == []


def test_other_folders_in_run_logs_are_not_day_folders(db, cfg, mails):
    (Path(cfg.run_logs_dir) / "notes").mkdir(parents=True)
    _created(db, NOW.isoformat())
    assert db_identity.check_at_startup(cfg, db) is False


def test_acknowledging_clears_the_item_and_sends_no_new_mail(db, cfg, mails):
    _day(cfg, "2026-10-01")
    _created(db, NOW.isoformat())
    db_identity.check_at_startup(cfg, db)
    assert db_identity.acknowledge(db, "admin") is True
    assert db_identity.pending(db) is None
    db_identity.check_at_startup(cfg, db)
    assert len(mails) == 1
    assert db_identity.acknowledge(db, "admin") is False       # nothing left to acknowledge


def test_the_red_attention_item_shows_until_acknowledged(db, cfg, mails):
    from test_m3_dashboard_truth import _cfg
    _day(cfg, "2026-10-01")
    _created(db, NOW.isoformat())
    db_identity.check_at_startup(cfg, db)
    items = [i for i in compute_attention(db, _cfg()) if i["code"] == "db:recreated"]
    assert len(items) == 1 and items[0]["level"] == RED
    assert "history" in items[0]["message"].lower()
    db_identity.acknowledge(db, "admin")
    assert not [i for i in compute_attention(db, _cfg()) if i["code"] == "db:recreated"]


def test_the_dashboard_ack_endpoint(db, cfg, mails, monkeypatch):
    from test_m3_dashboard_truth import _cfg
    _day(cfg, "2026-10-01")
    _created(db, NOW.isoformat())
    db_identity.check_at_startup(cfg, db)
    monkeypatch.setattr(ui, "_cfg", lambda: _cfg())
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    client = TestClient(ui.app, raise_server_exceptions=False)
    r = client.post("/attention/db-recreated/ack")
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    assert db_identity.pending(db) is None
    assert client.post("/attention/db-recreated/ack").status_code == 409


def test_the_ack_endpoint_needs_a_login(db, monkeypatch):
    from test_m3_dashboard_truth import _cfg
    monkeypatch.setattr(ui, "_cfg", lambda: _cfg())
    monkeypatch.setattr(ui, "get_db", lambda: db)
    anon = TestClient(ui.app, raise_server_exceptions=False)
    anon.cookies.clear()
    anon.headers.pop("Authorization", None)
    # conftest signs the shared client in through a header; a bare client without it is refused
    r = anon.post("/attention/db-recreated/ack", headers={"Authorization": "Bearer wrong"})
    assert r.status_code in (401, 403)
