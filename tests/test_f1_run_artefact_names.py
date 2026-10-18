"""Plan F1: run folders and files carry date, time and leg in their names."""
import os

import pendulum
import pytest

from core.run_artefacts import RunArtefacts, purge_older_than
from core.time_utils import IST

RUN_ID = "3f2a9c1e-77aa-4c6b-9d11-0a1b2c3d4e5f-lan"
STARTED = pendulum.datetime(2026, 10, 18, 21, 0, tz=IST)


def test_folder_is_day_then_stamp_leg_and_short_id(tmp_path):
    a = RunArtefacts(tmp_path, RUN_ID, started=STARTED)
    assert a.dir == tmp_path / "2026-10-18" / "2026-10-18_2100_lan_3f2a9c1e"
    assert a.run_id == RUN_ID                                   # the DB run id is unchanged


def test_files_are_named_with_date_time_leg_and_step(tmp_path):
    a = RunArtefacts(tmp_path, RUN_ID, started=STARTED)
    assert a.new_path("robocopy-sync", "log").name == "2026-10-18_2100_lan_robocopy-sync.log"
    assert a.new_path("rclone-lsjson", "log").name == "2026-10-18_2100_lan_rclone-lsjson.log"


def test_the_same_step_twice_never_overwrites(tmp_path):
    a = RunArtefacts(tmp_path, RUN_ID, started=STARTED)
    first, second, third = (a.new_path("robocopy", "log") for _ in range(3))
    assert len({first, second, third}) == 3
    assert second.name == "2026-10-18_2100_lan_robocopy-2.log"


def test_reopening_a_folder_keeps_the_names_and_does_not_reuse_one(tmp_path):
    a = RunArtefacts(tmp_path, RUN_ID, started=STARTED)
    taken = a.new_path("robocopy", "log")
    taken.write_text("x")
    b = RunArtefacts.open(str(a.dir))                           # a Prefect task gets only the path
    again = b.new_path("robocopy", "log")
    assert again != taken and again.name.startswith("2026-10-18_2100_lan_robocopy")
    assert b.new_path("health", "txt").name == "2026-10-18_2100_lan_health.txt"


def test_purge_still_removes_whole_day_folders(tmp_path):
    a = RunArtefacts(tmp_path, RUN_ID, started=STARTED)
    a.new_path("robocopy", "log").write_text("x")
    old = a.dir.parent
    os.utime(old, (0, 0))
    assert purge_older_than(tmp_path, 30, glob="*") == 1 and not old.exists()


def test_unknown_leg_or_adhoc_keeps_the_plain_run_id_folder(tmp_path):
    a = RunArtefacts(tmp_path, "adhoc-thing", day="adhoc")
    assert a.dir.name == "adhoc-thing"
    assert a.new_path("tool", "log").name == "01_tool.log"


@pytest.mark.parametrize("leg", ["cloud", "lan", "audit", "rollover"])
def test_leg_comes_from_the_run_id_suffix(tmp_path, leg):
    a = RunArtefacts(tmp_path, f"abcdef12-0000-0000-0000-000000000000-{leg}", started=STARTED)
    assert a.dir.name == f"2026-10-18_2100_{leg}_abcdef12"


def test_for_config_uses_the_same_naming(tmp_path):
    from types import SimpleNamespace
    cfg = SimpleNamespace(run_logs_dir=str(tmp_path), run_logs=SimpleNamespace(min_free_gb=0))
    a = RunArtefacts.for_config(cfg, RUN_ID, started=STARTED)
    assert a.dir.name == "2026-10-18_2100_lan_3f2a9c1e"


def test_audit_ids_get_an_audit_leg_name(tmp_path):
    a = RunArtefacts(tmp_path, "abcdef12-0000-0000-0000-000000000000-cloud-full", started=STARTED)
    assert a.dir.name == "2026-10-18_2100_audit-cloud_abcdef12"
    assert a.new_path("rclone-check", "log").name == "2026-10-18_2100_audit-cloud_rclone-check.log"
    assert RunArtefacts.open(str(a.dir)).new_path("x", "log").name.startswith("2026-10-18_2100_audit-cloud_x")
