# tests/test_f2_trigger_remediation.py
r"""F2 remediation regression tests: bounded, truthful deployment trigger.

Contract under test (evidence: C:\ChaosTest\evidence\F2_F4_LONG_HORIZON_VALIDATION.md,
F2_F4_LONG_HORIZON_FINAL_REPORT.md; verified live on the isolated rig):

  * TRIGGER_WAIT_SECONDS = 180 — the sync wait is BOUNDED (named constant).
  * ``?wait=false`` (also 0/no/off) -> ``arun_deployment(timeout=0)`` ->
    immediate 202 ``running`` + authoritative ``flow_run_id`` (SCHEDULED snapshot).
  * Terminal states are reported truthfully:
      COMPLETED -> 200 ``completed``
      FAILED / CRASHED -> 500 ``failed``
      CANCELLED (final) -> 409 ``cancelled``
  * Non-terminal at the bound -> 202 ``running`` + ``flow_run_id``; the run is
    NEVER cancelled by the timeout (Prefect 3.7.2 semantics, verified E02).
  * G15 preserved: deployment lookup happens before run creation; a creation
    error -> 500 ``trigger_failed`` ("Could not start ..."), never a bogus 200.
  * Guard ``_prefect_has_active_run``:
      - RUNNING / PENDING matching runs -> True
      - SCHEDULED counted ONLY when due-now (<= now + 60s skew); future cron
        SCHEDULED runs never block manual triggers (observed: cron runs exist
        days ahead on Prefect 3.7.2)
      - SCHEDULED with missing due-time, or an unrecognized state on a
        matching run -> True (fail-closed: refusing is safe, duplicating is not)
      - API unreachable -> None (tri-state; trigger endpoints refuse with 503)
  * Dashboard contract preserved: 2xx responses carry a ``detail`` string;
    ``flow_run_id`` is always present in trigger responses.

All Prefect interactions are stubbed — no API server, no deployments, no
network. Endpoints are invoked as async functions (repo convention: sync test
functions wrapping asyncio.run; no pytest-asyncio dependency).
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from prefect.client.schemas.objects import StateType
from starlette.requests import Request

import ui

FINAL_TYPES = (StateType.COMPLETED, StateType.FAILED, StateType.CRASHED, StateType.CANCELLED)


# ── helpers ──────────────────────────────────────────────────────────────────

def _mk_state(state_type, name=None):
    if state_type is None:
        return None
    return SimpleNamespace(
        type=state_type,
        name=name or state_type.name.title(),
        is_final=lambda st=state_type: st in FINAL_TYPES,
    )


def _mk_run(state_type, mode="cloud", tags=None, next_scheduled_start_time=None, run_id="11111111-2222-3333-4444-555555555555"):
    return SimpleNamespace(
        id=run_id,
        tags=tags if tags is not None else [],
        parameters={"mode": mode},
        state=_mk_state(state_type),
        state_type=state_type,
        next_scheduled_start_time=next_scheduled_start_time,
    )


class _FakeClient:
    """Minimal stand-in for the Prefect orchestration client context manager."""

    def __init__(self, runs=None, exc=None):
        self._runs = runs or []
        self._exc = exc
        self.filters = []
        self.limits = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read_flow_runs(self, flow_run_filter=None, limit=None):
        self.filters.append(flow_run_filter)
        self.limits.append(limit)
        if self._exc is not None:
            raise self._exc
        return self._runs


def _fake_request(query: str = "") -> Request:
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


def _body(resp):
    return json.loads(resp.body)


# ── bounded wait constant + ?wait= handling ─────────────────────────────────

def test_trigger_wait_constant_is_180_seconds():
    # audit 2026-10-18: a config key now (dashboard.trigger_wait_seconds)
    from models.config import DashboardConfig
    assert DashboardConfig(auth_enabled=False).trigger_wait_seconds == 180


@pytest.mark.parametrize("query", ["wait=false", "wait=False", "wait=0", "wait=no", "wait=off"])
def test_trigger_timeout_wait_false_variants_return_zero(query):
    assert ui._trigger_timeout(_fake_request(query)) == 0


@pytest.mark.parametrize("query", ["", "wait=true", "wait=yes", "wait=anything"])
def test_trigger_timeout_default_is_the_180s_bound(query):
    assert ui._trigger_timeout(_fake_request(query)) == ui._cfg().dashboard.trigger_wait_seconds == 180


# ── truthful state mapping (_trigger_result) ─────────────────────────────────

def test_mapping_completed_is_200_completed():
    resp = ui._trigger_result("Cloud", _mk_run(StateType.COMPLETED))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["status"] == "completed"
    assert body["flow_run_id"] == "11111111-2222-3333-4444-555555555555"
    assert "detail" in body and body["detail"]


def test_mapping_failed_is_500_failed():
    resp = ui._trigger_result("LAN", _mk_run(StateType.FAILED))
    assert resp.status_code == 500
    body = _body(resp)
    assert body["status"] == "failed"
    assert body["flow_run_id"]


def test_mapping_crashed_is_500_failed_truthful_state_name():
    resp = ui._trigger_result("Cloud", _mk_run(StateType.CRASHED))
    assert resp.status_code == 500
    body = _body(resp)
    assert body["status"] == "failed"
    assert "Crashed" in body["detail"]
    assert body["flow_run_id"]


def test_mapping_cancelled_is_409_cancelled():
    resp = ui._trigger_result("Cloud", _mk_run(StateType.CANCELLED))
    assert resp.status_code == 409
    body = _body(resp)
    assert body["status"] == "cancelled"
    assert body["flow_run_id"]


@pytest.mark.parametrize("state_type", [StateType.RUNNING, StateType.PENDING, StateType.SCHEDULED])
def test_mapping_nonterminal_states_are_202_running(state_type):
    resp = ui._trigger_result("Cloud", _mk_run(state_type))
    assert resp.status_code == 202
    body = _body(resp)
    assert body["status"] == "running"
    assert body["flow_run_id"]
    assert "detail" in body and body["detail"]


def test_mapping_missing_state_is_202_running():
    resp = ui._trigger_result("Cloud", _mk_run(None))
    assert resp.status_code == 202
    assert _body(resp)["status"] == "running"


def test_mapping_never_fabricates_success():
    """A FAILED/CRASHED run must never produce a 2xx 'completed'/'triggered'."""
    for bad in (StateType.FAILED, StateType.CRASHED):
        resp = ui._trigger_result("Cloud", _mk_run(bad))
        assert resp.status_code >= 400
        assert _body(resp)["status"] not in ("completed", "triggered")


# ── duplicate-run guard (_prefect_has_active_run) ───────────────────────────

def _run_guard(monkeypatch, runs, pipeline="cloud", exc=None):
    fake = _FakeClient(runs=runs, exc=exc)
    monkeypatch.setattr(ui, "get_client", lambda: fake)
    result = asyncio.run(ui._prefect_has_active_run(pipeline))
    return result, fake


def test_guard_running_run_is_active(monkeypatch):
    result, _ = _run_guard(monkeypatch, [_mk_run(StateType.RUNNING, mode="cloud")])
    assert result is True


def test_guard_pending_run_is_active(monkeypatch):
    result, _ = _run_guard(monkeypatch, [_mk_run(StateType.PENDING, mode="lan")], pipeline="lan")
    assert result is True


def test_guard_scheduled_due_now_is_active(monkeypatch):
    """A just-triggered run is SCHEDULED until the runner polls it up (~10s)."""
    now = datetime.now(timezone.utc)
    runs = [
        _mk_run(StateType.SCHEDULED, mode="cloud", next_scheduled_start_time=now - timedelta(seconds=5)),
        _mk_run(StateType.SCHEDULED, mode="cloud", next_scheduled_start_time=now + timedelta(seconds=30)),
    ]
    result, _ = _run_guard(monkeypatch, runs)
    assert result is True


def test_guard_scheduled_far_future_is_ignored(monkeypatch):
    """Cron SCHEDULED runs exist days ahead (observed on 3.7.2) — must not block triggers."""
    now = datetime.now(timezone.utc)
    runs = [_mk_run(StateType.SCHEDULED, mode="cloud", next_scheduled_start_time=now + timedelta(days=1))]
    result, _ = _run_guard(monkeypatch, runs)
    assert result is False


def test_guard_scheduled_missing_due_time_is_fail_closed(monkeypatch):
    result, _ = _run_guard(monkeypatch, [_mk_run(StateType.SCHEDULED, mode="cloud", next_scheduled_start_time=None)])
    assert result is True


def test_guard_unknown_state_is_fail_closed(monkeypatch):
    """H3: an unrecognized state on a matching run reads as active, never idle."""
    run = _mk_run(None, mode="cloud")
    run.state = SimpleNamespace(type="SOMETHING_NEW", name="Weird", is_final=lambda: False)
    run.state_type = "SOMETHING_NEW"
    result, _ = _run_guard(monkeypatch, [run])
    assert result is True


def test_guard_nonmatching_pipeline_is_idle(monkeypatch):
    result, _ = _run_guard(monkeypatch, [_mk_run(StateType.RUNNING, mode="lan")], pipeline="cloud")
    assert result is False


def test_guard_integrity_audit_never_hijacks_status(monkeypatch):
    run = _mk_run(StateType.RUNNING, mode="all", tags=["maintenance", "integrity"])
    result, _ = _run_guard(monkeypatch, [run])
    assert result is False


def test_guard_no_active_runs_is_idle(monkeypatch):
    result, _ = _run_guard(monkeypatch, [])
    assert result is False


def test_guard_api_unreachable_returns_none_tri_state(monkeypatch):
    result, _ = _run_guard(monkeypatch, [], exc=ConnectionError("api down"))
    assert result is None


def test_guard_query_filter_includes_scheduled_with_limit(monkeypatch):
    _, fake = _run_guard(monkeypatch, [])
    assert fake.limits and fake.limits[0] == 200
    any_ = fake.filters[0].state.type.any_
    assert StateType.SCHEDULED in any_
    assert StateType.RUNNING in any_
    assert StateType.PENDING in any_


# ── endpoint behaviour (trigger_cloud / trigger_lan) ─────────────────────────

def test_endpoint_wait_false_returns_202_with_run_id(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    scheduled_snapshot = _mk_run(StateType.SCHEDULED, mode="cloud")
    calls = {}

    async def fake_arun_deployment(name, timeout=None):
        calls["name"] = name
        calls["timeout"] = timeout
        return scheduled_snapshot

    monkeypatch.setattr(ui, "arun_deployment", fake_arun_deployment)
    resp = asyncio.run(ui.trigger_cloud(_fake_request("wait=false")))
    assert resp.status_code == 202
    body = _body(resp)
    assert body["status"] == "running"
    assert body["flow_run_id"] == str(scheduled_snapshot.id)
    assert calls["name"] == "aam-backup/backup-cloud"
    assert calls["timeout"] == 0  # wait=false -> immediate SCHEDULED snapshot


def test_endpoint_default_wait_passes_180s_bound(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    calls = {}

    async def fake_arun_deployment(name, timeout=None):
        calls["timeout"] = timeout
        return _mk_run(StateType.RUNNING, mode="cloud")

    monkeypatch.setattr(ui, "arun_deployment", fake_arun_deployment)
    resp = asyncio.run(ui.trigger_lan(_fake_request()))
    assert resp.status_code == 202
    assert calls["timeout"] == 180


def test_endpoint_completed_run_is_200_completed(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "arun_deployment", AsyncMock(return_value=_mk_run(StateType.COMPLETED, mode="lan")))
    resp = asyncio.run(ui.trigger_lan(_fake_request()))
    assert resp.status_code == 200
    assert _body(resp)["status"] == "completed"


def test_endpoint_failed_run_is_500_failed_not_triggered(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "arun_deployment", AsyncMock(return_value=_mk_run(StateType.FAILED, mode="cloud")))
    resp = asyncio.run(ui.trigger_cloud(_fake_request()))
    assert resp.status_code == 500
    body = _body(resp)
    assert body["status"] == "failed"
    assert body["flow_run_id"]  # truthful id even on failure


def test_endpoint_running_guard_returns_400_without_creating_run(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=True))

    async def boom(name, timeout=None):
        raise AssertionError("arun_deployment must not be called when guard is active")

    monkeypatch.setattr(ui, "arun_deployment", boom)
    resp = asyncio.run(ui.trigger_cloud(_fake_request()))
    assert resp.status_code == 400
    assert _body(resp)["status"] == "already_running"


def test_endpoint_api_unreachable_refuses_with_503(monkeypatch):
    """H3: guard None (API down) -> fail-closed 503, never a duplicate trigger."""
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=None))

    async def boom(name, timeout=None):
        raise AssertionError("arun_deployment must not be called when guard is unknown")

    monkeypatch.setattr(ui, "arun_deployment", boom)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(ui.trigger_cloud(_fake_request()))
    assert exc_info.value.status_code == 503


def test_endpoint_creation_failure_is_500_trigger_failed(monkeypatch):
    """G15: missing deployment / creation error -> 500 'Could not start', never 200."""
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))

    async def fail(name, timeout=None):
        raise RuntimeError("ObjectNotFound: deployment aam-backup/backup-cloud not found")

    monkeypatch.setattr(ui, "arun_deployment", fail)
    resp = asyncio.run(ui.trigger_cloud(_fake_request()))
    assert resp.status_code == 500
    body = _body(resp)
    assert body["status"] == "trigger_failed"
    assert "Could not start" in body["detail"]
