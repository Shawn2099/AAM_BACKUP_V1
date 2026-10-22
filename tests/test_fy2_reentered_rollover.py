"""FY-2 (plan docs/PLAN_FY_FOLDERS_2026-10-22.md): a FINISHED rollover must not be "completed" again.

`rollover_open` returns the existing journal of a transition. When config.yaml is put back to the old FY
(hand edit, restored backup) after the rollover finished, every step already counts as done, so the daily
check recorded ROLLOVER_COMPLETE each morning while the backups stayed on the old year. Now it stops, tells a
human and changes nothing."""

import pytest

from core import fy_rollover as fr
from core import rollover_journal as rj
from tests.test_a6_rollover_journal_flow import FROM, TO, _events, _reset_mocks, _states
from tests.test_m2_rollover import _Rig


def _put_config_back(r):
    r.cfg.paths.source_drive = f"E:\\DATA\\{FROM}"
    r.cfg.paths.lan_destination = f"\\\\nas\\backup\\{FROM}"


def _finished_rollover(r):
    assert r.run() == "ROLLOVER_COMPLETED"
    assert _states(r) == {s: rj.DONE for s in rj.STEPS}


def test_a_config_put_back_after_a_finished_rollover_stops_and_asks():
    with _Rig() as r:
        _finished_rollover(r)
        _put_config_back(r)
        _reset_mocks(r)
        before = len(r.statuses())
        with pytest.raises(fr.RolloverError, match="put back"):
            r.run()
        assert r.statuses()[before:] == ["ROLLOVER_BLOCKED"]
        assert "ROLLOVER_COMPLETE" not in r.statuses()[before:]
        [event] = [e for e in _events(r) if e.kind == "ACTION_REQUIRED"]
        assert event.key == f"rollover:reentered:{TO}" and event.fault_code == "AAM-ROL-04"
        assert FROM in event.message and TO in event.message and "support" in event.message.lower()
        # nothing was backed up, switched or archived
        r.m["_run_cloud_pipeline"].assert_not_called()
        r.m["_run_lan_pipeline"].assert_not_called()
        r.m["update_config_yaml"].assert_not_called()
        r.m["run_archive_transition"].assert_not_called()


def test_the_dashboard_does_not_offer_to_run_it_again():
    from core.manifest import ManifestDB  # noqa: F401  (the rig owns the DB)

    with _Rig() as r:
        _finished_rollover(r)
        _put_config_back(r)
        state = fr.manual_run_state(FROM, TO, finished_journal=True)
        assert state["can_run"] is False and "support" in state["message"].lower()
        free = fr.manual_run_state(FROM, TO)
        assert free["can_run"] is True        # unchanged for a rollover that was never done


def test_the_blocked_final_backup_says_the_new_year_is_not_backed_up_yet():
    from core import guards

    g = guards.g6_fy_rollover(FROM, TO, fr.FORWARD, {"lan": "LAN_FAILED", "cloud": "CLOUD_COMPLETE"})
    assert "nothing saved in " + TO + " is backed up yet" in g.reason
