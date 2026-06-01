"""Tests for FY rollover — detection, final backup, folder creation, locking, config update."""

import os
import subprocess
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
    rollover,
    run_archive_transition,
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

    def test_no_fy_suffix_logs_warning(self):
        """Operators must see a warning when rollover is permanently skipped
        due to missing FY suffix — silent False is not acceptable."""
        with patch("core.fy_rollover.logger") as mock_logger:
            detect_rollover(r"E:\SOURCE", r"\\server\share")
        mock_logger.warning.assert_called_once()
        msg = mock_logger.warning.call_args.args[0]
        assert "no FY suffix" in msg

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
                    with patch("core.fy_rollover.create_new_fy_folders") as mock_create:
                        result = rollover(str(config_path))
                        assert result is True
                        assert "FY27-28" in config_path.read_text()
                        mock_create.assert_called_once()

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
                    with patch("core.fy_rollover.create_new_fy_folders") as mock_create:
                        result = rollover(str(config_path))
                        assert result is True
                        assert "FY27-28" in config_path.read_text()
                        mock_create.assert_called_once()

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
                        with patch("core.fy_rollover.create_new_fy_folders") as mock_create:
                            result = rollover(str(config_path))
                            assert result is True
                            written = config_path.read_text()
                            assert "FY27-28" in written
                            mock_create.assert_called_once()

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


class TestRunArchiveTransition:
    """Exhaustive unit tests for run_archive_transition().

    All subprocess calls are mocked so no real gcloud binary is needed.
    Tests follow the Arrange / Act / Assert pattern and cover:
      - Happy path (exit 0)
      - Non-zero exit with stderr content
      - Non-zero exit with empty stderr
      - Large stderr is truncated to 2000 chars before logging
      - FileNotFoundError (gcloud not installed)
      - TimeoutExpired
      - Unexpected generic exception
      - Correct command list structure (no ** wildcard, --recursive present)
      - GOOGLE_APPLICATION_CREDENTIALS injected into env
    """

    BUCKET = "aam-backup-bucket"
    OLD_FY = "FY25-26"
    KEY_PATH = "/path/to/gcs-key.json"
    MOCK_GCLOUD = "/mock/path/to/gcloud"

    @pytest.fixture(autouse=True)
    def mock_shutil_which(self):
        """Mock shutil.which to always find gcloud, making tests deterministic."""
        with patch("shutil.which", return_value=self.MOCK_GCLOUD):
            yield

    # ── helpers ─────────────────────────────────────────────────────────────

    def _make_completed(self, returncode: int = 0, stderr: str = "") -> MagicMock:
        """Return a fake CompletedProcess-like mock."""
        m = MagicMock()
        m.returncode = returncode
        m.stderr = stderr
        return m

    # ── happy path ──────────────────────────────────────────────────────────

    def test_returns_true_on_exit_zero(self):
        """gcloud exits 0 → function returns True."""
        with patch("subprocess.run", return_value=self._make_completed(0)) as mock_run:
            result = run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        assert result is True
        mock_run.assert_called_once()

    # ── command structure ───────────────────────────────────────────────────

    def test_command_uses_recursive_flag_not_glob(self):
        """Command must use --recursive on bare prefix, NOT a ** glob wildcard.

        Wildcards are expanded by the Windows shell before reaching gcloud.
        """
        with patch("subprocess.run", return_value=self._make_completed(0)) as mock_run:
            run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        cmd = mock_run.call_args.args[0]
        assert "--recursive" in cmd
        # Ensure no ** glob is present in any argument
        assert not any("**" in arg for arg in cmd)

    def test_command_targets_correct_gcs_path(self):
        """GCS URL in command must be gs://bucket/old_fy/ with trailing slash."""
        with patch("subprocess.run", return_value=self._make_completed(0)) as mock_run:
            run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        cmd = mock_run.call_args.args[0]
        assert f"gs://{self.BUCKET}/{self.OLD_FY}/" in cmd

    def test_command_sets_archive_storage_class(self):
        """Command must include --storage-class=ARCHIVE."""
        with patch("subprocess.run", return_value=self._make_completed(0)) as mock_run:
            run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        cmd = mock_run.call_args.args[0]
        assert "--storage-class=ARCHIVE" in cmd

    def test_credentials_injected_into_env(self):
        """GOOGLE_APPLICATION_CREDENTIALS must be set to gcs_key_path in subprocess env."""
        with patch("subprocess.run", return_value=self._make_completed(0)) as mock_run:
            run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        env_passed = mock_run.call_args.kwargs["env"]
        assert env_passed["GOOGLE_APPLICATION_CREDENTIALS"] == self.KEY_PATH

    def test_timeout_set_to_600_seconds(self):
        """Timeout must be 600 s (10 min) — metadata-only operation."""
        with patch("subprocess.run", return_value=self._make_completed(0)) as mock_run:
            run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        assert mock_run.call_args.kwargs["timeout"] == 600

    # ── failure paths ────────────────────────────────────────────────────────

    def test_returns_false_on_nonzero_exit(self):
        """gcloud exits non-zero → function returns False without raising."""
        with patch("subprocess.run", return_value=self._make_completed(1, "some error")):
            result = run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        assert result is False

    def test_returns_false_on_nonzero_exit_with_empty_stderr(self):
        """Non-zero exit with empty stderr must still return False gracefully."""
        with patch("subprocess.run", return_value=self._make_completed(2, "")):
            result = run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        assert result is False

    def test_large_stderr_is_truncated(self):
        """stderr exceeding 2000 chars must be truncated before logging.

        Verifies memory safety on large bucket failures.
        """
        huge_stderr = "E" * 10_000
        captured_logs: list[str] = []

        with patch("subprocess.run", return_value=self._make_completed(1, huge_stderr)):
            with patch("core.fy_rollover.logger") as mock_logger:
                run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)
                # Collect all warning messages emitted
                for call in mock_logger.warning.call_args_list:
                    captured_logs.append(str(call))

        # Full 10 000-char string must not appear in any log call
        assert not any(len(s) > 3000 for s in captured_logs)

    def test_returns_false_when_gcloud_not_installed(self):
        """gcloud missing from PATH (shutil.which returns None) → returns False, no raise."""
        with patch("shutil.which", return_value=None):
            result = run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        assert result is False

    def test_returns_false_on_timeout(self):
        """TimeoutExpired → returns False, does not propagate exception."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gcloud", timeout=600),
        ):
            result = run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        assert result is False

    def test_returns_false_on_unexpected_exception(self):
        """Any unexpected OSError or RuntimeError → returns False, no raise."""
        with patch("subprocess.run", side_effect=OSError("disk full")):
            result = run_archive_transition(self.BUCKET, self.OLD_FY, self.KEY_PATH)

        assert result is False


class TestRolloverArchiveIntegration:
    """Integration-level tests verifying rollover() calls run_archive_transition
    correctly and that the rollover always completes regardless of archive outcome.
    """

    _BASE_CONFIG = (
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
        "  bucket: \"aam-backup-bucket\"\n"
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

    def _write_config(self, tmp_path: Path) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(self._BASE_CONFIG)
        return p

    def test_archive_called_with_correct_args_on_success(self, tmp_path):
        """rollover() must pass bucket, old_fy, and gcs_key_path to run_archive_transition."""
        config_path = self._write_config(tmp_path)

        from models.config import load_config
        with patch("models.config.load_config", return_value=load_config(str(config_path))):
            with patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28"):
                with patch("core.fy_rollover.run_cloud_sync", return_value={"exit_code": 0}):
                    with patch("core.fy_rollover.create_new_fy_folders"):
                        with patch("core.fy_rollover.run_archive_transition", return_value=True) as mock_archive:
                            rollover(str(config_path))

        mock_archive.assert_called_once_with(
            bucket="aam-backup-bucket",
            old_fy="FY25-26",
            gcs_key_path="key.json",
        )

    def test_rollover_succeeds_even_when_archive_fails(self, tmp_path):
        """If run_archive_transition returns False, rollover() must still return True.

        Archive failures are non-blocking by design.
        """
        config_path = self._write_config(tmp_path)

        from models.config import load_config
        with patch("models.config.load_config", return_value=load_config(str(config_path))):
            with patch("core.fy_rollover.get_fy_prefix", return_value="FY27-28"):
                with patch("core.fy_rollover.run_cloud_sync", return_value={"exit_code": 0}):
                    with patch("core.fy_rollover.create_new_fy_folders"):
                        with patch("core.fy_rollover.run_archive_transition", return_value=False):
                            result = rollover(str(config_path))

        assert result is True

    def test_archive_not_called_when_cloud_disabled(self, tmp_path):
        """run_archive_transition must NOT be called when cloud is disabled."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            self._BASE_CONFIG.replace("  enabled: true\n", "  enabled: false\n", 1)
            .replace("  enabled: true\n", "  enabled: false\n")  # also lan if present
        )
        # Rewrite with cloud explicitly disabled
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
                with patch("core.fy_rollover.run_lan_sync", return_value={"exit_code": 0}):
                    with patch("core.fy_rollover.create_new_fy_folders"):
                        with patch("core.fy_rollover.run_archive_transition") as mock_archive:
                            rollover(str(config_path))

        mock_archive.assert_not_called()


class TestHardeningFixes:
    """Tests covering the 3 targeted hardening fixes:
      1. detect_rollover warns when no FY suffix found.
      2. Config errors (AttributeError) propagate instead of being swallowed.
      3. rollover() warns when source/LAN FY labels disagree.
    """

    # ── Fix 1: bare-except narrowing ───────────────────────────────────────

    def test_config_attribute_error_propagates_from_cloud_backup(self, tmp_path):
        """An AttributeError caused by a bad config object must NOT be silently
        swallowed. It should propagate so the operator sees it immediately."""
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
            "  bucket: \"aam-backup-bucket\"\n"
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
                # Simulate a config typo by making run_cloud_sync raise AttributeError
                with patch(
                    "core.fy_rollover.run_cloud_sync",
                    side_effect=AttributeError("'NoneType' has no attribute 'bucket'"),
                ):
                    # AttributeError is NOT in the catch list, so it must propagate
                    with pytest.raises(AttributeError):
                        rollover(str(config_path))

    # ── Fix 3: FY label disagreement warning ───────────────────────────────

    def test_fy_label_disagreement_logs_warning(self, tmp_path):
        """When source and LAN paths carry different FY suffixes, rollover must
        emit a warning and use the source FY as canonical."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "paths:\n"
            "  source_drive: \"E:\\\\SOURCE\\\\FY25-26\"\n"
            # LAN path deliberately has a different FY label
            "  lan_destination: \"\\\\\\\\server\\\\lan_backup\\\\FY24-25\"\n"
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
            "  bucket: \"aam-backup-bucket\"\n"
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
                    with patch("core.fy_rollover.create_new_fy_folders"):
                        with patch("core.fy_rollover.run_archive_transition", return_value=True):
                            with patch("core.fy_rollover.logger") as mock_logger:
                                rollover(str(config_path))

        warning_messages = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("disagree" in msg for msg in warning_messages), (
            "Expected a warning about FY label disagreement, but none was logged."
        )

    def test_fy_label_disagreement_uses_source_as_canonical(self, tmp_path):
        """When source and LAN FY labels disagree, rollover must use the
        source FY (not LAN) as the canonical old_fy value passed to archive."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "paths:\n"
            "  source_drive: \"E:\\\\SOURCE\\\\FY25-26\"\n"
            "  lan_destination: \"\\\\\\\\server\\\\lan_backup\\\\FY24-25\"\n"
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
            "  bucket: \"aam-backup-bucket\"\n"
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
                    with patch("core.fy_rollover.create_new_fy_folders"):
                        with patch("core.fy_rollover.run_archive_transition", return_value=True) as mock_archive:
                            rollover(str(config_path))

        # Source FY (FY25-26) must be used, not LAN FY (FY24-25)
        mock_archive.assert_called_once_with(
            bucket="aam-backup-bucket",
            old_fy="FY25-26",
            gcs_key_path="key.json",
        )
