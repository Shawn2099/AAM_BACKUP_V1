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


def test_alert_wedged_lock_executes_and_calls_report(monkeypatch):
    """Verify runtime execution of _alert_wedged_lock and parameter dispatch."""
    mock_send = MagicMock(return_value=True)
    monkeypatch.setattr("core.report.send_failure_alert", mock_send)

    mock_cfg = MagicMock()
    mock_cfg.firm_name = "Test Firm"
    mock_cfg.notifications = MagicMock()
    monkeypatch.setattr("models.config.load_config", lambda _: mock_cfg)

    watchdog._alert_wedged_lock("Test deadlock reason", pid=8888)

    assert mock_send.called
    args, kwargs = mock_send.call_args
    assert args[0] is mock_cfg.notifications
    assert args[1] == "Test Firm"
    assert "PID 8888" in args[2]
    assert args[3]["pid"] == 8888
    assert args[3]["status"] == "LIVE_LOCK_CAP_EXCEEDED"
