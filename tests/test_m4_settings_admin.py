"""Advanced settings need a separate login; units are fixed labels (owner 2026-10-15).

Initially the admin password is the dashboard key; it can be changed inside
Advanced, after which only a PBKDF2 hash is stored. Enforced by the server:
without the admin login, advanced settings are neither sent nor accepted.
"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import ui
from core import admin_auth
from core import config_edit as ce
from core.manifest import ManifestDB
from models.config import DashboardConfig, load_config

ROOT = Path(__file__).resolve().parent.parent
KEY = {"X-API-Key": "dash-key-123"}


def _write_config(tmp_path):
    src = tmp_path / "src" / "FY26-27"
    src.mkdir(parents=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "paths:\n"
        f"  source_drive: '{src}'\n"
        "  lan_destination: '\\\\nas\\share\\FY26-27'\n"
        f"  runtime_dir: '{tmp_path / 'rt'}'\n"
        "  gcs_key_path: ''\n"
        "cloud:\n  enabled: false\n"
        "wol:\n  enabled: false\n"
        "dashboard:\n  auth_enabled: true\n  api_key: dash-key-123\n", encoding="utf-8")
    return cfg


class TestHashing:
    def test_hash_verifies_and_never_contains_the_password(self):
        h = admin_auth.hash_password("correct horse battery")
        assert h.startswith("pbkdf2_sha256$600000$") and "correct horse" not in h
        d = SimpleNamespace(admin_password_hash=h, api_key="dash-key-123")
        assert admin_auth.verify_admin_password("correct horse battery", d)
        assert not admin_auth.verify_admin_password("dash-key-123", d)   # old one no longer works
        assert not admin_auth.verify_admin_password("", d)

    def test_initially_the_dashboard_key_is_the_admin_password(self):
        d = SimpleNamespace(admin_password_hash="", api_key="dash-key-123")
        assert admin_auth.verify_admin_password("dash-key-123", d)
        assert not admin_auth.verify_admin_password("other", d)
        assert not admin_auth.verify_admin_password(
            "", SimpleNamespace(admin_password_hash="", api_key=""))

    @pytest.mark.parametrize("new,confirm,problem", [
        ("longenough1", "longenough2", "not the same"),
        ("short", "short", "at least 10"),
        (" spaced-out-pw ", " spaced-out-pw ", "space"),
        ("longenough1", "longenough1", None),
    ])
    def test_new_password_rules(self, new, confirm, problem):
        got = admin_auth.new_password_problem(new, confirm, 10)
        assert (got is None) if problem is None else (problem in got)

    def test_a_plain_password_in_config_yaml_is_refused(self):
        with pytest.raises(ValidationError, match="never a plain password"):
            DashboardConfig(auth_enabled=False, admin_password_hash="hunter2")


@pytest.fixture
def rig(monkeypatch, tmp_path):
    cfg_file = _write_config(tmp_path)
    db = ManifestDB(str(tmp_path / "m.db"))
    state = {"cfg": load_config(str(cfg_file))}

    def cfg():
        return state["cfg"]

    def reload_cfg():
        state["cfg"] = load_config(str(cfg_file))

    monkeypatch.setattr(ui, "_cfg", cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_settings_config_path", lambda: str(cfg_file))
    monkeypatch.setattr(ui, "_settings_lock_busy", lambda: None)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_admin_sessions", {})
    yield SimpleNamespace(client=TestClient(ui.app, raise_server_exceptions=False),
                          cfg_file=cfg_file, db=db, reload_cfg=reload_cfg)
    db.close()


def _login(rig, password="dash-key-123"):
    return rig.client.post("/api/admin/login", headers=KEY, json={"password": password})


class TestAdminEndpoints:
    def test_without_the_admin_login_advanced_is_neither_sent_nor_accepted(self, rig):
        data = rig.client.get("/api/settings", headers=KEY).json()
        keys = {f["key"] for s in data["sections"] for f in s["fields"]}
        assert data["admin"] is False
        assert "budget.lan_max_hours" in keys and "guards.max_delete_files" not in keys
        r = rig.client.post("/api/settings", headers=KEY, json={"changes": {"guards.max_delete_files": 100}})
        assert r.status_code == 403 and "Advanced login" in r.json()["detail"]
        v = rig.client.post("/api/settings/validate", headers=KEY,
                            json={"changes": {"guards.max_delete_files": 100}}).json()
        assert v["ok"] is False

    def test_dashboard_key_opens_advanced_initially(self, rig):
        assert _login(rig, "wrong").status_code == 401
        r = _login(rig)
        assert r.status_code == 200 and "admin_session" in r.cookies
        data = rig.client.get("/api/settings", headers=KEY).json()
        assert data["admin"] is True
        assert any(f["key"] == "guards.max_delete_files" for s in data["sections"] for f in s["fields"])
        assert not any(f["key"] == "dashboard.admin_password_hash"
                       for s in data["sections"] for f in s["fields"])

    def test_change_password_stores_only_a_hash_and_the_old_key_stops_working(self, rig):
        _login(rig)
        r = rig.client.post("/api/admin/password", headers=KEY, json={
            "current": "dash-key-123", "new": "a-new-admin-pass", "confirm": "a-new-admin-pass"})
        assert r.status_code == 200, r.text
        text = rig.cfg_file.read_text(encoding="utf-8")
        assert "a-new-admin-pass" not in text and "admin_password_hash: pbkdf2_sha256$" in text
        rig.reload_cfg()
        rig.client.post("/api/admin/logout", headers=KEY)
        assert _login(rig, "dash-key-123").status_code == 401
        assert _login(rig, "a-new-admin-pass").status_code == 200
        [row] = rig.db.config_changes()
        assert row["key"] == "dashboard.admin_password_hash" and row["new_value"] == "'(hidden)'"

    def test_change_password_rules(self, rig):
        _login(rig)
        r = rig.client.post("/api/admin/password", headers=KEY, json={
            "current": "wrong", "new": "a-new-admin-pass", "confirm": "a-new-admin-pass"})
        assert r.status_code == 400 and "current" in r.json()["detail"]
        r = rig.client.post("/api/admin/password", headers=KEY, json={
            "current": "dash-key-123", "new": "short", "confirm": "short"})
        assert r.status_code == 400 and "at least" in r.json()["detail"]

    def test_change_password_needs_the_admin_login(self, rig):
        r = rig.client.post("/api/admin/password", headers=KEY, json={
            "current": "dash-key-123", "new": "a-new-admin-pass", "confirm": "a-new-admin-pass"})
        assert r.status_code == 403

    def test_the_hash_cannot_be_set_through_the_settings_form(self, rig):
        _login(rig)
        r = rig.client.post("/api/settings", headers=KEY,
                            json={"changes": {"dashboard.admin_password_hash": "pbkdf2_sha256$1$a$b"}})
        assert r.status_code == 403


class TestUnits:
    def test_every_number_setting_has_a_fixed_unit(self, tmp_path):
        f = {x["key"]: x for s in ce.catalog(load_config(str(_write_config(tmp_path))))
             for x in s["fields"]}
        assert f["cloud.bandwidth_limit"]["widget"] == "size_mb" and f["cloud.bandwidth_limit"]["unit"] == "MB/s"
        assert f["cloud.buffer_size"]["unit"] == "MB"
        assert f["wol.wake_timeout_seconds"]["unit"] == "seconds"
        assert f["budget.lan_max_hours"]["unit"] == "hours"
        assert f["guards.max_delete_percent"]["unit"] == "%"
        no_unit = [k for k, x in f.items() if x["type"] in ("int", "float") and not x["unit"]
                   and k not in ("notifications.smtp_port", "dashboard.port")]
        assert no_unit == []

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
    def test_size_conversion_keeps_rclone_format(self):
        js = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")

        def fn(name):
            start = js.index(f"function {name}(")
            return js[start:js.index("\n}\n", start) + 2]
        script = (fn("sizeToMb") + fn("mbToSize") + "\nconsole.log(JSON.stringify(["
                  "sizeToMb('10M'), sizeToMb('500k'), sizeToMb('1G'), sizeToMb('junk'),"
                  "mbToSize(10), mbToSize(0.5), mbToSize(2048)]));")
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout) == [10, 0.488, 1024, "", "10M", "512k", "2048M"]


class TestPlainLanguage:
    def test_every_setting_has_a_plain_name_and_every_section_a_plain_title(self, tmp_path):
        from core.config_help import HELP, SECTION_TITLES
        sections = ce.catalog(load_config(str(_write_config(tmp_path))))
        missing = [f["key"] for s in sections for f in s["fields"] if f["key"] not in HELP]
        assert missing == []
        assert all(s["name"] in SECTION_TITLES for s in sections)

    def test_basic_settings_explain_themselves(self, tmp_path):
        sections = ce.catalog(load_config(str(_write_config(tmp_path))), include_advanced=False)
        f = {x["key"]: x for s in sections for x in s["fields"]}
        assert f["schedule.lan_cron"]["label"] == "LAN backup (copy to the NAS)"
        assert f["notifications.recipients"]["help"]
        assert [s["name"] for s in sections][:3] == ["general", "schedule", "notifications"]

    def test_schedule_clash_is_explained_in_plain_words(self):
        from core.budget import schedule_problems
        [p] = schedule_problems({"cloud": ("0 22 * * *", 6 * 3600), "lan": ("0 21 * * *", 4 * 3600),
                                 "audit": ("0 10 * * SUN", 8 * 3600)}, "Asia/Kolkata", 3600)
        assert p.startswith("The LAN backup starts") and "could be skipped" in p
        assert "may run until Sat 01:00" in p or "may run until" in p
