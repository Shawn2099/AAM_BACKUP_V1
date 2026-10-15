"""M4 4.7 - settings editable on the dashboard (owner decision DEC-9).

Contract: nothing is written unless the whole candidate loads exactly like
service start; risky keys need a typed phrase; lock-out keys and secrets are
protected; a backup holding the lock blocks the write; every write is
backed up, atomic, comment-preserving and recorded in the history.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import ui
from core import config_edit as ce
from core.manifest import ManifestDB
from models.config import load_config

KEY = {"X-API-Key": "secret-key"}


def _write_config(tmp_path):
    src = tmp_path / "src" / "FY26-27"
    src.mkdir(parents=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# operator comment at the top\n"
        "firm_name: Test Firm\n"
        "paths:\n"
        f"  source_drive: '{src}'\n"
        "  lan_destination: '\\\\nas\\share\\FY26-27'\n"
        f"  runtime_dir: '{tmp_path / 'rt'}'\n"
        "  gcs_key_path: ''\n"
        "cloud:\n  enabled: false\n"
        "wol:\n  enabled: false\n"
        "notifications:\n  smtp_password: 'old-secret'\n"
        "budget:\n"
        "  # how long LAN may run\n"
        "  lan_max_hours: 4\n"
        "dashboard:\n  auth_enabled: true\n  api_key: secret-key\n",
        encoding="utf-8")
    return cfg


@pytest.fixture
def cfg_file(tmp_path):
    return _write_config(tmp_path)


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _apply(cfg_file, changes, phrase=None, busy=None, db=None):
    return ce.apply_changes(str(cfg_file), changes, phrase=phrase, actor="127.0.0.1 (api-key)",
                            lock_busy=lambda: busy, db=db, now_iso=lambda: "2026-10-15T12:00:00")


class TestCatalog:
    def test_every_setting_with_metadata_and_no_secret_value(self, cfg_file):
        sections = ce.catalog(load_config(str(cfg_file)))
        fields = {f["key"]: f for s in sections for f in s["fields"]}
        assert fields["budget.lan_max_hours"]["value"] == 4
        assert fields["wol.wake_timeout_seconds"]["ge"] == 60
        assert fields["wol.wake_timeout_seconds"]["le"] == 3600
        assert fields["notifications.smtp_security"]["choices"] == ["auto", "starttls", "ssl", "none"]
        assert fields["notifications.smtp_password"]["value"] is None
        assert fields["notifications.smtp_password"]["is_set"] is True
        assert "old-secret" not in repr(sections)
        assert fields["dashboard.api_key"]["locked"] is True
        assert fields["paths.lan_destination"]["risky"] is True
        assert fields["guards.max_delete_percent"]["risky"] is True
        assert fields["schedule.lan_cron"]["effect"] == ce.AGENT_RESTART
        assert fields["notifications.recipients"]["type"] == "list"
        assert fields["cloud.max_duration_seconds"]["optional"] is True


class TestApply:
    def test_valid_change_is_written_typed_with_comments_backup_and_history(self, cfg_file, db):
        res = _apply(cfg_file, {"budget.lan_max_hours": "3"}, db=db)   # "3" from a form
        text = cfg_file.read_text(encoding="utf-8")
        assert "lan_max_hours: 3" in text and "lan_max_hours: '3'" not in text
        assert "# operator comment at the top" in text and "# how long LAN may run" in text
        assert load_config(str(cfg_file)).budget.lan_max_hours == 3
        assert list(cfg_file.parent.glob("config.yaml.bak-*"))
        [row] = db.config_changes()
        assert row["key"] == "budget.lan_max_hours" and row["changed_by"] == "127.0.0.1 (api-key)"
        assert row["old_value"] == "4.0" and row["new_value"] == "3.0"
        assert res.effects == [ce.EFFECT_TEXT[ce.NEXT_RUN]]

    def test_invalid_value_is_rejected_per_key_and_nothing_written(self, cfg_file):
        before = cfg_file.read_bytes()
        with pytest.raises(ce.ConfigEditError) as e:
            _apply(cfg_file, {"wol.wake_timeout_seconds": 5})
        assert e.value.status == 400
        assert e.value.extra["errors"][0]["key"] == "wol.wake_timeout_seconds"
        assert cfg_file.read_bytes() == before
        assert not list(cfg_file.parent.glob("config.yaml.bak-*"))

    def test_schedule_that_does_not_fit_the_budgets_is_rejected(self, cfg_file):
        before = cfg_file.read_bytes()
        with pytest.raises(ce.ConfigEditError) as e:
            _apply(cfg_file, {"schedule.lan_cron": "0 21 * * *",
                              "schedule.audit_cron": "0 22 * * SUN"})
        assert "run budgets" in " ".join(x["message"] for x in e.value.extra["errors"])
        assert cfg_file.read_bytes() == before

    @pytest.mark.parametrize("key", ["dashboard.api_key", "dashboard.auth_enabled",
                                     "paths.runtime_dir", "paths.database_path"])
    def test_lock_out_keys_are_refused(self, cfg_file, key):
        with pytest.raises(ce.ConfigEditError) as e:
            _apply(cfg_file, {key: "x"})
        assert e.value.status == 403

    def test_unknown_key_is_refused(self, cfg_file):
        with pytest.raises(ce.ConfigEditError) as e:
            _apply(cfg_file, {"lan.shutdown_after_bakup": False})
        assert e.value.status == 400 and "lan.shutdown_after_bakup" in str(e.value)

    def test_risky_change_needs_the_typed_phrase(self, cfg_file):
        new = "\\\\nas2\\share\\FY26-27"
        with pytest.raises(ce.ConfigEditError) as e:
            _apply(cfg_file, {"paths.lan_destination": new})
        assert e.value.status == 409
        assert e.value.extra["confirm_phrase"] == "CHANGE paths.lan_destination"
        _apply(cfg_file, {"paths.lan_destination": new}, phrase="CHANGE paths.lan_destination")
        assert load_config(str(cfg_file)).paths.lan_destination == new

    def test_several_risky_changes_need_a_count_phrase(self, cfg_file):
        with pytest.raises(ce.ConfigEditError) as e:
            _apply(cfg_file, {"guards.max_delete_files": 100, "guards.max_delete_percent": 5})
        assert e.value.extra["confirm_phrase"] == "CHANGE 2 SETTINGS"

    def test_secret_is_write_only(self, cfg_file, db):
        assert _apply(cfg_file, {"notifications.smtp_password": ""}, db=db).changed == []
        _apply(cfg_file, {"notifications.smtp_password": "new-secret"}, db=db)
        assert load_config(str(cfg_file)).notifications.smtp_password == "new-secret"
        [row] = db.config_changes()
        assert row["old_value"] == "'(hidden)'" and row["new_value"] == "'(hidden)'"

    def test_a_running_backup_blocks_the_write(self, cfg_file):
        before = cfg_file.read_bytes()
        with pytest.raises(ce.ConfigEditError) as e:
            _apply(cfg_file, {"budget.lan_max_hours": 3}, busy="a backup is running")
        assert e.value.status == 409 and cfg_file.read_bytes() == before

    def test_unchanged_values_write_nothing(self, cfg_file):
        assert _apply(cfg_file, {"budget.lan_max_hours": 4}).changed == []
        assert not list(cfg_file.parent.glob("config.yaml.bak-*"))

    def test_old_backups_are_pruned_to_the_configured_count(self, cfg_file):
        for i in range(7):
            (cfg_file.parent / f"config.yaml.bak-old{i}").write_text("x", encoding="utf-8")
        _apply(cfg_file, {"maintenance.config_backups_keep": 5})
        assert len(list(cfg_file.parent.glob("config.yaml.bak-*"))) == 5

    def test_a_written_file_that_does_not_load_is_rolled_back(self, cfg_file, monkeypatch):
        import models.config as mc
        before = cfg_file.read_bytes()
        real = mc.load_config
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            if calls["n"] == 3:          # current, candidate, then the written file
                raise OSError("disk hiccup")
            return real(path)
        monkeypatch.setattr(mc, "load_config", flaky)
        with pytest.raises(ce.ConfigEditError) as e:
            _apply(cfg_file, {"budget.lan_max_hours": 3})
        assert e.value.status == 500 and "restored" in str(e.value)
        assert cfg_file.read_bytes() == before

    def test_schedule_change_says_the_agent_must_restart(self, cfg_file):
        res = _apply(cfg_file, {"schedule.weekly_cron": "0 9 * * MON"})
        assert res.effects == [ce.EFFECT_TEXT[ce.AGENT_RESTART]]


@pytest.fixture
def rig(monkeypatch, tmp_path, db):
    cfg_file = _write_config(tmp_path)
    cfg = SimpleNamespace(dashboard=SimpleNamespace(
        auth_enabled=True, api_key="secret-key", admin_password_hash="",
        admin_session_minutes=30, admin_password_min_length=10))
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_settings_config_path", lambda: str(cfg_file))
    monkeypatch.setattr(ui, "_settings_lock_busy", lambda: None)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    return SimpleNamespace(cfg_file=cfg_file, db=db, cfg=cfg,
                           client=TestClient(ui.app, raise_server_exceptions=False))


class TestEndpoints:
    def test_get_needs_sign_in(self, rig):
        assert rig.client.get("/api/settings").status_code == 401
        body = rig.client.get("/api/settings", headers=KEY).json()
        assert any(s["name"] == "budget" for s in body["sections"])

    def test_save_and_history(self, rig):
        r = rig.client.post("/api/settings", headers=KEY,
                            json={"changes": {"budget.lan_max_hours": 3}})
        assert r.status_code == 200, r.text
        assert r.json()["changed"][0]["key"] == "budget.lan_max_hours"
        hist = rig.client.get("/api/settings/history", headers=KEY).json()["changes"]
        assert hist[0]["key"] == "budget.lan_max_hours"

    def test_errors_and_phrase_are_returned_as_json(self, rig):
        assert rig.client.post("/api/admin/login", headers=KEY,
                               json={"password": "secret-key"}).status_code == 200
        r = rig.client.post("/api/settings", headers=KEY,
                            json={"changes": {"wol.wake_timeout_seconds": 5}})
        assert r.status_code == 400 and r.json()["errors"][0]["key"] == "wol.wake_timeout_seconds"
        r = rig.client.post("/api/settings", headers=KEY,
                            json={"changes": {"guards.max_delete_files": 100}})
        assert r.status_code == 409 and r.json()["confirm_phrase"] == "CHANGE guards.max_delete_files"

    def test_saving_needs_dashboard_sign_in_enabled(self, rig):
        rig.cfg.dashboard.auth_enabled = False
        r = rig.client.post("/api/settings", json={"changes": {"budget.lan_max_hours": 3}})
        assert r.status_code == 403 and "auth_enabled" in r.json()["detail"]

    def test_settings_page_renders(self, rig):
        r = rig.client.get("/settings", headers=KEY)
        assert r.status_code == 200 and "settings.js" in r.text
