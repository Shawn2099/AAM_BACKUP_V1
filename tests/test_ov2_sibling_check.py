"""OV-2.6 (plan docs/PLAN_FY_OVERLAP_2026-11-20.md 5.2): the daily sibling check replaces the ROL-06 alarm.

Folders next to the year folders that are not backed up: an FY-looking misnamed one, and a canonical NEWER year
holding real files that is not active (December use, overlap off, clock not proven). Past years, active years,
`rollover.sibling_ignore`, empty / placeholder-only folders and links are not reported."""

import datetime as dt
import subprocess
from unittest.mock import patch

import pytest

import flow
from core import attention, faults, fy_overlap
from core import notify as nt
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support import ui_login

FROM, TO = "FY26-27", "FY27-28"
PLACEHOLDER = "README_FY.txt"
DEC = dt.date(2026, 12, 15)          # outside the window: only FY26-27 is active
JAN = dt.date(2027, 1, 15)           # inside the window
FEB = dt.date(2027, 2, 10)


def _cfg(tmp_path, **rollover):
    (tmp_path / "src" / FROM).mkdir(parents=True, exist_ok=True)
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / "src" / FROM), "lan_destination": f"\\\\nas\\share\\{FROM}",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": 25, "sender": "b@firm.example",
                       "recipients": ["ops@firm.example"], "smtp_security": "none", "smtp_auth": False},
        rollover=rollover)


@pytest.fixture
def cfg(tmp_path):
    return _cfg(tmp_path)


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _folder(tmp_path, name, *files):
    folder = tmp_path / "src" / name
    folder.mkdir(parents=True, exist_ok=True)
    for rel in files:
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return folder


def _kinds(found):
    return [(f["kind"], f["name"], f["fy"]) for f in found]


# ---- sibling_findings -----------------------------------------------------------------------

def test_december_use_of_the_next_year_with_files_is_unprotected(tmp_path, cfg):
    _folder(tmp_path, TO, PLACEHOLDER, "a.xlsx", "clients/b.pdf")
    found = fy_overlap.sibling_findings(cfg, [FROM])
    assert found == [{"kind": "unprotected", "name": TO, "fy": TO, "files": 2}]


def test_overlap_off_in_february_with_files_is_unprotected(tmp_path, db):
    cfg = _cfg(tmp_path, overlap_enabled=False)
    _folder(tmp_path, TO, "a.xlsx")
    active = fy_overlap.active_folders(cfg, db, FEB)
    assert active == [FROM]
    assert _kinds(fy_overlap.sibling_findings(cfg, active, db)) == [("unprotected", TO, TO)]


def test_the_same_folder_in_the_active_set_is_not_reported(tmp_path, cfg, db):
    _folder(tmp_path, TO, "a.xlsx")
    db.record_activation(TO, activated_at="2027-01-01T02:01:00+05:30", source_path=str(tmp_path / "src" / TO),
                         created_by_app=True)
    active = fy_overlap.active_folders(cfg, db, JAN)
    assert active == [TO, FROM]
    assert fy_overlap.sibling_findings(cfg, active, db) == []


@pytest.mark.parametrize("name, good", [("FY 27-28", "FY27-28"), ("FY2027-28", "FY27-28"), ("FY_27_28", "FY27-28"),
                                        ("fy 27 28", "FY27-28")])
def test_fy_looking_but_not_canonical_is_misnamed(tmp_path, cfg, name, good):
    _folder(tmp_path, name)                       # even empty: it is a folder the office will fill
    found = fy_overlap.sibling_findings(cfg, [FROM])
    assert found == [{"kind": "misnamed", "name": name, "fy": None, "files": None}]
    assert fy_overlap.suggested_name(name) == good


def test_a_lower_case_canonical_name_is_not_misnamed_but_follows_the_files_rule(tmp_path, cfg):
    _folder(tmp_path, "fy27-28")
    assert fy_overlap.sibling_findings(cfg, [FROM]) == []                      # canonical, no files
    _folder(tmp_path, "fy27-28", "a.xlsx")
    assert _kinds(fy_overlap.sibling_findings(cfg, [FROM])) == [("unprotected", "fy27-28", TO)]


def test_a_past_year_and_the_configured_year_are_ignored(tmp_path, cfg):
    _folder(tmp_path, "FY25-26", "old.xlsx")
    _folder(tmp_path, FROM, "a.xlsx")
    assert fy_overlap.sibling_findings(cfg, [FROM]) == []


def test_sibling_ignore_is_respected_case_insensitively(tmp_path):
    cfg = _cfg(tmp_path, sibling_ignore=["fy 2020-21", "fy27-28"])
    _folder(tmp_path, "FY 2020-21")                # ignored (case-insensitive)
    _folder(tmp_path, "FY 20 21")                  # another name: still reported
    _folder(tmp_path, TO, "a.xlsx")                # ignored (case-insensitive)
    assert [f["name"] for f in fy_overlap.sibling_findings(cfg, [FROM])] == ["FY 20 21"]


def test_empty_and_placeholder_only_and_system_file_folders_are_not_reported(tmp_path, cfg):
    _folder(tmp_path, TO)
    assert fy_overlap.sibling_findings(cfg, [FROM]) == []
    _folder(tmp_path, TO, PLACEHOLDER, "desktop.ini", "Thumbs.db")
    assert fy_overlap.sibling_findings(cfg, [FROM]) == []


def test_other_folders_and_files_next_to_the_year_folders_are_not_reported(tmp_path, cfg):
    _folder(tmp_path, "Scans", "a.pdf")
    (tmp_path / "src" / "FY 27-28.zip").write_text("x", encoding="utf-8")     # a file, not a folder
    assert fy_overlap.sibling_findings(cfg, [FROM]) == []


def test_presence_only_mode_does_not_count(tmp_path, cfg):
    _folder(tmp_path, TO, "a.xlsx", "b.xlsx")
    [f] = fy_overlap.sibling_findings(cfg, [FROM], count=False)
    assert f["files"] is None and f["fy"] == TO


def _junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("cannot create a junction here")


def test_a_junction_child_is_not_followed(tmp_path, cfg):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "big.dat").write_text("x", encoding="utf-8")
    _junction(tmp_path / "src" / TO, outside)
    assert fy_overlap.sibling_findings(cfg, [FROM]) == []                       # the link itself holds nothing
    _folder(tmp_path, "FY30-31", "a.xlsx")
    _junction(tmp_path / "src" / "FY30-31" / "linked", outside)
    [f] = fy_overlap.sibling_findings(cfg, [FROM])
    assert f["fy"] == "FY30-31" and f["files"] == 1                             # the junction inside is not counted


def test_a_missing_parent_gives_nothing(tmp_path, cfg):
    cfg.paths.source_drive = str(tmp_path / "nowhere" / FROM)
    assert fy_overlap.sibling_findings(cfg, [FROM]) == []


# ---- the daily alarm in the flow ------------------------------------------------------------

class _Mail:
    """flow.notify stand-in that records the event and a SENT row like the real one."""

    def __init__(self, db):
        self.db, self.events = db, []

    def __call__(self, config, event, db=None):
        self.events.append(event)
        self.db.record_notification({
            "event_key": event.key, "kind": event.kind, "mode": event.mode, "subject": event.title,
            "status": "SENT", "error": None, "attempts": 1, "created_at": "2026-12-15T06:00:00+00:00",
            "fault_code": event.fault_code, "message_id": None, "thread_id": None, "attachments_rung": None,
            "audience": event.audience, "recipients": None, "fy": event.fy})


def test_the_alarm_for_next_years_files_has_the_right_key_audience_and_mode(tmp_path, cfg, db):
    _folder(tmp_path, TO, "a.xlsx")
    mail = _Mail(db)
    with patch("flow.notify", mail):
        flow._alarm_siblings(cfg, db, DEC)
    [e] = mail.events
    assert e.key == f"overlap:unprotected:{TO}" and e.kind == "ACTION_REQUIRED" and e.mode == "rollover_files"
    assert e.audience == nt.AUDIENCE_ALERTS and e.fy == TO and e.fault_code == "AAM-ROL-06"
    assert e.cooldown_hours == cfg.alerts.stale_repeat_hours
    assert TO in e.title and "NOT backed up" in e.message and "1 file(s)" in e.message
    assert "January" in e.message                                  # the reason in plain words


def test_the_reason_names_the_switched_off_setting_and_the_not_yet_activated_case(tmp_path, db):
    _folder(tmp_path, TO, "a.xlsx")
    off = _cfg(tmp_path, overlap_enabled=False)
    mail = _Mail(db)
    with patch("flow.notify", mail):
        flow._alarm_siblings(off, db, FEB)
        flow._alarm_siblings(_cfg(tmp_path), db, FEB)              # in window, never activated
    assert "switched off" in mail.events[0].message
    assert "not been set up" in mail.events[1].message


def test_the_alarm_for_a_misnamed_folder(tmp_path, cfg, db):
    _folder(tmp_path, "FY 27-28")
    mail = _Mail(db)
    with patch("flow.notify", mail):
        flow._alarm_siblings(cfg, db, DEC)
    [e] = mail.events
    assert e.key == "overlap:sibling:FY 27-28" and e.fy is None and e.mode == "rollover_files"
    assert e.fault_code == "AAM-ROL-07" and e.kind == "ACTION_REQUIRED"
    assert e.message == ('The folder "FY 27-28" looks like a new financial-year folder but is not named '
                         "FY27-28, so it is NOT backed up. Rename it to FY27-28.")


def test_nothing_is_sent_when_clean_and_a_failing_check_never_raises(tmp_path, cfg, db):
    mail = _Mail(db)
    with patch("flow.notify", mail):
        flow._alarm_siblings(cfg, db, DEC)
        with patch("core.fy_overlap.sibling_findings", side_effect=OSError("disk")):
            flow._alarm_siblings(cfg, db, DEC)
    assert mail.events == []


def test_one_resolved_mail_when_the_problem_ends_by_activation(tmp_path, cfg, db):
    _folder(tmp_path, TO, "a.xlsx")
    mail = _Mail(db)
    with patch("flow.notify", mail):
        flow._alarm_siblings(cfg, db, DEC)
        db.record_activation(TO, activated_at="2027-01-01T02:01:00+05:30", source_path="x", created_by_app=False)
        flow._alarm_siblings(cfg, db, JAN)
        flow._alarm_siblings(cfg, db, JAN)                          # never again
    assert [e.kind for e in mail.events] == ["ACTION_REQUIRED", "RECOVERED"]
    r = mail.events[1]
    assert r.key == f"recovered:overlap:unprotected:{TO}" and r.mode == "rollover_files"
    assert r.resolved_code == "AAM-ROL-06" and r.fy == TO and "backed up now" in r.title


def test_one_resolved_mail_when_the_misnamed_folder_is_renamed_and_the_files_one_is_emptied(tmp_path, cfg, db):
    bad = _folder(tmp_path, "FY 27-28")
    mail = _Mail(db)
    with patch("flow.notify", mail):
        flow._alarm_siblings(cfg, db, DEC)
        bad.rename(tmp_path / "src" / "FY27-28")                    # fixed (empty: nothing waits for a backup)
        flow._alarm_siblings(cfg, db, DEC)
        flow._alarm_siblings(cfg, db, DEC)
    assert [(e.kind, e.key) for e in mail.events] == [
        ("ACTION_REQUIRED", "overlap:sibling:FY 27-28"), ("RECOVERED", "recovered:overlap:sibling:FY 27-28")]
    assert mail.events[1].resolved_code == "AAM-ROL-07" and mail.events[1].fy is None


def test_no_resolved_mail_when_the_office_was_never_mailed(tmp_path, cfg, db):
    folder = _folder(tmp_path, TO, "a.xlsx")
    mail = _Mail(db)
    db.record_notification({
        "event_key": f"overlap:unprotected:{TO}", "kind": "ACTION_REQUIRED", "mode": "rollover_files",
        "subject": "s", "status": "FAILED", "error": "smtp", "attempts": 1,
        "created_at": "2026-12-15T06:00:00+00:00", "fault_code": "AAM-ROL-06", "message_id": None,
        "thread_id": None, "attachments_rung": None, "audience": "alerts", "recipients": None, "fy": TO})
    (folder / "a.xlsx").unlink()
    with patch("flow.notify", mail):
        flow._alarm_siblings(cfg, db, DEC)
    assert mail.events == []


def test_an_unproven_check_sends_no_false_resolved(tmp_path, cfg, db):
    _folder(tmp_path, TO, "a.xlsx")
    mail = _Mail(db)
    with patch("flow.notify", mail):
        flow._alarm_siblings(cfg, db, DEC)
        with patch("core.fy_overlap.sibling_findings", side_effect=OSError("share down")):
            flow._alarm_siblings(cfg, db, DEC)
    assert [e.kind for e in mail.events] == ["ACTION_REQUIRED"]


def test_the_check_runs_at_the_top_of_the_daily_flow_before_anything_can_raise():
    import inspect

    src = inspect.getsource(flow.rollover_check_flow.fn)
    assert src.index("_alarm_siblings") < src.index("_rollover_resume")


def test_the_old_alarm_is_gone():
    from core import fy_rollover

    assert not hasattr(fy_rollover, "unprotected_new_fy") and not hasattr(fy_rollover, "unprotected_message")
    assert not hasattr(flow, "_alarm_unprotected_new_fy")


# ---- incident handling (notify._incident_closed) ----------------------------------------------

def _event(key, fy):
    return nt.AlertEvent(key=key, kind="ACTION_REQUIRED", mode="rollover_files", title="t", message="m", fy=fy)


_PRIOR = {"created_at": "2026-12-15T06:00:00+00:00"}


def test_the_unprotected_incident_ends_when_active_configured_or_empty(tmp_path, cfg, db):
    folder = _folder(tmp_path, TO, "a.xlsx")
    ev = _event(f"overlap:unprotected:{TO}", TO)
    with patch("core.fy_overlap.run_date", return_value=JAN):
        assert nt._incident_closed(ev, db, cfg, _PRIOR) is False
        db.record_activation(TO, activated_at="2027-01-01T02:01:00+05:30", source_path="x", created_by_app=False)
        assert nt._incident_closed(ev, db, cfg, _PRIOR) is True                  # now active
    with patch("core.fy_overlap.run_date", return_value=DEC):
        assert nt._incident_closed(ev, db, cfg, _PRIOR) is False                 # active set is FY26-27 only
        (folder / "a.xlsx").unlink()
        assert nt._incident_closed(ev, db, cfg, _PRIOR) is True                  # nothing waits any more
        (folder / "a.xlsx").write_text("x", encoding="utf-8")
        cfg.paths.source_drive = str(tmp_path / "src" / TO)
        cfg.paths.lan_destination = f"\\\\nas\\share\\{TO}"
        assert nt._incident_closed(ev, db, cfg, _PRIOR) is True                  # the config points at it


def test_the_misnamed_incident_ends_when_the_folder_is_renamed_or_gone(tmp_path, cfg, db):
    bad = _folder(tmp_path, "FY 27-28")
    ev = _event("overlap:sibling:FY 27-28", None)
    with patch("core.fy_overlap.run_date", return_value=DEC):
        assert nt._incident_closed(ev, db, cfg, _PRIOR) is False
        bad.rename(tmp_path / "src" / "FY27-28")
        assert nt._incident_closed(ev, db, cfg, _PRIOR) is True


def test_an_unreadable_parent_keeps_the_incident_open(tmp_path, cfg, db):
    _folder(tmp_path, TO, "a.xlsx")
    ev = _event(f"overlap:unprotected:{TO}", TO)
    cfg.paths.source_drive = str(tmp_path / "gone" / FROM)
    with patch("core.fy_overlap.run_date", return_value=DEC):
        assert nt._incident_closed(ev, db, cfg, _PRIOR) is False


# ---- dashboard item and fault codes ------------------------------------------------------------

def test_the_dashboard_shows_a_red_item_for_each_finding(tmp_path, cfg, db):
    _folder(tmp_path, TO, "a.xlsx")
    _folder(tmp_path, "FY 28-29")
    with patch("core.fy_overlap.run_date", return_value=DEC):
        items = attention.compute_attention(db, cfg)
    by_code = {i["code"]: i for i in items if i["code"].startswith("overlap:")}
    assert set(by_code) == {f"overlap:unprotected:{TO}", "overlap:sibling:FY 28-29"}
    assert all(i["level"] == attention.RED for i in by_code.values())
    assert "not backed up" in by_code[f"overlap:unprotected:{TO}"]["message"].lower()
    assert "Rename it to FY28-29" in by_code["overlap:sibling:FY 28-29"]["message"]


def test_no_dashboard_item_when_clean(tmp_path, cfg, db):
    with patch("core.fy_overlap.run_date", return_value=DEC):
        assert not [i for i in attention.compute_attention(db, cfg) if i["code"].startswith("overlap:")]


def test_the_fault_codes_exist_and_are_actions():
    for code in ("AAM-ROL-06", "AAM-ROL-07"):
        assert faults.get(code).code == code and faults.get(code).severity == faults.ACTION
