"""B2/B3 (final audit 2026-10-20, owner decision): the deletion limits are controlled from the main Settings tab.

`guards.max_delete_files` / `guards.max_delete_percent` (one pair for the Cloud and the Office copy) were
Advanced-only. They are basic settings now: visible and savable with the dashboard login, changing them still
needs the typed phrase (the whole `guards` section is risky), and the next backup's G3 uses the saved value."""

from test_support import ui_login  # noqa: E402
import pytest

from core import config_edit as ce
from core import guards
from core.config_help import HELP
from models.config import load_config

LIMITS = ("guards.max_delete_files", "guards.max_delete_percent")


def _write_config(tmp_path):
    src = tmp_path / "src" / "FY26-27"
    src.mkdir(parents=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "paths:\n"
        f"  source_drive: '{src}'\n"
        "  lan_destination: '\\\\nas\\share\\FY26-27'\n"
        f"  runtime_dir: '{tmp_path / 'rt'}'\n"
        "  gcs_key_path: ''\n"
        "cloud:\n  enabled: false\n"
        "wol:\n  enabled: false\n"
        "notifications:\n  recipients: ['a@firm.com']\n"
        + ui_login.DASHBOARD_YAML, encoding="utf-8")
    return cfg


def _save(cfg, changes, phrase=None):
    return ce.apply_changes(str(cfg), changes, phrase=phrase, actor="owner", lock_busy=lambda: None,
                            allow_advanced=False)       # the dashboard login only, no Advanced session


def test_both_limits_are_basic_settings():
    assert set(LIMITS) <= ce.BASIC_KEYS


def test_the_basic_catalog_shows_them_with_units_and_the_risky_flag(tmp_path):
    cfg = load_config(str(_write_config(tmp_path)))
    fields = {f["key"]: f for s in ce.catalog(cfg, include_advanced=False) for f in s["fields"]}
    for key in LIMITS:
        assert fields[key]["level"] == "basic" and fields[key]["risky"] is True
    assert fields["guards.max_delete_files"]["unit"] == "files"
    assert fields["guards.max_delete_percent"]["unit"] == "%"
    # the other safety checks stay with support
    assert "guards.max_source_shrink_percent" not in fields


def test_changing_them_needs_the_typed_phrase_but_not_the_advanced_password(tmp_path):
    cfg = _write_config(tmp_path)
    with pytest.raises(ce.ConfigEditError) as refused:
        _save(cfg, {"guards.max_delete_files": 100})
    assert refused.value.status == 409 and refused.value.extra["confirm_phrase"] == "APPROVE"
    _save(cfg, {"guards.max_delete_files": 100, "guards.max_delete_percent": 5}, phrase="APPROVE")
    saved = load_config(str(cfg)).guards
    assert (saved.max_delete_files, saved.max_delete_percent) == (100, 5)


def test_a_support_only_guard_still_needs_the_advanced_password(tmp_path):
    with pytest.raises(ce.ConfigEditError) as refused:
        _save(_write_config(tmp_path), {"guards.max_source_shrink_percent": 50}, phrase="APPROVE")
    assert refused.value.status == 403


def test_the_next_backups_g3_uses_the_saved_limits(tmp_path):
    cfg = _write_config(tmp_path)
    _save(cfg, {"guards.max_delete_files": 100, "guards.max_delete_percent": 5}, phrase="APPROVE")
    g = load_config(str(cfg)).guards
    assert guards.g3_mass_delete(100, 10_000, g.max_delete_files, g.max_delete_percent).passed is True
    assert guards.g3_mass_delete(101, 10_000, g.max_delete_files, g.max_delete_percent).passed is False


def test_the_help_explains_what_the_two_numbers_mean_together():
    files, percent = HELP["guards.max_delete_files"], HELP["guards.max_delete_percent"]
    text = " ".join(files + percent)
    assert "already backed up" in text and "stricter" in text and "0" in text
    # the worked example in the help is computed by the real rule
    allowed = guards.max_delete_allowance(20_000, 500, 10)
    assert f"{allowed:,}" in text and "20,000" in text
