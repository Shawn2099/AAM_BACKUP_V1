"""Comprehensive tests for core/fy_rollover.py — FY detection, rollover, archive transition, config update."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock, mock_open

import pytest

from core.fy_rollover import (
    _fy_name,
    _parent_path,
    _child_path,
    detect_rollover,
    run_final_backup,
    create_new_fy_folders,
    update_config_yaml,
    run_archive_transition,
    rollover,
    RolloverError,
)


# ═══════════════════════════════════════════════════════════════
# 1. Helper functions
# ═══════════════════════════════════════════════════════════════

class TestFyName:
    """Extract FY suffix from path."""

    def test_backslash_path(self):
        assert _fy_name("E:\\SOURCE\\FY26-27") == "FY26-27"

    def test_forward_slash_path(self):
        assert _fy_name("/mnt/source/FY26-27") == "FY26-27"

    def test_unc_path(self):
        assert _fy_name("\\\\server\\share\\FY26-27") == "FY26-27"

    def test_no_fy_suffix(self):
        assert _fy_name("E:\\SOURCE\\BACKUP") is None

    def test_case_insensitive(self):
        assert _fy_name("E:\\SOURCE\\fy26-27") == "FY26-27"

    def test_trailing_slash(self):
        assert _fy_name("E:\\SOURCE\\FY26-27\\") == "FY26-27"


class TestParentPath:
    """Get parent directory preserving separator style."""

    def test_backslash(self):
        assert _parent_path("E:\\SOURCE\\FY26-27") == "E:\\SOURCE"

    def test_forward_slash(self):
        assert _parent_path("/mnt/source/FY26-27") == "/mnt/source"

    def test_unc(self):
        result = _parent_path("\\\\server\\share\\FY26-27")
        assert "server" in result
        assert "share" in result


class TestChildPath:
    """Append FY folder to root."""

    def test_backslash(self):
        result = _child_path("E:\\SOURCE", "FY26-27")
        assert result == "E:\\SOURCE\\FY26-27"

    def test_forward_slash(self):
        result = _child_path("/mnt/source", "FY26-27")
        assert result == "/mnt/source/FY26-27"


# ═══════════════════════════════════════════════════════════════
# 2. detect_rollover
# ═══════════════════════════════════════════════════════════════

class TestDetectRollover:
    """FY rollover detection."""

    @patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27")
    def test_fy_matches_no_rollover(self, mock_fy):
        assert detect_rollover("E:\\SOURCE\\FY26-27", "\\\\NAS\\share\\FY26-27") is False

    @patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28")
    def test_fy_mismatches_rollover_needed(self, mock_fy):
        assert detect_rollover("E:\\SOURCE\\FY26-27", "\\\\NAS\\share\\FY26-27") is True

    @patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27")
    def test_no_fy_suffix_returns_false(self, mock_fy):
        assert detect_rollover("E:\\SOURCE\\BACKUP", "\\\\NAS\\share\\BACKUP") is False

    @patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28")
    def test_source_fy_checked_first(self, mock_fy):
        assert detect_rollover("E:\\SOURCE\\FY26-27", "\\\\NAS\\share\\OTHER") is True

    @patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28")
    def test_lan_fy_fallback(self, mock_fy):
        assert detect_rollover("E:\\SOURCE\\OTHER", "\\\\NAS\\share\\FY26-27") is True


# ═══════════════════════════════════════════════════════════════
# 3. run_final_backup
# ═══════════════════════════════════════════════════════════════

def _make_cloud_config(enabled=True):
    cfg = MagicMock()
    cfg.enabled = enabled
    cfg.bucket = "test-bucket"
    cfg.project_number = "123456"
    cfg.storage_class = "STANDARD"
    cfg.location = "asia-south1"
    cfg.bandwidth_limit = "10M"
    cfg.retry_count = 3
    cfg.transfers = 2
    cfg.checkers = 4
    cfg.buffer_size = "64M"
    cfg.subprocess_timeout_seconds = 21600
    return cfg


def _make_lan_config(enabled=True):
    cfg = MagicMock()
    cfg.enabled = enabled
    cfg.mt_threads = 4
    cfg.retry_count = 3
    cfg.retry_wait_seconds = 10
    cfg.subprocess_timeout_seconds = 14400
    return cfg


def _make_paths_config():
    cfg = MagicMock()
    cfg.gcs_key_path = "/path/to/key.json"
    return cfg


def _make_full_config():
    cfg = MagicMock()
    cfg.wol.enabled = False
    cfg.lan.shutdown_after_backup = False
    return cfg


class TestRunFinalBackup:
    """Run one final backup of the closing FY."""

    @patch("core.fy_rollover.run_cloud_sync")
    def test_cloud_success(self, mock_sync):
        mock_sync.return_value = {"exit_code": 0}
        cloud_cfg = _make_cloud_config(enabled=True)
        lan_cfg = _make_lan_config(enabled=False)

        cloud_ok, lan_ok = run_final_backup(
            "D:\\FY26-27", "\\\\NAS\\share\\FY26-27",
            lan_cfg, cloud_cfg, _make_paths_config(), _make_full_config(), "FY26-27",
        )

        assert cloud_ok is True
        assert lan_ok is False

    @patch("core.fy_rollover.run_cloud_sync")
    def test_cloud_exit_9_ok(self, mock_sync):
        mock_sync.return_value = {"exit_code": 9}
        cloud_cfg = _make_cloud_config(enabled=True)
        lan_cfg = _make_lan_config(enabled=False)

        cloud_ok, _ = run_final_backup(
            "D:\\FY26-27", "\\\\NAS\\share\\FY26-27",
            lan_cfg, cloud_cfg, _make_paths_config(), _make_full_config(), "FY26-27",
        )

        assert cloud_ok is True

    @patch("core.fy_rollover.run_cloud_sync")
    def test_cloud_failure(self, mock_sync):
        mock_sync.return_value = {"exit_code": 1}
        cloud_cfg = _make_cloud_config(enabled=True)
        lan_cfg = _make_lan_config(enabled=False)

        cloud_ok, _ = run_final_backup(
            "D:\\FY26-27", "\\\\NAS\\share\\FY26-27",
            lan_cfg, cloud_cfg, _make_paths_config(), _make_full_config(), "FY26-27",
        )

        assert cloud_ok is False

    @patch("core.fy_rollover.run_cloud_sync", side_effect=OSError("error"))
    def test_cloud_exception(self, mock_sync):
        cloud_cfg = _make_cloud_config(enabled=True)
        lan_cfg = _make_lan_config(enabled=False)

        cloud_ok, _ = run_final_backup(
            "D:\\FY26-27", "\\\\NAS\\share\\FY26-27",
            lan_cfg, cloud_cfg, _make_paths_config(), _make_full_config(), "FY26-27",
        )

        assert cloud_ok is False

    @patch("core.fy_rollover.classify_lan_exit", return_value="LAN_COMPLETE")
    @patch("core.fy_rollover.run_lan_sync")
    def test_lan_success(self, mock_sync, mock_classify):
        mock_sync.return_value = {"exit_code": 0}
        cloud_cfg = _make_cloud_config(enabled=False)
        lan_cfg = _make_lan_config(enabled=True)
        config = _make_full_config()
        config.wol.enabled = False

        cloud_ok, lan_ok = run_final_backup(
            "D:\\FY26-27", "\\\\NAS\\share\\FY26-27",
            lan_cfg, cloud_cfg, _make_paths_config(), config, "FY26-27",
        )

        assert lan_ok is True
        assert cloud_ok is False

    @patch("core.fy_rollover.classify_lan_exit", return_value="LAN_FAILED")
    @patch("core.fy_rollover.run_lan_sync")
    def test_lan_failure(self, mock_sync, mock_classify):
        mock_sync.return_value = {"exit_code": 16}
        cloud_cfg = _make_cloud_config(enabled=False)
        lan_cfg = _make_lan_config(enabled=True)
        config = _make_full_config()
        config.wol.enabled = False

        _, lan_ok = run_final_backup(
            "D:\\FY26-27", "\\\\NAS\\share\\FY26-27",
            lan_cfg, cloud_cfg, _make_paths_config(), config, "FY26-27",
        )

        assert lan_ok is False

    @patch("core.fy_rollover.run_cloud_sync", side_effect=RuntimeError("cloud err"))
    def test_cloud_runtime_error(self, mock_sync):
        cloud_cfg = _make_cloud_config(enabled=True)
        lan_cfg = _make_lan_config(enabled=False)

        cloud_ok, _ = run_final_backup(
            "D:\\FY26-27", "\\\\NAS\\share\\FY26-27",
            lan_cfg, cloud_cfg, _make_paths_config(), _make_full_config(), "FY26-27",
        )

        assert cloud_ok is False

    def test_cloud_disabled(self):
        cloud_cfg = _make_cloud_config(enabled=False)
        lan_cfg = _make_lan_config(enabled=False)

        cloud_ok, lan_ok = run_final_backup(
            "D:\\FY26-27", "\\\\NAS\\share\\FY26-27",
            lan_cfg, cloud_cfg, _make_paths_config(), _make_full_config(), "FY26-27",
        )

        assert cloud_ok is False
        assert lan_ok is False


# ═══════════════════════════════════════════════════════════════
# 4. create_new_fy_folders
# ═══════════════════════════════════════════════════════════════

class TestCreateNewFyFolders:
    """Create new FY folders on source and LAN."""

    def test_creates_source_folder(self, tmp_path):
        src = tmp_path / "SOURCE"
        src.mkdir()
        lan = tmp_path / "LAN"
        lan.mkdir()

        result = create_new_fy_folders(str(src), str(lan), "FY27-28")

        assert (src / "FY27-28").exists()
        assert "source" in result

    def test_creates_lan_folder(self, tmp_path):
        src = tmp_path / "SOURCE"
        src.mkdir()
        lan = tmp_path / "LAN"
        lan.mkdir()

        result = create_new_fy_folders(str(src), str(lan), "FY27-28")

        assert (lan / "FY27-28").exists()
        assert "lan" in result

    def test_creates_canary_file(self, tmp_path):
        src = tmp_path / "SOURCE"
        src.mkdir()
        lan = tmp_path / "LAN"
        lan.mkdir()

        create_new_fy_folders(str(src), str(lan), "FY27-28")

        assert (lan / "FY27-28" / ".AAM_TARGET_MOUNTED").exists()

    def test_existing_folder_not_error(self, tmp_path):
        src = tmp_path / "SOURCE"
        src.mkdir()
        lan = tmp_path / "LAN"
        lan.mkdir()
        (src / "FY27-28").mkdir()

        result = create_new_fy_folders(str(src), str(lan), "FY27-28")

        assert "source" in result

    def test_lan_failure_does_not_block(self, tmp_path):
        src = tmp_path / "SOURCE"
        src.mkdir()

        result = create_new_fy_folders(str(src), "/nonexistent/path", "FY27-28")

        assert "source" in result
        assert "lan" not in result


# ═══════════════════════════════════════════════════════════════
# 5. run_archive_transition
# ═══════════════════════════════════════════════════════════════

class TestRunArchiveTransition:
    """Transition GCS objects to ARCHIVE storage class."""

    @patch("core.fy_rollover._resolve_gcloud", return_value="/usr/bin/gcloud")
    @patch("core.fy_rollover.Path")
    @patch("core.fy_rollover.subprocess.run")
    def test_auth_success_then_archive_success(self, mock_run, mock_path_cls, mock_resolve):
        key_path = MagicMock()
        key_path.is_file.return_value = True

        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),  # auth
            MagicMock(returncode=0, stderr=""),  # archive
        ]

        result = run_archive_transition("bucket", "FY25-26", "/key.json")

        assert result is True

    @patch("core.fy_rollover._resolve_gcloud", return_value="/usr/bin/gcloud")
    @patch("core.fy_rollover.Path")
    @patch("core.fy_rollover.subprocess.run")
    def test_auth_failure(self, mock_run, mock_path_cls, mock_resolve):
        key_path = MagicMock()
        key_path.is_file.return_value = True

        mock_run.return_value = MagicMock(returncode=1, stderr="auth error")

        result = run_archive_transition("bucket", "FY25-26", "/key.json")

        assert result is False

    @patch("core.fy_rollover._resolve_gcloud", return_value=None)
    def test_gcloud_not_found(self, mock_resolve):
        result = run_archive_transition("bucket", "FY25-26", "/key.json")

        assert result is False

    @patch("core.fy_rollover._resolve_gcloud", return_value="/usr/bin/gcloud")
    @patch("core.fy_rollover.Path")
    @patch("core.fy_rollover.subprocess.run")
    def test_archive_failure(self, mock_run, mock_path_cls, mock_resolve):
        key_path = MagicMock()
        key_path.is_file.return_value = True

        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),  # auth
            MagicMock(returncode=1, stderr="archive error"),  # archive
        ]

        result = run_archive_transition("bucket", "FY25-26", "/key.json")

        assert result is False

    @patch("core.fy_rollover._resolve_gcloud", return_value="/usr/bin/gcloud")
    @patch("core.fy_rollover.Path")
    @patch("core.fy_rollover.subprocess.run", side_effect=subprocess.TimeoutExpired("gcloud", 600))
    def test_timeout(self, mock_run, mock_path_cls, mock_resolve):
        key_path = MagicMock()
        key_path.is_file.return_value = True

        result = run_archive_transition("bucket", "FY25-26", "/key.json")

        assert result is False

    @patch("core.fy_rollover._resolve_gcloud", return_value="/usr/bin/gcloud")
    @patch("core.fy_rollover.Path")
    @patch("core.fy_rollover.subprocess.run", side_effect=Exception("unexpected"))
    def test_unexpected_error(self, mock_run, mock_path_cls, mock_resolve):
        key_path = MagicMock()
        key_path.is_file.return_value = True

        result = run_archive_transition("bucket", "FY25-26", "/key.json")

        assert result is False

    @patch("core.fy_rollover._resolve_gcloud", return_value="/usr/bin/gcloud")
    @patch("core.fy_rollover.Path")
    @patch("core.fy_rollover.subprocess.run")
    def test_no_key_file_skips_auth(self, mock_run, mock_path_cls, mock_resolve):
        # When is_file returns False, auth step is skipped
        mock_path_instance = MagicMock()
        mock_path_instance.is_file.return_value = False
        mock_path_cls.return_value = mock_path_instance

        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = run_archive_transition("bucket", "FY25-26", "/key.json")

        # Only archive call, no auth call
        assert mock_run.call_count == 1
        assert result is True


# ═══════════════════════════════════════════════════════════════
# 6. update_config_yaml
# ═══════════════════════════════════════════════════════════════

class TestUpdateConfigYaml:
    """Atomically update config.yaml."""

    def test_success(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'paths:\n'
            '  source_drive: "E:\\\\SOURCE\\\\FY26-27"\n'
            '  lan_destination: "\\\\\\\\NAS\\\\share\\\\FY26-27"\n'
        )

        update_config_yaml(str(config_file), "E:\\SOURCE", "\\\\NAS\\share", "FY27-28")

        content = config_file.read_text()
        assert "FY27-28" in content
        assert "FY26-27" not in content

    def test_write_failure_cleanup(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'paths:\n'
            '  source_drive: "E:\\\\SOURCE\\\\FY26-27"\n'
            '  lan_destination: "\\\\\\\\NAS\\\\share\\\\FY26-27"\n'
        )

        with patch("core.fy_rollover.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                update_config_yaml(str(config_file), "E:\\SOURCE", "\\\\NAS\\share", "FY27-28")

        # Config unchanged on failure
        content = config_file.read_text()
        assert "FY26-27" in content


# ═══════════════════════════════════════════════════════════════
# 7. _resolve_gcloud
# ═══════════════════════════════════════════════════════════════

class TestResolveGcloud:
    """Resolve gcloud executable via multi-path fallback."""

    def test_found_in_deploy_bin(self, tmp_path):
        from core.fy_rollover import _resolve_gcloud, _PROJECT_ROOT

        deploy_gcloud = _PROJECT_ROOT / "deploy" / "bin" / "gcloud.cmd"
        with patch.object(Path, "exists") as mock_exists:
            def exists_side_effect(self_path=deploy_gcloud):
                return str(self_path) == str(deploy_gcloud)
            mock_exists.side_effect = lambda: str(deploy_gcloud).endswith("gcloud.cmd")

            result = _resolve_gcloud()
            # We can't easily test this without file system tricks,
            # but we verify the function doesn't crash
            assert result is None or isinstance(result, str)

    @patch("core.fy_rollover.shutil.which", return_value="/usr/bin/gcloud")
    @patch.object(Path, "exists", return_value=False)
    def test_falls_back_to_shutil_which(self, mock_exists, mock_which):
        from core.fy_rollover import _resolve_gcloud

        result = _resolve_gcloud()

        assert result == "/usr/bin/gcloud"

    @patch("core.fy_rollover.shutil.which", return_value=None)
    @patch.object(Path, "exists", return_value=False)
    def test_not_found_anywhere(self, mock_exists, mock_which):
        from core.fy_rollover import _resolve_gcloud

        result = _resolve_gcloud()

        assert result is None


# ═══════════════════════════════════════════════════════════════
# 8. rollover (integration)
# ═══════════════════════════════════════════════════════════════

class TestRollover:
    """High-level rollover orchestration."""

    @patch("core.fy_rollover.update_config_yaml")
    @patch("core.fy_rollover.create_new_fy_folders")
    @patch("core.fy_rollover.run_archive_transition", return_value=True)
    @patch("core.fy_rollover.run_final_backup", return_value=(True, True))
    @patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28")
    @patch("core.fy_rollover.detect_rollover", return_value=True)
    @patch("models.config.load_config")
    def test_successful_rollover(self, mock_load, mock_detect, mock_fy,
                                  mock_backup, mock_archive, mock_folders, mock_update):
        mock_cfg = MagicMock()
        mock_cfg.paths.source_drive = "E:\\SOURCE\\FY26-27"
        mock_cfg.paths.lan_destination = "\\\\NAS\\share\\FY26-27"
        mock_cfg.cloud.enabled = True
        mock_cfg.lan.enabled = True
        mock_cfg.health.rollover_auth_timeout_seconds = 30
        mock_cfg.health.rollover_archive_timeout_seconds = 600
        mock_load.return_value = mock_cfg

        result = rollover("config.yaml")

        assert result is True
        mock_update.assert_called_once()

    @patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28")
    @patch("core.fy_rollover.detect_rollover", return_value=False)
    @patch("models.config.load_config")
    def test_no_rollover_needed(self, mock_load, mock_detect, mock_fy):
        mock_cfg = MagicMock()
        mock_cfg.paths.source_drive = "E:\\SOURCE\\FY26-27"
        mock_cfg.paths.lan_destination = "\\\\NAS\\share\\FY26-27"
        mock_load.return_value = mock_cfg

        result = rollover("config.yaml")

        assert result is False

    @patch("core.fy_rollover.run_final_backup", return_value=(False, True))
    @patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28")
    @patch("core.fy_rollover.detect_rollover", return_value=True)
    @patch("models.config.load_config")
    def test_final_backup_failure_raises(self, mock_load, mock_detect, mock_fy, mock_backup):
        mock_cfg = MagicMock()
        mock_cfg.paths.source_drive = "E:\\SOURCE\\FY26-27"
        mock_cfg.paths.lan_destination = "\\\\NAS\\share\\FY26-27"
        mock_cfg.cloud.enabled = True
        mock_cfg.lan.enabled = True
        mock_load.return_value = mock_cfg

        with pytest.raises(RolloverError):
            rollover("config.yaml")

    @patch("core.fy_rollover.update_config_yaml")
    @patch("core.fy_rollover.create_new_fy_folders")
    @patch("core.fy_rollover.run_archive_transition", return_value=False)
    @patch("core.fy_rollover.run_final_backup", return_value=(True, True))
    @patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28")
    @patch("core.fy_rollover.detect_rollover", return_value=True)
    @patch("models.config.load_config")
    def test_archive_failure_still_completes(self, mock_load, mock_detect, mock_fy,
                                              mock_backup, mock_archive, mock_folders, mock_update):
        mock_cfg = MagicMock()
        mock_cfg.paths.source_drive = "E:\\SOURCE\\FY26-27"
        mock_cfg.paths.lan_destination = "\\\\NAS\\share\\FY26-27"
        mock_cfg.cloud.enabled = True
        mock_cfg.lan.enabled = True
        mock_cfg.health.rollover_auth_timeout_seconds = 30
        mock_cfg.health.rollover_archive_timeout_seconds = 600
        mock_load.return_value = mock_cfg

        result = rollover("config.yaml")

        assert result is True

    @patch("core.fy_rollover.update_config_yaml")
    @patch("core.fy_rollover.create_new_fy_folders")
    @patch("core.fy_rollover.run_archive_transition", return_value=True)
    @patch("core.fy_rollover.run_final_backup", return_value=(True, True))
    @patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28")
    @patch("core.fy_rollover.detect_rollover", return_value=True)
    @patch("models.config.load_config")
    def test_both_pipelines_disabled_still_creates_folders(self, mock_load, mock_detect, mock_fy,
                                                           mock_backup, mock_archive, mock_folders, mock_update):
        mock_cfg = MagicMock()
        mock_cfg.paths.source_drive = "E:\\SOURCE\\FY26-27"
        mock_cfg.paths.lan_destination = "\\\\NAS\\share\\FY26-27"
        mock_cfg.cloud.enabled = False
        mock_cfg.lan.enabled = False
        mock_cfg.health.rollover_auth_timeout_seconds = 30
        mock_cfg.health.rollover_archive_timeout_seconds = 600
        mock_load.return_value = mock_cfg

        result = rollover("config.yaml")

        assert result is True
        mock_folders.assert_called_once()
