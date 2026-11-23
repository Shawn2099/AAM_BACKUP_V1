"""Plan A7 (+ F-9): a failed `nas_folder` rollover step is completed by the next LAN pipeline
(NAS awake) from the journal, BEFORE the guards - so our own new folder never needs an
approval - and only under the F-9 conditions (switch done, destination is the journal's new
folder, the share root exists)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import flow
from core import rollover_journal as rj
from core.guards import MOUNT_MARKER
from core.manifest import ManifestDB

FROM, TO = "FY26-27", "FY27-28"


@pytest.fixture
def rig(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    share = tmp_path / "share"
    share.mkdir()
    cfg = SimpleNamespace(paths=SimpleNamespace(lan_destination=str(share / TO)))

    def open_journal(**states):
        db.rollover_open(FROM, TO, "t0")
        for step, state in states.items():
            if state == rj.DONE:
                db.rollover_step_done(FROM, TO, step, "t1")
            elif state == rj.FAILED:
                db.rollover_step_failed(FROM, TO, step, "t1", error="NAS off")

    real_close = db.close
    db.close = lambda: None
    with patch("flow.ManifestDB.from_config", return_value=db):
        yield SimpleNamespace(db=db, cfg=cfg, share=share, open_journal=open_journal)
    real_close()


def _step(rig, name):
    return rig.db.rollover_get(FROM, TO)["steps"][name]


def test_failed_nas_folder_is_completed_by_the_next_lan_run(rig):
    rig.open_journal(final_backups=rj.DONE, source_folder=rj.DONE, config_switch=rj.DONE,
                     nas_folder=rj.FAILED)
    flow._complete_pending_rollover_nas_folder(rig.cfg)
    assert (rig.share / TO / MOUNT_MARKER).is_file()
    assert _step(rig, rj.NAS_FOLDER)["state"] == rj.DONE
    # this LAN run's own shutdown follows, so the journal's shutdown step is not left pending
    assert _step(rig, rj.NAS_SHUTDOWN)["state"] == rj.SKIPPED


def test_nothing_happens_before_the_switch(rig):
    rig.open_journal(final_backups=rj.DONE, nas_folder=rj.FAILED)      # config_switch pending
    flow._complete_pending_rollover_nas_folder(rig.cfg)
    assert not (rig.share / TO).exists()
    assert _step(rig, rj.NAS_FOLDER)["state"] == rj.FAILED


def test_nothing_happens_when_the_destination_is_not_the_journals_new_folder(rig):
    rig.open_journal(config_switch=rj.DONE, nas_folder=rj.FAILED)
    rig.cfg.paths.lan_destination = str(rig.share / "FY25-26")          # somewhere else
    flow._complete_pending_rollover_nas_folder(rig.cfg)
    assert not (rig.share / TO).exists() and not (rig.share / "FY25-26").exists()
    assert _step(rig, rj.NAS_FOLDER)["state"] == rj.FAILED


def test_a_missing_share_root_is_recorded_and_never_created(rig, tmp_path):
    rig.open_journal(config_switch=rj.DONE, nas_folder=rj.FAILED)
    rig.cfg.paths.lan_destination = str(tmp_path / "nas-is-off" / "share" / TO)
    flow._complete_pending_rollover_nas_folder(rig.cfg)                  # must not raise
    assert not (tmp_path / "nas-is-off").exists()
    s = _step(rig, rj.NAS_FOLDER)
    assert s["state"] == rj.FAILED and "not reachable" in s["last_error"]
    assert s["attempts"] == 1


def test_no_open_journal_is_a_no_op(rig):
    flow._complete_pending_rollover_nas_folder(rig.cfg)                  # must not raise


def test_a_database_problem_never_blocks_the_backup(rig):
    with patch("flow.ManifestDB.from_config", side_effect=RuntimeError("db gone")):
        flow._complete_pending_rollover_nas_folder(rig.cfg)              # must not raise


def test_lan_pipeline_runs_it_after_wake_and_before_the_guards():
    from tests.test_lan_shutdown_policy import _make_lan_config
    order = MagicMock()
    with patch("flow.notify"), patch("flow.lan_shutdown_task"), \
            patch("flow.lan_publish_artifact_task"), patch("flow.lan_record_task"), \
            patch("flow.lan_snapshot_after_task", return_value={"f": (1, 1.0)}), \
            patch("flow.lan_access_task", new=MagicMock()), \
            patch("flow.lan_guards_task", new=order.guards), \
            patch("flow.lan_snapshot_before_task", return_value={}), \
            patch("flow.lan_sync_task") as sync, patch("flow.lan_preflight_task"), \
            patch("flow.wol_check_task", new=order.wake), \
            patch("flow._complete_pending_rollover_nas_folder", new=order.pending_folder), \
            patch("flow.health_check_task"), patch("flow._record_run"):
        sync.with_options.return_value.return_value = {"status": "LAN_COMPLETE", "exit_code": 1}
        flow._run_lan_pipeline(_make_lan_config(), "run-a7", "2026-04-02T21:00:00")
    names = [c[0] for c in order.mock_calls]
    assert names.index("wake") < names.index("pending_folder") < names.index("guards")
