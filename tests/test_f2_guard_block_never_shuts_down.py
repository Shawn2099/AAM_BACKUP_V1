"""F2 (manual-button review 2026-11-23): `lan.shutdown_on_all_retries_exhausted=True` is documented for
LAN_PARTIAL / LAN_FAILED only. A guard BLOCK is a decision pending for a human (data risk): the NAS must stay on
and the loop must not vote for a switch-off. GuardBlocked is a RuntimeError, so it used to count as a failure."""
from unittest.mock import patch

import pytest

import flow
from core import guards
from test_a2_interrupted_run_status import _lan_pipeline, _make_config
from test_a3_blocked_text_after_changes import RECORD


def _run(**kw):
    cfg = _make_config()
    cfg.lan.shutdown_on_all_retries_exhausted = True
    with _lan_pipeline(None, None):
        with patch("flow._sync_with_retries", side_effect=guards.GuardBlocked("lan", RECORD)), \
                patch("flow._open_block_request"), patch("flow.lan_shutdown_task") as shutdown:
            with pytest.raises(guards.GuardBlocked):
                flow._run_lan_pipeline(cfg, "run-f2", "2026-11-23T21:00:00", **kw)
    return shutdown


def test_a_guard_block_never_switches_the_nas_off_when_the_pipeline_decides():
    shutdown = _run()
    shutdown.assert_not_called()


def test_a_guard_block_votes_to_keep_the_nas_on_in_the_leg_loop():
    votes = []
    shutdown = _run(allow_shutdown=False, shutdown_votes=votes)
    shutdown.assert_not_called()
    assert votes and votes[0][0] is False


def test_a_plain_failure_still_follows_the_flag():
    cfg = _make_config()
    cfg.lan.shutdown_on_all_retries_exhausted = True
    with _lan_pipeline(None, None):
        with patch("flow._sync_with_retries", side_effect=RuntimeError("network dropped")), \
                patch("flow.lan_shutdown_task") as shutdown:
            with pytest.raises(RuntimeError):
                flow._run_lan_pipeline(cfg, "run-f2b", "2026-11-23T21:00:00")
    shutdown.assert_called_once()
