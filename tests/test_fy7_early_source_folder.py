"""FY-7 (plan docs/PLAN_FY_FOLDERS_2026-10-22.md): the new local FY folder is created BEFORE the final backups
(rollover.create_source_folder_early, default on), so the office can save into it on 1 April even when the
final backup fails and the rollover waits for an approval. Only for the normal next year on a proven clock."""

import pytest

from core import fy_rollover as fr
from core import rollover_journal as rj
from models.config import RolloverConfig
from tests.test_a6_rollover_journal_flow import FROM, TO, _events, _reset_mocks, _states
from tests.test_m2_rollover import _Rig


def _order(r):
    return [c[0] for c in r.order.mock_calls if c[0] in (
        "create_source_folder", "update_config_yaml", "run_archive_transition")]


def test_the_key_defaults_to_on():
    assert RolloverConfig().create_source_folder_early is True


def test_the_folder_exists_even_when_the_final_backup_fails():
    with _Rig(lan="LAN_FAILED") as r:
        with pytest.raises(fr.RolloverError):
            r.run()
        r.m["create_source_folder"].assert_called_once()           # the office can save into it
        st = _states(r)
        assert st[rj.SOURCE_FOLDER] == rj.DONE and st[rj.FINAL_BACKUPS] == rj.FAILED
        assert st[rj.CONFIG_SWITCH] != rj.DONE                      # the switch still waits
        r.m["update_config_yaml"].assert_not_called()


def test_a_normal_rollover_still_ends_complete_and_creates_the_folder_once():
    with _Rig() as r:
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["create_source_folder"].assert_called_once()
        assert _states(r) == {s: rj.DONE for s in rj.STEPS}


def test_an_approved_rerun_does_not_create_the_folder_twice():
    with _Rig(lan="LAN_FAILED") as r:
        with pytest.raises(fr.RolloverError):
            r.run()
        _reset_mocks(r)
        r.m["_apply_approval"].return_value = (frozenset({"G6_FY_ROLLOVER"}), {"approval_id": "a1"})
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["create_source_folder"].assert_not_called()


def test_with_the_key_off_the_folder_comes_after_the_final_backups_as_before():
    with _Rig(lan="LAN_FAILED") as r:
        r.cfg.rollover = RolloverConfig(create_source_folder_early=False)
        with pytest.raises(fr.RolloverError):
            r.run()
        r.m["create_source_folder"].assert_not_called()
        assert _states(r)[rj.SOURCE_FOLDER] == rj.PENDING


def test_a_failed_early_folder_does_not_stop_the_old_years_final_backup_and_is_retried_as_blocking():
    with _Rig() as r:
        r.m["create_source_folder"].side_effect = OSError("disk full")
        with pytest.raises(fr.RolloverError):
            r.run()
        r.m["_run_cloud_pipeline"].assert_called_once()             # the final backups still ran
        r.m["_run_lan_pipeline"].assert_called_once()
        r.m["update_config_yaml"].assert_not_called()               # ... but the switch did not
        assert _states(r)[rj.SOURCE_FOLDER] == rj.FAILED
        _reset_mocks(r)
        r.m["create_source_folder"].side_effect = None
        assert r.run() == "ROLLOVER_COMPLETED"
        assert _states(r) == {s: rj.DONE for s in rj.STEPS}


def test_a_jump_of_more_than_one_year_creates_nothing_before_the_approval():
    with _Rig(computed="FY28-29", lan="LAN_COMPLETE") as r:
        with pytest.raises(fr.RolloverError):
            r.run()                                                 # G6 blocks the jump
        r.m["create_source_folder"].assert_not_called()


def test_an_unproven_clock_creates_nothing():
    from core.health import CLOCK_UNVERIFIED, ClockResult
    with _Rig(clock=ClockResult(CLOCK_UNVERIFIED, None, "none", "no time server")) as r:
        with pytest.raises(fr.RolloverError):
            r.run()
        r.m["create_source_folder"].assert_not_called()


def test_a_clock_that_is_behind_creates_nothing():
    with _Rig(computed="FY25-26") as r:
        with pytest.raises(fr.RolloverError):
            r.run()
        r.m["create_source_folder"].assert_not_called()


def test_an_old_finished_journal_is_still_readable(tmp_path):
    from core.manifest import ManifestDB
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        db.rollover_open(FROM, TO, "2026-04-01T06:00:00+05:30")
        for step in rj.STEPS:
            db.rollover_step_done(FROM, TO, step, "2026-04-01T06:05:00+05:30")
        assert rj.is_complete(db.rollover_get(FROM, TO)) and db.rollover_incomplete() is None
    finally:
        db.close()
