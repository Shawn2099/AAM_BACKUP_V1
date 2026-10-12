"""Verify-trigger API: POST /trigger/verify/{lan,cloud,all} exposes the integrity-audit deployment.

Read-only audit; must refuse while a backup is active (shared backup lock)
or while another audit runs, and must respect disabled legs.
Deterministic; Prefect interactions stubbed.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.exceptions import HTTPException
from starlette.requests import Request

import ui


def _fake_request(query=""):
    scope = {
        "type": "http",
        "query_string": query.encode(),
        "client": ("127.0.0.1", 55555),
        "headers": [],
    }
    return Request(scope)


def _patch_auth(monkeypatch):
    monkeypatch.setattr(ui, "_require_auth", lambda request: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)


def _fake_cfg(cloud_enabled=True, lan_enabled=True):
    return SimpleNamespace(
        cloud=SimpleNamespace(enabled=cloud_enabled),
        lan=SimpleNamespace(enabled=lan_enabled),
    )


def _body(resp):
    return json.loads(bytes(resp.body).decode())


def _idle(monkeypatch):
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))


def test_invalid_mode_rejected(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg())
    with pytest.raises(HTTPException, match="Invalid mode"):
        asyncio.run(ui.trigger_verify("gcs", _fake_request()))


def test_disabled_legs_rejected_without_deployment(monkeypatch):
    _patch_auth(monkeypatch)

    async def boom(**kwargs):
        raise AssertionError("arun_deployment must not run for a disabled leg")

    monkeypatch.setattr(ui, "arun_deployment", boom)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(cloud_enabled=True, lan_enabled=False))
    resp = asyncio.run(ui.trigger_verify("lan", _fake_request()))
    assert resp.status_code == 409
    assert _body(resp)["status"] == "disabled"
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(cloud_enabled=False, lan_enabled=True))
    resp = asyncio.run(ui.trigger_verify("cloud", _fake_request()))
    assert resp.status_code == 409
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(cloud_enabled=False, lan_enabled=False))
    resp = asyncio.run(ui.trigger_verify("all", _fake_request()))
    assert resp.status_code == 409


def test_enabled_modes_proceed_with_mode_parameter(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(True, True))
    _idle(monkeypatch)
    calls = []

    async def fake_run(name=None, parameters=None, timeout=None):
        calls.append((name, parameters, timeout))
        return SimpleNamespace(id="verify-run", state=SimpleNamespace(type=None, is_final=lambda: False))

    monkeypatch.setattr(ui, "arun_deployment", fake_run)
    for mode in ("lan", "cloud", "all"):
        resp = asyncio.run(ui.trigger_verify(mode, _fake_request()))
<<<<<<< HEAD
        assert resp.status_code in (200, 202)
        body = _body(resp)
        assert body["status"] == "triggered"
        assert "verify-run" in body["detail"]
=======
        assert resp.status_code == 202
        assert _body(resp)["flow_run_id"] == "verify-run"
>>>>>>> origin/production
    assert all(name == "integrity-audit/integrity-audit" for name, _, _ in calls)
    assert [params["mode"] for _, params, _ in calls] == ["lan", "cloud", "all"]


def test_refuses_while_backup_running(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(True, True))
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=True))

    async def boom(**kwargs):
        raise AssertionError("arun_deployment must not run while a backup is active")

    monkeypatch.setattr(ui, "arun_deployment", boom)
    resp = asyncio.run(ui.trigger_verify("lan", _fake_request()))
    assert resp.status_code == 400
    assert _body(resp)["status"] == "already_running"


def test_refuses_while_verify_running(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(True, True))
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=True))

    async def boom(**kwargs):
        raise AssertionError("arun_deployment must not run while a verification is active")

    monkeypatch.setattr(ui, "arun_deployment", boom)
    resp = asyncio.run(ui.trigger_verify("all", _fake_request()))
    assert resp.status_code == 400
    assert _body(resp)["status"] == "already_running"


def test_prefect_unreachable_fails_closed(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(True, True))
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=None))
    with pytest.raises(HTTPException, match="Prefect API unavailable"):
        asyncio.run(ui.trigger_verify("lan", _fake_request()))


<<<<<<< HEAD
def test_trigger_creation_failure_is_500(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(True, True))
    _idle(monkeypatch)

    async def boom(**kwargs):
        raise RuntimeError("Prefect deployment not found")

    monkeypatch.setattr(ui, "arun_deployment", boom)
    resp = asyncio.run(ui.trigger_verify("cloud", _fake_request()))
    assert resp.status_code == 500
    assert _body(resp)["status"] == "trigger_failed"
=======
def test_wait_false_passes_zero_timeout(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(True, True))
    _idle(monkeypatch)
    seen = {}

    async def fake_run(name=None, parameters=None, timeout=None):
        seen["timeout"] = timeout
        return SimpleNamespace(id="v", state=SimpleNamespace(type=None, is_final=lambda: False))

    monkeypatch.setattr(ui, "arun_deployment", fake_run)
    resp = asyncio.run(ui.trigger_verify("cloud", _fake_request("wait=false")))
    assert resp.status_code == 202
    assert seen["timeout"] == 0
>>>>>>> origin/production
