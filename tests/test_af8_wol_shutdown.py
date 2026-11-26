"""AF-8: distinguish already_up vs woken; skip shutdown when NAS was already online."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from core import wol
import flow
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, WolConfig


def _cfg(tmp_path, *, shutdown_only_if_woken: bool = True, wol_enabled: bool = True) -> AppConfig:
    src = tmp_path / "FY26-27"
    src.mkdir(exist_ok=True)
    (src / "a.txt").write_text("x", encoding="utf-8")
    return AppConfig(
        paths=PathsConfig(
            source_drive=str(src),
            lan_destination=r"\\192.168.1.50\backup\FY26-27",
            database_path=str(tmp_path / "m.db"),
        ),
        cloud=CloudConfig(enabled=False, bucket="test-bucket", project_number="1"),
        lan=LanConfig(enabled=True, shutdown_after_backup=True,
                      shutdown_only_if_woken=shutdown_only_if_woken),
        wol=WolConfig(enabled=wol_enabled, mac_address="11:22:33:44:55:66",
                      server_ip="192.168.1.50"),
    )


def test_af8_config_default_shutdown_only_if_woken():
    cfg = LanConfig()
    assert cfg.shutdown_only_if_woken is True


def test_af8_ensure_server_online_status_distinguishes_already_up_vs_woken(tmp_path):
    cfg = _cfg(tmp_path)
    with patch("core.wol.wait_for_smb_ready", return_value=MagicMock(ready=True)):
        assert wol.ensure_server_online_status(cfg) == "already_up"

    with patch("core.wol.wait_for_smb_ready", return_value=MagicMock(ready=False)), \
         patch("core.wol._send_magic_packet"), \
         patch("core.wol.wait_for_server"):
        assert wol.ensure_server_online_status(cfg) == "woken"


def test_af8_lan_pipeline_skips_shutdown_when_already_up(tmp_path):
    cfg = _cfg(tmp_path, shutdown_only_if_woken=True)
    with patch("flow._open_run_artefacts", return_value=None), \
         patch("flow._backup_slot", return_value=MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: None)), \
         patch("flow._hold_backup_lock", return_value=MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: None)), \
         patch("flow._record_running"), \
         patch("flow._record_run"), \
         patch("flow.health_check_task"), \
         patch("flow._complete_pending_rollover_nas_folder"), \
         patch("flow.wol_check_task", side_effect=flow.wol_check_task.fn), \
         patch("flow.ensure_server_online_status", return_value="already_up"), \
         patch("flow.lan_preflight_task") as pre, \
         patch("flow.lan_guards_task") as g_task, \
         patch("flow.lan_access_task"), \
         patch("flow.lan_sync_task") as sync, \
         patch("flow.lan_verify_task") as verify, \
         patch("flow.lan_shutdown_task") as shut:
        pre.with_options.return_value.return_value = {"ok": True, "canary_present": True}
        g_task.return_value = {"fy": "FY26-27", "source_file_count": 1, "destination": "D", "results": [], "max_delete": 5}
        sync.with_options.return_value.return_value = {"status": "LAN_COMPLETE", "exit_code": 0}
        verify.return_value = {"verified": True, "matching": 1, "missing": 0, "differ": 0,
                               "changed_during_backup": 0, "reason": None}
        flow._run_lan_pipeline(cfg, "run-already", "2026-10-14T21:00:00")
    shut.assert_not_called()


def test_af8_lan_pipeline_shuts_down_when_already_up_and_setting_false(tmp_path):
    cfg = _cfg(tmp_path, shutdown_only_if_woken=False)
    with patch("flow._open_run_artefacts", return_value=None), \
         patch("flow._backup_slot", return_value=MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: None)), \
         patch("flow._hold_backup_lock", return_value=MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: None)), \
         patch("flow._record_running"), \
         patch("flow._record_run"), \
         patch("flow.health_check_task"), \
         patch("flow._complete_pending_rollover_nas_folder"), \
         patch("flow.wol_check_task", side_effect=flow.wol_check_task.fn), \
         patch("flow.ensure_server_online_status", return_value="already_up"), \
         patch("flow.lan_preflight_task") as pre, \
         patch("flow.lan_guards_task") as g_task, \
         patch("flow.lan_access_task"), \
         patch("flow.lan_sync_task") as sync, \
         patch("flow.lan_verify_task") as verify, \
         patch("flow.lan_shutdown_task") as shut:
        pre.with_options.return_value.return_value = {"ok": True, "canary_present": True}
        g_task.return_value = {"fy": "FY26-27", "source_file_count": 1, "destination": "D", "results": [], "max_delete": 5}
        sync.with_options.return_value.return_value = {"status": "LAN_COMPLETE", "exit_code": 0}
        verify.return_value = {"verified": True, "matching": 1, "missing": 0, "differ": 0,
                               "changed_during_backup": 0, "reason": None}
        flow._run_lan_pipeline(cfg, "run-force", "2026-10-14T21:00:00")
    shut.assert_called_once()
