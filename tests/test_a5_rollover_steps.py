"""Plan A5: the rollover steps as idempotent functions.

Every function can run twice (a resumed rollover repeats whatever it cannot prove done):
folders are never overwritten, the NAS folder is only created where the share root exists
(F-9), and the config switch is compare-and-set with three outcomes decided under the lock.
"""

from test_support import ui_login  # noqa: E402
import pytest
from filelock import FileLock

from core import config_file, fy_rollover
from core.guards import MOUNT_MARKER


def _config(tmp_path, source, lan, extra=""):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# operator comment\n"
        "firm_name: Test Firm\n"
        "paths:\n"
        f"  source_drive: '{source}'\n"
        f"  lan_destination: '{lan}'\n"
        f"  runtime_dir: '{tmp_path / 'rt'}'\n"
        "  gcs_key_path: ''\n"
        "cloud:\n  enabled: false\n"
        "wol:\n  enabled: false\n"
        + ui_login.DASHBOARD_YAML + extra,
        encoding="utf-8")
    return cfg


# ---- folders ------------------------------------------------------------------------------

def test_source_folder_and_placeholder_are_created_once_and_never_overwritten(tmp_path):
    root = tmp_path / "SRC"
    root.mkdir()
    made = fy_rollover.create_source_folder(str(root), "FY27-28", "README_FY.txt")
    assert made.is_dir() and (made / "README_FY.txt").is_file()
    (made / "README_FY.txt").write_text("edited by the office", encoding="utf-8")
    fy_rollover.create_source_folder(str(root), "FY27-28", "README_FY.txt")   # second run
    assert (made / "README_FY.txt").read_text(encoding="utf-8") == "edited by the office"


def test_nas_folder_gets_the_mount_marker_and_is_idempotent(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    made = fy_rollover.create_nas_folder(str(share), "FY27-28")
    assert made == share / "FY27-28" and (made / MOUNT_MARKER).is_file()
    fy_rollover.create_nas_folder(str(share), "FY27-28")                      # second run
    assert (made / MOUNT_MARKER).is_file()


def test_nas_folder_is_not_created_when_the_share_root_is_missing(tmp_path):
    missing = tmp_path / "nas-is-off" / "share"
    with pytest.raises(OSError, match="not reachable"):
        fy_rollover.create_nas_folder(str(missing), "FY27-28")
    assert not (tmp_path / "nas-is-off").exists()      # mkdir(parents=True) must not invent it


# ---- config switch: compare-and-set --------------------------------------------------------

def test_switch_from_the_expected_fy_writes_and_returns_the_backup(tmp_path):
    cfg = _config(tmp_path, tmp_path / "SRC" / "FY26-27", "\\\\nas\\share\\FY26-27")
    backup = fy_rollover.update_config_yaml(
        str(cfg), str(tmp_path / "SRC"), "\\\\nas\\share", "FY27-28", expect_fy="FY26-27")
    assert backup is not None and backup.exists()
    text = cfg.read_text(encoding="utf-8")
    assert "FY27-28" in text and "FY26-27" not in text and "# operator comment" in text


def test_switch_when_already_at_the_new_fy_changes_nothing(tmp_path):
    # R3: a crash after the config write but before the journal/row was saved
    cfg = _config(tmp_path, tmp_path / "SRC" / "FY27-28", "\\\\nas\\share\\FY27-28")
    before = cfg.read_bytes()
    result = fy_rollover.update_config_yaml(
        str(cfg), str(tmp_path / "SRC"), "\\\\nas\\share", "FY27-28", expect_fy="FY26-27")
    assert result is None
    assert cfg.read_bytes() == before
    assert not list(tmp_path.glob("config.yaml.bak-*"))


def test_switch_refuses_when_the_config_points_somewhere_else(tmp_path):
    cfg = _config(tmp_path, tmp_path / "SRC" / "FY25-26", "\\\\nas\\share\\FY25-26")
    before = cfg.read_bytes()
    with pytest.raises(fy_rollover.RolloverError, match="FY25-26"):
        fy_rollover.update_config_yaml(
            str(cfg), str(tmp_path / "SRC"), "\\\\nas\\share", "FY27-28", expect_fy="FY26-27")
    assert cfg.read_bytes() == before


def test_switch_compare_happens_under_the_lock(tmp_path):
    cfg = _config(tmp_path, tmp_path / "SRC" / "FY26-27", "\\\\nas\\share\\FY26-27",
                  "maintenance:\n  config_lock_timeout_seconds: 1\n")
    with FileLock(str(cfg) + ".lock"):
        with pytest.raises(config_file.ConfigWriteError, match="in use"):
            fy_rollover.update_config_yaml(
                str(cfg), str(tmp_path / "SRC"), "\\\\nas\\share", "FY27-28",
                expect_fy="FY26-27")


def test_without_expect_fy_the_old_unconditional_behaviour_remains(tmp_path):
    cfg = _config(tmp_path, tmp_path / "SRC" / "FY26-27", "\\\\nas\\share\\FY26-27")
    assert fy_rollover.update_config_yaml(
        str(cfg), str(tmp_path / "SRC"), "\\\\nas\\share", "FY27-28") is not None
