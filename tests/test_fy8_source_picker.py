"""FY-8 (plan docs/PLAN_FY_FOLDERS_2026-10-22.md): the Advanced source picker.

The owner picks a PARENT folder (a drive or a folder that holds the FY folders); the FY leaf is always the
configured FY, never typed or chosen. The picker only lists and checks - it never creates a folder and it
saves nothing itself (the save goes through the normal Settings flow, which also refuses any FY change)."""

import os

from test_support import ui_login  # noqa: E402,F401
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import ui
from core import source_picker as sp
from core.manifest import ManifestDB
from models.config import load_config
from tests.test_m4_settings import _write_config

FY = "FY26-27"
KEY = {}      # the TestClient is signed in already (tests/conftest.py)


@pytest.fixture
def cfg(tmp_path):
    return load_config(str(_write_config(tmp_path)))


def _parent_with_fy(tmp_path, name="moved", files=("a.xlsx",)):
    folder = tmp_path / name / FY
    folder.mkdir(parents=True)
    for f in files:
        (folder / f).write_text("x", encoding="utf-8")
    return tmp_path / name


# ── listing ───────────────────────────────────────────────────────────────────

def test_listing_shows_sub_folders_only_sorted_and_skips_system_ones(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "A").mkdir()
    (tmp_path / "$RECYCLE.BIN").mkdir()
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    out = sp.list_folders(str(tmp_path))
    assert out["folders"] == ["A", "b"] and out["path"] == str(tmp_path)
    assert out["parent"] == str(tmp_path.parent)


def test_a_missing_or_unreadable_folder_is_a_plain_error(tmp_path):
    with pytest.raises(sp.PickerError, match="does not exist"):
        sp.list_folders(str(tmp_path / "nope"))
    with pytest.raises(sp.PickerError, match="not a folder"):
        f = tmp_path / "f.txt"
        f.write_text("x", encoding="utf-8")
        sp.list_folders(str(f))


def test_an_empty_path_lists_the_drives(monkeypatch):
    monkeypatch.setattr(sp.os.path, "isdir", lambda p: p in ("C:\\", "D:\\"))
    out = sp.list_folders("")
    assert out["path"] == "" and out["parent"] is None and out["folders"] == ["C:\\", "D:\\"]


# ── checking a chosen parent ──────────────────────────────────────────────────

def test_a_good_parent_is_accepted_and_shows_the_resulting_folder(tmp_path, cfg):
    parent = _parent_with_fy(tmp_path)
    out = sp.check_source_parent(cfg, str(parent))
    assert out["ok"] is True and out["problems"] == []
    assert out["source"].endswith(FY) and out["fy"] == FY
    assert any("fewer files" in w for w in out["warnings"])         # G2 will stop a smaller folder


def test_the_fy_folder_must_already_exist_the_picker_never_creates_it(tmp_path, cfg):
    (tmp_path / "empty").mkdir()
    out = sp.check_source_parent(cfg, str(tmp_path / "empty"))
    assert out["ok"] is False and FY in out["problems"][0] and "does not exist" in out["problems"][0]
    assert not (tmp_path / "empty" / FY).exists()


def test_a_folder_without_any_real_file_is_refused(tmp_path, cfg):
    parent = _parent_with_fy(tmp_path, files=("desktop.ini",))
    out = sp.check_source_parent(cfg, str(parent))
    assert out["ok"] is False and "no files" in " ".join(out["problems"]).lower()


def test_the_folder_already_in_use_is_not_a_change(tmp_path, cfg):
    out = sp.check_source_parent(cfg, str(tmp_path / "src"))
    assert out["ok"] is False and "already" in " ".join(out["problems"]).lower()


def test_a_junction_or_link_is_refused(tmp_path, cfg, monkeypatch):
    parent = _parent_with_fy(tmp_path)
    monkeypatch.setattr(sp.os.path, "isjunction", lambda p: str(p).endswith(FY), raising=False)
    out = sp.check_source_parent(cfg, str(parent))
    assert out["ok"] is False and "link" in " ".join(out["problems"]).lower()


def test_without_fy_folders_the_picker_is_off(tmp_path, cfg):
    cfg.paths.source_drive = str(tmp_path / "src")
    cfg.paths.lan_destination = "\\\\nas\\share"
    out = sp.check_source_parent(cfg, str(tmp_path))
    assert out["ok"] is False and "support" in " ".join(out["problems"]).lower()


def test_the_cloud_target_is_shown_when_the_cloud_backup_is_on(tmp_path, cfg):
    cfg.cloud.enabled = True
    cfg.cloud.bucket = "aam-bucket"
    out = sp.check_source_parent(cfg, str(_parent_with_fy(tmp_path)))
    assert out["cloud_target"] == f"gs://aam-bucket/{FY}"


# ── the endpoints ─────────────────────────────────────────────────────────────

@pytest.fixture
def rig(monkeypatch, tmp_path):
    cfg_file = _write_config(tmp_path)
    cfg = SimpleNamespace(dashboard=SimpleNamespace(
        **ui_login.DASHBOARD, admin_session_minutes=30, admin_password_min_length=10))
    db = ManifestDB(str(tmp_path / "m.db"))
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_settings_config_path", lambda: str(cfg_file))
    monkeypatch.setattr(ui, "_settings_lock_busy", lambda: None)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    yield SimpleNamespace(client=TestClient(ui.app, raise_server_exceptions=False), tmp=tmp_path)
    db.close()


def _admin(rig):
    assert rig.client.post("/api/admin/login", headers=KEY,
                           json={"password": ui_login.ADMIN_PASSWORD}).status_code == 200


def test_both_endpoints_need_the_advanced_password(rig):
    h = ui_login.HEADERS
    assert rig.client.get("/api/settings/browse", headers=h).status_code == 403
    assert rig.client.post("/api/settings/check-source", headers=h, json={"parent": "x"}).status_code == 403


@pytest.mark.anonymous
def test_both_endpoints_need_sign_in(rig):
    assert rig.client.get("/api/settings/browse").status_code == 401
    assert rig.client.post("/api/settings/check-source", json={"parent": "x"}).status_code == 401


def test_browse_and_check_work_for_an_advanced_user(rig):
    _admin(rig)
    parent = _parent_with_fy(rig.tmp)
    r = rig.client.get("/api/settings/browse", headers=KEY, params={"path": str(rig.tmp)})
    assert r.status_code == 200 and "moved" in r.json()["folders"]
    r = rig.client.get("/api/settings/browse", headers=KEY, params={"path": str(rig.tmp / "x")})
    assert r.status_code == 400 and "does not exist" in r.json()["detail"]
    r = rig.client.post("/api/settings/check-source", headers=KEY, json={"parent": str(parent)})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["source"].endswith(FY)


def test_the_catalog_draws_the_source_path_as_a_folder_picker_and_the_page_calls_the_server(cfg):
    from core import config_edit as ce

    fields = {f["key"]: f for s in ce.catalog(cfg) for f in s["fields"]}
    assert fields["paths.source_drive"]["widget"] == "folder"
    assert fields["paths.lan_destination"].get("widget") != "folder"      # the NAS path is typed
    js = open("static/js/settings.js", encoding="utf-8").read()
    assert "/api/settings/browse" in js and "/api/settings/check-source" in js


# ── FY-12: path handling found by the Opus review ─────────────────────────────

def test_a_drive_root_has_no_parent_so_up_shows_the_drives():
    root = os.path.splitdrive(os.getcwd())[0] + "\\"
    assert sp.list_folders(root)["parent"] is None


@pytest.mark.parametrize("bad", ["\\\\host\\share", "\\\\host\\share\\data", "D:", "data", "..\\x", "/data"])
def test_only_absolute_local_paths_are_listed_or_checked(tmp_path, cfg, bad):
    with pytest.raises(sp.PickerError, match="full path|local"):
        sp.list_folders(bad)
    out = sp.check_source_parent(cfg, bad)
    assert out["ok"] is False and out["problems"]


def test_a_parent_typed_in_another_case_is_not_mistaken_for_a_link(tmp_path, cfg):
    parent = _parent_with_fy(tmp_path)
    out = sp.check_source_parent(cfg, str(parent).upper())
    assert out["ok"] is True, out["problems"]


def test_a_parent_below_a_link_is_refused(tmp_path, cfg, monkeypatch):
    parent = _parent_with_fy(tmp_path)
    monkeypatch.setattr(sp.os.path, "realpath", lambda p: "Z:\\somewhere\\else")
    out = sp.check_source_parent(cfg, str(parent))
    assert out["ok"] is False and "link" in " ".join(out["problems"]).lower()


def test_a_trailing_separator_does_not_change_the_result(tmp_path, cfg):
    parent = _parent_with_fy(tmp_path)
    a = sp.check_source_parent(cfg, str(parent))
    b = sp.check_source_parent(cfg, str(parent) + "\\")
    assert a["ok"] and b["ok"] and a["source"] == b["source"]


def test_the_page_survives_a_server_error_instead_of_staying_on_loading():
    js = open("static/js/settings.js", encoding="utf-8").read()
    assert js.count("The server did not answer properly") >= 2 and "} catch (e) {" in js
