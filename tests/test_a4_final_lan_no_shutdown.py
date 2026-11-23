"""Plan A4 (R1): the final LAN backup of a rollover must NOT power the NAS off - the new NAS
FY folder still has to be created afterwards. Every shutdown call site of the LAN pipeline is
suppressed, including shutdown_on_all_retries_exhausted (partial and exception branches)."""

from unittest.mock import MagicMock, patch

import pytest

import flow
from tests.test_lan_shutdown_policy import _make_lan_config


def _run(status, *, allow_shutdown, on_exhausted=False, sync_raises=False):
    cfg = _make_lan_config(shutdown_on_all_retries_exhausted=on_exhausted)
    sync_task = MagicMock()
    if sync_raises:
        sync_task.with_options.return_value.side_effect = RuntimeError("robocopy exploded")
    else:
        sync_task.with_options.return_value.return_value = {
            "status": status, "exit_code": 1, "error": "x"}
    with patch("flow.notify"), patch("flow.lan_shutdown_task") as shutdown, \
            patch("flow.lan_publish_artifact_task"), patch("flow.lan_record_task"), \
            patch("flow.lan_snapshot_after_task", return_value={"f": (1, 1.0)}), \
            patch("flow.lan_access_task", new=MagicMock()), \
            patch("flow.lan_guards_task", new=MagicMock()), \
            patch("flow.lan_snapshot_before_task", return_value={}), \
            patch("flow.lan_sync_task", sync_task), patch("flow.lan_preflight_task"), \
            patch("flow.wol_check_task"), patch("flow.health_check_task"), \
            patch("flow._record_run"):
        try:
            flow._run_lan_pipeline(cfg, "run-a4", "2026-04-01T06:00:00",
                                   allow_shutdown=allow_shutdown)
        except Exception:  # noqa: BLE001 - PartialRun / the sync error: not what is tested
            pass
    return shutdown


@pytest.mark.parametrize("allow", [True, False])
@pytest.mark.parametrize("status,on_exhausted,sync_raises", [
    ("LAN_COMPLETE", False, False),
    ("LAN_PARTIAL", True, False),
    ("LAN_FAILED", True, True),
])
def test_shutdown_follows_allow_shutdown_at_every_call_site(status, on_exhausted, sync_raises,
                                                            allow):
    shutdown = _run(status, allow_shutdown=allow, on_exhausted=on_exhausted,
                    sync_raises=sync_raises)
    # allowed: the harness really reaches the call site (1 call); not allowed: 0
    assert shutdown.call_count == (1 if allow else 0)


def test_final_backups_of_a_rollover_run_the_lan_leg_without_shutdown():
    cfg = MagicMock()
    cfg.cloud.enabled = True
    cfg.lan.enabled = True
    seen = {}

    def fake(leg):
        def run(config, run_id, started_at, monotonic_start=None, **kwargs):
            seen[leg] = kwargs
            return {"status": f"{leg.upper()}_COMPLETE"}
        return run

    with patch("flow._run_cloud_pipeline", fake("cloud")), \
            patch("flow._run_lan_pipeline", fake("lan")):
        status, errors = flow._run_final_backups(cfg, "rollover-x")
    assert status == {"cloud": "CLOUD_COMPLETE", "lan": "LAN_COMPLETE"} and not errors
    assert seen["lan"] == {"allow_shutdown": False}
    assert "allow_shutdown" not in seen["cloud"]
