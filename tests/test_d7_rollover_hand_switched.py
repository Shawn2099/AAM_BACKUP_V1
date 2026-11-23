"""D7 (final audit 2026-10-20): a hand-edited FY switch must not mark the old year's final backup "done".

R3 treats "config.yaml already points at the new FY while the journal's config_switch is still open" as a crash
between the config write and the journal write (the switch step is then closed). That is right when the final
backup is already DONE (the steps run in order). But if the owner switched config.yaml BY HAND while the rollover
was blocked at the final backup (G6, a failed leg), the next run closed the switch, ran the "final backup" against
the already-switched config (= the NEW year), recorded final_backups DONE and archived the old year without its
last changes ever being uploaded. Now: stop, ask a human, change nothing."""

import pytest

from core import fy_rollover as fr
from core import rollover_journal as rj
from tests.test_a6_rollover_journal_flow import (FROM, TO, _config_now_points_at_the_new_fy, _events,
                                                 _reset_mocks, _states)
from tests.test_m2_rollover import _Rig


def _blocked_at_the_final_backup(r):
    with pytest.raises(fr.RolloverError):
        r.run()
    assert r.statuses()[-1] == "ROLLOVER_BLOCKED"
    assert _states(r)[rj.FINAL_BACKUPS] == rj.FAILED


def test_hand_switched_config_with_an_unfinished_final_backup_stops_and_asks():
    with _Rig(lan="LAN_FAILED") as r:
        _blocked_at_the_final_backup(r)
        _config_now_points_at_the_new_fy(r)                 # the owner edits config.yaml by hand ...
        _reset_mocks(r)
        r.m["_run_lan_pipeline"].side_effect = r._leg("lan", "LAN_COMPLETE")   # ... and the NAS works again
        with pytest.raises(fr.RolloverError, match="by hand"):
            r.run()
        assert r.statuses()[-1] == "ROLLOVER_BLOCKED"
        [event] = [e for e in _events(r) if e.kind == "ACTION_REQUIRED"]
        assert event.key == "rollover:switched_by_hand" and event.fault_code == "AAM-ROL-02"
        assert FROM in event.message and TO in event.message
        # nothing was run against the switched config, nothing was archived, no step was faked
        r.m["_run_cloud_pipeline"].assert_not_called()
        r.m["_run_lan_pipeline"].assert_not_called()
        r.m["run_archive_transition"].assert_not_called()
        r.m["update_config_yaml"].assert_not_called()
        st = _states(r)
        assert st[rj.FINAL_BACKUPS] == rj.FAILED and st[rj.CONFIG_SWITCH] != rj.DONE
        assert st[rj.ARCHIVE] != rj.DONE


def test_putting_the_config_back_lets_the_rollover_finish_the_old_year_first():
    with _Rig(lan="LAN_FAILED") as r:
        _blocked_at_the_final_backup(r)
        _config_now_points_at_the_new_fy(r)
        _reset_mocks(r)
        with pytest.raises(fr.RolloverError):
            r.run()
        r.cfg.paths.source_drive = f"E:\\DATA\\{FROM}"            # back to the old year
        r.cfg.paths.lan_destination = f"\\\\nas\\backup\\{FROM}"
        _reset_mocks(r)
        r.m["_run_lan_pipeline"].side_effect = r._leg("lan", "LAN_COMPLETE")   # the LAN problem is fixed
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["_run_cloud_pipeline"].assert_called_once()            # the final backups of the OLD year run
        r.m["_run_lan_pipeline"].assert_called_once()
        assert _states(r) == {s: rj.DONE for s in rj.STEPS}


def test_a_real_crash_after_the_config_write_is_still_recognised(tmp_path):
    """The genuine R3 case: every step before the switch is DONE (they run in order), so the open
    config_switch is closed exactly as before - covered by the existing R3 tests; this pins the
    unchanged contract on the journal side."""
    assert rj.BLOCKING >= {rj.FINAL_BACKUPS, rj.SOURCE_FOLDER, rj.CONFIG_SWITCH}
    assert rj.STEPS.index(rj.FINAL_BACKUPS) < rj.STEPS.index(rj.CONFIG_SWITCH)
