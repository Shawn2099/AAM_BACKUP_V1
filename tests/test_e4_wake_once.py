"""Regression tests for WS-E E4 (P6): WoL/wake once per LAN flow across FY overlap passes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import flow


def _fake_config(*, shutdown_after_backup: bool = True):
    return SimpleNamespace(
        firm_name="Test Firm",
        paths=SimpleNamespace(
            source_drive="X:/src/FY90-91",
            lan_destination="Y:/nas/FY90-91",
            log_directory="X:/logs",
        ),
        cloud=SimpleNamespace(enabled=False),
        lan=SimpleNamespace(
            enabled=True,
            shutdown_after_backup=shutdown_after_backup,
            shutdown_on_all_retries_exhausted=False,
        ),
        budget=SimpleNamespace(lan_max_hours=8.0, lan_post_reserve_minutes=30),
        maintenance=SimpleNamespace(log_retention_days=90),
    )


def _pass(fy: str, cfg, *, extra: bool):
    return SimpleNamespace(fy=fy, config=cfg, extra=extra)


def test_er4_two_fy_overlap_wakes_nas_once_and_shuts_down_once(monkeypatch):
    cfg1 = _fake_config()
    cfg2 = _fake_config()
    passes = [_pass("FY90-91", cfg1, extra=False), _pass("FY91-92", cfg2, extra=True)]

    monkeypatch.setattr(flow, "load_config", lambda p=None: cfg1)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow, "configure_prefect_bridge", lambda: None)
    monkeypatch.setattr(flow, "_scheduled_lateness_seconds", lambda: 0.0)
    monkeypatch.setattr(flow, "_switched_fy", lambda *a, **k: None)
    monkeypatch.setattr(flow.fy_overlap, "active_passes", lambda c, fn: passes)

    wake_calls = []
    shut_calls = []
    pipeline_calls = []

    def fake_wol(c):
        wake_calls.append(c)
        return {"nas_online": True}

    def fake_shut(c, artefacts_dir=None):
        shut_calls.append(c)
        return {"shutdown": True}

    def fake_pipeline(c, run_id, started_at, monotonic_start=None, **kw):
        pipeline_calls.append((kw.get("fy_pass"), kw.get("allow_shutdown", True)))
        wake_fn = kw.get("wake_nas")
        if wake_fn is not None:
            wake_fn()
        else:
            flow.wol_check_task(c)
        if kw.get("allow_shutdown", True):
            flow.lan_shutdown_task(c, None)
        return {"status": "LAN_COMPLETE"}

    monkeypatch.setattr(flow, "wol_check_task", fake_wol)
    monkeypatch.setattr(flow, "lan_shutdown_task", fake_shut)
    monkeypatch.setattr(flow, "_run_lan_pipeline", fake_pipeline)

    flow.backup.fn(mode="lan")

    assert len(pipeline_calls) == 2
    assert len(wake_calls) == 1, f"expected 1 wake across 2 passes, got {len(wake_calls)}"
    assert len(shut_calls) == 1, f"expected 1 shutdown after last pass, got {len(shut_calls)}"


def test_er4_single_fy_wakes_once(monkeypatch):
    cfg = _fake_config()
    monkeypatch.setattr(flow, "load_config", lambda p=None: cfg)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow, "configure_prefect_bridge", lambda: None)
    monkeypatch.setattr(flow, "_scheduled_lateness_seconds", lambda: 0.0)
    monkeypatch.setattr(flow.fy_overlap, "active_passes", lambda c, fn: [_pass("FY90-91", cfg, extra=False)])

    wake_calls = []
    monkeypatch.setattr(flow, "wol_check_task", lambda c: wake_calls.append(c) or {"nas_online": True})

    def fake_pipeline(c, run_id, started_at, monotonic_start=None, **kw):
        wake_fn = kw.get("wake_nas")
        if wake_fn is not None:
            wake_fn()
        else:
            flow.wol_check_task(c)
        return {"status": "LAN_COMPLETE"}

    monkeypatch.setattr(flow, "_run_lan_pipeline", fake_pipeline)
    flow.backup.fn(mode="lan")
    assert len(wake_calls) == 1


def test_er4_failed_wake_on_pass1_fails_both_passes_without_retrying_wake(monkeypatch):
    cfg1 = _fake_config()
    cfg2 = _fake_config()
    passes = [_pass("FY90-91", cfg1, extra=False), _pass("FY91-92", cfg2, extra=True)]

    monkeypatch.setattr(flow, "load_config", lambda p=None: cfg1)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow, "configure_prefect_bridge", lambda: None)
    monkeypatch.setattr(flow, "_scheduled_lateness_seconds", lambda: 0.0)
    monkeypatch.setattr(flow, "_switched_fy", lambda *a, **k: None)
    monkeypatch.setattr(flow.fy_overlap, "active_passes", lambda c, fn: passes)

    wake_calls = []

    def boom_wol(c):
        wake_calls.append(c)
        raise RuntimeError("NAS did not wake")

    def fake_pipeline(c, run_id, started_at, monotonic_start=None, **kw):
        wake_fn = kw.get("wake_nas")
        if wake_fn is not None:
            wake_fn()
        else:
            flow.wol_check_task(c)
        return {"status": "LAN_COMPLETE"}

    monkeypatch.setattr(flow, "wol_check_task", boom_wol)
    monkeypatch.setattr(flow, "_run_lan_pipeline", fake_pipeline)

    with pytest.raises(ExceptionGroup) as exc_info:
        flow.backup.fn(mode="lan")

    assert len(exc_info.value.exceptions) == 2
    assert all("NAS did not wake" in str(e) for e in exc_info.value.exceptions)
    assert len(wake_calls) == 1
