# tests/conftest.py
import os
from pathlib import Path

import pytest

os.environ["PREFECT_TEST_MODE"] = "1"

# WP16 test isolation: every CONFIG_PATH default must resolve to a throwaway
# sandbox config, never the real config.yaml. Must run before any project
# module (models.config computes CONFIG_PATH at import) is imported.
from test_support import sandbox_config  # noqa: E402

sandbox_config.install()

# BUG-10: Inject deploy\bin into PATH so bundled rclone.exe is discovered by tests
deploy_bin = Path(__file__).resolve().parent.parent / "deploy" / "bin"
if deploy_bin.exists() and str(deploy_bin) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{deploy_bin};{os.environ.get('PATH', '')}"


@pytest.fixture
def tmp_db(tmp_path):
    """Fresh ManifestDB in a temp directory."""
    from core.manifest import ManifestDB

    db = ManifestDB(str(tmp_path / "test_manifest.db"))
    yield db
    db.close()


@pytest.fixture
def populated_db(tmp_db):
    """Database pre-populated with files synced to both cloud and lan."""
    tmp_db.bulk_upsert_synced(
        [{"path": "docs/client_report.pdf", "size": 2048, "mtime": 1700000000.0}],
        mode="lan",
    )
    tmp_db.bulk_upsert_synced(
        [{"path": "docs/client_report.pdf", "size": 2048, "mtime": 1700000000.0}],
        mode="cloud",
    )
    return tmp_db


@pytest.fixture(autouse=True)
def _reset_loguru_sinks():
    """Isolate loguru between tests.

    core.logging.configure() (and some tests) add a sink bound to the
    sys.stderr object of the *current* test; pytest closes that capture
    stream afterwards, so a later test's log call hit a closed file
    ("OSError: [Errno 9] Bad file descriptor") depending on test order.
    After every test, drop all sinks and re-add one that resolves
    sys.stderr at write time.
    """
    import sys

    from loguru import logger

    yield
    logger.remove()
    logger.add(lambda message: sys.stderr.write(message), level="INFO")


@pytest.fixture(autouse=True)
def _guard_standard_fds():
    """Fail any test that closes the process's stdin/stdout/stderr fds.

    A mocked mkstemp returning fd 0 made production code os.close(0),
    breaking unrelated later tests. Detect it at the source instead.
    """
    import os

    def _open_fds():
        alive = set()
        for fd in (0, 1, 2):
            try:
                os.fstat(fd)
                alive.add(fd)
            except OSError:
                pass
        return alive

    before = _open_fds()
    yield
    closed = before - _open_fds()
    assert not closed, f"test closed standard file descriptor(s) {sorted(closed)}"

@pytest.fixture(autouse=True)
def _dashboard_session():
    """The dashboard always needs a sign-in (plan E10): register the fixed test session and
    Advanced-session tokens (test_support.ui_login.HEADERS / ADMIN_HEADERS) in the ui module."""
    import ui
    from test_support import ui_login

    ui_login.register(ui)
    yield
    ui_login.unregister(ui)


@pytest.fixture(autouse=True)
def _dashboard_test_client(request, monkeypatch):
    """TestClient(ui.app) starts signed in (the session registered above) unless the test is
    marked `anonymous` (tests of the sign-in itself)."""
    if request.node.get_closest_marker("anonymous"):
        yield
        return
    import ui
    from fastapi.testclient import TestClient

    from test_support import ui_login

    original = TestClient.__init__

    def signed_in_init(self, app, *args, **kwargs):
        if app is ui.app and not kwargs.get("headers") and not kwargs.get("cookies"):
            kwargs["cookies"] = {"session": ui_login.SESSION_TOKEN}
        original(self, app, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", signed_in_init)
    yield
