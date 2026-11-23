# tests/test_fix_23_1_1_watchdog_import.py
from unittest.mock import MagicMock

import pytest

import watchdog


def test_watchdog_import_resolves():
    from core.report import send_failure_alert

    assert callable(send_failure_alert)


def test_core_notifications_does_not_exist():
    with pytest.raises(ModuleNotFoundError):
        import core.notifications  # noqa: F401


def test_alert_wedged_lock_goes_through_notify(monkeypatch):
    """WP8: the watchdog alert uses the single alert path (core.notify)."""
    mock_notify = MagicMock()
    monkeypatch.setattr("core.notify.notify", mock_notify)

    mock_cfg = MagicMock()
    monkeypatch.setattr("models.config.load_config", lambda _: mock_cfg)

    watchdog._alert_wedged_lock("Test deadlock reason", pid=8888)

    assert mock_notify.called
    cfg, event = mock_notify.call_args.args
    assert cfg is mock_cfg
    assert "PID 8888" in event.message
    assert event.status == "LIVE_LOCK_CAP_EXCEEDED" and event.kind == "ACTION_REQUIRED"
