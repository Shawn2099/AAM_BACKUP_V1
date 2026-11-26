"""AF-16: overlap_activate_on window, IST reuse, end_overlap, race guard, source_picker."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core import fy_overlap, source_picker
from core.manifest import ManifestDB
from core.time_utils import IST as TIME_UTILS_IST
import flow
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, RolloverConfig, WolConfig


@pytest.mark.parametrize("bad_day", ["04-01", "04-02", "06-15", "11-30"])
def test_af16_overlap_activate_on_refuses_apr_to_nov(bad_day):
    with pytest.raises(ValueError):
        RolloverConfig(overlap_activate_on=bad_day)


@pytest.mark.parametrize("good_day", ["12-01", "12-31", "01-01", "03-31"])
def test_af16_overlap_activate_on_accepts_dec_to_mar(good_day):
    cfg = RolloverConfig(overlap_activate_on=good_day)
    assert cfg.overlap_activate_on == good_day


def test_af16_fy_overlap_reuses_time_utils_ist():
    assert fy_overlap.IST is TIME_UTILS_IST


def test_af16_backup_runs_switched_fy_check_before_first_pass(tmp_path):
    src = tmp_path / "FY26-27"
    src.mkdir()
    (src / "a.txt").write_text("x", encoding="utf-8")
    key = tmp_path / "gcs.json"
    key.write_text("{}", encoding="utf-8")
    cfg = AppConfig(
        paths=PathsConfig(
            source_drive=str(src),
            lan_destination=r"\\192.168.1.50\backup\FY26-27",
            database_path=str(tmp_path / "m.db"),
            gcs_key_path=str(key),
        ),
        cloud=CloudConfig(enabled=True, bucket="test-bucket", project_number="123456"),
        lan=LanConfig(enabled=False),
        wol=WolConfig(enabled=False),
    )
    with patch("flow.load_config", return_value=cfg), \
         patch("flow.configure_logging"), \
         patch("flow.configure_prefect_bridge"), \
         patch("flow.fy_overlap.active_passes",
               return_value=[
                   SimpleNamespace(fy="FY26-27", extra=False, config=cfg),
                   SimpleNamespace(fy="FY27-28", extra=True, config=cfg),
               ]), \
         patch("flow._switched_fy", return_value="FY27-28") as sw, \
         patch("flow._run_cloud_pipeline") as rcp:
        with pytest.raises(ExceptionGroup) as eg:
            flow.backup.fn(mode="cloud")
    assert len(eg.value.exceptions) == 1
    assert isinstance(eg.value.exceptions[0], flow.PartialRun)
    assert "FY26-27->FY27-28" in str(eg.value.exceptions[0])
    sw.assert_called()
    rcp.assert_not_called()


def test_af16_source_picker_checks_next_fy_during_overlap(tmp_path):
    parent_old = tmp_path / "old_root"
    parent_old.mkdir()
    (parent_old / "FY26-27").mkdir()
    (parent_old / "FY26-27" / "a.txt").write_text("x", encoding="utf-8")

    parent_new = tmp_path / "new_root"
    parent_new.mkdir()
    (parent_new / "FY26-27").mkdir()
    (parent_new / "FY26-27" / "b.txt").write_text("y", encoding="utf-8")
    # Note: parent_new/FY27-28 does NOT exist!

    db_path = tmp_path / "m.db"
    db = ManifestDB(str(db_path))
    db.set_overlap_record("FY27-28", {
        "activated_at": "2027-01-01T06:00:00+05:30",
        "from_fy": "FY26-27",
        "source_folder": str(parent_old / "FY27-28"),
        "clock": "CLOCK_OK",
    })
    db.close()

    cfg = AppConfig(
        paths=PathsConfig(
            source_drive=str(parent_old / "FY26-27"),
            lan_destination=r"\\192.168.1.50\backup\FY26-27",
            database_path=str(db_path),
        ),
        cloud=CloudConfig(enabled=False, bucket="test-bucket", project_number="123456"),
        lan=LanConfig(enabled=True),
        wol=WolConfig(enabled=False),
    )
    res = source_picker.check_source_parent(cfg, str(parent_new))
    assert res["ok"] is False
    assert any("FY27-28" in p for p in res["problems"])
