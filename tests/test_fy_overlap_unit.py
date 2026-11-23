"""A2 - pure helpers of core/fy_overlap.py (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, section 2.1, task A2).

Owner layout: source E:\\FY26-27 (siblings on the drive root), NAS \\\\nas\\backup\\FY26-27. Real drives and the real
NAS are never touched: NAS reachability is faked, folders that are needed are temp folders.
"""

import datetime as dt
import json

import pytest

from core import fy_overlap
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, WolConfig


def _config(source=r"E:\FY26-27", nas=r"\\nas\backup\FY26-27", cloud=True, lan=True):
    return AppConfig(
        paths=PathsConfig(source_drive=source, lan_destination=nas, gcs_key_path=r"C:\keys\aam-key.json"),
        lan=LanConfig(enabled=lan),
        cloud=CloudConfig(enabled=cloud, bucket="aam-test-bucket", project_number="123456789012"),
        wol=WolConfig(enabled=False),
    )


# --- next_fy / activation_date / closing_moment ---------------------------------------------------------

def test_next_fy_is_the_following_year():
    assert fy_overlap.next_fy("FY26-27") == "FY27-28"


def test_next_fy_wraps_at_99():
    assert fy_overlap.next_fy("FY99-00") == "FY00-01"


def test_next_fy_refuses_a_malformed_name():
    with pytest.raises(ValueError):
        fy_overlap.next_fy("FY2026-27")


def test_activation_date_31_december_of_the_configured_fy_start_year():
    assert fy_overlap.activation_date("FY26-27", "12-31") == dt.date(2026, 12, 31)


def test_activation_date_in_january_falls_in_the_next_calendar_year():
    assert fy_overlap.activation_date("FY26-27", "01-15") == dt.date(2027, 1, 15)


def test_activation_date_for_the_following_fy():
    assert fy_overlap.activation_date("FY27-28", "12-31") == dt.date(2027, 12, 31)


def test_activation_date_uses_the_real_calendar_in_a_leap_year():
    assert fy_overlap.activation_date("FY27-28", "02-29") == dt.date(2028, 2, 29)


def test_closing_moment_is_1_april_ist_of_the_next_fy_start_year():
    moment = fy_overlap.closing_moment("FY26-27")
    assert moment == dt.datetime(2027, 4, 1, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert moment.utcoffset() == dt.timedelta(hours=5, minutes=30)


# --- ids ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("run_id, expected", [
    ("x-cloud", None),
    ("x-cloud-FY27-28", "FY27-28"),
    ("x-rollover-final-cloud", None),
    ("u-cloud-full-FY27-28", "FY27-28"),
])
def test_fy_of_id_reads_only_the_trailing_suffix(run_id, expected):
    assert fy_overlap.fy_of_id(run_id) == expected


def test_pass_run_id_appends_the_fy():
    assert fy_overlap.pass_run_id("flow-1-cloud", "FY27-28") == "flow-1-cloud-FY27-28"


def test_artifact_key_is_lowercase_for_prefect():
    assert fy_overlap.artifact_key("cloud-backup-summary", "FY27-28") == "cloud-backup-summary-fy27-28"


# --- config_for_fy --------------------------------------------------------------------------------------

def test_config_for_fy_moves_both_paths_to_the_next_year_folders():
    nxt = fy_overlap.config_for_fy(_config(), "FY27-28")
    assert nxt.paths.source_drive == r"E:\FY27-28"
    assert nxt.paths.lan_destination == r"\\nas\backup\FY27-28"


def test_config_for_fy_has_no_colon_or_slash_mix_on_the_drive_root():
    nxt = fy_overlap.config_for_fy(_config(), "FY27-28")
    assert nxt.paths.source_drive not in (r"E:FY27-28", "E:/FY27-28")
    assert "/" not in nxt.paths.source_drive


def test_config_for_fy_works_for_a_non_root_source():
    nxt = fy_overlap.config_for_fy(_config(source=r"D:\Data\FY26-27", lan=False), "FY27-28")
    assert nxt.paths.source_drive == r"D:\Data\FY27-28"


def test_config_for_fy_keeps_a_cloud_only_placeholder_nas_path():
    nxt = fy_overlap.config_for_fy(_config(nas=r"\\nas\backup\placeholder", lan=False), "FY27-28")
    assert nxt.paths.lan_destination == r"\\nas\backup\placeholder"


def test_config_for_fy_changes_nothing_else():
    cfg = _config()
    nxt = fy_overlap.config_for_fy(cfg, "FY27-28")
    before, after = cfg.model_dump(), nxt.model_dump()
    for section in ("paths",):
        before.pop(section), after.pop(section)
    assert before == after
    assert nxt.paths.gcs_key_path == cfg.paths.gcs_key_path
    assert nxt.paths.runtime_dir == cfg.paths.runtime_dir


def test_config_for_fy_result_passes_the_cross_field_validation_again():
    nxt = fy_overlap.config_for_fy(_config(), "FY27-28")
    AppConfig.model_validate(nxt.model_dump())   # raises if the pair no longer agrees


def test_config_for_fy_refuses_a_source_that_is_not_a_year_folder():
    cfg = _config(lan=False)
    odd = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"source_drive": r"E:\Data"})})
    with pytest.raises(ValueError):
        fy_overlap.config_for_fy(odd, "FY27-28")


# --- row_fy ---------------------------------------------------------------------------------------------

def test_row_fy_reads_the_run_id_suffix_first():
    assert fy_overlap.row_fy({"run_id": "a-cloud-FY27-28", "extended_metrics": json.dumps({"fy": "FY26-27"})}) == "FY27-28"


def test_row_fy_reads_the_audit_id_suffix():
    assert fy_overlap.row_fy({"audit_id": "u-cloud-full-FY27-28"}) == "FY27-28"


def test_row_fy_falls_back_to_extended_metrics_fy():
    assert fy_overlap.row_fy({"run_id": "a-cloud", "extended_metrics": json.dumps({"fy": "FY26-27"})}) == "FY26-27"


def test_row_fy_falls_back_to_the_guard_fy_and_accepts_a_dict():
    assert fy_overlap.row_fy({"run_id": "a-cloud", "extended_metrics": {"guards": {"fy": "FY25-26"}}}) == "FY25-26"


def test_row_fy_is_none_for_a_plain_baseline_row():
    assert fy_overlap.row_fy({"run_id": "a-cloud", "extended_metrics": None}) is None
    assert fy_overlap.row_fy({"run_id": "a-cloud", "extended_metrics": "not json"}) is None


# --- ensure_nas_folder ----------------------------------------------------------------------------------

def test_ensure_nas_folder_asks_the_share_root_for_the_parent(monkeypatch):
    seen = []

    def fake_is_dir(path):
        seen.append(path)
        return False

    monkeypatch.setattr(fy_overlap, "_is_dir", fake_is_dir)
    with pytest.raises(OSError):
        fy_overlap.ensure_nas_folder(r"\\nas\backup", "FY27-28")
    assert seen == [r"\\nas\backup"]


def test_ensure_nas_folder_refuses_an_unreachable_root_and_creates_nothing(tmp_path):
    missing = tmp_path / "no-such-share"
    with pytest.raises(OSError):
        fy_overlap.ensure_nas_folder(str(missing), "FY27-28")
    assert not missing.exists()


def test_ensure_nas_folder_creates_the_folder_with_the_mount_marker(tmp_path):
    from core.guards import MOUNT_MARKER

    result = fy_overlap.ensure_nas_folder(str(tmp_path), "FY27-28")
    assert result == fy_overlap.NAS_CREATED
    assert (tmp_path / "FY27-28" / MOUNT_MARKER).is_file()


def test_ensure_nas_folder_is_idempotent_with_the_marker_present(tmp_path):
    from core.guards import MOUNT_MARKER

    fy_overlap.ensure_nas_folder(str(tmp_path), "FY27-28")
    (tmp_path / "FY27-28" / "kept.txt").write_text("x", encoding="utf-8")
    assert fy_overlap.ensure_nas_folder(str(tmp_path), "FY27-28") == fy_overlap.NAS_READY
    assert (tmp_path / "FY27-28" / MOUNT_MARKER).is_file()
    assert (tmp_path / "FY27-28" / "kept.txt").is_file()


def test_ensure_nas_folder_marks_an_empty_folder(tmp_path):
    from core.guards import MOUNT_MARKER

    (tmp_path / "FY27-28").mkdir()
    assert fy_overlap.ensure_nas_folder(str(tmp_path), "FY27-28") == fy_overlap.NAS_MARKED
    assert (tmp_path / "FY27-28" / MOUNT_MARKER).is_file()


def test_ensure_nas_folder_leaves_a_folder_with_files_and_no_marker_alone(tmp_path):
    from core.guards import MOUNT_MARKER

    folder = tmp_path / "FY27-28"
    folder.mkdir()
    (folder / "office-file.txt").write_text("x", encoding="utf-8")
    assert fy_overlap.ensure_nas_folder(str(tmp_path), "FY27-28") == fy_overlap.NAS_NEEDS_APPROVAL
    assert not (folder / MOUNT_MARKER).exists()
