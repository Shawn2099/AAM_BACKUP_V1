"""I-4 (fresh-install plan R-4): Settings refuses a Wake-on-LAN target that is not the machine the NAS path
points at, and the documentation placeholders. The wake-up and the remote shutdown (shutdown /m to server_ip)
would otherwise go to another machine than the backup. Not part of AppConfig loading (it would stop existing
sites on upgrade)."""

from test_support import ui_login  # noqa: E402,F401
import pytest

from core import config_edit as ce
from tests.test_m4_settings import _apply, _write_config  # noqa: F401  (fixture helpers)

MAC = "00:11:22:33:44:55"
NAS_IP_PATH = "\\\\10.10.151.53\\share\\FY26-27"


@pytest.fixture
def cfg_file(tmp_path):
    return _write_config(tmp_path)


def _wol_on(host_ip="10.10.151.53", mac=MAC, dest=NAS_IP_PATH):
    return {"wol.enabled": True, "wol.mac_address": mac, "wol.server_ip": host_ip,
            "paths.lan_destination": dest}


def test_wol_host_that_differs_from_the_nas_path_is_refused(cfg_file):
    before = cfg_file.read_bytes()
    with pytest.raises(ce.ConfigEditError) as err:
        _apply(cfg_file, _wol_on(host_ip="10.10.186.231"), phrase="x")
    assert err.value.status == 400
    shown = str(err.value.extra["errors"])
    assert "10.10.186.231" in shown and "10.10.151.53" in shown
    assert cfg_file.read_bytes() == before


def test_the_live_check_gives_the_same_verdict(cfg_file):
    assert ce.validate_changes(str(cfg_file), _wol_on(host_ip="10.10.186.231"))["ok"] is False


@pytest.mark.parametrize("changes", [
    _wol_on(mac="AA:BB:CC:DD:EE:FF"),
    _wol_on(host_ip="192.0.2.10", dest="\\\\192.0.2.10\\share\\FY26-27"),
])
def test_documentation_placeholders_are_refused(cfg_file, changes):
    assert ce.validate_changes(str(cfg_file), changes)["ok"] is False


def test_matching_host_is_accepted(cfg_file):
    assert ce.validate_changes(str(cfg_file), _wol_on())["ok"] is True


def test_a_name_in_the_nas_path_is_only_a_warning(cfg_file):
    assert ce.validate_changes(str(cfg_file), _wol_on(dest="\\\\nas01\\share\\FY26-27"))["ok"] is True


def test_wol_off_ignores_a_stale_server_ip(cfg_file):
    changes = {"wol.server_ip": "10.10.186.231", "paths.lan_destination": NAS_IP_PATH}
    assert ce.validate_changes(str(cfg_file), changes)["ok"] is True


def test_an_unrelated_save_is_not_blocked_by_an_existing_mismatch(cfg_file):
    """The check looks at what this save touches, so a site already in this state can still change other settings."""
    text = cfg_file.read_text(encoding="utf-8")
    text = text.replace("wol:\n  enabled: false\n",
                        f"wol:\n  enabled: true\n  mac_address: '{MAC}'\n  server_ip: '10.9.9.9'\n")
    text = text.replace("\\\\nas\\share\\FY26-27", NAS_IP_PATH)
    cfg_file.write_text(text, encoding="utf-8")
    assert ce.validate_changes(str(cfg_file), {"budget.lan_max_hours": 5})["ok"] is True
