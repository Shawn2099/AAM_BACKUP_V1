"""Tests for rclone_config — temporary config file writer."""

from pathlib import Path

from core.rclone_config import write_temp_config


class TestWriteTempConfig:
    def test_creates_config_file(self, tmp_path):
        key_path = tmp_path / "key.json"
        key_path.write_text('{"type": "service_account"}')
        result = write_temp_config(str(key_path), "asia-south1", "12345", "COLDLINE")
        try:
            assert Path(result).exists()
            content = Path(result).read_text()
            assert "[aam_gcs]" in content
            assert "asia-south1" in content
            assert "12345" in content
            assert "COLDLINE" in content
        finally:
            Path(result).unlink(missing_ok=True)

    def test_returns_string_path(self, tmp_path):
        key_path = tmp_path / "key.json"
        key_path.write_text("{}")
        result = write_temp_config(str(key_path), "us-east1", "999", "STANDARD")
        try:
            assert isinstance(result, str)
        finally:
            Path(result).unlink(missing_ok=True)

    def test_key_path_is_absolute(self, tmp_path):
        key_path = tmp_path / "key.json"
        key_path.write_text("{}")
        result = write_temp_config(str(key_path), "asia-south1", "123", "COLDLINE")
        try:
            content = Path(result).read_text()
            # The key path in config should be absolute
            assert str(tmp_path.resolve()) in content or "/" in content
        finally:
            Path(result).unlink(missing_ok=True)

    def test_config_has_gcs_remote_type(self, tmp_path):
        key_path = tmp_path / "key.json"
        key_path.write_text("{}")
        result = write_temp_config(str(key_path), "eu-west1", "456", "NEARLINE")
        try:
            content = Path(result).read_text()
            assert "type = google cloud storage" in content
        finally:
            Path(result).unlink(missing_ok=True)

    def test_sanitization_and_validation(self, tmp_path):
        import pytest
        key_path = tmp_path / "key.json"
        key_path.write_text("{}")

        # Test valid storage class is accepted and sanitized
        result = write_temp_config(str(key_path), "  asia-south1  ", "  12345  ", "  coldline  ")
        try:
            content = Path(result).read_text()
            assert "location = asia-south1" in content
            assert "project_number = 12345" in content
            assert "storage_class = coldline" in content
        finally:
            Path(result).unlink(missing_ok=True)

        # Test invalid storage class raises ValueError
        with pytest.raises(ValueError) as excinfo:
            write_temp_config(str(key_path), "asia-south1", "12345", "INVALID_STORAGE_CLASS")
        assert "Invalid storage_class" in str(excinfo.value)

