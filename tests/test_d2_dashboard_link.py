"""Plan D2/D3: the dashboard link in emails is resolved from the network, and a wrong manual
`dashboard.public_url` or an unreachable bind address is reported (never a dead link)."""
from types import SimpleNamespace

import pytest

from core import netinfo
from core import notify as nt


def _cfg(public_url="", bind="0.0.0.0", port=8080, nas="10.0.0.9"):
    return SimpleNamespace(
        dashboard=SimpleNamespace(public_url=public_url, bind_address=bind, port=port),
        wol=SimpleNamespace(server_ip=nas), paths=SimpleNamespace(lan_destination=r"\\10.0.0.9\share"))


@pytest.fixture(autouse=True)
def _network(monkeypatch):
    nt._link_cache.clear()
    monkeypatch.setattr(netinfo, "office_ip", lambda nas_host=None: "10.10.151.20")
    monkeypatch.setattr(netinfo, "all_ipv4", lambda: ["10.10.151.20", "172.16.0.2"])
    monkeypatch.setattr(nt.socket, "gethostname", lambda: "backup-server")
    yield
    nt._link_cache.clear()


def test_empty_public_url_and_all_interfaces_uses_the_detected_office_address():
    assert nt.dashboard_url(_cfg()) == "http://10.10.151.20:8080"
    assert nt.dashboard_link_problem(_cfg()) is None


def test_the_hostname_is_offered_as_the_alternative():
    assert nt.dashboard_urls(_cfg()) == ("http://10.10.151.20:8080", "http://backup-server:8080")


def test_without_a_detected_address_the_hostname_is_the_link(monkeypatch):
    monkeypatch.setattr(netinfo, "office_ip", lambda nas_host=None: None)
    assert nt.dashboard_url(_cfg()) == "http://backup-server:8080"
    assert nt.dashboard_link_problem(_cfg()) is None


def test_a_specific_bind_address_is_the_link_when_it_is_usable():
    assert nt.dashboard_url(_cfg(bind="172.16.0.2")) == "http://172.16.0.2:8080"


def test_bind_to_another_address_than_the_detected_one_is_reported():
    problem = nt.dashboard_link_problem(_cfg(bind="172.16.0.2", public_url="http://10.10.151.20:8080"))
    assert problem and "10.10.151.20" in problem and "172.16.0.2" in problem


def test_loopback_bind_without_public_url_is_a_dead_link_and_reported():
    assert nt.dashboard_url(_cfg(bind="127.0.0.1")) == "http://127.0.0.1:8080"
    assert "opens only on the server" in nt.dashboard_link_problem(_cfg(bind="127.0.0.1"))


def test_a_manual_public_url_is_used_as_written():
    assert nt.dashboard_url(_cfg(public_url="http://backup-server:8080")) == "http://backup-server:8080"
    assert nt.dashboard_link_problem(_cfg(public_url="http://backup-server:8080")) is None


def test_a_manual_ip_that_is_not_an_address_of_this_server_is_reported():
    problem = nt.dashboard_link_problem(_cfg(public_url="http://10.10.151.99:8080"))
    assert problem and "10.10.151.99" in problem and "10.10.151.20" in problem


def test_a_manual_ip_that_is_an_address_of_this_server_is_fine():
    assert nt.dashboard_link_problem(_cfg(public_url="http://172.16.0.2:8080")) is None


def test_the_answer_is_cached_for_a_short_time(monkeypatch):
    calls = []
    monkeypatch.setattr(netinfo, "office_ip", lambda nas_host=None: calls.append(1) or "10.10.151.20")
    nt.dashboard_url(_cfg())
    nt.dashboard_url(_cfg())
    assert len(calls) == 1
