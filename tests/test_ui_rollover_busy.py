"""Review I3: a running FY rollover (flow run tagged `rollover`) refuses manual backups, safety checks and
approved re-runs, like any running backup does."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import ui


def _req() -> Request:
    return Request({"type": "http", "query_string": b"", "client": ("127.0.0.1", 5), "headers": []})


@pytest.fixture
def rollover_running(monkeypatch):
    monkeypatch.setattr(ui, "_require_auth", lambda request: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_running", AsyncMock(side_effect=lambda p: p == "rollover"))

    async def boom(*a, **k):
        raise AssertionError("nothing may start while the rollover runs")

    monkeypatch.setattr(ui, "arun_deployment", boom)


def test_manual_backup_is_refused_while_the_rollover_runs(rollover_running):
    resp = asyncio.run(ui._refuse_if_busy("lan"))
    assert resp is not None and resp.status_code == 400
    assert b"rollover" in resp.body.lower()


def test_approved_rerun_is_refused_while_the_rollover_runs(rollover_running):
    with pytest.raises(HTTPException) as e:
        asyncio.run(ui._ensure_leg_idle("lan"))
    assert e.value.status_code == 409


def test_safety_check_is_refused_while_the_rollover_runs(rollover_running, monkeypatch):
    monkeypatch.setattr(ui, "_requested_fy", lambda request: None)
    resp = asyncio.run(ui.trigger_verify("lan", _req()))
    assert resp.status_code == 400
