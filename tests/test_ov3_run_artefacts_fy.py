"""OV-3.5: run ids and run-log folders carry the FY (plan 5.3); old id forms keep working."""
import os

import pendulum
import pytest

from core.run_artefacts import RunArtefacts, _fy_of, _leg_of, purge_older_than
from core.time_utils import IST

U = "a1b2c3d4-0000-4000-8000-000000000000"
STARTED = pendulum.datetime(2027, 1, 5, 2, 1, tz=IST)


@pytest.mark.parametrize("run_id, leg, fy", [
    (f"{U}-cloud", "cloud", None),
    (f"{U}-lan", "lan", None),
    (f"{U}-rollover", "rollover", None),
    (f"{U}-cloud-full", "audit-cloud", None),
    (f"{U}-lan-quick", "audit-lan", None),
    (f"{U}-cloud-FY27-28", "cloud", "FY27-28"),
    (f"{U}-lan-FY26-27", "lan", "FY26-27"),
    (f"{U}-rollover-FY27-28", "rollover", "FY27-28"),
    (f"{U}-cloud-FY27-28-full", "audit-cloud", "FY27-28"),
    (f"{U}-lan-FY26-27-quick", "audit-lan", "FY26-27"),
    (f"{U}-cloud-fy27-28", "cloud", "FY27-28"),
    (f"{U}-cloud-FY99-00", "cloud", "FY99-00"),
    (f"{U}-final-lan", "lan", None),
    ("adhoc-thing", None, None),
    (f"{U}-FY27-28", None, None),
])
def test_leg_and_fy_of_old_and_new_ids(run_id, leg, fy):
    assert _leg_of(run_id) == leg
    assert _fy_of(run_id) == fy


def test_old_forms_keep_exact_names(tmp_path):
    a = RunArtefacts(tmp_path, f"{U}-cloud", started=STARTED)
    assert a.dir.name == "2027-01-05_0201_cloud_a1b2c3d4"
    assert a.new_path("rclone-sync").name == "2027-01-05_0201_cloud_rclone-sync.log"


@pytest.mark.parametrize("run_id, folder, prefix", [
    (f"{U}-cloud-FY27-28", "2027-01-05_0201_cloud_FY27-28_a1b2c3d4", "2027-01-05_0201_cloud_FY27-28"),
    (f"{U}-lan-FY26-27", "2027-01-05_0201_lan_FY26-27_a1b2c3d4", "2027-01-05_0201_lan_FY26-27"),
    (f"{U}-rollover-FY27-28", "2027-01-05_0201_rollover_FY27-28_a1b2c3d4",
     "2027-01-05_0201_rollover_FY27-28"),
    (f"{U}-cloud-FY27-28-full", "2027-01-05_0201_audit-cloud_FY27-28_a1b2c3d4",
     "2027-01-05_0201_audit-cloud_FY27-28"),
    (f"{U}-lan-FY99-00-quick", "2027-01-05_0201_audit-lan_FY99-00_a1b2c3d4",
     "2027-01-05_0201_audit-lan_FY99-00"),
])
def test_new_folder_and_file_names_and_open_round_trip(tmp_path, run_id, folder, prefix):
    a = RunArtefacts(tmp_path, run_id, started=STARTED)
    assert a.dir == tmp_path / "2027-01-05" / folder
    first = a.new_path("robocopy", "log")
    assert first.name == f"{prefix}_robocopy.log"
    first.write_text("x")
    b = RunArtefacts.open(str(a.dir))
    assert b._prefix == prefix
    again = b.new_path("robocopy", "log")
    assert again.name == f"{prefix}_robocopy-2.log"
    assert b.new_path("health", "txt").name == f"{prefix}_health.txt"


def test_open_of_an_old_folder_still_recovers_the_prefix(tmp_path):
    a = RunArtefacts(tmp_path, f"{U}-cloud-full", started=STARTED)
    assert RunArtefacts.open(str(a.dir))._prefix == "2027-01-05_0201_audit-cloud"


def test_two_fys_in_the_same_minute_do_not_collide(tmp_path):
    a = RunArtefacts(tmp_path, f"{U}-cloud-FY26-27", started=STARTED)
    b = RunArtefacts(tmp_path, f"{U}-cloud-FY27-28", started=STARTED)
    assert a.dir != b.dir
    assert a.new_path("rclone-sync").name != b.new_path("rclone-sync").name


def test_same_minute_same_fy_different_short_id_is_fine(tmp_path):
    a = RunArtefacts(tmp_path, f"{U}-cloud-FY27-28", started=STARTED)
    b = RunArtefacts(tmp_path, "ffffeeee-0000-4000-8000-000000000000-cloud-FY27-28", started=STARTED)
    assert a.dir != b.dir and a.dir.parent == b.dir.parent


def test_purge_removes_new_folders_by_day(tmp_path):
    a = RunArtefacts(tmp_path, f"{U}-cloud-FY99-00", started=STARTED)
    a.new_path("rclone-sync").write_text("x")
    day = a.dir.parent
    for p in (*day.rglob("*"), day):      # W40: a day folder goes only when everything inside is old too
        os.utime(p, (0, 0))
    assert purge_older_than(tmp_path, 30, glob="*") == 1 and not day.exists()
