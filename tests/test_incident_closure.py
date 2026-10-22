"""Incidents close by what actually happened, not by whether a "resolved" mail was sent
(plan docs/PLAN_ALERT_CONTROLS_2026-10-22.md, step B).

Reminder limits count the mails of ONE incident; if an incident is never closed, a new problem with the same
key a month later would count as the old one and be silenced. Closing signals: a later successful run (legs),
a later VERIFIED audit, a healthy watchdog mark, or a long quiet gap (modes with no resolution signal)."""
from __future__ import annotations

import pendulum
import pytest

from core import notify as nt
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import AlertsConfig

NOW = pendulum.datetime(2026, 10, 22, 12, 0, tz=IST)


@pytest.fixture(autouse=True)
def _frozen():
    with pendulum.travel_to(NOW, freeze=True):
        yield


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


class Cfg:
    alerts = AlertsConfig()


def _ago(hours):
    return NOW.subtract(hours=hours).isoformat()


def _row(db, key, mode, status="SENT", hours_ago=10, kind="FAILURE", code="AAM-LAN-01", thread="<t1>"):
    db.record_notification({"event_key": key, "kind": kind, "mode": mode, "subject": "s", "status": status,
                            "created_at": _ago(hours_ago), "fault_code": code, "message_id": thread,
                            "thread_id": thread})


def _event(key, mode, kind="FAILURE", code="AAM-LAN-01"):
    return nt.AlertEvent(key=key, kind=kind, mode=mode, title="t", message="m", fault_code=code)


def _same(db, event):
    return nt._incident(event, db, Cfg)[0]


def _run(db, mode, status, hours_ago, run_id):
    t = _ago(hours_ago)
    db.insert_run({"run_id": run_id, "mode": mode, "started_at": t, "ended_at": t, "status": status})


# ── legs: a later success closes ─────────────────────────────────────────────

def test_an_open_leg_incident_stays_open_without_a_later_success(db):
    _row(db, "backup:lan:LAN_FAILED", "lan", hours_ago=10)
    _run(db, "lan", "LAN_FAILED", 5, "f1")
    assert _same(db, _event("backup:lan:LAN_FAILED", "lan")) is not None


def test_a_later_successful_run_closes_it_even_though_no_resolved_mail_was_sent(db):
    _row(db, "backup:lan:LAN_FAILED", "lan", hours_ago=10)
    _run(db, "lan", "LAN_COMPLETE", 5, "ok1")
    assert _same(db, _event("backup:lan:LAN_FAILED", "lan")) is None


def test_a_success_from_before_the_mail_does_not_close_it(db):
    _run(db, "lan", "LAN_COMPLETE", 30, "old")
    _row(db, "backup:lan:LAN_FAILED", "lan", hours_ago=10)
    assert _same(db, _event("backup:lan:LAN_FAILED", "lan")) is not None


def test_the_other_legs_success_does_not_close_it(db):
    _row(db, "backup:lan:LAN_FAILED", "lan", hours_ago=10)
    _run(db, "cloud", "CLOUD_COMPLETE", 5, "c1")
    assert _same(db, _event("backup:lan:LAN_FAILED", "lan")) is not None


def test_a_dead_scheduler_followed_by_a_success_closes_the_overdue_incident(db):
    _row(db, "stale:lan", "lan", kind="ACTION_REQUIRED", code="AAM-SYS-03", hours_ago=40)
    _run(db, "lan", "LAN_COMPLETE", 2, "back")
    assert _same(db, _event("stale:lan", "lan", "ACTION_REQUIRED", "AAM-SYS-03")) is None


# ── audit ────────────────────────────────────────────────────────────────────

def test_a_later_verified_audit_closes_an_audit_incident(db):
    _row(db, "audit:all:lan=VERIFICATION_FAILED", "audit", kind="ACTION_REQUIRED", code="AAM-AUD-01",
         hours_ago=100)
    key = "audit:all:lan=VERIFICATION_FAILED"
    assert _same(db, _event(key, "audit", "ACTION_REQUIRED", "AAM-AUD-01")) is not None
    db.record_audit({"audit_id": "a1", "mode": "lan", "started_at": _ago(20), "status": "VERIFIED",
                     "created_at": _ago(20)})
    assert _same(db, _event(key, "audit", "ACTION_REQUIRED", "AAM-AUD-01")) is None


def test_a_failed_later_audit_does_not_close_it(db):
    key = "audit:all:lan=VERIFICATION_FAILED"
    _row(db, key, "audit", kind="ACTION_REQUIRED", code="AAM-AUD-01", hours_ago=100)
    db.record_audit({"audit_id": "a2", "mode": "lan", "started_at": _ago(20),
                     "status": "VERIFICATION_FAILED", "created_at": _ago(20)})
    assert _same(db, _event(key, "audit", "ACTION_REQUIRED", "AAM-AUD-01")) is not None


# ── watchdog ─────────────────────────────────────────────────────────────────

def test_a_healthy_watchdog_mark_after_the_mail_closes_a_watchdog_incident(db):
    _row(db, "watchdog:agent_down", "watchdog", kind="ACTION_REQUIRED", code="AAM-SVC-01", hours_ago=10)
    ev = _event("watchdog:agent_down", "watchdog", "ACTION_REQUIRED", "AAM-SVC-01")
    assert _same(db, ev) is not None
    db.set_watchdog_healthy_at(_ago(2))
    assert _same(db, ev) is None


def test_a_healthy_mark_from_before_the_mail_does_not_close_it(db):
    db.set_watchdog_healthy_at(_ago(20))
    _row(db, "watchdog:agent_down", "watchdog", kind="ACTION_REQUIRED", code="AAM-SVC-01", hours_ago=10)
    assert _same(db, _event("watchdog:agent_down", "watchdog", "ACTION_REQUIRED", "AAM-SVC-01")) is not None


# ── modes with no resolution signal: a long quiet gap ────────────────────────

def test_a_rollover_problem_still_recurring_stays_open_even_when_only_suppressed_rows_appear(db):
    key = "rollover:clock_behind"
    _row(db, key, "rollover", kind="ACTION_REQUIRED", code="AAM-ROL-03", hours_ago=24 * 20)
    _row(db, key, "rollover", status="SUPPRESSED", kind="ACTION_REQUIRED", code="AAM-ROL-03", hours_ago=24)
    assert _same(db, _event(key, "rollover", "ACTION_REQUIRED", "AAM-ROL-03")) is not None


def test_a_rollover_problem_quiet_for_longer_than_the_quiet_days_is_a_new_incident(db):
    key = "rollover:clock_behind"
    days = AlertsConfig().incident_quiet_days
    _row(db, key, "rollover", kind="ACTION_REQUIRED", code="AAM-ROL-03", hours_ago=24 * (days + 1))
    assert _same(db, _event(key, "rollover", "ACTION_REQUIRED", "AAM-ROL-03")) is None


def test_quiet_days_is_a_bounded_config_key():
    f = AlertsConfig.model_fields["incident_quiet_days"]
    assert f.default == 7
    with pytest.raises(Exception):
        AlertsConfig(incident_quiet_days=0)
    with pytest.raises(Exception):
        AlertsConfig(incident_quiet_days=91)


# ── what did not change ──────────────────────────────────────────────────────

def test_a_sent_resolved_mail_still_closes_and_a_changed_fault_code_still_starts_a_new_incident(db):
    _row(db, "backup:lan:LAN_FAILED", "lan", hours_ago=10)
    assert _same(db, _event("backup:lan:LAN_FAILED", "lan", code="AAM-LAN-02")) is None      # new code
    db.record_notification({"event_key": "recovered:lan", "kind": "RECOVERED", "mode": "lan", "subject": "s",
                            "status": "SENT", "created_at": _ago(5)})
    assert _same(db, _event("backup:lan:LAN_FAILED", "lan")) is None                          # RESOLVED sent


# ── the watchdog writes the healthy mark ─────────────────────────────────────

def test_the_watchdog_records_a_healthy_mark_at_most_once_per_window(monkeypatch):
    import watchdog as wd
    from core.manifest import ManifestDB
    from models.config import CONFIG_PATH, WatchdogConfig, load_config

    assert WatchdogConfig().healthy_mark_seconds == 300
    monkeypatch.setattr(wd, "_healthy_marked_at", 0.0)
    stored = []
    real = ManifestDB.set_watchdog_healthy_at
    monkeypatch.setattr(ManifestDB, "set_watchdog_healthy_at",
                        lambda self, iso: (stored.append(iso), real(self, iso))[1])
    wd._mark_healthy()
    wd._mark_healthy()                       # inside the window: no second write
    assert len(stored) == 1
    db = ManifestDB.from_config(load_config(CONFIG_PATH))
    try:
        assert db.watchdog_healthy_at() == stored[0]
    finally:
        db.close()


def test_a_database_error_never_stops_the_watchdog_loop(monkeypatch):
    import watchdog as wd
    from core.manifest import ManifestDB

    monkeypatch.setattr(wd, "_healthy_marked_at", 0.0)
    monkeypatch.setattr(ManifestDB, "from_config",
                        classmethod(lambda cls, c: (_ for _ in ()).throw(OSError("locked"))))
    wd._mark_healthy()                       # must not raise
