"""H3 — dashboard active-run check: typed filter, tri-state, fail-closed triggers.

Old behavior: a raw nested dict was passed as the Prefect filter (works only
via Pydantic coercion), any API exception returned False ("nothing running" —
silently disabling the duplicate-run guard on both trigger endpoints), and
limit=20 could hide an active run behind queued ones.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from prefect.client.schemas.filters import (
    FlowRunFilter,
    FlowRunFilterStateType,
)

import ui


@pytest.fixture(scope="session", autouse=True)
def prefect_harness():
    yield


def _tmp_db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Path(f.name)


def _make_cfg():
    cfg = MagicMock()
    cfg.dashboard.auth_enabled = False
    cfg.dashboard.api_key = ""
    cfg.dashboard.bind_address = "127.0.0.1"
    cfg.dashboard.port = 8080
    cfg.paths.source_drive = "/tmp/src"
    cfg.paths.database_path = str(_tmp_db())
    cfg.firm_name = "TestFirm"
    cfg.schedule.cloud_cron = "0 18 * * *"
    cfg.schedule.lan_cron = "0 1 * * *"
    cfg.schedule.timezone = "Asia/Kolkata"
    return cfg


@pytest.fixture
def client():
    with patch("ui._cfg", return_value=_make_cfg()):
        with TestClient(ui.app, raise_server_exceptions=False) as c:
            yield c


def _fake_prefect_client(read_behavior):
    """Build a mock async context manager for ui.get_client.

    read_behavior may be: a return value, an exception instance (raised), or
    a callable used as side_effect.
    """
    client_inst = MagicMock()
    if isinstance(read_behavior, BaseException):
        def _raise(*a, **kw):
            raise read_behavior
        client_inst.read_flow_runs = AsyncMock(side_effect=_raise)
    elif callable(read_behavior):
        client_inst.read_flow_runs = AsyncMock(side_effect=read_behavior)
    else:
        client_inst.read_flow_runs = AsyncMock(return_value=read_behavior)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client_inst)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _run(coro):
    return asyncio.run(coro)


class TestTypedFilter:
    def test_filter_is_typed_model_with_limit_200(self):
        """Regression guard: the filter must be Prefect's typed models (same
        construction as launch.py), never a raw dict; limit must cover >20."""
        captured = {}

        with patch("ui.get_client",
                   return_value=_fake_prefect_client(
                       lambda **kw: captured.update(kw) or [])):
            result = _run(ui._prefect_has_active_run("cloud"))

        assert result is False
        flt = captured["flow_run_filter"]
        assert isinstance(flt, FlowRunFilter)
        assert isinstance(flt.state.type, FlowRunFilterStateType)
        assert set(flt.state.type.any_) == {
            ui.StateType.RUNNING,
            ui.StateType.PENDING,
        }
        assert captured["limit"] == 200


class TestTriState:
    def test_api_failure_returns_none_not_false(self):
        with patch("ui.get_client",
                   return_value=_fake_prefect_client(
                       httpx.ConnectError("dead"))):
            result = _run(ui._prefect_has_active_run("cloud"))
        assert result is None


class TestTriggerFailClosed:
    def test_trigger_refused_with_503_when_prefect_unreachable(self, client):
        with patch("ui._is_running", new=AsyncMock(return_value=None)), \
             patch("ui.arun_deployment", new=AsyncMock()) as m_dep:
            resp = client.post("/trigger/cloud")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()
        m_dep.assert_not_called()

    def test_lan_trigger_refused_with_503_when_prefect_unreachable(self, client):
        with patch("ui._is_running", new=AsyncMock(return_value=None)), \
             patch("ui.arun_deployment", new=AsyncMock()) as m_dep:
            resp = client.post("/trigger/lan")
        assert resp.status_code == 503
        m_dep.assert_not_called()

    def test_trigger_still_blocks_on_true(self, client):
        with patch("ui._is_running", new=AsyncMock(return_value=True)):
            resp = client.post("/trigger/cloud")
        assert resp.status_code == 400
        assert resp.json()["status"] == "already_running"

    def test_trigger_proceeds_on_false(self, client):
        run = MagicMock()
        run.id = "abc-123"
        with patch("ui._is_running", new=AsyncMock(return_value=False)), \
             patch("ui.arun_deployment", new=AsyncMock(return_value=run)):
            resp = client.post("/trigger/cloud")
        assert resp.status_code == 200
        assert resp.json()["status"] == "triggered"


class TestStatusDegradesHonestly:
    def _patch_db(self):
        db = MagicMock()
        db.get_recent_runs.return_value = []
        db.file_count.return_value = 0
        return db

    def test_status_keeps_boolean_and_reports_unknown(self, client):
        with patch("ui.get_db", return_value=self._patch_db()), \
             patch("ui._last_run_summary", return_value=None), \
             patch("ui._get_last_success", return_value=None), \
             patch("ui._get_health", new=AsyncMock(return_value={})), \
             patch("ui._prefect_has_active_run", new=AsyncMock(return_value=None)):
            resp = client.get("/status")

        assert resp.status_code == 200
        data = resp.json()
        # Booleans stay booleans — dashboard.js does truthiness on them.
        assert data["cloud"]["running"] is False
        assert data["lan"]["running"] is False
        assert data["cloud"]["run_state"] == "unknown"
        assert data["lan"]["run_state"] == "unknown"

    def test_status_reports_running_state(self, client):
        with patch("ui.get_db", return_value=self._patch_db()), \
             patch("ui._last_run_summary", return_value=None), \
             patch("ui._get_last_success", return_value=None), \
             patch("ui._get_health", new=AsyncMock(return_value={})), \
             patch("ui._prefect_has_active_run",
                   new=AsyncMock(side_effect=[True, False])):
            resp = client.get("/status")

        data = resp.json()
        assert data["cloud"]["running"] is True
        assert data["cloud"]["run_state"] == "running"
        assert data["lan"]["running"] is False
        assert data["lan"]["run_state"] == "idle"


class TestRunStateFields:
    def test_field_mapping(self):
        assert ui._run_state_fields(True) == {"running": True, "run_state": "running"}
        assert ui._run_state_fields(False) == {"running": False, "run_state": "idle"}
        assert ui._run_state_fields(None) == {"running": False, "run_state": "unknown"}
