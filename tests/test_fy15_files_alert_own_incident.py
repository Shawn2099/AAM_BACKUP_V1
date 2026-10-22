"""FY-15 (plan docs/PLAN_FY_FIXES_2026-10-22.md, S2): the "files in the new year are not backed up" alert and its
RESOLVED mail have their own mail group (mode `rollover_files`), so the RESOLVED does not end an unrelated open
rollover incident (e.g. the archive warning), and it goes out the SAME day the switch is done."""

from unittest.mock import patch

import pytest

from core import fy_rollover as fr
from core import notify as nt
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support import ui_login
from test_support.smtp_server import smtp_server
from tests.test_a6_rollover_journal_flow import FROM, TO, _events
from tests.test_m2_rollover import _Rig

KEY_PROBLEM, KEY_OK = f"rollover:unprotected:{TO}", f"recovered:rollover:{TO}"


def _cfg(tmp_path, port=25, source=None, **alerts):
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": source or str(tmp_path / "src" / FROM), "lan_destination": f"\\\\nas\\share\\{FROM}",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@firm.example",
                       "recipients": ["ops@firm.example"], "smtp_security": "none", "smtp_auth": False,
                       "retry_attempts": 1, "retry_base_seconds": 0.01},
        alerts=alerts)


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _archive_warning():
    return nt.AlertEvent(key=f"rollover:{TO}:archive", kind="WARNING", mode="rollover",
                         title="Old year not moved to cheaper storage yet", message="m", fault_code="AAM-ROL-05")


def _files_alert(kind="ACTION_REQUIRED", key=KEY_PROBLEM):
    return nt.AlertEvent(key=key, kind=kind, mode="rollover_files", title="Files not backed up", message="m",
                         fault_code="AAM-ROL-06" if kind != "RECOVERED" else None,
                         resolved_code="AAM-ROL-06" if kind == "RECOVERED" else None)


def test_the_alarm_and_its_resolved_mail_use_their_own_mode(tmp_path, db):
    import flow

    (tmp_path / "src" / FROM).mkdir(parents=True)
    (tmp_path / "src" / TO).mkdir(parents=True)
    (tmp_path / "src" / TO / "a.xlsx").write_text("x", encoding="utf-8")
    sent = []
    with patch("core.fy_rollover.get_fy_prefix", return_value=TO), \
            patch("flow.notify", side_effect=lambda c, e, **kw: sent.append(e)):
        flow._alarm_unprotected_new_fy(_cfg(tmp_path), db)
    assert [e.mode for e in sent] == ["rollover_files"]


def test_the_resolved_mail_does_not_end_an_open_archive_warning_incident(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        assert nt.notify(cfg, _archive_warning(), db=db).status == nt.SENT
        assert nt.notify(cfg, _files_alert(), db=db).status == nt.SENT
        assert nt.notify(cfg, _files_alert("RECOVERED", KEY_OK), db=db).status == nt.SENT
        # the archive problem is STILL the same open incident: its repeat is suppressed, not a "new" first mail
        again = nt.notify(cfg, _archive_warning(), db=db)
    assert again.status == nt.SUPPRESSED, again


def test_the_subject_names_the_switch_not_the_internal_mode(tmp_path, db):
    subject, _, _ = nt.render_alert(_cfg(tmp_path), _files_alert(), db)
    assert "ROLLOVER_FILES" not in subject and "Financial-year switch" in subject


def test_the_incident_closes_once_the_switch_is_done_even_with_resolved_mails_switched_off(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], send_resolved=False)
        assert nt.notify(cfg, _files_alert(), db=db).status == nt.SENT
        # the config now points at the new year: the problem ended, so a later alarm is a NEW first mail
        cfg.paths.source_drive = str(tmp_path / "src" / TO)
        cfg.paths.lan_destination = f"\\\\nas\\share\\{TO}"
        prior = db.last_notification(KEY_PROBLEM, status=nt.SENT)
        assert nt._incident_closed(_files_alert(), db, cfg, prior) is True
        cfg.paths.source_drive = str(tmp_path / "src" / FROM)
        cfg.paths.lan_destination = f"\\\\nas\\share\\{FROM}"
        assert nt._incident_closed(_files_alert(), db, cfg, prior) is False


def test_the_resolved_mail_goes_out_in_the_same_run_that_switches():
    with _Rig() as r:
        r.db.record_notification({
            "event_key": KEY_PROBLEM, "kind": "ACTION_REQUIRED", "mode": "rollover_files", "subject": "s",
            "status": "SENT", "error": None, "attempts": 1, "created_at": "2026-04-01T06:00:00+00:00",
            "fault_code": "AAM-ROL-06", "message_id": None, "thread_id": None, "attachments_rung": None,
            "audience": "alerts", "recipients": None})
        assert r.run() == "ROLLOVER_COMPLETED"
        recovered = [e for e in _events(r) if e.kind == "RECOVERED"]
    assert [e.key for e in recovered] == [KEY_OK] and recovered[0].mode == "rollover_files"


def test_no_resolved_mail_in_the_switching_run_when_nobody_was_mailed():
    with _Rig() as r:
        assert r.run() == "ROLLOVER_COMPLETED"
        assert not [e for e in _events(r) if e.kind == "RECOVERED"]
