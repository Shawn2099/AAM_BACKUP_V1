"""A5 - alerts per FY (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, section 2.3 and task A5).

An alert of a next-FY pass carries `AlertEvent.fy`; its incident group is `<leg>:<FY>` (notifications.mode), so a
RECOVERED mail or a successful run of one FY never closes an incident of the other FY. Alerts without an FY keep
today's group (the leg name) exactly.
"""

import pendulum
import pytest

import watchdog
from core import alert_class
from core import faults
from core import notify as nt
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support.smtp_server import smtp_server

UNC = "\\\\nas\\backup\\FY26-27"


def _cfg(tmp_path, port, cooldown=12) -> AppConfig:
    return AppConfig(
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": UNC,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080"},
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


def _lan_fail(fy=None, code="AAM-NAS-01"):
    suffix = f"{fy}:" if fy else ""
    return nt.AlertEvent(key=f"backup:lan:{suffix}LAN_FAILED", kind="FAILURE", mode="lan", title="t",
                         message="m", status="LAN_FAILED", fault_code=code, run_id="r", fy=fy)


def _lan_recovered(fy=None):
    suffix = f"{fy}" if fy else ""
    key = f"recovered:lan:{suffix}" if fy else "recovered:lan"
    return nt.AlertEvent(key=key, kind="RECOVERED", mode="lan", title="t", message="m",
                         status="RECOVERED", resolved_code="AAM-NAS-01", fy=fy)


def _later_run(run_id, status):
    """A run row that started after any mail sent in this test (incident closing needs a later start)."""
    started = pendulum.now("Asia/Kolkata").add(hours=1).isoformat()
    return {"run_id": run_id, "mode": "lan" if status.startswith("LAN") else "cloud",
            "started_at": started, "status": status}


# --- the incident group ------------------------------------------------------------------------------

def test_alert_without_fy_keeps_the_leg_as_its_group():
    assert nt.AlertEvent(key="k", kind="FAILURE", mode="lan", title="t", message="m").fy is None
    assert nt.incident_group(_lan_fail()) == "lan"


def test_alert_of_a_next_fy_pass_groups_by_leg_and_fy():
    assert nt.incident_group(_lan_fail("FY27-28")) == "lan:FY27-28"


def test_notification_row_records_the_group(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        assert nt.notify(cfg, _lan_fail(), db=db).status == nt.SENT
        assert nt.notify(cfg, _lan_fail("FY27-28"), db=db).status == nt.SENT
    modes = [r["mode"] for r in db._get_conn().execute("SELECT mode FROM notifications ORDER BY id")]
    assert modes == ["lan", "lan:FY27-28"]


# --- RECOVERED / repeats never cross the FY boundary -------------------------------------------------

def test_recovered_of_the_next_fy_does_not_close_the_configured_fy_incident(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        assert nt.notify(cfg, _lan_fail(), db=db).status == nt.SENT
        assert nt.notify(cfg, _lan_recovered("FY27-28"), db=db).status == nt.SENT
        # the FY26-27 problem is still open: its repeat stays suppressed
        assert nt.notify(cfg, _lan_fail(), db=db).status == nt.SUPPRESSED


def test_recovered_of_the_configured_fy_does_not_close_the_next_fy_incident(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        assert nt.notify(cfg, _lan_fail("FY27-28"), db=db).status == nt.SENT
        assert nt.notify(cfg, _lan_recovered(), db=db).status == nt.SENT
        assert nt.notify(cfg, _lan_fail("FY27-28"), db=db).status == nt.SUPPRESSED


def test_recovered_of_the_next_fy_reply_threads_to_its_own_incident(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        assert nt.notify(cfg, _lan_fail(), db=db).status == nt.SENT
        fy_first = nt.notify(cfg, _lan_fail("FY27-28"), db=db)
        assert fy_first.status == nt.SENT
        nt.notify(cfg, _lan_recovered("FY27-28"), db=db)
        root = db.last_open_incident("lan")
        assert root is not None and root["event_key"] == "backup:lan:LAN_FAILED"


# --- a successful run of one FY only closes that FY's incident ---------------------------------------

def test_success_of_the_next_fy_does_not_close_the_configured_fy_incident(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        assert nt.notify(cfg, _lan_fail(), db=db).status == nt.SENT
        db.insert_run({**_later_run("x-lan-FY27-28", "LAN_COMPLETE"),
                       "extended_metrics": None})
        assert nt.notify(cfg, _lan_fail(), db=db).status == nt.SUPPRESSED


def test_success_of_the_next_fy_closes_its_own_incident(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        assert nt.notify(cfg, _lan_fail("FY27-28"), db=db).status == nt.SENT
        db.insert_run(_later_run("x-lan-FY27-28", "LAN_COMPLETE"))
        assert nt.notify(cfg, _lan_fail("FY27-28"), db=db).status == nt.SENT   # a new incident


# --- the watchdog reads the FY of a stale code -------------------------------------------------------

def test_watchdog_parses_the_fy_of_a_stale_code(tmp_path, db, monkeypatch):
    import core.attention as attention

    sent = []
    monkeypatch.setattr(attention, "compute_attention", lambda *a, **k: [
        {"code": "stale:cloud:FY27-28", "message": "overdue", "due_at": None},
        {"code": "stale:lan", "message": "overdue", "due_at": None}])
    monkeypatch.setattr(nt, "notify", lambda cfg, event, db=None: sent.append(event))
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        legs = watchdog._check_stale_legs(cfg)
    assert legs == ["cloud:FY27-28", "lan"]
    by_key = {e.key: e for e in sent}
    fy_event = by_key["stale:cloud:FY27-28"]
    assert (fy_event.mode, fy_event.fy) == ("cloud", "FY27-28")
    plain = by_key["stale:lan"]
    assert (plain.mode, plain.fy) == ("lan", None)


# --- classes and the fault catalogue -----------------------------------------------------------------

@pytest.mark.parametrize("kind,plain,with_fy", [
    ("FAILURE", "backup:cloud:CLOUD_FAILED", "backup:cloud:FY27-28:CLOUD_FAILED"),
    ("ACTION_REQUIRED", "blocked:cloud", "blocked:cloud:FY27-28"),
    ("ACTION_REQUIRED", "stale:cloud", "stale:cloud:FY27-28"),
    ("RECOVERED", "recovered:cloud", "recovered:cloud:FY27-28"),
    ("FAILURE", "missed:cloud", "missed:cloud:FY27-28"),
])
def test_per_fy_keys_have_the_class_of_their_plain_key(kind, plain, with_fy):
    assert alert_class.classify(kind, with_fy) == alert_class.classify(kind, plain)


def test_rollover_activation_key_is_known():
    assert alert_class.classify("ACTION_REQUIRED", "rollover:activation:FY27-28") == alert_class.ACTION


def test_fault_aam_rol_07_is_in_the_catalogue():
    fault = faults.CATALOG["AAM-ROL-07"]
    assert "clock" in fault.title.lower()
    assert fault.steps
