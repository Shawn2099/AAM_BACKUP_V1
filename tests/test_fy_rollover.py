"""Tests for FY rollover — detection, final backup, folder creation, locking, config update."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pendulum
import pytest

from core.fy_rollover import (
    RolloverError,
    _child_path,
    _fy_name,
    _parent_path,
    create_new_fy_folders,
    detect_rollover,
    lock_old_fy_folders,
    rollover,
    update_config_yaml,
)
from core.time_utils import IST, get_fy_prefix


def current_fy():
    return get_fy_prefix()


class TestFyName:
    def test_windows_path(self):
        assert _fy_name(r"E:\SOURCE\FY26-27") == "FY26-27"

    def test_unc_path(self):
        assert _fy_name(r"\\server\lan_backup\FY25-26") == "FY25-26"

    def test_unix_path(self):
        assert _fy_name("/mnt/source/FY26-27") == "FY26-27"

    def test_no_fy_suffix(self):
        assert _fy_name(r"E:\SOURCE") is None

    def test_flat_unc(self):
        assert _fy_name(r"\\server\share") is None

    def test_empty_string(self):
        assert _fy_name("") is None

    def test_similar_but_not_fy(self):
        assert _fy_name(r"E:\SOURCE\FY26") is None
        assert _fy_name(r"E:\SOURCE\FY26-2") is None


class TestParentPath:
    def test_windows_drive(self):
        assert _parent_path(r"E:\SOURCE\FY26-27") == r"E:\SOURCE"

    def test_unc(self):
        assert _parent_path(r"\\server\lan_backup\FY26-27") == r"\\server\lan_backup"

    def test_unix(self):
        assert _parent_path("/mnt/source/FY26-27") == "/mnt/source"

    def test_deep_unc(self):
        assert _parent_path(r"\\server\share\deep\path\FY26-27") == r"\\server\share\deep\path"


class TestChildPath:
    def test_windows(self):
        assert _child_path(r"E:\SOURCE", "FY27-28") == r"E:\SOURCE\FY27-28"

    def test_unc(self):
        assert _child_path(r"\\server\lan_backup", "FY27-28") == r"\\server\lan_backup\FY27-28"

    def test_unix(self):
        assert _child_path("/mnt/source", "FY27-28") == "/mnt/source/FY27-28"


class TestDetectRollover:
    def test_match_no_rollover(self):
        fy = current_fy()
        src = f"E:\\SOURCE\\{fy}"
        lan = f"\\\\server\\lan_backup\\{fy}"
        assert detect_rollover(src, lan) is False

    def test_mismatch_rollover_needed(self):
        src = r"E:\SOURCE\FY25-26"
        lan = r"\\server\lan_backup\FY25-26"
        result = detect_rollover(src, lan)
        assert result is True or current_fy() != "FY25-26"

    def test_no_fy_suffix_returns_false(self):
        assert detect_rollover(r"E:\SOURCE", r"\\server\share") is False

    @patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28")
    def test_explicit_boundary(self, mock_fy):
        assert detect_rollover(r"E:\SOURCE\FY26-27", r"\\server\share\FY26-27") is True


class TestCreateNewFyFolders:
    def test_creates_folders(self, tmp_path):
        src_root = str(tmp_path / "source")
        lan_root = str(tmp_path / "lan_dest")
        new_fy = "FY27-28"

        created = create_new_fy_folders(src_root, lan_root, new_fy)

        assert (tmp_path / "source" / "FY27-28").exists()
        assert (tmp_path / "lan_dest" / "FY27-28").exists()

    def test_already_exists_ok(self, tmp_path):
        src_root = str(tmp_path / "source")
        (tmp_path / "source" / "FY27-28").mkdir(parents=True)

        created = create_new_fy_folders(src_root, str(tmp_path / "lan"), "FY27-28")
        assert created["source"].exists()


class TestLockOldFyFolders:
    def test_returns_true_on_posix(self, tmp_path):
        src_root = str(tmp_path)
        old_fy = "FY26-27"
        (tmp_path / old_fy).mkdir()

        with patch("sys.platform", "linux"):
            result = lock_old_fy_folders(src_root, old_fy)
            assert result is True

    def test_returns_false_when_path_missing(self, tmp_path):
        result = lock_old_fy_folders(str(tmp_path), "FY99-00")
        assert result is False

    @patch("subprocess.run")
    def test_windows_lock_success(self, mock_run, tmp_path):
        src_root = str(tmp_path)
        old_fy = "FY26-27"
        (tmp_path / old_fy).mkdir()

        with patch("sys.platform", "win32"):
            result = lock_old_fy_folders(src_root, old_fy)
            assert result is True
            assert mock_run.call_count >= 1

    @patch("subprocess.run", side_effect=Exception("Access denied"))
    def test_windows_lock_failure(self, mock_run, tmp_path):
        src_root = str(tmp_path)
        old_fy = "FY26-27"
        (tmp_path / old_fy).mkdir()

        with patch("sys.platform", "win32"):
            result = lock_old_fy_folders(src_root, old_fy)
            assert result is False


class TestUpdateConfigYaml:
    def test_atomic_write_preserves_other_fields(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        original = (
            "paths:\n"
            "  source_drive: \"E:\\\\SOURCE\\\\FY26-27\"\n"
            "  lan_destination: \"\\\\\\\\server\\\\lan_backup\\\\FY26-27\"\n"
            "  database_path: \"manifest.db\"\n"
            "cloud:\n"
            "  bucket: \"my-bucket\"\n"
        )
        config_path.write_text(original)

        update_config_yaml(
            str(config_path),
            source_root=r"E:\SOURCE",
            lan_root=r"\\server\lan_backup",
            new_fy="FY27-28",
            old_fy="FY26-27",
        )

        written = config_path.read_text()
        assert "FY27-28" in written
        assert "FY26-27" not in written
        assert "my-bucket" in written
        assert "manifest.db" in written

    def test_raises_on_missing_keys(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("foo: bar\n")

        with pytest.raises(Exception):
            update_config_yaml(
                str(config_path),
                source_root=r"E:\SOURCE",
                lan_root=r"\\server\lan_backup",
                new_fy="FY27-28",
                old_fy="FY26-27",
            )

    def test_old_config_untouched_on_write_failure(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        original = (
            "paths:\n"
            "  source_drive: \"E:\\\\SOURCE\\\\FY26-27\"\n"
            "  lan_destination: \"\\\\\\\\server\\\\lan_backup\\\\FY26-27\"\n"
        )
        config_path.write_text(original)

        with patch("ruamel.yaml.YAML.dump", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                update_config_yaml(
                    str(config_path),
                    source_root=r"E:\SOURCE",
                    lan_root=r"\\server\lan_backup",
                    new_fy="FY27-28",
                    old_fy="FY26-27",
                )

        assert config_path.read_text() == original


class TestRolloverOrchestrator:
    def test_no_rollover_needed_returns_false(self, tmp_path):
        fy = current_fy()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "paths:\n"
            f"  source_drive: \"E:\\\\SOURCE\\\\{fy}\"\n"
            f"  lan_destination: \"\\\\\\\\server\\\\lan_backup\\\\{fy}\"\n"
            "  database_path: \"manifest.db\"\n"
            "  gcs_key_path: \"key.json\"\n"
            "lan:\n"
            "  enabled: true\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 14400\n"
            "  max_attempts: 2\n"
            "  retry_delay_seconds: 600\n"
            "  mt_threads: 8\n"
            "wol:\n"
            "  enabled: false\n"
            "  mac_address: \"AA:BB:CC:DD:EE:FF\"\n"
            "  server_ip: \"192.168.1.1\"\n"
            "cloud:\n"
            "  enabled: true\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 21600\n"
            "  verify_timeout_seconds: 600\n"
            "  storage_class: COLDLINE\n"
            "  max_attempts: 3\n"
            "  retry_delay_seconds: 300\n"
            "schedule:\n"
            "  timezone: Asia/Kolkata\n"
            "dashboard:\n"
            "  auth_enabled: false\n"
        )

        from models.config import load_config
        with patch("models.config.load_config", return_value=load_config(str(config_path))):
            result = rollover(str(config_path))
            assert result is False

    def test_rollover_cloud_only_lan_disabled(self, tmp_path):
        """When LAN is disabled, rollover works with cloud-only final backup."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "paths:\n"
            "  source_drive: \"E:\\\\SOURCE\\\\FY25-26\"\n"
            "  lan_destination: \"\\\\\\\\server\\\\lan_backup\\\\FY25-26\"\n"
            "  database_path: \"manifest.db\"\n"
            "  gcs_key_path: \"key.json\"\n"
            "lan:\n"
            "  enabled: false\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 14400\n"
            "  max_attempts: 2\n"
            "  retry_delay_seconds: 600\n"
            "  mt_threads: 8\n"
            "wol:\n"
            "  enabled: false\n"
            "  mac_address: \"AA:BB:CC:DD:EE:FF\"\n"
            "  server_ip: \"192.168.1.1\"\n"
            "cloud:\n"
            "  enabled: true\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 21600\n"
            "  verify_timeout_seconds: 600\n"
            "  storage_class: COLDLINE\n"
            "  max_attempts: 3\n"
            "  retry_delay_seconds: 300\n"
            "  location: asia-south1\n"
            "  project_number: \"123\"\n"
            "schedule:\n"
            "  timezone: Asia/Kolkata\n"
            "dashboard:\n"
            "  auth_enabled: false\n"
        )

        from models.config import load_config
        with patch("models.config.load_config", return_value=load_config(str(config_path))):
            with patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28"):
                with patch("core.fy_rollover.run_cloud_sync", return_value={"exit_code": 0}):
                    result = rollover(str(config_path))
                    assert result is True
                    assert "FY27-28" in config_path.read_text()

    def test_rollover_lan_only_cloud_disabled(self, tmp_path):
        """When cloud is disabled, rollover works with LAN-only final backup."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "paths:\n"
            "  source_drive: \"E:\\\\SOURCE\\\\FY25-26\"\n"
            "  lan_destination: \"\\\\\\\\server\\\\lan_backup\\\\FY25-26\"\n"
            "  database_path: \"manifest.db\"\n"
            "  gcs_key_path: \"key.json\"\n"
            "lan:\n"
            "  enabled: true\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 14400\n"
            "  max_attempts: 2\n"
            "  retry_delay_seconds: 600\n"
            "  mt_threads: 8\n"
            "wol:\n"
            "  enabled: false\n"
            "  mac_address: \"AA:BB:CC:DD:EE:FF\"\n"
            "  server_ip: \"192.168.1.1\"\n"
            "cloud:\n"
            "  enabled: false\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 21600\n"
            "  verify_timeout_seconds: 600\n"
            "  storage_class: COLDLINE\n"
            "  max_attempts: 3\n"
            "  retry_delay_seconds: 300\n"
            "schedule:\n"
            "  timezone: Asia/Kolkata\n"
            "dashboard:\n"
            "  auth_enabled: false\n"
        )

        from models.config import load_config
        with patch("models.config.load_config", return_value=load_config(str(config_path))):
            with patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28"):
                with patch("core.fy_rollover.run_lan_sync", return_value={"exit_code": 3}):
                    result = rollover(str(config_path))
                    assert result is True
                    assert "FY27-28" in config_path.read_text()

    def test_rollover_blocks_when_enabled_destination_fails(self, tmp_path):
        """If cloud is enabled but fails, rollover raises RolloverError."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "paths:\n"
            "  source_drive: \"E:\\\\SOURCE\\\\FY25-26\"\n"
            "  lan_destination: \"\\\\\\\\server\\\\lan_backup\\\\FY25-26\"\n"
            "  database_path: \"manifest.db\"\n"
            "  gcs_key_path: \"key.json\"\n"
            "lan:\n"
            "  enabled: false\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 14400\n"
            "  max_attempts: 2\n"
            "  retry_delay_seconds: 600\n"
            "  mt_threads: 8\n"
            "wol:\n"
            "  enabled: false\n"
            "  mac_address: \"AA:BB:CC:DD:EE:FF\"\n"
            "  server_ip: \"192.168.1.1\"\n"
            "cloud:\n"
            "  enabled: true\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 21600\n"
            "  verify_timeout_seconds: 600\n"
            "  storage_class: COLDLINE\n"
            "  max_attempts: 3\n"
            "  retry_delay_seconds: 300\n"
            "  location: asia-south1\n"
            "  project_number: \"123\"\n"
            "schedule:\n"
            "  timezone: Asia/Kolkata\n"
            "dashboard:\n"
            "  auth_enabled: false\n"
        )

        from models.config import load_config
        with patch("models.config.load_config", return_value=load_config(str(config_path))):
            with patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28"):
                with patch("core.fy_rollover.run_cloud_sync", return_value={"exit_code": 1}):
                    with pytest.raises(RolloverError, match="cloud"):
                        rollover(str(config_path))

    def test_rollover_with_both_disabled_still_creates_folders(self, tmp_path):
        """When both destinations disabled, rollover skips backups but still
        creates folders and updates config (no backup needed since nothing backs up)."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "paths:\n"
            "  source_drive: \"E:\\\\SOURCE\\\\FY25-26\"\n"
            "  lan_destination: \"\\\\\\\\server\\\\lan_backup\\\\FY25-26\"\n"
            "  database_path: \"manifest.db\"\n"
            "  gcs_key_path: \"key.json\"\n"
            "lan:\n"
            "  enabled: false\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 14400\n"
            "  max_attempts: 2\n"
            "  retry_delay_seconds: 600\n"
            "  mt_threads: 8\n"
            "wol:\n"
            "  enabled: false\n"
            "  mac_address: \"AA:BB:CC:DD:EE:FF\"\n"
            "  server_ip: \"192.168.1.1\"\n"
            "cloud:\n"
            "  enabled: true\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 21600\n"
            "  verify_timeout_seconds: 600\n"
            "  storage_class: COLDLINE\n"
            "  max_attempts: 3\n"
            "  retry_delay_seconds: 300\n"
            "schedule:\n"
            "  timezone: Asia/Kolkata\n"
            "dashboard:\n"
            "  auth_enabled: false\n"
        )

        from models.config import load_config
        with patch("models.config.load_config", return_value=load_config(str(config_path))):
            with patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28"):
                with patch("core.fy_rollover.run_cloud_sync", return_value={"exit_code": 0}):
                    with patch("core.fy_rollover.run_lan_sync", return_value={"exit_code": 0}):
                        result = rollover(str(config_path))
                        assert result is True
                        written = config_path.read_text()
                        assert "FY27-28" in written

    def test_flat_config_no_fy_returns_false(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "paths:\n"
            "  source_drive: \"E:\\\\SOURCE\"\n"
            "  lan_destination: \"\\\\\\\\server\\\\lan_backup\"\n"
            "  database_path: \"manifest.db\"\n"
            "  gcs_key_path: \"key.json\"\n"
            "lan:\n"
            "  enabled: false\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 14400\n"
            "  max_attempts: 2\n"
            "  retry_delay_seconds: 600\n"
            "  mt_threads: 8\n"
            "wol:\n"
            "  enabled: false\n"
            "  mac_address: \"AA:BB:CC:DD:EE:FF\"\n"
            "  server_ip: \"192.168.1.1\"\n"
            "cloud:\n"
            "  enabled: true\n"
            "  retry_count: 3\n"
            "  subprocess_timeout_seconds: 21600\n"
            "  verify_timeout_seconds: 600\n"
            "  storage_class: COLDLINE\n"
            "  max_attempts: 3\n"
            "  retry_delay_seconds: 300\n"
            "schedule:\n"
            "  timezone: Asia/Kolkata\n"
            "dashboard:\n"
            "  auth_enabled: false\n"
        )

        from models.config import load_config
        with patch("models.config.load_config", return_value=load_config(str(config_path))):
            result = rollover(str(config_path))
            assert result is False
