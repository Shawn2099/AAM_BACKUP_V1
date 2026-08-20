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


# ── S2-20: rollover-test safety guard ───────────────────────────────────────

def live_rollover_path_set() -> set:
    """S2-20: the production paths a rollover test must never touch — the
    live source/lan destinations from config.yaml and the current-FY folders
    derived from them (what a real rollover would target if the live FY went
    stale)."""
    from core.fy_rollover import _child_path, _fy_name, _parent_path
    from core.time_utils import get_fy_prefix

    cfg = load_config(str(CONFIG_PATH))
    paths = set()
    for base in (cfg.paths.source_drive, cfg.paths.lan_destination):
        paths.add(os.path.normcase(os.path.normpath(base)))
        if _fy_name(base):
            paths.add(os.path.normcase(os.path.normpath(
                _child_path(_parent_path(base), get_fy_prefix())
            )))
    return paths


def assert_safe_rollover_targets(candidates, live_paths) -> None:
    """S2-20: refuse loudly (AssertionError) if any rollover-test path
    matches a live production path, case-insensitively.

    The original test_fy_07 built its scenario under the live config's
    parents and its finally block rmtree'd the rollover TARGET — which on
    this machine resolved to E:\\FY26-27 (the live source dataset) and the
    NAS FY26-27 folder: a test that destroys production data on success.
    """
    norm_live = {os.path.normcase(os.path.normpath(p)) for p in live_paths}
    bad = [c for c in candidates
           if os.path.normcase(os.path.normpath(c)) in norm_live]
    assert not bad, (
        "S2-20 SAFETY ABORT: rollover test target(s) match live production "
        f"path(s): {bad} — refusing to run (would destroy production data)"
    )
