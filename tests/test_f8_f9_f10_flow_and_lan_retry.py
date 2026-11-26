"""F8, F9, F10: failure/overlap NAS shutdown, copy-only keeps GuardBlocked, LAN stall retryable."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import flow
from core import fy_overlap, guards, lan_sync
from models.config import load_config


def test_f8_failure_path_shutdown_respects_shutdown_only_if_woken():
    cfg = load_config()
    cfg.lan.enabled = True
    cfg.lan.shutdown_after_backup = True
    cfg.lan.shutdown_only_if_woken = True
    cfg.lan.shutdown_on_all_retries_exhausted = True

    with (
        patch.object(flow, "_backup_slot") as slot,
        patch.object(flow, "_record_run"),
        patch.object(flow, "_send_run_alert"),
        patch.object(flow, "_finalize_run_artefacts"),
        patch.object(flow, "_open_run_artefacts", return_value=None),
        patch.object(flow, "health_check_task", return_value={"verified": True}),
        patch.object(flow, "wol_check_task", return_value="already_up"),
        patch.object(
            flow,
            "lan_preflight_task",
            MagicMock(**{"with_options.return_value": MagicMock(side_effect=RuntimeError("smb down"))}),
        ),
        patch.object(flow, "lan_shutdown_task") as shutdown_mock,
    ):
        slot.return_value.__enter__ = MagicMock()
        slot.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(RuntimeError, match="smb down"):
            flow._run_lan_pipeline(
                cfg, "r-f8a", "2026-11-26T21:01:00+05:30", time.monotonic(),
                allow_shutdown=True,
            )
    shutdown_mock.assert_not_called()


def test_f8_overlap_last_pass_skipped_still_shuts_down_woken_nas():
    cfg = load_config()
    cfg.cloud.enabled = False
    cfg.lan.enabled = True
    cfg.lan.shutdown_after_backup = True
    cfg.lan.shutdown_only_if_woken = True

    p1 = SimpleNamespace(fy="FY26-27", config=cfg, is_next_fy=False)
    p2 = SimpleNamespace(fy="FY27-28", config=cfg, is_next_fy=True)
    calls = []

    def fake_lan_pipeline(*args, **kwargs):
        if kwargs.get("wake_nas"):
            kwargs["wake_nas"]()
        calls.append(kwargs.get("allow_shutdown"))
        return {"status": "LAN_COMPLETE"}

    with (
        patch.object(flow, "load_config", return_value=cfg),
        patch.object(fy_overlap, "active_passes", return_value=[p1, p2]),
        patch.object(flow, "_switched_fy", side_effect=[None, "FY27-28"]),
        patch.object(flow, "wol_check_task", return_value="woken"),
        patch.object(flow, "_run_lan_pipeline", side_effect=fake_lan_pipeline),
        patch.object(flow, "lan_shutdown_task", return_value={"ok": True}) as shutdown_mock,
        patch.object(flow, "_open_run_artefacts", return_value=None),
        patch.object(flow, "_finalize_run_artefacts"),
    ):
        with pytest.raises(Exception):
            flow.backup.fn("lan")

    assert calls == [False]
    shutdown_mock.assert_called_once()


def _g3_only_guard_blocked():
    r = guards.GuardResult(
        guard="G3_MASS_DELETE",
        verdict=guards.BLOCK,
        reason="too many deletes",
        metrics={"planned_deletes": 500},
    )
    record = {"verdict": guards.BLOCK, "results": [r.__dict__], "max_delete": 10}
    return guards.GuardBlocked("G3 blocked", record=record)


def test_f9_cloud_copy_only_keeps_guard_blocked_when_access_raises():
    cfg = load_config()
    cfg.cloud.enabled = True
    gb = _g3_only_guard_blocked()

    with (
        patch.object(flow, "_backup_slot") as slot,
        patch.object(flow, "_record_run"),
        patch.object(flow, "_send_run_alert"),
        patch.object(flow, "_finalize_run_artefacts"),
        patch.object(flow, "_open_run_artefacts", return_value=None),
        patch.object(flow, "health_check_task", return_value={"verified": True}),
        patch.object(flow, "cloud_preflight_task"),
        patch.object(flow, "cloud_guards_task", side_effect=gb),
        patch.object(flow, "cloud_access_task", side_effect=RuntimeError("access probe failed")),
    ):
        slot.return_value.__enter__ = MagicMock()
        slot.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(guards.GuardBlocked):
            flow._run_cloud_pipeline(
                cfg, "r-f9c", "2026-11-26T02:01:00+05:30", time.monotonic(),
            )
    assert "access probe failed" in (gb.record.get("copy_only", {}).get("error") or "")


def test_f9_lan_copy_only_keeps_guard_blocked_when_access_raises():
    cfg = load_config()
    cfg.lan.enabled = True
    gb = _g3_only_guard_blocked()

    with (
        patch.object(flow, "_backup_slot") as slot,
        patch.object(flow, "_record_run"),
        patch.object(flow, "_send_run_alert"),
        patch.object(flow, "_finalize_run_artefacts"),
        patch.object(flow, "_open_run_artefacts", return_value=None),
        patch.object(flow, "health_check_task", return_value={"verified": True}),
        patch.object(flow, "wol_check_task", return_value="woken"),
        patch.object(flow, "lan_preflight_task", return_value={"planned_deletes": 500}),
        patch.object(flow, "lan_guards_task", side_effect=gb),
        patch.object(flow, "lan_access_task", side_effect=RuntimeError("nas full")),
    ):
        slot.return_value.__enter__ = MagicMock()
        slot.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(guards.GuardBlocked):
            flow._run_lan_pipeline(
                cfg, "r-f9l", "2026-11-26T21:01:00+05:30", time.monotonic(),
            )
    assert "nas full" in (gb.record.get("copy_only", {}).get("error") or "")


def test_f10_lan_stall_not_retryable_without_stats_or_on_max_delete(tmp_path):
    cfg = load_config().lan
    # 1. Stalled with no stats -> retryable must be False
    log_empty = tmp_path / "empty.jsonl"
    log_empty.write_text("", encoding="utf-8")
    tr_no_stats = SimpleNamespace(
        spawn_error=None, stalled=True, timed_out=True, exit_code=None,
        log_path=log_empty, log_text=lambda: "", stderr_text=lambda: "",
        describe_failure=lambda: "stalled",
    )
    art = tmp_path / "art"
    art.mkdir()
    with patch.object(lan_sync, "run_tool", return_value=tr_no_stats):
        res_no_stats = lan_sync.run_lan_mirror(cfg, str(tmp_path), r"\\nas\share\FY26-27", str(art), max_delete=10)
    assert res_no_stats["stalled"] is True
    assert res_no_stats["retryable"] is False

    # 2. Stalled with stats and no fatalError/max_delete -> retryable is True
    stats_line = json.dumps({"level": "info", "msg": "stats", "stats": {"deletes": 3, "transfers": 1}})
    log_stats = tmp_path / "stats.jsonl"
    log_stats.write_text(stats_line + "\n", encoding="utf-8")
    tr_with_stats = SimpleNamespace(
        spawn_error=None, stalled=True, timed_out=True, exit_code=None,
        log_path=log_stats, log_text=lambda: stats_line + "\n", stderr_text=lambda: "",
        describe_failure=lambda: "stalled",
    )
    with patch.object(lan_sync, "run_tool", return_value=tr_with_stats):
        res_with_stats = lan_sync.run_lan_mirror(cfg, str(tmp_path), r"\\nas\share\FY26-27", str(art), max_delete=10)
    assert res_with_stats["retryable"] is True
    assert res_with_stats["stats"]["deletes"] == 3
