"""core.notify._nas_host: the host the backup really connects to (the NAS path), not a stale wol.server_ip
that is kept while Wake-on-LAN is off (trial box carried 10.10.186.231 into the alert link probe)."""

from types import SimpleNamespace as NS

from core import notify

NAS_PATH = "\\\\10.10.151.53\\share\\FY26-27"


def _cfg(dest, wol_on, ip="10.10.186.231"):
    return NS(wol=NS(enabled=wol_on, server_ip=ip), paths=NS(lan_destination=dest))


def test_the_nas_path_host_wins_over_a_stale_wol_ip():
    assert notify._nas_host(_cfg(NAS_PATH, wol_on=False)) == "10.10.151.53"
    assert notify._nas_host(_cfg(NAS_PATH, wol_on=True)) == "10.10.151.53"


def test_wol_ip_is_only_a_fallback_when_wol_is_on():
    assert notify._nas_host(_cfg("", wol_on=True)) == "10.10.186.231"
    assert notify._nas_host(_cfg("", wol_on=False)) is None
