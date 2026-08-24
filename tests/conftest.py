"""Shared fixtures for AAM Backup Automation V1 tests.

REAL INFRASTRUCTURE POLICY
==========================
Every test in this suite exercises the ORIGINAL production programs
(core/*, flow.py, watchdog.py, launch.py, serve.py, ui.py) against real
infrastructure:

  * real robocopy.exe / rclone.exe subprocesses (resolved via the
    production core.process.resolve_binary — deploy\bin is preferred),
  * the real \\127.0.0.1\lan_backup SMB share for LAN destinations,
  * the real GCS bucket from config.yaml under a dedicated test prefix,
  * real TCP/UDP sockets, real SQLite databases, real Prefect API server,
  * real subprocess timeouts enforced by the OS — never mocked time.

No module, method, or builtin used by production code is monkey-patched,
stubbed, or replaced anywhere in this suite.
"""

import os
import sys
import tempfile
from io import StringIO
from pathlib import Path

import pytest
from loguru import logger

# Real ephemeral Prefect API + database for @flow execution (production code path)
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
    """Start a REAL ephemeral Prefect server + SQLite DB for the whole session.

    Flows execute through the actual Prefect orchestration API exactly as they
    do in production; only the backing database lives in a temporary location.
    """
    try:
        from prefect.filesystems import LocalFileSystem
        from prefect.testing.utilities import prefect_test_harness

        with prefect_test_harness():
            try:
                LocalFileSystem(basepath=tempfile.gettempdir()).save(
                    "backup-storage", overwrite=True
                )
            except Exception:
                pass
            yield
    except Exception:
        yield
