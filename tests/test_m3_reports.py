"""M3 3.4 part 2 — report delivery is truthful (plan WP8).

Findings: report flows ended "Completed" when the email failed (Aug 3,
Sep 1); a week with no runs was logged as "normal" and nothing was sent;
report.py's own SMTP code gave up on transient 4xx replies after one try.
"""

from unittest.mock import MagicMock, patch

import pendulum
import pytest

import flow
from core import notify as nt
from core import report
from core.time_utils import IST


def _cfg(enabled=True):
    cfg = MagicMock()
    cfg.notifications.weekly_enabled = enabled
    cfg.notifications.monthly_enabled = enabled
    cfg.firm_name = "Firm"
    return cfg


def _run(flow_fn, *, runs, delivery=None):
    db = MagicMock()
    db.get_runs_between.return_value = runs
    with patch("flow.load_config", return_value=_cfg()), patch("flow.configure_logging"), \
         patch("flow.configure_prefect_bridge"), patch("flow.ManifestDB") as mdb, \
         patch("flow.notify") as notify, \
         patch("flow.send_email", return_value=delivery or nt.DeliveryResult(nt.SENT)) as send, \
         patch("flow.report.generate_report_html", return_value="<html/>"):
        mdb.from_config.return_value = db
        try:
            out = flow_fn.fn("cfg.yaml")
        except Exception as e:  # noqa: BLE001
            out = e
    return out, db, notify, send


# a run in the previous calendar month: inside the monthly report's period
# (audit 2026-10-18) and returned by the mocked weekly query alike
_LAST_MONTH = pendulum.now(IST).start_of("month").subtract(days=3).isoformat()
RUNS = [{"run_id": "r", "mode": "lan", "status": "LAN_COMPLETE", "started_at": _LAST_MONTH}]


@pytest.mark.parametrize("flow_fn,period", [(flow.weekly_report_flow, "Weekly"),
                                            (flow.monthly_report_flow, "Monthly")])
def test_report_sent_and_recorded(flow_fn, period):
    out, db, notify, send = _run(flow_fn, runs=RUNS)
    assert not isinstance(out, Exception), out
    assert period in send.call_args.args[1]
    rec = db.record_notification.call_args.args[0]
    assert rec["kind"] == "REPORT" and rec["status"] == nt.SENT
    notify.assert_not_called()


def test_failed_delivery_fails_the_flow_and_is_recorded():
    out, db, _n, _s = _run(flow.weekly_report_flow, runs=RUNS,
                           delivery=nt.DeliveryResult(nt.FAILED, "451 try again", 3))
    assert isinstance(out, RuntimeError) and "not delivered" in str(out)
    assert db.record_notification.call_args.args[0]["status"] == nt.FAILED


def test_no_runs_in_the_period_still_sends_the_report_saying_action_needed():
    """Plan C P1: the week nothing ran is the week the owner must hear about - one report email."""
    out, db, notify, send = _run(flow.weekly_report_flow, runs=[])
    assert not isinstance(out, Exception), out
    send.assert_called_once()
    assert "action needed" in send.call_args.args[1] and "no backup ran" in send.call_args.args[1]
    assert db.record_notification.call_args.args[0]["status"] == nt.SENT
    notify.assert_not_called()


def test_disabled_report_is_skipped_quietly():
    with patch("flow.load_config", return_value=_cfg(enabled=False)), \
         patch("flow.configure_logging"), patch("flow.configure_prefect_bridge"), \
         patch("flow.send_email") as send, patch("flow.notify") as notify:
        assert flow.weekly_report_flow.fn("cfg.yaml") is None
    send.assert_not_called()
    notify.assert_not_called()


def test_report_email_uses_the_single_smtp_transport():
    """report.py has no SMTP code of its own any more (4xx retry fix)."""
    import inspect
    assert "smtplib" not in inspect.getsource(report)
    with patch("core.report.send_email", return_value=nt.DeliveryResult(nt.SENT)) as send:
        assert report._send_email_with_attachments(MagicMock(), "s", "<b/>", [], audience="reports") is True
    send.assert_called_once()
    with patch("core.report.send_email", return_value=nt.DeliveryResult(nt.FAILED, "x")):
        assert report._send_email_with_attachments(MagicMock(), "s", "<b/>", [], audience="reports") is False


def test_monthly_report_no_longer_collides_with_the_weekly_one():
    from models.config import ScheduleConfig
    s = ScheduleConfig()
    assert s.monthly_cron == "30 8 1 * *"
