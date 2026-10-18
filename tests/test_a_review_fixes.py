"""Findings F1-F4 of the independent review of the rollover journal (2026-10-18):

F1 a journal whose NAS step is skipped (or whose archive keeps failing) never completed, so
   the NEXT year's rollover was never started;
F2 crash after the config write (R3) re-ran the "final backup" against the NEW year and could
   stay blocked;
F3 a crashed rollover left ROLLOVER_RUNNING forever with no alert;
F4 the abandoned-run email said "backup abandoned ... copies what is missing" for a rollover.
"""

import json
from unittest.mock import MagicMock, patch

from core import backup_repository as repo
from core import rollover_journal as rj
from core import statuses
from core.time_utils import now_iso
from models.config import MaintenanceConfig
from tests.test_m2_rollover import _Rig
from tests.test_m5_abandoned_runs import _dead_token

FROM, TO = "FY26-27", "FY27-28"


# ---- F1 ---------------------------------------------------------------------------------------

def test_f1_a_skipped_prerequisite_skips_the_dependent_step_and_the_journal_completes():
    with _Rig() as r:
        r.cfg.lan.enabled = False                      # nas_folder is skipped
        assert r.run() == "ROLLOVER_COMPLETED"
        st = {k: v["state"] for k, v in r.db.rollover_get(FROM, TO)["steps"].items()}
        assert st[rj.NAS_FOLDER] == rj.SKIPPED and st[rj.NAS_SHUTDOWN] == rj.SKIPPED
        assert r.db.rollover_incomplete() is None      # nothing keeps the next FY waiting


def test_f1_a_stuck_old_journal_never_blocks_the_next_years_rollover():
    with _Rig() as r:                                  # config at FY26-27, date says FY27-28
        r.db.rollover_open("FY25-26", FROM, "t0")
        for step in (rj.FINAL_BACKUPS, rj.SOURCE_FOLDER, rj.NAS_FOLDER, rj.CONFIG_SWITCH,
                     rj.NAS_SHUTDOWN, rj.ANNOUNCE):
            r.db.rollover_step_done("FY25-26", FROM, step, "t1")
        r.db.rollover_step_failed("FY25-26", FROM, rj.ARCHIVE, "t1", error="gcloud down")
        assert r.run() == "ROLLOVER_COMPLETED"
        r._tmp  # noqa: B018 - rig keeps the DB
        r.m["_run_cloud_pipeline"].assert_called_once()        # the NEW rollover really ran
        r.m["update_config_yaml"].assert_called_once()
        old = r.db.rollover_get("FY25-26", FROM)["steps"][rj.ARCHIVE]
        assert old["state"] == rj.SKIPPED and "superseded" in old["last_error"]
        assert r.db.rollover_get(FROM, TO) is not None


# ---- F2 ---------------------------------------------------------------------------------------

def test_f2_crash_after_the_config_write_does_not_rerun_the_final_backup():
    with _Rig() as r:
        # the config already points at the NEW year, but the journal never saw the switch
        r.cfg.paths.source_drive = "E:\\DATA\\FY27-28"
        r.cfg.paths.lan_destination = "\\\\nas\\backup\\FY27-28"
        r.db.rollover_open(FROM, TO, "t0")
        r.db.rollover_step_done(FROM, TO, rj.FINAL_BACKUPS, "t1")
        r.db.rollover_step_done(FROM, TO, rj.SOURCE_FOLDER, "t1")
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["_run_cloud_pipeline"].assert_not_called()
        r.m["_run_lan_pipeline"].assert_not_called()
        r.m["update_config_yaml"].assert_not_called()
        r.m["check_clock"].assert_not_called()
        steps = r.db.rollover_get(FROM, TO)["steps"]
        assert steps[rj.CONFIG_SWITCH]["state"] == rj.DONE
        assert steps[rj.FINAL_BACKUPS]["attempts"] == 0 or steps[rj.FINAL_BACKUPS]["state"] == rj.DONE


# ---- F3 / F4 ----------------------------------------------------------------------------------

def test_f3_status_vocabulary_has_rollover_abandoned():
    assert statuses.category("ROLLOVER_ABANDONED") == statuses.CATEGORY_FAILED
    assert statuses.label("ROLLOVER_ABANDONED") == "Abandoned"
    assert "rollover" in statuses.describe("ROLLOVER_ABANDONED").lower()


def test_f3_f4_a_crashed_rollover_is_swept_with_a_rollover_specific_alert(tmp_path):
    from core.manifest import ManifestDB
    db_path = str(tmp_path / "m.db")
    db = ManifestDB(db_path)
    cfg = MagicMock()
    cfg.paths.database_path = db_path
    cfg.maintenance = MaintenanceConfig()
    try:
        db.insert_run({"run_id": "ro1", "mode": "rollover", "started_at": now_iso(),
                       "status": "ROLLOVER_RUNNING",
                       "extended_metrics": json.dumps({"owner": _dead_token(),
                                                       "to_fy": TO})})
        with patch.object(repo, "notify") as notify:
            rows = repo.sweep_abandoned_runs(cfg, db)
    finally:
        db.close()
    assert [r["status"] for r in rows] == ["ROLLOVER_ABANDONED"]
    (_cfg, event), _ = notify.call_args
    assert event.status == "ROLLOVER_ABANDONED" and event.fault_code == "AAM-ROL-04"
    assert event.key.startswith("rollover:abandoned")
    assert "copies what is missing" not in event.message
    assert "rollover" in event.title.lower() and "morning" in event.message.lower()
