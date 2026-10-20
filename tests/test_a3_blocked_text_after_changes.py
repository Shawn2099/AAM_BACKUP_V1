"""A3 (final audit 2026-10-20): a BLOCKED run never claims "nothing was copied or deleted" after a sync ran.

GuardBlocked is also raised AFTER data changed: when rclone/robocopy stopped at the delete backstop, or when
the guards are re-measured between retry attempts. The email (flow._open_block_request) and the dashboard
text (statuses.describe) always said "Nothing was copied or deleted" / "blocked before any change". The
signal is "a sync attempt ran" (sync_state["attempts"], stored in the row as extended_metrics.sync_attempts)
- NOT `already_deleted`, which misses 'attempt 1 copied files, the re-measure then blocked'."""

import json
from unittest.mock import MagicMock, patch

import pytest

import flow
import ui
from core import guards, statuses
from test_a2_interrupted_run_status import _cloud_pipeline, _lan_pipeline, _make_config

BLOCK = {"guard": "G3_MASS_DELETE", "verdict": "BLOCK", "reason": "This backup would delete 900 of 1,000 files.",
         "metrics": {"planned_deletes": 900}}
RECORD = {"fy": "FY26-27", "results": [BLOCK], "max_delete": 10}


def _message_of_block_email(sync_attempted):
    with patch("flow.notify") as notify, patch("flow.ManifestDB") as db:
        db.from_config.return_value.open_approval_request.return_value = "appr-1"
        kwargs = {} if sync_attempted is None else {"sync_attempted": sync_attempted}
        flow._open_block_request(_make_config(), "cloud", "run-1", RECORD, "2026-10-20T02:00:00", **kwargs)
    return notify.call_args.args[1].message


def test_block_before_any_sync_keeps_the_nothing_changed_text():
    assert "Nothing was copied or deleted" in _message_of_block_email(None)
    assert "Nothing was copied or deleted" in _message_of_block_email(False)


def test_block_after_a_sync_attempt_does_not_claim_nothing_changed():
    text = _message_of_block_email(True)
    assert "Nothing was copied or deleted" not in text
    assert "may already have been changed" in text and "Open the dashboard" in text


def test_dashboard_description_follows_the_same_fact():
    assert "before any change" in statuses.describe("CLOUD_BLOCKED")
    after = statuses.describe("CLOUD_BLOCKED", sync_attempted=True)
    assert "before any change" not in after and "may already have been changed" in after
    assert statuses.describe("LAN_BLOCKED", sync_attempted=True) == after   # the text is leg-neutral


@pytest.mark.parametrize("metrics,expect_after", [
    ({"sync_attempts": [{"attempt": 1, "status": "CLOUD_FAILED", "deleted": 0}]}, True),
    ({"sync_attempts": []}, False), ({}, False),
])
def test_status_fields_read_the_stored_attempts(metrics, expect_after):
    fields = ui._status_fields({"status": "CLOUD_BLOCKED", "extended_metrics": json.dumps(metrics)})
    assert ("may already have been changed" in fields["status_description"]) is expect_after


def _blocked_mid_sync(**kwargs):
    kwargs["state"]["attempts"] = [{"attempt": 1, "status": "CLOUD_FAILED", "exit_code": 1, "deleted": 0}]
    raise guards.GuardBlocked("cloud", RECORD)


def test_cloud_pipeline_passes_the_fact_when_the_block_comes_after_attempt_1():
    with _cloud_pipeline(None, None):
        with patch("flow._sync_with_retries", side_effect=lambda leg, **kw: _blocked_mid_sync(**kw)), \
                patch("flow._open_block_request") as open_request:
            with pytest.raises(guards.GuardBlocked):
                flow._run_cloud_pipeline(_make_config(), "run-a3", "2026-10-20T02:00:00")
    assert open_request.call_args.kwargs.get("sync_attempted") is True


def test_cloud_pipeline_pre_sync_block_says_nothing_changed():
    with _cloud_pipeline(None, None):
        with patch("flow.cloud_guards_task", side_effect=guards.GuardBlocked("cloud", RECORD)), \
                patch("flow._open_block_request") as open_request:
            with pytest.raises(guards.GuardBlocked):
                flow._run_cloud_pipeline(_make_config(), "run-a3b", "2026-10-20T02:00:00")
    assert open_request.call_args.kwargs.get("sync_attempted") is False


def test_lan_pipeline_passes_the_fact_when_the_block_comes_after_attempt_1():
    def blocked(leg, **kw):
        kw["state"]["attempts"] = [{"attempt": 1, "status": "LAN_FAILED", "exit_code": 8, "deleted": 0}]
        raise guards.GuardBlocked("lan", RECORD)

    with _lan_pipeline(None, None):
        with patch("flow._sync_with_retries", side_effect=blocked), \
                patch("flow._open_block_request") as open_request:
            with pytest.raises(guards.GuardBlocked):
                flow._run_lan_pipeline(_make_config(), "run-a3l", "2026-10-20T21:00:00")
    assert open_request.call_args.kwargs.get("sync_attempted") is True
