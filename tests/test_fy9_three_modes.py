"""FY-9 (plan docs/PLAN_FY_FIXES_2026-10-22.md): cloud-only, LAN-only and both all work.

The SOURCE folder decides the financial year. The NAS path only counts while the NAS leg is on (then it must
match); with the NAS off it is a placeholder that nothing reads and the rollover never rewrites."""

import pytest
from pydantic import ValidationError

from core import fy_rollover as fr
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig
from tests.test_config_cross_field import _quiet_legs
from tests.test_a5_rollover_steps import _config

SRC_FY, SRC_NONE = "E:\\DATA\\FY26-27", "E:\\DATA"
NAS_FY, NAS_OTHER, NAS_NONE = "\\\\nas\\share\\FY26-27", "\\\\nas\\share\\FY25-26", "\\\\nas\\share"


@pytest.mark.parametrize("source, nas, expected", [
    (SRC_FY, NAS_FY, "FY26-27"), (SRC_FY, NAS_NONE, "FY26-27"), (SRC_FY, NAS_OTHER, "FY26-27"),
    (SRC_NONE, NAS_FY, None), (SRC_NONE, NAS_NONE, None),
])
def test_the_source_leaf_alone_decides_the_year(source, nas, expected):
    assert fr.configured_fy(source, nas) == expected
    assert fr.detect_rollover(source, nas) == (expected is not None and expected != fr.get_fy_prefix())


def _cloud_only(tmp_path, source, nas):
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}')
    return AppConfig(
        paths=PathsConfig(source_drive=source, lan_destination=nas, gcs_key_path=str(key)),
        lan=LanConfig(enabled=False),
        **_quiet_legs(cloud=CloudConfig(enabled=True, bucket="my-bucket", project_number="123")))


@pytest.mark.parametrize("source, nas", [
    (SRC_FY, NAS_NONE),      # a plain placeholder
    (SRC_FY, NAS_OTHER),     # a placeholder from another year: nothing reads it
    (SRC_FY, NAS_FY),
])
def test_cloud_only_loads_whatever_the_unused_nas_path_looks_like(tmp_path, source, nas):
    assert _cloud_only(tmp_path, source, nas).lan.enabled is False


@pytest.mark.parametrize("nas", [NAS_FY, NAS_NONE])
def test_the_cloud_backup_needs_a_year_folder_so_its_cloud_folder_never_changes_by_itself(tmp_path, nas):
    """Owner 2026-10-22: a year folder is mandatory whenever the cloud backup is on (predictable; no calendar
    fallback that renames the cloud folder at midnight on 1 April)."""
    with pytest.raises(ValidationError, match="year folder") as err:
        _cloud_only(tmp_path, SRC_NONE, nas)
    assert "FY26-27" in str(err.value) and "cloud" in str(err.value).lower()


def test_a_nas_only_install_needs_the_year_folder_too():
    with pytest.raises(ValidationError, match="financial-year folder"):
        AppConfig(paths=PathsConfig(source_drive=SRC_NONE, lan_destination=NAS_NONE),
                  lan=LanConfig(enabled=True), **_quiet_legs())


@pytest.mark.parametrize("source, nas, word", [
    (SRC_NONE, NAS_FY, "financial-year folder"), (SRC_FY, NAS_NONE, "both"), (SRC_FY, NAS_OTHER, "same"),
])
def test_with_the_nas_on_the_paths_must_agree(source, nas, word):
    with pytest.raises(ValidationError, match=word) as err:
        AppConfig(paths=PathsConfig(source_drive=source, lan_destination=nas),
                  lan=LanConfig(enabled=True), **_quiet_legs())
    assert "Advanced" in str(err.value)           # paths are Advanced settings: say where to fix it


def test_the_four_digit_trap_is_not_checked_on_an_unused_nas_path(tmp_path):
    assert _cloud_only(tmp_path, SRC_FY, "\\\\nas\\share\\FY2026-27").lan.enabled is False
    with pytest.raises(ValidationError, match="4-digit"):
        AppConfig(paths=PathsConfig(source_drive=SRC_FY, lan_destination="\\\\nas\\share\\FY2026-27"),
                  lan=LanConfig(enabled=True), **_quiet_legs())


# ── the rollover's config switch ──────────────────────────────────────────────

def _cloud_only_yaml(tmp_path, source, nas):
    from test_support import ui_login

    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}')
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "firm_name: Test Firm\n"
        "paths:\n"
        f"  source_drive: '{source}'\n"
        f"  lan_destination: '{nas}'\n"
        f"  runtime_dir: '{tmp_path / 'rt'}'\n"
        f"  gcs_key_path: '{key}'\n"
        "cloud:\n  enabled: true\n  bucket: my-bucket\n  project_number: '123'\n"
        "lan:\n  enabled: false\n"
        "wol:\n  enabled: false\n" + ui_login.DASHBOARD_YAML, encoding="utf-8")
    return cfg


def _switch(tmp_path, source, nas, cloud_only=False):
    cfg = _cloud_only_yaml(tmp_path, source, nas) if cloud_only else _config(tmp_path, source, nas)
    fr.update_config_yaml(str(cfg), str(tmp_path / "SRC"), "\\\\nas\\share", "FY27-28", expect_fy="FY26-27")
    return cfg.read_text(encoding="utf-8")


def test_the_switch_leaves_a_placeholder_nas_path_without_a_year_folder_untouched(tmp_path):
    text = _switch(tmp_path, tmp_path / "SRC" / "FY26-27", "\\\\nas\\share", cloud_only=True)
    assert "FY27-28" in text and "'\\\\nas\\share'" in text and text.count("FY27-28") == 1


def test_the_switch_moves_a_matching_nas_path_with_the_source(tmp_path):
    text = _switch(tmp_path, tmp_path / "SRC" / "FY26-27", "\\\\nas\\share\\FY26-27")
    assert text.count("FY27-28") == 2 and "FY26-27" not in text


def test_the_switch_ignores_an_unrelated_year_on_an_unused_nas_path_when_deciding_where_the_config_is(tmp_path):
    text = _switch(tmp_path, tmp_path / "SRC" / "FY26-27", "\\\\nas\\share\\FY25-26", cloud_only=True)
    assert "FY27-28" in text           # the source says FY26-27: that is the year being closed


def test_a_source_without_an_fy_folder_is_never_rewritten(tmp_path):
    cfg = _config(tmp_path, tmp_path / "SRC", "\\\\nas\\share\\FY26-27")
    before = cfg.read_bytes()
    with pytest.raises(fr.RolloverError):
        fr.update_config_yaml(str(cfg), str(tmp_path / "SRC"), "\\\\nas\\share", "FY27-28", expect_fy="FY26-27")
    assert cfg.read_bytes() == before


# ── drive-root layout: D:\FY25-26 (owner's example) ───────────────────────────

def test_a_drive_root_keeps_its_backslash_so_the_new_path_is_clean():
    assert fr._parent_path("D:\\FY25-26") == "D:\\"
    assert fr._child_path(fr._parent_path("D:\\FY25-26"), "FY26-27") == "D:\\FY26-27"
    assert fr._child_path(fr._parent_path("D:\\archives\\FY10-11"), "FY11-12") == "D:\\archives\\FY11-12"
    assert fr._child_path(fr._parent_path("\\\\nas\\share\\FY25-26"), "FY26-27") == "\\\\nas\\share\\FY26-27"
    assert fr._child_path(fr._parent_path("/data/FY25-26"), "FY26-27") == "/data/FY26-27"


def test_the_switch_on_a_drive_root_writes_a_clean_windows_path(tmp_path):
    cfg = _cloud_only_yaml(tmp_path, "D:\\FY26-27", "\\\\nas\\share\\FY26-27")
    fr.update_config_yaml(str(cfg), fr._parent_path("D:\\FY26-27"), "\\\\nas\\share", "FY27-28",
                          expect_fy="FY26-27")
    text = cfg.read_text(encoding="utf-8")
    assert "D:\\FY27-28" in text and "D:/" not in text
