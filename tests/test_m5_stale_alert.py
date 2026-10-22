"""M5 - a leg with no successful backup for too long is EMAILED (plan §2 rule 11).

Finding (M5 review, 2026-10-16): only the dashboard banner showed a stale leg.
When scheduled runs never start (Prefect scheduler not firing, a flow run
crashing before our code - e.g. a bad deploy failing at import - or the agent
up but its scheduler stuck), no run row and no alert is ever written, so
nothing reaches the only push channel (email). Rule 11 ("stale leg -> one
email") was approved but not built.

Fix: the watchdog - an independent service - evaluates the same
core.attention items as the banner every watchdog.stale_check_interval_seconds
and emails each stale leg (notify: recorded, one per cooldown).
"""

import email

import pendulum
import pytest
from pydantic import ValidationError

import watchdog as wd
from core import notify as nt
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import AppConfig, WatchdogConfig
from test_support.smtp_server import smtp_server

UNC = "\\\\nas\\backup"


@pytest.fixture(autouse=True)
def _frozen_mid_afternoon():
    """The overdue rule follows the schedule (default cloud 18:00 + 6 h budget, LAN 01:00 + 4 h, 2 h
    grace), so "40 h ago" must mean the same thing whatever the time of day the suite runs."""
    with pendulum.travel_to(pendulum.datetime(2026, 10, 22, 15, 0, tz="Asia/Kolkata"), freeze=True):
        yield


def _cfg(tmp_path, port=25, cloud=False):
    return AppConfig(
        paths={"source_drive": str(tmp_path), "lan_destination": UNC,
               "runtime_dir": str(tmp_path / "rt"),
               "gcs_key_path": str(tmp_path / "key.json")},
        wol={"enabled": False},
        cloud={"enabled": True, "bucket": "aam-test-bucket", "project_number": "0"}
        if cloud else {"enabled": False},
        dashboard={"public_url": "http://backup-server:8080"},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@test.local",
                       "recipients": ["ops@test.local"], "smtp_security": "none",
                       "smtp_auth": False, "retry_attempts": 1, "retry_base_seconds": 0.01,
                       "smtp_timeout_seconds": 5, "send_on_failure": True},
        alerts={"repeat_cooldown_hours": 12},
    )


def _success(db, leg, hours_ago):
    t = pendulum.now(IST).subtract(hours=hours_ago).isoformat()
    db.insert_run({"run_id": f"{leg}-{hours_ago}", "mode": leg, "started_at": t, "ended_at": t,
                   "status": f"{leg.upper()}_COMPLETE"})


def _body(raw: str) -> str:
    msg = email.message_from_string(raw)
    return "\n".join((p.get_payload(decode=True) or b"").decode("utf-8", "replace")
                     for p in msg.walk() if not p.is_multipart())


def test_stale_leg_is_emailed_for_real_once_per_cooldown(tmp_path):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1])
        db = ManifestDB.from_config(cfg)
        try:
            _success(db, "lan", 40)
        finally:
            db.close()
        assert wd._check_stale_legs(cfg) == ["lan"]
        assert wd._check_stale_legs(cfg) == ["lan"]      # still stale: recorded, not re-sent
    assert len(srv.messages) == 1
    body = _body(srv.messages[0]["data"])
    assert "40 h ago" in body and "http://backup-server:8080" in body
    db = ManifestDB.from_config(cfg)
    try:
        row = db.last_notification("stale:lan")
    finally:
        db.close()
    assert row["status"] == nt.SUPPRESSED and row["kind"] == "ACTION_REQUIRED"
    assert row["mode"] == "lan"


def test_fresh_and_disabled_legs_send_nothing(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(nt, "notify", lambda cfg, ev, db=None: sent.append(ev))
    cfg = _cfg(tmp_path)                       # cloud disabled
    db = ManifestDB.from_config(cfg)
    try:
        _success(db, "lan", 2)
    finally:
        db.close()
    assert wd._check_stale_legs(cfg) == []
    assert sent == []


def test_never_succeeded_leg_is_stale_too(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(nt, "notify", lambda cfg, ev, db=None: sent.append(ev))
    cfg = _cfg(tmp_path, cloud=True)
    db = ManifestDB.from_config(cfg)
    try:    # the installation is three days old and no backup ever finished
        for leg in ("cloud", "lan"):
            t = pendulum.now(IST).subtract(days=3).isoformat()
            db.insert_run({"run_id": f"old-{leg}", "mode": leg, "started_at": t, "ended_at": t,
                           "status": f"{leg.upper()}_FAILED"})
    finally:
        db.close()
    assert wd._check_stale_legs(cfg) == ["cloud", "lan"]
    assert [e.key for e in sent] == ["stale:cloud", "stale:lan"]
    assert all(e.kind == "ACTION_REQUIRED" for e in sent)


def test_check_failure_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("config unreadable")
    monkeypatch.setattr("models.config.load_config", boom)
    assert wd._check_stale_legs() == []


def test_interval_is_config():
    f = WatchdogConfig.model_fields["stale_check_interval_seconds"]
    assert f.default == 1800
    with pytest.raises(ValidationError):
        WatchdogConfig(stale_check_interval_seconds=10)
    wd._apply_config(WatchdogConfig(stale_check_interval_seconds=900))
    try:
        assert wd.STALE_CHECK_INTERVAL == 900
    finally:
        wd._apply_config(WatchdogConfig())


class _StopLoop(BaseException):
    pass


def test_main_checks_at_start_and_every_interval(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "_resolve_paths", lambda: None)
    monkeypatch.setattr(wd, "_configure_logging", lambda: None)
    monkeypatch.setattr(wd, "BACKUP_LOCK_PATH", tmp_path / "backup.lock")
    monkeypatch.setattr(wd, "_check_health", lambda: True)
    monkeypatch.setattr(wd, "_service_state", lambda s: "RUNNING")
    monkeypatch.setattr(wd, "CHECK_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(wd, "STALE_CHECK_INTERVAL", 1800)
    t = [0.0]
    monkeypatch.setattr(wd.time, "monotonic", lambda: t[0])
    checks = []
    monkeypatch.setattr(wd, "_check_stale_legs", lambda cfg=None: checks.append(t[0]) or [])

    def sleep(s):
        t[0] += s
        if t[0] > 3700:
            raise _StopLoop()
    monkeypatch.setattr(wd.time, "sleep", sleep)
    with pytest.raises(_StopLoop):
        wd.main()
    assert checks == [0.0, 1800.0, 3600.0]


def test_main_checks_while_the_api_is_down_too(monkeypatch, tmp_path):
    """The stale email matters most when the scheduler is broken."""
    monkeypatch.setattr(wd, "_resolve_paths", lambda: None)
    monkeypatch.setattr(wd, "_configure_logging", lambda: None)
    monkeypatch.setattr(wd, "BACKUP_LOCK_PATH", tmp_path / "backup.lock")
    monkeypatch.setattr(wd, "_check_health", lambda: False)
    monkeypatch.setattr(wd, "FAILURE_THRESHOLD", 1000)
    monkeypatch.setattr(wd, "CHECK_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(wd, "STALE_CHECK_INTERVAL", 600)
    t = [0.0]
    monkeypatch.setattr(wd.time, "monotonic", lambda: t[0])
    checks = []
    monkeypatch.setattr(wd, "_check_stale_legs", lambda cfg=None: checks.append(t[0]) or [])

    def sleep(s):
        t[0] += s
        if t[0] > 1250:
            raise _StopLoop()
    monkeypatch.setattr(wd.time, "sleep", sleep)
    with pytest.raises(_StopLoop):
        wd.main()
    assert checks == [0.0, 600.0, 1200.0]
