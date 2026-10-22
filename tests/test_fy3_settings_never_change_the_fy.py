"""FY-3 (plan docs/PLAN_FY_FOLDERS_2026-10-22.md): Settings can move the backups to another drive or parent
folder, but can NEVER change the financial year. Only the rollover does that (final backup of the old year,
archive, journal). A hand switch in Settings was invisible to the rollover: no final backup, no archive, no alert."""

from test_support import ui_login  # noqa: E402,F401
import pytest

from core import config_edit as ce
from models.config import load_config
from tests.test_m4_settings import _apply, _write_config  # noqa: F401  (fixture helpers)

NAS = "\\\\nas\\share\\"


@pytest.fixture
def cfg_file(tmp_path):
    return _write_config(tmp_path)


def _both(tmp_path, source_fy, nas_fy, parent="src"):
    return {"paths.source_drive": str(tmp_path / parent / source_fy),
            "paths.lan_destination": NAS + nas_fy}


@pytest.mark.parametrize("fy", ["FY27-28", "FY25-26"])
def test_switching_both_paths_to_another_fy_is_refused(tmp_path, cfg_file, fy):
    before = cfg_file.read_bytes()
    with pytest.raises(ce.ConfigEditError, match="Run FY rollover") as err:
        _apply(cfg_file, _both(tmp_path, fy, fy), phrase="x")
    assert err.value.status == 409
    assert cfg_file.read_bytes() == before


def test_the_live_check_gives_the_same_verdict(tmp_path, cfg_file):
    verdict = ce.validate_changes(str(cfg_file), _both(tmp_path, "FY27-28", "FY27-28"))
    assert verdict["ok"] is False and "Run FY rollover" in verdict["detail"]


def test_a_new_drive_or_parent_with_the_same_fy_is_allowed(tmp_path, cfg_file):
    changes = _both(tmp_path, "FY26-27", "FY26-27", parent="moved")
    verdict = ce.validate_changes(str(cfg_file), changes)
    assert verdict["ok"] is True and verdict["risky"]          # still a risky, typed-phrase change
    phrase = verdict["confirm_phrase"]
    _apply(cfg_file, changes, phrase=phrase)
    assert load_config(str(cfg_file)).paths.source_drive == changes["paths.source_drive"]


def test_turning_the_fy_folders_off_or_on_is_a_support_edit(tmp_path, cfg_file):
    off = {"paths.source_drive": str(tmp_path / "src"), "paths.lan_destination": "\\\\nas\\share"}
    with pytest.raises(ce.ConfigEditError, match="support") as err:
        _apply(cfg_file, off, phrase="x")
    assert err.value.status == 409


def test_settings_that_are_not_paths_are_not_affected(cfg_file):
    changes = {"budget.lan_max_hours": 5}
    verdict = ce.validate_changes(str(cfg_file), changes)
    assert verdict["ok"] is True


def test_the_two_folder_paths_are_advanced_settings():
    for key in ("paths.source_drive", "paths.lan_destination"):
        assert key not in ce.BASIC_KEYS and ce.is_risky(key), key
