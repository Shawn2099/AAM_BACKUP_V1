"""Comprehensive tests for core/rclone_config.py — temp config writer, context manager, cleanup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.rclone_config import temp_rclone_config, write_temp_config

# ═══════════════════════════════════════════════════════════════
# 1. write_temp_config
# ═══════════════════════════════════════════════════════════════

class TestWriteTempConfig:
    """Write temporary rclone config file."""

    def test_valid_inputs_creates_file(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        cfg_path = write_temp_config(
            gcs_key_path=str(key),
            location="asia-south1",
            project_number="123456",
            storage_class="STANDARD",
        )

        assert os.path.exists(cfg_path)
        # Cleanup
        os.unlink(cfg_path)

    def test_config_content_has_rclone_format(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        cfg_path = write_temp_config(
            gcs_key_path=str(key),
            location="asia-south1",
            project_number="123456",
            storage_class="STANDARD",
        )

        content = Path(cfg_path).read_text()
        assert "[aam_gcs]" in content
        assert "type = google cloud storage" in content
        assert "project_number = 123456" in content
        assert "location = asia-south1" in content
        assert "storage_class = STANDARD" in content

        os.unlink(cfg_path)

    def test_key_path_is_absolute(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        cfg_path = write_temp_config(
            gcs_key_path=str(key),
            location="asia-south1",
            project_number="123456",
            storage_class="STANDARD",
        )

        content = Path(cfg_path).read_text()
        # Key path should be absolute (resolved)
        for line in content.splitlines():
            if "service_account_file" in line:
                key_val = line.split("=", 1)[1].strip()
                assert os.path.isabs(key_val)
                break

        os.unlink(cfg_path)

    def test_empty_gcs_key_path(self, tmp_path):
        """Empty gcs_key_path still creates config (validated elsewhere)."""
        cfg_path = write_temp_config(
            gcs_key_path="",
            location="asia-south1",
            project_number="123456",
            storage_class="STANDARD",
        )

        assert os.path.exists(cfg_path)
        os.unlink(cfg_path)

    def test_special_characters_in_location(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        cfg_path = write_temp_config(
            gcs_key_path=str(key),
            location="us-east1",
            project_number="999999",
            storage_class="NEARLINE",
        )

        content = Path(cfg_path).read_text()
        assert "location = us-east1" in content
        assert "storage_class = NEARLINE" in content

        os.unlink(cfg_path)

    def test_config_file_has_suffix(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        cfg_path = write_temp_config(
            gcs_key_path=str(key),
            location="asia-south1",
            project_number="123456",
            storage_class="STANDARD",
        )

        assert cfg_path.endswith(".conf")

        os.unlink(cfg_path)

    def test_strips_whitespace_from_inputs(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        cfg_path = write_temp_config(
            gcs_key_path=str(key),
            location="  asia-south1  ",
            project_number="123456",
            storage_class="  STANDARD  ",
        )

        content = Path(cfg_path).read_text()
        assert "location = asia-south1" in content
        assert "storage_class = STANDARD" in content

        os.unlink(cfg_path)

    def test_invalid_storage_class_raises(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        with pytest.raises(ValueError, match="Invalid storage_class"):
            write_temp_config(
                gcs_key_path=str(key),
                location="asia-south1",
                project_number="123456",
                storage_class="INVALID",
            )

    def test_bucket_policy_only_true(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        cfg_path = write_temp_config(
            gcs_key_path=str(key),
            location="asia-south1",
            project_number="123456",
            storage_class="STANDARD",
        )

        content = Path(cfg_path).read_text()
        assert "bucket_policy_only = true" in content

        os.unlink(cfg_path)

    def test_object_acl_empty(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        cfg_path = write_temp_config(
            gcs_key_path=str(key),
            location="asia-south1",
            project_number="123456",
            storage_class="STANDARD",
        )

        content = Path(cfg_path).read_text()
        assert "object_acl =" in content

        os.unlink(cfg_path)


# ═══════════════════════════════════════════════════════════════
# 2. temp_rclone_config context manager
# ═══════════════════════════════════════════════════════════════

class TestTempRcloneConfig:
    """Context manager: write temp config, yield path, auto-cleanup."""

    def test_yields_config_path(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        with temp_rclone_config(str(key), "asia-south1", "123456", "STANDARD") as path:
            assert os.path.exists(path)

    def test_cleanup_after_use(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        with temp_rclone_config(str(key), "asia-south1", "123456", "STANDARD") as path:
            cfg_path = path

        assert not os.path.exists(cfg_path)

    def test_cleanup_on_exception(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        try:
            with temp_rclone_config(str(key), "asia-south1", "123456", "STANDARD") as path:
                cfg_path = path
                raise ValueError("test error")
        except ValueError:
            pass

        assert not os.path.exists(cfg_path)

    def test_config_content_valid(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')

        with temp_rclone_config(str(key), "asia-south1", "123456", "STANDARD") as path:
            content = Path(path).read_text()
            assert "[aam_gcs]" in content
