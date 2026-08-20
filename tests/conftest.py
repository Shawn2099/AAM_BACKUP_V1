"""Shared fixtures for AAM Backup Automation V1 tests."""

import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

# Mock Windows-specific modules for cross-platform test execution
if sys.platform != 'win32':
    sys.modules['msvcrt'] = MagicMock()

# Disable Prefect result persistence at import time so @flow decorators don't fail
os.environ["PREFECT_TEST_MODE"] = "1"
os.environ["PREFECT_RESULTS_PERSIST_BY_DEFAULT"] = "false"
os.environ.pop("PREFECT_RESULTS_DEFAULT_STORAGE_BLOCK", None)
os.environ["PREFECT_SERVER_ANALYTICS_ENABLED"] = "false"

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
def capture_logs():
    """Capture loguru logs to a string buffer."""
    buf = StringIO()
    handler_id = logger.add(buf, format="{level} | {message}", level="DEBUG")
    yield buf
    logger.remove(handler_id)


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
    """Start an ephemeral Prefect in-memory database and API for the duration of tests.

    Gracefully degrades if the Prefect server is unavailable — tests that mock
    all Prefect dependencies will still pass.
    """
    try:
        import tempfile

        from prefect.filesystems import LocalFileSystem
        from prefect.testing.utilities import prefect_test_harness
        
        with prefect_test_harness():
            try:
                # Create the block expected by the production profile inside the ephemeral DB
                LocalFileSystem(basepath=tempfile.gettempdir()).save("backup-storage", overwrite=True)
            except Exception:
                pass
            yield
    except Exception:
        # Prefect server unavailable — tests that mock Prefect will still work
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


@pytest.fixture(scope="session", autouse=True)
def redirect_test_logging():
    """Never let tests configure loguru into the configured log directory.

    Several tests execute real flow code paths with production-derived
    configs; without this guard their configure_logging() calls would create
    loguru file sinks under the configured (production) log directory and
    interleave test noise into production log files.

    The shim is a no-op (adds no sink and never calls logger.remove()):
    loguru's default stderr sink keeps showing logs, no file fds are churned
    (a per-call file sink leaked an atexit "Bad file descriptor" crash), and
    logger.remove() would destroy other tests' capture sinks mid-session.
    """
    import core.logging as core_logging

    def safe_configure(log_dir, log_retention_days=30):
        return None  # deliberately add no file sink in tests

    core_logging._aam_original_configure = core_logging.configure
    core_logging.configure = safe_configure
    patched = []
    try:
        import flow as flow_mod
        flow_mod.configure_logging = safe_configure
        patched.append(flow_mod)
    except Exception:
        pass
    try:
        import core as core_pkg
        core_pkg.configure_logging = safe_configure
        patched.append(core_pkg)
    except Exception:
        pass

    yield

    original = core_logging._aam_original_configure
    core_logging.configure = original
    for mod in patched:
        mod.configure_logging = original
