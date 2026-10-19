"""Settings > "Apply changes": restart the backup service so saved settings take effect.

Contract: only settings the scheduler reads once at start are "pending"; the pending list is
read from the history (survives a page reload); the restart is refused - never queued - while
any backup / audit / rollover runs or is about to start, or when Prefect cannot be asked;
the clean shutdown signal is sent after the reply, a hard exit follows if it does not end the
process, and /health exposes a boot id so the page can tell the NEW process from the old one.
"""

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import ui
from core import app_restart
from core import config_edit as ce
from core.manifest import ManifestDB
from test_support import ui_login

from test_m4_settings import _write_config  # noqa: E402  (same minimal config as the settings tests)

KEY = {}


@pytest.fixture(autouse=True)
def _fresh_restart_state(monkeypatch):
    app_restart._requested.clear()
    monkeypatch.setattr(app_restart, "STARTED_AT", "2026-10-19T08:00:00+05:30")
    yield
    app_restart._requested.clear()


@pytest.fixture
def rig(monkeypatch, tmp_path):
    cfg_file = _write_config(tmp_path)
    db = ManifestDB(str(tmp_path / "m.db"))
    cfg = SimpleNamespace(dashboard=SimpleNamespace(
        **ui_login.DASHBOARD, admin_session_minutes=30, admin_password_min_length=10,
        apply_delay_seconds=1, apply_force_exit_seconds=30, apply_block_before_run_seconds=120,
        apply_reconnect_timeout_seconds=300, apply_poll_seconds=3))
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_settings_config_path", lambda: str(cfg_file))
    monkeypatch.setattr(ui, "_settings_lock_busy", lambda: None)
    monkeypatch.setattr(ui, "_prefect_busy_reason", AsyncMock(return_value=None))
    started = []
    monkeypatch.setattr(app_restart, "request_restart", lambda **kw: started.append(kw) or True)
    yield SimpleNamespace(db=db, cfg=cfg, started=started,
                          client=TestClient(ui.app, raise_server_exceptions=False))
    db.close()


def _change(db, key, at="2026-10-19T09:00:00+05:30"):
    db.record_config_change({"changed_at": at, "changed_by": "t", "key": key,
                             "old_value": "1", "new_value": "2", "risky": 0, "backup_file": "b"})


class TestEffects:
    @pytest.mark.parametrize("key", ["schedule.lan_cron", "cloud.enabled", "lan.enabled"])
    def test_settings_read_once_at_service_start_need_apply(self, key):
        assert ce.effect_of(key) == ce.AGENT_RESTART

    def test_watchdog_settings_reload_and_next_run_settings_do_not_need_apply(self):
        assert ce.effect_of("watchdog.check_interval_seconds") == ce.WATCHDOG_RELOAD
        assert ce.effect_of("budget.lan_max_hours") == ce.NEXT_RUN

    def test_no_text_sends_people_to_a_bat_file(self):
        assert not any(".bat" in text for text in ce.EFFECT_TEXT.values())


class TestPending:
    def test_only_changes_newer_than_this_process_and_needing_a_restart(self, rig):
        _change(rig.db, "schedule.lan_cron", at="2026-10-19T07:00:00+05:30")   # before start
        _change(rig.db, "lan.enabled")
        _change(rig.db, "budget.lan_max_hours")                                # next run
        body = rig.client.get("/api/settings", headers=ui_login.HEADERS).json()
        assert body["pending_restart"] == ["lan.enabled"]
        assert body["boot_id"] == app_restart.BOOT_ID

    def test_health_carries_the_boot_id(self, rig):
        assert rig.client.get("/health").json()["boot_id"] == app_restart.BOOT_ID


class TestApplyEndpoint:
    @pytest.mark.anonymous
    def test_needs_sign_in(self, rig):
        assert rig.client.post("/api/settings/apply").status_code == 401
        assert rig.started == []

    def test_nothing_pending_restarts_nothing(self, rig):
        r = rig.client.post("/api/settings/apply", headers=KEY)
        assert r.json()["status"] == "nothing_to_apply" and rig.started == []

    def test_restarts_and_records_who_and_what(self, rig):
        _change(rig.db, "schedule.lan_cron")
        r = rig.client.post("/api/settings/apply", headers=KEY)
        assert r.status_code == 200 and r.json()["status"] == "restarting"
        assert r.json()["applied"] == ["schedule.lan_cron"]
        [call] = rig.started
        assert call["delay_s"] == 1 and call["force_exit_s"] == 30
        top = rig.db.config_changes(limit=1)[0]
        assert top["key"] == "service.restart" and "schedule.lan_cron" in top["new_value"]

    def test_refused_while_the_backup_lock_is_held(self, rig, monkeypatch):
        _change(rig.db, "schedule.lan_cron")
        monkeypatch.setattr(ui, "_settings_lock_busy", lambda: "a backup is running")
        r = rig.client.post("/api/settings/apply", headers=KEY)
        assert r.status_code == 409 and "a backup is running" in r.json()["detail"]
        assert rig.started == []

    def test_refused_while_any_scheduler_run_is_active_or_unknown(self, rig, monkeypatch):
        _change(rig.db, "schedule.lan_cron")
        monkeypatch.setattr(ui, "_prefect_busy_reason",
                            AsyncMock(return_value="weekly-audit is running"))
        r = rig.client.post("/api/settings/apply", headers=KEY)
        assert r.status_code == 409 and "weekly-audit is running" in r.json()["detail"]
        assert rig.started == []

    def test_a_second_press_does_not_schedule_a_second_restart(self, rig, monkeypatch):
        _change(rig.db, "schedule.lan_cron")
        monkeypatch.setattr(app_restart, "restart_pending", lambda: True)
        r = rig.client.post("/api/settings/apply", headers=KEY)
        assert r.json()["status"] == "restarting" and rig.started == []


class TestPrefectBusy:
    @pytest.mark.parametrize("state, due_in_s, expect_busy", [
        ("RUNNING", None, True), ("PENDING", None, True),
        ("SCHEDULED", 30, True), ("SCHEDULED", 3600, False), ("SCHEDULED", None, True)])
    def test_reasons(self, monkeypatch, state, due_in_s, expect_busy):
        from datetime import datetime, timedelta, timezone

        from prefect.client.schemas.objects import StateType
        nxt = None if due_in_s is None else datetime.now(timezone.utc) + timedelta(seconds=due_in_s)
        run = SimpleNamespace(name="r1", state=SimpleNamespace(type=StateType[state]),
                              next_scheduled_start_time=nxt)

        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def read_flow_runs(self, **kw): return [run]
        monkeypatch.setattr(ui, "get_client", lambda: Client())
        import asyncio
        reason = asyncio.run(ui._prefect_busy_reason(120))
        assert (reason is not None) == expect_busy

    def test_prefect_unreachable_is_busy(self, monkeypatch):
        class Client:
            async def __aenter__(self): raise OSError("down")
            async def __aexit__(self, *a): return False
        monkeypatch.setattr(ui, "get_client", lambda: Client())
        import asyncio
        assert asyncio.run(ui._prefect_busy_reason(120))


class TestRequestRestart:
    def test_signals_after_the_delay_then_hard_exits_if_still_alive(self):
        events, done = [], threading.Event()
        assert app_restart.request_restart(
            delay_s=0.01, force_exit_s=0.01, actor="x",
            interrupt=lambda: events.append("interrupt"),
            hard_exit=lambda: (events.append("exit"), done.set())) is True
        assert done.wait(5)
        assert events == ["interrupt", "exit"]

    def test_only_one_restart_is_scheduled(self):
        kw = dict(delay_s=60, force_exit_s=60, actor="x", interrupt=lambda: None, hard_exit=lambda: None)
        assert app_restart.request_restart(**kw) is True
        assert app_restart.request_restart(**kw) is False


class TestWatchdogReload:
    def test_reloads_only_when_config_yaml_changed(self, monkeypatch):
        import watchdog
        applied = []
        monkeypatch.setattr(watchdog, "_apply_config", lambda wc, rl=None: applied.append(wc))
        monkeypatch.setattr(watchdog, "_config_mtime", lambda: 5.0)
        assert watchdog._reload_tunables(5.0) == 5.0 and applied == []      # unchanged
        assert watchdog._reload_tunables(1.0) == 5.0 and len(applied) == 1  # changed

    def test_a_broken_config_keeps_the_settings_in_use(self, monkeypatch):
        import models.config as mc
        import watchdog
        applied = []
        monkeypatch.setattr(watchdog, "_apply_config", lambda wc, rl=None: applied.append(wc))
        monkeypatch.setattr(watchdog, "_config_mtime", lambda: 9.0)
        monkeypatch.setattr(mc, "load_config", lambda path: (_ for _ in ()).throw(ValueError("bad")))
        assert watchdog._reload_tunables(1.0) == 9.0 and applied == []


class TestPageAssets:
    @pytest.mark.parametrize("name", ["settings.js", "dashboard.js", "report.js"])
    def test_scripts_parse(self, name):
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            pytest.skip("node is not installed")
        script = Path(ui.__file__).parent / "static" / "js" / name
        res = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    def test_the_restart_overlay_is_hidden_until_a_restart_starts(self):
        from pathlib import Path
        css = (Path(ui.__file__).parent / "static" / "css" / "settings.css").read_text(encoding="utf-8")
        assert "#restart-overlay { display: none;" in css
