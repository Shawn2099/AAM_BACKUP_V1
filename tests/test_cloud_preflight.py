"""Tests for core/cloud_preflight.py — run_cloud_dry_run().

Covers: Probe A (source drive), Probe B (GCS auth), error paths, timeout,
FileNotFoundError, OSError, empty source, and command construction.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.cloud_preflight import run_cloud_dry_run


class TestProbeA:
    """Probe A — source drive alive check."""

    def test_source_not_accessible(self, tmp_path):
        """Source drive does not exist → immediate failure."""
        missing = str(tmp_path / "nonexistent_drive")
        result = run_cloud_dry_run(
            source=missing,
            bucket="test-bucket",
            fy_prefix="FY26-27",
            gcs_key_path="key.json",
            project_number="123",
            storage_class="STANDARD",
        )
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "not accessible" in result["error"]

    def test_source_empty_dir_warns_but_continues(self, tmp_path):
        """Empty source dir → warning logged, but Probe B still runs."""
        empty_dir = tmp_path / "empty_source"
        empty_dir.mkdir()

        with patch("core.cloud_preflight.temp_rclone_config") as mock_cfg, \
             patch("core.cloud_preflight.resolve_binary", return_value="rclone"), \
             patch("core.cloud_preflight.subprocess.run") as mock_run:
            mock_cfg.return_value.__enter__ = MagicMock(return_value="/tmp/config")
            mock_cfg.return_value.__exit__ = MagicMock(return_value=False)
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = run_cloud_dry_run(
                source=str(empty_dir),
                bucket="test-bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
            )
            assert result["ok"] is True

    def test_source_read_error(self, tmp_path):
        """OSError during iterdir → failure."""
        source = tmp_path / "source"
        source.mkdir()

        with patch("core.cloud_preflight.temp_rclone_config") as mock_cfg, \
             patch("core.cloud_preflight.resolve_binary", return_value="rclone"), \
             patch("core.cloud_preflight.subprocess.run") as mock_run, \
             patch.object(Path, "iterdir", side_effect=OSError("Access denied")):
            mock_cfg.return_value.__enter__ = MagicMock(return_value="/tmp/config")
            mock_cfg.return_value.__exit__ = MagicMock(return_value=False)
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = run_cloud_dry_run(
                source=str(source),
                bucket="test-bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
            )
            assert result["ok"] is False
            assert "read error" in result["error"]


class TestProbeB:
    """Probe B — GCS auth + bucket probe."""

    def test_success(self, tmp_path):
        """Both probes pass → ok=True."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")

        with patch("core.cloud_preflight.temp_rclone_config") as mock_cfg, \
             patch("core.cloud_preflight.resolve_binary", return_value="rclone"), \
             patch("core.cloud_preflight.subprocess.run") as mock_run:
            mock_cfg.return_value.__enter__ = MagicMock(return_value="/tmp/config")
            mock_cfg.return_value.__exit__ = MagicMock(return_value=False)
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = run_cloud_dry_run(
                source=str(source),
                bucket="test-bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
            )
            assert result["ok"] is True
            assert result["exit_code"] == 0
            assert result["error"] is None

    def test_gcs_nonzero_exit(self, tmp_path):
        """rclone exits non-zero → failure with stderr in error."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")

        with patch("core.cloud_preflight.temp_rclone_config") as mock_cfg, \
             patch("core.cloud_preflight.resolve_binary", return_value="rclone"), \
             patch("core.cloud_preflight.subprocess.run") as mock_run:
            mock_cfg.return_value.__enter__ = MagicMock(return_value="/tmp/config")
            mock_cfg.return_value.__exit__ = MagicMock(return_value=False)
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="Couldn't find bucket"
            )

            result = run_cloud_dry_run(
                source=str(source),
                bucket="test-bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
            )
            assert result["ok"] is False
            assert result["exit_code"] == 1
            assert "Couldn't find bucket" in result["error"]

    def test_gcs_timeout(self, tmp_path):
        """subprocess.TimeoutExpired → failure with timeout message."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")

        with patch("core.cloud_preflight.temp_rclone_config") as mock_cfg, \
             patch("core.cloud_preflight.resolve_binary", return_value="rclone"), \
             patch("core.cloud_preflight.subprocess.run") as mock_run:
            mock_cfg.return_value.__enter__ = MagicMock(return_value="/tmp/config")
            mock_cfg.return_value.__exit__ = MagicMock(return_value=False)
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=30)

            result = run_cloud_dry_run(
                source=str(source),
                bucket="test-bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
                timeout=30,
            )
            assert result["ok"] is False
            assert result["error"] == "Timeout after 30s"

    def test_rclone_not_found(self, tmp_path):
        """FileNotFoundError → rclone not found message."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")

        with patch("core.cloud_preflight.temp_rclone_config") as mock_cfg, \
             patch("core.cloud_preflight.resolve_binary", return_value="rclone"), \
             patch("core.cloud_preflight.subprocess.run") as mock_run:
            mock_cfg.return_value.__enter__ = MagicMock(return_value="/tmp/config")
            mock_cfg.return_value.__exit__ = MagicMock(return_value=False)
            mock_run.side_effect = FileNotFoundError("rclone not found")

            result = run_cloud_dry_run(
                source=str(source),
                bucket="test-bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
            )
            assert result["ok"] is False
            assert "rclone not found" in result["error"]

    def test_os_error_launching_rclone(self, tmp_path):
        """OSError during subprocess.run → failure."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")

        with patch("core.cloud_preflight.temp_rclone_config") as mock_cfg, \
             patch("core.cloud_preflight.resolve_binary", return_value="rclone"), \
             patch("core.cloud_preflight.subprocess.run") as mock_run:
            mock_cfg.return_value.__enter__ = MagicMock(return_value="/tmp/config")
            mock_cfg.return_value.__exit__ = MagicMock(return_value=False)
            mock_run.side_effect = OSError("Access is denied")

            result = run_cloud_dry_run(
                source=str(source),
                bucket="test-bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
            )
            assert result["ok"] is False
            assert "OS error" in result["error"]


class TestCommandConstruction:
    """Verify the rclone command is built correctly."""

    def test_command_flags(self, tmp_path):
        """Command includes --max-depth 0, --retries 2, --config."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")

        with patch("core.cloud_preflight.temp_rclone_config") as mock_cfg, \
             patch("core.cloud_preflight.resolve_binary", return_value="rclone"), \
             patch("core.cloud_preflight.subprocess.run") as mock_run:
            mock_cfg.return_value.__enter__ = MagicMock(return_value="/tmp/config")
            mock_cfg.return_value.__exit__ = MagicMock(return_value=False)
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            run_cloud_dry_run(
                source=str(source),
                bucket="my-bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
            )

            cmd = mock_run.call_args[0][0]
            assert "lsjson" in cmd
            assert "--max-depth" in cmd
            assert "0" in cmd
            assert "--retries" in cmd
            assert "2" in cmd
            assert "--config" in cmd
            assert "/tmp/config" in cmd
            # --gcs-no-check-bucket must be ABSENT (forces IAM check)
            assert "--gcs-no-check-bucket" not in cmd

    def test_dest_format(self, tmp_path):
        """Destination is aam_gcs:{bucket}/{fy_prefix}."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")

        with patch("core.cloud_preflight.temp_rclone_config") as mock_cfg, \
             patch("core.cloud_preflight.resolve_binary", return_value="rclone"), \
             patch("core.cloud_preflight.subprocess.run") as mock_run:
            mock_cfg.return_value.__enter__ = MagicMock(return_value="/tmp/config")
            mock_cfg.return_value.__exit__ = MagicMock(return_value=False)
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            run_cloud_dry_run(
                source=str(source),
                bucket="my-bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
            )

            cmd = mock_run.call_args[0][0]
            assert "aam_gcs:my-bucket/FY26-27" in cmd
