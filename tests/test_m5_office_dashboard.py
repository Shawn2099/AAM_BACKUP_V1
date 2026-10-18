"""Audit 2026-10-18 item 2 (docs/EMAIL_CONTENT_SPEC.md §2): the dashboard is
reachable on the office network only (owner 2026-10-18). The "open the
dashboard" link in every email must work from the office PCs, the installer
opens the dashboard port to the office subnet only, and Prefect's API (no
login) is never offered to the network."""

import pytest
from pydantic import ValidationError

from core import notify as nt
from core.attention import AMBER, compute_attention
from core.manifest import ManifestDB
from models.config import AppConfig, DashboardConfig
from tests.test_m4_installer import INSTALLER, READINESS, ROOT

UNC = "\\\\nas\\backup\\FY26-27"


def _cfg(tmp_path, **dash) -> AppConfig:
    return AppConfig(
        paths={"source_drive": str(tmp_path), "lan_destination": UNC,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={**dash},
    )


@pytest.mark.parametrize("dash,problem", [
    ({"public_url": "http://backup-server:8080"}, False),
    ({"public_url": "http://10.10.151.20:8080/"}, False),
    ({"public_url": ""}, True),                                   # falls back to 127.0.0.1
    ({"public_url": "http://127.0.0.1:8080"}, True),
    ({"public_url": "http://localhost:8080"}, True),
    ({"public_url": "http://0.0.0.0:8080"}, True),
    ({"public_url": "backup-server:8080"}, True),                 # no http:// - not a link
])
def test_dashboard_link_problem(tmp_path, dash, problem, monkeypatch):
    monkeypatch.setattr(nt.netinfo, "all_ipv4", lambda: ["10.10.151.20"])      # this server's address
    assert bool(nt.dashboard_link_problem(_cfg(tmp_path, **dash))) is problem


def test_link_never_points_at_0_0_0_0(tmp_path, monkeypatch):
    monkeypatch.setattr(nt.socket, "gethostname", lambda: "BACKUP-SRV")
    monkeypatch.setattr(nt.netinfo, "office_ip", lambda nas_host=None: None)
    nt._link_cache.clear()
    cfg = _cfg(tmp_path, bind_address="0.0.0.0", port=8080)
    assert nt.dashboard_url(cfg) == "http://BACKUP-SRV:8080"


def test_dashboard_amber_when_email_link_only_works_on_the_server(tmp_path):
    db = ManifestDB(tmp_path / "m.db")
    try:
        bad = [i for i in compute_attention(db, _cfg(tmp_path)) if i["code"] == "email_link"]
        assert len(bad) == 1 and bad[0]["level"] == AMBER
        ok = _cfg(tmp_path, public_url="http://backup-server:8080")
        assert not [i for i in compute_attention(db, ok) if i["code"] == "email_link"]
    finally:
        db.close()


def test_firewall_addresses_default_to_the_office_subnet():
    assert DashboardConfig().firewall_remote_addresses == ["LocalSubnet"]
    DashboardConfig(firewall_remote_addresses=["10.10.151.0/24", "10.10.152.5"])
    for bad in (["Any"], ["*"], [], ["10.10.151.0/24; Any"]):
        with pytest.raises(ValidationError):
            DashboardConfig(firewall_remote_addresses=bad)


def test_firewall_addresses_change_only_on_the_server():
    from core.config_edit import LOCKED
    assert "dashboard.firewall_remote_addresses" in LOCKED


def test_installer_opens_the_dashboard_port_to_the_office_only():
    assert "New-NetFirewallRule" in INSTALLER
    rule = INSTALLER.split("New-NetFirewallRule", 1)[1].split("\n", 1)[0]
    assert "-RemoteAddress" in rule and "-Direction Inbound" in rule
    assert "firewall_remote_addresses" in INSTALLER
    # Prefect's API has no login: it stays on 127.0.0.1 and is never opened
    assert "--host 127.0.0.1 --port 4200" in INSTALLER
    assert "4200" not in rule


def test_readiness_fails_when_email_link_only_works_on_the_server():
    assert "dashboard_link_problem" in READINESS


def test_discovery_no_longer_advises_opening_prefect_to_the_network():
    text = (ROOT / "deploy" / "01_server_discovery.ps1").read_text(encoding="utf-8")
    assert "localport=4200" not in text
