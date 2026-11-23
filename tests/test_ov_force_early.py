"""Owner decision 2026-11-23: Settings button "Create next year's folder now (force FY activation)".

Core (`fy_overlap.force_early_activation`) and the dashboard route `/api/rollover/early`. The forced
activation uses the SAME code path as the normal 1 January one (`ensure_overlap_activated`,
`ensure_overlap_nas_folder`) and writes the same `overlap_activations` row.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pendulum
import pytest
from fastapi.testclient import TestClient

import ui
from core import fy_overlap as ov
from core.guards import MOUNT_MARKER
from core.health import CLOCK_OK
from core.manifest import ManifestDB
from test_support import ui_login
from tests.test_ov2_fy_overlap import CFG_FY, NEXT
from tests.test_ov2_fy_overlap import _cfg as _unc_cfg

NOV = pendulum.date(2026, 11, 23)       # long before the 1 January window
H = {"Accept": "application/json"}


class Mail:
    def __init__(self):
        self.events = []

    def __call__(self, config, event, db=None):
        self.events.append(event)
        return SimpleNamespace(status="SENT")


def _ok_clock():
    return SimpleNamespace(state=CLOCK_OK, reason="", to_metrics=lambda: {"state": CLOCK_OK})


def _cfg(tmp_path, source, nas, **kw):
    """The validator wants a UNC NAS path; the test then points it at a local folder (the code only uses Path)."""
    cfg = _unc_cfg(tmp_path, source=source, **kw)
    object.__setattr__(cfg.paths, "lan_destination", nas)
    for k, v in ui_login.DASHBOARD.items():
        object.__setattr__(cfg.dashboard, k, v)
    return cfg


@pytest.fixture
def rig(tmp_path):
    src = tmp_path / "src"
    nas = tmp_path / "nas"
    (src / CFG_FY).mkdir(parents=True)
    (nas / CFG_FY).mkdir(parents=True)
    cfg = _cfg(tmp_path, source=str(src / CFG_FY), nas=str(nas / CFG_FY))
    db = ManifestDB(tmp_path / "m.db")
    yield SimpleNamespace(cfg=cfg, db=db, src=src, nas=nas, mail=Mail())
    db.close()


def _force(rig, cfg=None):
    return ov.force_early_activation(cfg or rig.cfg, rig.db, run_date=NOV,
                                     clock_check=_ok_clock, notify_fn=rig.mail)


# ---- core -----------------------------------------------------------------------------------

def test_forced_activation_creates_folders_marker_and_activation(rig):
    assert ov.active_folders(rig.cfg, rig.db, NOV) == [CFG_FY]
    r = _force(rig)
    assert r["state"] == ov.ACTIVATED and r["fy"] == NEXT
    assert (rig.src / NEXT).is_dir()
    assert (rig.nas / NEXT / MOUNT_MARKER).exists()
    assert rig.db.get_activation(NEXT) is not None
    # the same state the normal activation writes: both years are active, in a month before the window
    assert ov.active_folders(rig.cfg, rig.db, NOV) == [NEXT, CFG_FY]
    assert ov.role_of(rig.cfg, NEXT) == ov.EARLY
    assert [e.key for e in rig.mail.events] == [f"overlap:start:{NEXT}"]      # the normal INFO mail, no new one


def test_forced_activation_survives_to_the_switch_and_ends_with_it(rig):
    _force(rig)
    assert ov.active_folders(rig.cfg, rig.db, pendulum.date(2027, 2, 1)) == [NEXT, CFG_FY]
    assert ov.active_folders(rig.cfg, rig.db, pendulum.date(2027, 4, 2)) == [NEXT, CFG_FY]   # switch pending


def test_second_press_changes_nothing_and_says_so(rig):
    _force(rig)
    first = rig.db.get_activation(NEXT)
    rig.mail.events.clear()
    r = _force(rig)
    assert r["state"] == ov.ALREADY_ACTIVE and "already" in r["message"].lower()
    assert rig.db.get_activation(NEXT) == first
    assert rig.mail.events == []


def test_existing_matching_folders_are_adopted_as_they_are(rig):
    (rig.src / NEXT.lower()).mkdir()                       # case differs: adopted, not duplicated
    (rig.src / NEXT.lower() / "old.txt").write_text("x")
    (rig.nas / NEXT).mkdir()
    (rig.nas / NEXT / MOUNT_MARKER).touch()
    (rig.nas / NEXT / "kept.txt").write_text("y")
    r = _force(rig)
    assert r["state"] == ov.ACTIVATED
    assert not (rig.src / NEXT.upper()).exists() or (rig.src / NEXT.lower()).exists()
    assert (rig.src / NEXT.lower() / "old.txt").read_text() == "x"
    assert (rig.nas / NEXT / "kept.txt").read_text() == "y"
    assert r["nas"]["state"] == "ok"


@pytest.mark.parametrize("where", ["src", "nas"])
def test_wrong_name_folder_is_refused_with_the_expected_format(rig, where):
    (getattr(rig, where) / "FY 27-28").mkdir()
    r = _force(rig)
    assert r["state"] == ov.REFUSED and "FY27-28" in r["message"]
    assert rig.db.get_activation(NEXT) is None
    assert not (rig.src / NEXT).exists()


def test_non_empty_nas_folder_without_marker_is_never_marked(rig):
    (rig.nas / NEXT).mkdir()
    (rig.nas / NEXT / "office.docx").write_text("real")
    r = _force(rig)
    assert r["state"] == ov.ACTIVATED                      # the activation itself is recorded
    assert r["nas"]["state"] == "needs_confirmation"       # G5 asks at the first backup
    assert not (rig.nas / NEXT / MOUNT_MARKER).exists()
    assert "marker" in r["message"].lower()


def test_nas_unreachable_is_refused_and_changes_nothing(rig, tmp_path):
    cfg = _cfg(tmp_path, source=str(rig.src / CFG_FY), nas=str(tmp_path / "gone" / CFG_FY))
    r = _force(rig, cfg)
    assert r["state"] == ov.REFUSED and "switch the nas on" in r["message"].lower()
    assert rig.db.get_activation(NEXT) is None and not (rig.src / NEXT).exists()


def test_cloud_only_activates_without_a_nas(rig, tmp_path):
    cfg = _cfg(tmp_path, source=str(rig.src / CFG_FY), nas=str(tmp_path / "gone" / CFG_FY), lan=False)
    r = _force(rig, cfg)
    assert r["state"] == ov.ACTIVATED and r["nas"] is None
    assert rig.db.get_activation(NEXT) is not None


def test_overlap_disabled_is_refused(tmp_path, rig):
    cfg = _cfg(tmp_path, source=str(rig.src / CFG_FY), nas=str(rig.nas / CFG_FY), overlap_enabled=False)
    r = _force(rig, cfg)
    assert r["state"] == ov.REFUSED and "overlap_enabled" in r["message"]
    assert rig.db.get_activation(NEXT) is None


def test_clock_not_proven_is_refused_and_nothing_made(rig):
    bad = lambda: SimpleNamespace(state="SKEW", reason="off by an hour")      # noqa: E731
    r = ov.force_early_activation(rig.cfg, rig.db, run_date=NOV, clock_check=bad, notify_fn=rig.mail)
    assert r["state"] == ov.REFUSED and "clock" in r["message"].lower()
    assert rig.db.get_activation(NEXT) is None and not (rig.src / NEXT).exists()


def test_early_state_reasons(tmp_path, rig):
    assert ov.early_state(rig.cfg, rig.db, NOV)["can_run"] is True
    _force(rig)
    st = ov.early_state(rig.cfg, rig.db, NOV)
    assert st["can_run"] is False and "already" in st["message"].lower()
    off = _cfg(tmp_path, str(rig.src / CFG_FY), str(rig.nas / CFG_FY), overlap_enabled=False)
    st = ov.early_state(off, rig.db, NOV)
    assert st["can_run"] is False and "overlap_enabled" in st["message"]


# ---- dashboard ------------------------------------------------------------------------------

@pytest.fixture
def ui_rig(monkeypatch, rig):
    ui._RATE_LIMITS.clear()
    running = AsyncMock(return_value=False)
    monkeypatch.setattr(ui, "_cfg", lambda: rig.cfg)
    monkeypatch.setattr(ui, "get_db", lambda: rig.db)
    monkeypatch.setattr(ui, "_is_running", running)
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_settings_lock_busy", lambda: None)
    monkeypatch.setattr(ov, "_default_clock_check", lambda config: _ok_clock())
    monkeypatch.setattr(ov, "run_date", lambda scheduled=None: NOV)
    monkeypatch.setattr("core.notify.notify", rig.mail)
    rig.running = running
    rig.client = TestClient(ui.app, raise_server_exceptions=False, headers=H,
                            cookies={"session": ui_login.SESSION_TOKEN})
    yield rig
    ui._RATE_LIMITS.clear()


def test_settings_page_has_the_button_and_the_script_wires_it():
    html = (Path(ui.__file__).parent / "templates" / "settings.html").read_text(encoding="utf-8")
    js = (Path(ui.__file__).parent / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    assert "Create next year's folder now" in html and 'id="btn-early"' in html
    assert "/api/rollover/early" in js and "runEarlyActivation" in js


def test_route_activates_and_status_follows(ui_rig):
    st = ui_rig.client.get("/api/rollover/early").json()
    assert st["can_run"] is True and st["fy"] == NEXT and "does not switch" in st["confirm"]
    r = ui_rig.client.post("/api/rollover/early")
    assert r.status_code == 200 and NEXT in r.json()["detail"]
    assert (ui_rig.nas / NEXT / MOUNT_MARKER).exists()
    st = ui_rig.client.get("/api/rollover/early").json()
    assert st["can_run"] is False and "already" in st["message"].lower()
    again = ui_rig.client.post("/api/rollover/early")
    assert again.status_code == 200 and "nothing" in again.json()["detail"].lower()


def test_route_needs_sign_in():
    r = TestClient(ui.app, raise_server_exceptions=False, follow_redirects=False,
                   headers=H).post("/api/rollover/early")
    assert r.status_code in (401, 403)


def test_route_refuses_while_a_backup_runs(ui_rig):
    ui_rig.running.side_effect = lambda leg: leg == "lan"
    r = ui_rig.client.post("/api/rollover/early")
    assert r.status_code == 400 and r.json()["status"] == "already_running"
    assert ui_rig.db.get_activation(NEXT) is None


def test_route_refuses_while_the_backup_lock_is_held(ui_rig, monkeypatch):
    monkeypatch.setattr(ui, "_settings_lock_busy", lambda: "a backup is running (backup lock held by PID 1)")
    r = ui_rig.client.post("/api/rollover/early")
    assert r.status_code == 400 and ui_rig.db.get_activation(NEXT) is None


def test_route_nas_unreachable_is_409(ui_rig, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, source=str(ui_rig.src / CFG_FY), nas=str(tmp_path / "gone" / CFG_FY))
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    r = ui_rig.client.post("/api/rollover/early")
    assert r.status_code == 409 and "switch the nas on" in r.json()["detail"].lower()


def test_route_overlap_disabled_is_409_and_status_says_why(ui_rig, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, source=str(ui_rig.src / CFG_FY), nas=str(ui_rig.nas / CFG_FY), overlap_enabled=False)
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    assert ui_rig.client.post("/api/rollover/early").status_code == 409
    st = ui_rig.client.get("/api/rollover/early").json()
    assert st["can_run"] is False and "overlap_enabled" in st["message"]


def test_route_wrong_name_is_409(ui_rig):
    (ui_rig.nas / "FY2027-28").mkdir()
    r = ui_rig.client.post("/api/rollover/early")
    assert r.status_code == 409 and "FY27-28" in r.json()["detail"], r.text


def test_route_cross_site_post_is_refused(ui_rig):
    r = ui_rig.client.post("/api/rollover/early", headers={
        "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code in (400, 403) and ui_rig.db.get_activation(NEXT) is None
