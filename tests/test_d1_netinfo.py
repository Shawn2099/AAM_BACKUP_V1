"""Plan D1: the office address of the server (default route first, NAS route second)."""
import socket

import pytest

from core import netinfo


class _FakeSocket:
    def __init__(self, routes):
        self.routes, self.target = routes, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def connect(self, addr):
        self.target = addr[0]
        if self.target not in self.routes:
            raise OSError("Network is unreachable")

    def getsockname(self):
        return (self.routes[self.target], 12345)


def _routes(monkeypatch, routes):
    monkeypatch.setattr(netinfo.socket, "socket", lambda *a, **k: _FakeSocket(routes))


@pytest.mark.parametrize("address,ok", [
    ("10.10.151.20", True), ("192.168.1.5", True), ("127.0.0.1", False), ("0.0.0.0", False),
    ("169.254.3.4", False), ("224.0.0.1", False), ("::1", False), ("", False), (None, False),
    ("not-an-ip", False)])
def test_usable(address, ok):
    assert netinfo.usable(address) is ok


def test_default_route_address_wins_over_the_nas_route(monkeypatch):
    _routes(monkeypatch, {netinfo.DEFAULT_ROUTE_PROBE: "10.10.151.20", "10.0.0.9": "172.16.0.2"})
    assert netinfo.office_ip("10.0.0.9") == "10.10.151.20"


def test_nas_route_is_the_fallback_when_there_is_no_default_route(monkeypatch):
    _routes(monkeypatch, {"10.0.0.9": "172.16.0.2"})
    assert netinfo.office_ip("10.0.0.9") == "172.16.0.2"


def test_no_network_gives_none_never_loopback(monkeypatch):
    _routes(monkeypatch, {})
    assert netinfo.office_ip("10.0.0.9") is None
    _routes(monkeypatch, {netinfo.DEFAULT_ROUTE_PROBE: "127.0.0.1"})
    assert netinfo.office_ip() is None


def test_all_ipv4_lists_only_usable_addresses(monkeypatch):
    monkeypatch.setattr(netinfo.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, 0, 0, "", ("10.10.151.20", 0)), (socket.AF_INET, 0, 0, "", ("127.0.0.1", 0)),
        (socket.AF_INET, 0, 0, "", ("169.254.1.1", 0)), (socket.AF_INET, 0, 0, "", ("10.10.151.20", 0))])
    assert netinfo.all_ipv4() == ["10.10.151.20"]


def test_real_udp_connect_sends_nothing_and_does_not_raise():
    assert netinfo.local_ip_for("127.0.0.1") is None            # loopback target -> loopback source
