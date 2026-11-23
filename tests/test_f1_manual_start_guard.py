"""F1 (manual-button review 2026-11-23): a manual start while the backup service is down must not queue a
hidden run that starts at an unplanned time (for LAN: wakes the NAS in office hours).

Two layers, both fail closed:
  1. the trigger endpoints refuse (503) when no service has asked the deployment for work lately
     (Prefect `last_polled`), and nothing is created;
  2. a manual run that nevertheless begins later than `dashboard.manual_start_max_wait_minutes` after it was
     requested is not executed: a *_MISSED row per folder, no slot, no WoL, and the flow fails."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import flow
import ui
from models.config import DashboardConfig

PLAIN = "the backup service is not running, nothing was started"


def _request():
    return Request({"type": "http", "method": "POST", "query_string": b"", "headers": [],
                    "client": ("127.0.0.1", 1)})


def _body(resp):
    return json.loads(resp.body)


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(ui, "_require_auth", lambda request: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    started = []

    async def fake_arun(name, parameters=None, timeout=None):
        started.append(name)
        return SimpleNamespace(id="r1", state=None)

    monkeypatch.setattr(ui, "arun_deployment", fake_arun)
    # the real check, with the Prefect client replaced
    monkeypatch.setattr(ui, "_agent_serving", ui._agent_serving_real)
    return started


def _client_returning(polled_ago_s=None, error=None, missing=False):
    from prefect.exceptions import ObjectNotFound

    dep = SimpleNamespace(last_polled=None if polled_ago_s is None
                          else datetime.now(timezone.utc) - timedelta(seconds=polled_ago_s))

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read_deployment_by_name(self, name):
            if error:
                raise error
            if missing:
                raise ObjectNotFound(http_exc=Exception("x"))
            return dep

    return lambda: Client()


@pytest.mark.parametrize("trigger", ["trigger_cloud", "trigger_lan"])
@pytest.mark.parametrize("ago", [None, 10_000])
def test_silent_service_refuses_and_creates_no_run(rig, monkeypatch, trigger, ago):
    monkeypatch.setattr(ui, "get_client", _client_returning(ago))
    resp = asyncio.run(getattr(ui, trigger)(_request()))
    assert resp.status_code == 503
    assert PLAIN in _body(resp)["detail"].lower()
    assert rig == []


def test_missing_deployment_counts_as_not_running(rig, monkeypatch):
    monkeypatch.setattr(ui, "get_client", _client_returning(missing=True))
    resp = asyncio.run(ui.trigger_cloud(_request()))
    assert resp.status_code == 503 and rig == []


def test_unreachable_api_stays_the_scheduler_not_answering_503(rig, monkeypatch):
    monkeypatch.setattr(ui, "get_client", _client_returning(error=OSError("down")))
    with pytest.raises(HTTPException) as e:
        asyncio.run(ui.trigger_lan(_request()))
    assert e.value.status_code == 503 and rig == []


def test_recently_polling_service_lets_the_start_through(rig, monkeypatch):
    monkeypatch.setattr(ui, "get_client", _client_returning(20))
    asyncio.run(ui.trigger_cloud(_request()))
    assert rig == ["aam-backup/backup-cloud"]


def test_silence_limit_is_a_bounded_config_key():
    f = DashboardConfig.model_fields["agent_max_silence_seconds"]
    assert f.default == 120
    m = {x.__class__.__name__: getattr(x, a) for x in f.metadata for a in ("ge", "le") if hasattr(x, a)}
    assert (m["Ge"], m["Le"]) == (30, 3600)


def test_start_wait_limit_is_a_bounded_config_key():
    f = DashboardConfig.model_fields["manual_start_max_wait_minutes"]
    assert f.default == 30
    m = {x.__class__.__name__: getattr(x, a) for x in f.metadata for a in ("ge", "le") if hasattr(x, a)}
    assert (m["Ge"], m["Le"]) == (5, 1440)


# ── layer 2: the flow ────────────────────────────────────────────────────────

def _flow_run(auto_scheduled, late_s):
    exp = datetime.now(timezone.utc) - timedelta(seconds=late_s)
    return MagicMock(auto_scheduled=auto_scheduled, expected_start_time=exp)


def test_manual_delay_is_measured_only_for_manual_runs():
    with patch("prefect.context.FlowRunContext.get", return_value=MagicMock(flow_run=_flow_run(False, 3600))):
        assert flow._manual_start_delay_seconds() == pytest.approx(3600, abs=5)
    with patch("prefect.context.FlowRunContext.get", return_value=MagicMock(flow_run=_flow_run(True, 3600))):
        assert flow._manual_start_delay_seconds() == 0.0
    with patch("prefect.context.FlowRunContext.get", return_value=None):
        assert flow._manual_start_delay_seconds() == 0.0


@pytest.fixture
def flow_rig(tmp_path, monkeypatch):
    from tests.test_ov2_fy_overlap import _cfg
    cfg = _cfg(tmp_path)
    cfg.paths.database_path = str(tmp_path / "m.db")
    recorded, ran = [], []
    monkeypatch.setattr(flow, "load_config", lambda p: cfg)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow, "configure_prefect_bridge", lambda *a, **k: None)
    monkeypatch.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda **k: None)
    monkeypatch.setattr(flow, "_activate_overlap", lambda *a, **k: None)
    monkeypatch.setattr(flow, "_scheduled_lateness_seconds", lambda: 0.0)
    monkeypatch.setattr(flow, "_flow_run_prefix", lambda: "flow1")
    monkeypatch.setattr(flow, "_run_leg_folders", lambda *a, **k: ran.append(a[1]) or [])
    monkeypatch.setattr(flow, "_record_run", lambda config, run_id, leg, started, status, *a, **k:
                        recorded.append((leg, status, a[1] if len(a) > 1 else None)))
    return cfg, recorded, ran


def test_a_manual_run_that_starts_too_late_is_missed_and_runs_nothing(flow_rig, monkeypatch):
    cfg, recorded, ran = flow_rig
    cfg.dashboard.manual_start_max_wait_minutes = 30
    monkeypatch.setattr(flow, "_manual_start_delay_seconds", lambda: 31 * 60.0)
    with pytest.raises(flow.PartialRun):
        flow.backup.fn("config.yaml", "lan")
    assert ran == []                                   # no slot, no WoL, no copy
    assert recorded and all(r[0] == "lan" and r[1] == "LAN_MISSED" for r in recorded)
    assert "service was not running" in recorded[0][2].lower() or "requested" in recorded[0][2].lower()


def test_a_manual_run_inside_the_limit_runs_normally(flow_rig, monkeypatch):
    cfg, recorded, ran = flow_rig
    cfg.dashboard.manual_start_max_wait_minutes = 30
    monkeypatch.setattr(flow, "_manual_start_delay_seconds", lambda: 29 * 60.0)
    flow.backup.fn("config.yaml", "cloud")
    assert ran == ["cloud"] and recorded == []
