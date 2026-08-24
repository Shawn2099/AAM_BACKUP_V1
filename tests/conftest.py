"""Shared fixtures for AAM Backup Automation V1 tests.

REAL INFRASTRUCTURE POLICY
==========================
Every test in this suite exercises the ORIGINAL production programs
(core/*, flow.py, watchdog.py, launch.py, serve.py, ui.py) against real
infrastructure:

  * real robocopy.exe / rclone.exe subprocesses (resolved via the
    production core.process.resolve_binary - deploy\\bin is preferred),
  * the real \\\\127.0.0.1\\lan_backup SMB share for LAN destinations,
  * the real GCS bucket from config.yaml under a dedicated test prefix,
  * real TCP/UDP sockets, real SQLite databases, real Prefect API server,
  * real subprocess timeouts enforced by the OS - never mocked time.

No module, method, or builtin used by production code is monkey-patched,
stubbed, or replaced anywhere in this suite.
"""

import os
import tempfile
from io import StringIO
from pathlib import Path

import pytest
from loguru import logger

os.environ["PREFECT_TEST_MODE"] = "1"
os.environ["PREFECT_RESULTS_PERSIST_BY_DEFAULT"] = "false"
os.environ.pop("PREFECT_RESULTS_DEFAULT_STORAGE_BLOCK", None)
os.environ["PREFECT_SERVER_ANALYTICS_ENABLED"] = "false"


def pytest_configure(config):
    import logging
    logging.getLogger("prefect.server").setLevel(logging.ERROR)


@pytest.fixture
def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_aam_")
    os.close(fd)
    yield path
    try:
        Path(path).unlink()
    except OSError:
        pass


@pytest.fixture
def capture_logs():
    buf = StringIO()
    handler_id = logger.add(buf, format="{level} | {message}", level="DEBUG")
    yield buf
    logger.remove(handler_id)


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory(prefix="test_aam_") as d:
        yield Path(d)


@pytest.fixture(scope="session")
def gcs_sandbox():
    """REAL GCS environment: production bucket, dedicated AAM_PYTEST_FY prefix,
    created and cleaned up by real rclone runs."""
    from core.rclone_config import temp_rclone_config

    key_path = Path(__file__).parent.parent / (
        "deploy/keys/aam-demo-gcs-083e65c0184f.json"
    )
    params = dict(
        gcs_key_path=str(key_path),
        location="asia-south1",
        project_number="aam-demo-gcs",
        storage_class="STANDARD",
    )
    yield dict(bucket="aam-backup-demo-innovizta", fy_prefix="AAM_PYTEST_FY",
               **params)

    import subprocess
    from core.process import resolve_binary

    with temp_rclone_config(**params) as cfg_path:
        subprocess.run(
            [resolve_binary("rclone") or "rclone", "delete",
             "aam_gcs:aam-backup-demo-innovizta/AAM_PYTEST_FY",
             "--config", cfg_path, "--rmdirs"],
            capture_output=True, timeout=600,
        )


@pytest.fixture(scope="session", autouse=True)
def prefect_harness():
    """REAL ephemeral Prefect server + SQLite DB for the whole session."""
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
