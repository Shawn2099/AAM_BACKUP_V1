"""M1 / plan §5.7 — config hygiene.

* Unknown keys fail loudly (a typo must never silently fall back to a
  default — e.g. `shutdown_after_bakup: false` used to leave NAS shutdown ON).
* Documented deprecated keys are dropped with a warning instead of failing.
* CONFIG_PATH is absolute (AAM_CONFIG env, else <project>/config.yaml), so a
  service started from another working directory loads the right file.
"""

import importlib
from pathlib import Path

import pytest
from pydantic import ValidationError

import models.config as config_mod
from models.config import AppConfig

UNC_DEST = "\\\\nas\\backup\\FY26-27"


def _base(**overrides) -> dict:
    data = {
        "paths": {"source_drive": "E:\\FY26-27", "lan_destination": UNC_DEST},
        "wol": {"enabled": False},
        "cloud": {"enabled": False},
        "dashboard": {"auth_enabled": False},
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return data


@pytest.mark.parametrize(
    "section, key",
    [
        ("lan", "shutdown_after_bakup"),        # typo of a safety switch
        ("paths", "source_drive_typo"),
        ("cloud", "bandwith_limit"),
        ("notifications", "send_on_failur"),
        ("maintenance", "db_retention"),
        ("schedule", "lan_corn"),
        ("dashboard", "auth"),
        ("health", "clock_skew"),
        ("wol", "mac"),
    ],
)
def test_unknown_key_in_any_section_is_rejected(section, key):
    with pytest.raises(ValidationError) as exc:
        AppConfig(**_base(**{section: {key: False}}))
    assert key in str(exc.value)


def test_unknown_top_level_section_is_rejected():
    data = _base()
    data["lann"] = {"enabled": True}
    with pytest.raises(ValidationError) as exc:
        AppConfig(**data)
    assert "lann" in str(exc.value)


def test_deprecated_key_is_dropped_with_warning(tmp_path, caplog):
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        cfg_file = tmp_path / "config.yaml"
        import yaml

        data = _base(notifications={"send_on_success": True})
        cfg_file.write_text(yaml.safe_dump(data), encoding="utf-8")
        cfg = config_mod.load_config(str(cfg_file))
    finally:
        logger.remove(sink_id)
    assert not hasattr(cfg.notifications, "send_on_success")
    assert any("send_on_success" in m and "deprecated" in m.lower() for m in messages)


def test_real_config_files_load():
    project = Path(config_mod.__file__).resolve().parent.parent
    for name in ("config.example.yaml",):
        config_mod.load_config(str(project / name))


def test_config_path_is_absolute_project_file(monkeypatch):
    monkeypatch.delenv("AAM_CONFIG", raising=False)
    mod = importlib.reload(config_mod)
    try:
        project = Path(mod.__file__).resolve().parent.parent
        assert Path(mod.CONFIG_PATH).is_absolute()
        assert Path(mod.CONFIG_PATH) == project / "config.yaml"
    finally:
        monkeypatch.undo()  # restore AAM_CONFIG (test sandbox) BEFORE reload
        importlib.reload(config_mod)


def test_config_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom.yaml"
    monkeypatch.setenv("AAM_CONFIG", str(target))
    mod = importlib.reload(config_mod)
    try:
        assert Path(mod.CONFIG_PATH) == target.resolve()
    finally:
        monkeypatch.undo()  # restore AAM_CONFIG (test sandbox) BEFORE reload
        importlib.reload(config_mod)


def test_rollover_default_config_path_is_the_shared_constant():
    import inspect

    from core import fy_rollover

    default = inspect.signature(fy_rollover.rollover).parameters["config_path"].default
    assert default is None or Path(default).is_absolute()


# ── M1 1.8: bool exit codes are never usable (kept with the M1 tests)

@pytest.mark.parametrize("code", [True, False, None, -1])
def test_exit_code_deciders_reject_non_int(code):
    from core.cloud_sync import classify_rclone_exit
    from core.lan_sync import classify_exit_code, decide_lan_result

    log = "   Files :   1   1   0   0   0   0\n   Ended : x\n"
    assert decide_lan_result(code, log)["status"] == "LAN_FAILED"
    assert classify_exit_code(code) == "LAN_FAILED"
    assert classify_rclone_exit(code) == "CLOUD_FAILED"
