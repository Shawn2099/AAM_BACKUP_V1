import os
import shutil
from io import StringIO
from pathlib import Path

import pytest
from loguru import logger

from models.config import CONFIG_PATH, load_config


@pytest.fixture(autouse=True)
def disable_prefect_storage(monkeypatch):
    """Ensure Prefect does not try to use the production result storage block."""
    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    monkeypatch.delenv("PREFECT_RESULTS_DEFAULT_STORAGE_BLOCK", raising=False)
    monkeypatch.setenv("PREFECT_RESULTS_PERSIST_BY_DEFAULT", "false")


def cfg():
    """Load production config."""
    return load_config(str(CONFIG_PATH))


def assert_sandbox_safe(test_root, prod_root) -> None:
    """P0-DATA guard: refuse any sandbox path that touches the production tree.

    Compares RESOLVED and CASEFOLDED absolute paths in BOTH directions so a
    sandbox that equals, sits inside, or contains the production root raises
    AssertionError. resolve() collapses junctions/symlinks on Windows, which
    closes the mklink /J bypass. This exists because test_e2e_real_hardware
    once built its fixture tree INSIDE C:\\BackupData\\FY26-27 and wiped the
    real mirror (incident 2026-08-23, IMPLEMENTATION_FIX_PLAN.md P0-DATA).
    """
    t = str(Path(test_root).resolve()).casefold().rstrip("\\")
    p = str(Path(prod_root).resolve()).casefold().rstrip("\\")
    if t == p:
        raise AssertionError(
            f"Sandbox equals production root: {test_root!r} == {prod_root!r}"
        )
    sep = "\\"
    if t.startswith(p + sep) or p.startswith(t + sep):
        raise AssertionError(
            f"Sandbox and production roots overlap (nesting forbidden): "
            f"sandbox={test_root!r} production={prod_root!r}"
        )


def source_test_dir() -> Path:
    """Return a safe E2E test folder on the local source drive (OUTSIDE prod)."""
    d = Path(cfg().paths.source_drive).parent / "E2E_TEST_SOURCE"
    assert_sandbox_safe(d, cfg().paths.source_drive)
    return d


def nas_test_dir() -> Path:
    """Return a safe E2E test folder on the NAS destination (OUTSIDE prod FY dir)."""
    d = Path(cfg().paths.lan_destination).parent / "E2E_TEST_DEST"
    assert_sandbox_safe(d, cfg().paths.lan_destination)
    return d


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
