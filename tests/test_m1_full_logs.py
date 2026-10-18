"""M1 1.5 / WP2 — full run logs in the pipelines + retention housekeeping."""

import json
import os
import time
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

import flow
from models.config import AppConfig

UNC = "\\\\nas\\backup\\FY26-27"


def _cfg(tmp_path, **run_logs) -> AppConfig:
    src = tmp_path / "src" / "FY26-27"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("x")
    return AppConfig(
        paths={"source_drive": str(src), "lan_destination": UNC,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={},
        notifications={"send_on_failure": False},
        run_logs=run_logs or {},
    )


def _age(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_log_retention_purges_only_expired(tmp_path):
    cfg = _cfg(tmp_path, retention_days=90, service_log_retention_days=30)
    runs = cfg.run_logs_dir
    old_day, new_day = runs / "2026-01-01", runs / "2026-10-13"
    (old_day / "r1").mkdir(parents=True)
    (new_day / "r2").mkdir(parents=True)
    _age(old_day, 91)
    logs = Path(cfg.paths.log_directory)
    logs.mkdir(parents=True, exist_ok=True)
    svc_old, svc_new = logs / "agent_svc-20260101T000000.log", logs / "watchdog_svc-20261013T000000.log"
    app_log = logs / "backup_2026-01-01.log"  # loguru owns app logs: never touched here
    for f in (svc_old, svc_new, app_log):
        f.write_text("x")
    _age(svc_old, 31)
    _age(app_log, 400)

    removed = flow.log_retention_task.fn(cfg)

    assert removed == {"run_logs": 1, "service_logs": 1}
    assert not old_day.exists() and new_day.exists()
    assert not svc_old.exists() and svc_new.exists()
    assert app_log.exists()


def test_rollover_check_runs_retention_even_when_rollover_blocked(tmp_path):
    from core.fy_rollover import RolloverError

    cfg = _cfg(tmp_path)
    # configured FY25-26 but the clock says FY24-25 -> rollover refused (blocked)
    cfg.paths.source_drive = "E:\\DATA\\FY25-26"
    with patch.object(flow, "load_config", return_value=cfg), \
         patch.object(flow, "log_retention_task") as retention, \
         patch.object(flow, "get_fy_prefix", return_value="FY24-25"), \
         patch.object(flow, "_record_run"), patch.object(flow, "notify"):
        with pytest.raises(RolloverError):              # clock behind -> refused
            flow.rollover_check_flow.fn("cfg.yaml")
    retention.assert_called_once_with(cfg)


def test_pipeline_records_artefacts_dir_and_finalizes(tmp_path):
    """The run row's extended_metrics carries the run-log folder, also on failure."""
    cfg = _cfg(tmp_path)
    recorded = {}

    def fake_record(*args, **kwargs):
        recorded.update(kwargs)

    sync_mock = MagicMock()
    sync_mock.with_options.return_value = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(flow, "health_check_task"), \
         patch.object(flow, "wol_check_task"), \
         patch.object(flow, "lan_preflight_task", MagicMock()), \
         patch.object(flow, "lan_snapshot_before_task", return_value={}), \
         patch.object(flow, "lan_guards_task", MagicMock()), \
         patch.object(flow, "lan_access_task", MagicMock()), \
         patch.object(flow, "lan_sync_task", sync_mock), \
         patch.object(flow, "_record_run", side_effect=fake_record):
        with pytest.raises(RuntimeError):
            flow._run_lan_pipeline(cfg, "run-art-lan", "2026-10-14T01:00:00+05:30")

    metrics = json.loads(recorded["extended_metrics"])
    art_dir = Path(metrics["artefacts_dir"])
    assert art_dir.is_relative_to(cfg.run_logs_dir)
    assert (art_dir / "manifest.json").exists()
    manifest = json.loads((art_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["notes"][-1]["text"] == "finalized"
    # the sync task received the folder path (Prefect-safe plain string)
    sync_mock.with_options.return_value.assert_called_once_with(cfg, str(art_dir), budget=ANY)


def test_artefact_folder_failure_never_blocks_the_backup(tmp_path):
    cfg = _cfg(tmp_path)
    with patch.object(flow.RunArtefacts, "for_config", side_effect=PermissionError("denied")):
        assert flow._open_run_artefacts(cfg, "r") is None
    assert flow._with_artefacts('{"a": 1}', None) == '{"a": 1}'
