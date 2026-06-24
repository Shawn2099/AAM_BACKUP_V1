import os
import shutil
from pathlib import Path
from loguru import logger
import pytest
from io import StringIO

from models.config import load_config, CONFIG_PATH


@pytest.fixture(autouse=True)
def disable_prefect_storage(monkeypatch):
    """Ensure Prefect does not try to use the production result storage block."""
    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    monkeypatch.delenv("PREFECT_RESULTS_DEFAULT_STORAGE_BLOCK", raising=False)
    monkeypatch.setenv("PREFECT_RESULTS_PERSIST_BY_DEFAULT", "false")


def cfg():
    """Load production config."""
    return load_config(str(CONFIG_PATH))


def source_test_dir() -> Path:
    """Return a safe E2E test folder on the local source drive."""
    return Path(cfg().paths.source_drive).parent / "E2E_TEST_SOURCE"


def nas_test_dir() -> Path:
    """Return a safe E2E test folder on the NAS destination."""
    return Path(cfg().paths.lan_destination).parent / "E2E_TEST_DEST"


def make_file(path: Path, size_bytes: int = 1024):
    """Create a file of exact size in bytes with random data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size_bytes))


def clean_test_dirs():
    """Remove E2E test directories from local and NAS."""
    shutil.rmtree(source_test_dir(), ignore_errors=True)
    shutil.rmtree(nas_test_dir(), ignore_errors=True)


import pytest
from io import StringIO

@pytest.fixture
def capture_logs():
    """Capture loguru logs to a string buffer."""
    buf = StringIO()
    handler_id = logger.add(buf, format="{level} | {message}", level="DEBUG")
    yield buf
    logger.remove(handler_id)


def assert_log_contains(captured_buf, keyword: str):
    """Assert that a specific keyword exists in the log output."""
    messages = captured_buf.getvalue()
    assert keyword.lower() in messages.lower(), \
        f"Expected log to contain '{keyword}' but got:\n{messages}"
