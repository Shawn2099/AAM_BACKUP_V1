"""Lab fixtures (pytest discovers fixtures from conftest.py only)."""
from __future__ import annotations

# WP16 test isolation (see test_support/sandbox_config.py): must precede any
# project import so CONFIG_PATH resolves to the sandbox, not config.yaml.
from test_support import sandbox_config

sandbox_config.install()

from audit_lab import labclock  # noqa: F401
from audit_lab.labconf import (  # noqa: F401
    LAB_ROOT,
    RCLONE_EXE,
    ROBOCOPY_EXE,
    _lab_root,
    assert_lab_path,
    build_config,
    md5,
    patch_cloud_modules_to_local,
    record,
    robocopy_local,
    run,
    sandbox,
    unc_nas,
    write_file,
    write_local_rclone_config,
)

import pytest  # noqa: E402

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
