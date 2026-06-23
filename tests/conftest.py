"""Shared fixtures for AAM Backup Automation V1 tests."""

import tempfile
from pathlib import Path

import pytest


def pytest_configure(config):
    """Global pytest configuration."""
    import logging
    # Suppress noisy teardown logs from the ephemeral Prefect server
    logging.getLogger("prefect.server").setLevel(logging.ERROR)

@pytest.fixture
def temp_db_path():
    """Create a temporary SQLite database path, cleaned up after test."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_aam_")
    import os
    os.close(fd)
    yield path
    try:
        Path(path).unlink()
    except OSError:
        pass


@pytest.fixture
def temp_dir():
    """Create a temporary directory, cleaned up after test."""
    with tempfile.TemporaryDirectory(prefix="test_aam_") as d:
        yield Path(d)


@pytest.fixture
def sample_yaml_config():
    """Return minimal valid YAML config string for testing."""
    return """firm_name: "TestFirm"
paths:
  source_drive: "C:\\\\test_source"
  lan_destination: "\\\\\\\\10.0.0.1\\\\share"
  database_path: "C:\\\\test\\\\manifest.db"
  log_directory: "C:\\\\test\\\\logs"
  temp_directory: "C:\\\\test\\\\temp"
  gcs_key_path: "C:\\\\test\\\\key.json"
lan:
  enabled: true
  retry_count: 3
  retry_wait_seconds: 10
  subprocess_timeout_seconds: 14400
  shutdown_after_backup: true
  max_attempts: 2
  retry_delay_seconds: 600
  mt_threads: 8
wol:
  enabled: true
  mac_address: "AA-BB-CC-DD-EE-FF"
  server_ip: "10.0.0.1"
  wake_timeout_seconds: 300
  ping_interval_seconds: 15
  stability_wait_seconds: 30
cloud:
  enabled: true
  bucket: "test-bucket"
  project_number: "123456"
  location: "asia-south1"
  storage_class: "COLDLINE"
  bandwidth_limit: "10M"
  retry_count: 3
  subprocess_timeout_seconds: 21600
  max_attempts: 3
  retry_delay_seconds: 300
  verify_timeout_seconds: 600
  transfers: 4
  checkers: 16
schedule:
  cloud_cron: "0 18 * * *"
  lan_cron: "0 1 * * *"
  weekly_cron: "0 8 * * MON"
  monthly_cron: "0 8 1 * *"
  timezone: "Asia/Kolkata"
dashboard:
  auth_enabled: true
  api_key: "test-key-123"
  bind_address: "127.0.0.1"
  port: 8080
notifications:
  smtp_host: ""
  smtp_port: 587
  smtp_username: ""
  smtp_password: ""
  sender: ""
  recipients: []
  send_on_failure: false
"""


@pytest.fixture(scope="session", autouse=True)
def prefect_harness():
    """Start an ephemeral Prefect in-memory database and API for the duration of tests."""
    from prefect.testing.utilities import prefect_test_harness
    with prefect_test_harness():
        yield


@pytest.fixture(autouse=True)
def prevent_mock_db_leaks(monkeypatch):
    """Prevent ManifestDB from creating SQLite files named after MagicMock objects."""
    try:
        from core.manifest import ManifestDB
        original_init = ManifestDB.__init__
        
        def patched_init(self, db_path, *args, **kwargs):
            if "MagicMock" in str(db_path):
                db_path = ":memory:"
            return original_init(self, db_path, *args, **kwargs)
            
        monkeypatch.setattr("core.manifest.ManifestDB.__init__", patched_init)
    except ImportError:
        pass
