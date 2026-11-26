"""F5 + F6: UI Prefect queries never crowd out RUNNING/PENDING with SCHEDULED; install script enables vacuum."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import ui
from prefect.client.schemas.objects import StateType


def _fake_client(calls_log: list):
    client = MagicMock()

    async def fake_read_flow_runs(*, flow_run_filter=None, sort=None, limit=None):
        types = list(flow_run_filter.state.type.any_)
        calls_log.append(types)
        return []

    client.read_flow_runs = AsyncMock(side_effect=fake_read_flow_runs)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def test_f5_read_active_runs_queries_running_pending_separately_from_scheduled():
    calls = []
    with patch.object(ui, "get_client", side_effect=lambda: _fake_client(calls)):
        asyncio.run(ui._read_active_runs())
    assert len(calls) >= 2
    assert set(calls[0]) == {StateType.RUNNING, StateType.PENDING}
    assert set(calls[1]) == {StateType.SCHEDULED}


def test_f5_is_verify_running_queries_running_pending_separately():
    calls = []
    with patch.object(ui, "get_client", side_effect=lambda: _fake_client(calls)):
        asyncio.run(ui._is_verify_running())
    assert len(calls) >= 2
    assert set(calls[0]) == {StateType.RUNNING, StateType.PENDING}


def test_f5_prefect_busy_reason_queries_running_pending_separately():
    calls = []
    with patch.object(ui, "get_client", side_effect=lambda: _fake_client(calls)):
        asyncio.run(ui._prefect_busy_reason(600))
    assert len(calls) >= 2
    assert set(calls[0]) == {StateType.RUNNING, StateType.PENDING}


def test_f6_install_script_enables_prefect_db_vacuum():
    ps1 = Path("deploy/06_install_services.ps1").read_text(encoding="utf-8")
    assert "PREFECT_SERVER_SERVICES_DB_VACUUM_ENABLED=True" in ps1
