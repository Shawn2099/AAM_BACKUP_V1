"""Tests for wol — REAL sockets, REAL magic packets, REAL timeouts."""

import socket
import time

import pytest

from core.wol import (
    WolTimeout,
    _send_magic_packet,
    _smb_port_open,
    ensure_server_online,
    wait_for_server,
)
from models.config import (
    AppConfig,
    CloudConfig,
    DashboardConfig,
    PathsConfig,
    WolConfig,
)


@pytest.fixture
def tcp_listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    yield srv.getsockname()
    srv.close()


@pytest.fixture
def closed_tcp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _magic_payload(mac: str) -> bytes:
    hex_str = mac.replace(":", "").replace("-", "")
    return b"\xff" * 6 + bytes.fromhex(hex_str) * 16


class TestSmbPortOpen:

    def test_listening_port_returns_true(self, tcp_listener):
        ip, port = tcp_listener
        assert _smb_port_open(ip, port=port) is True

    def test_closed_port_returns_false(self, closed_tcp_port):
        assert _smb_port_open("127.0.0.1", port=closed_tcp_port) is False

    def test_unroutable_address_returns_false(self):
        started = time.monotonic()
        assert _smb_port_open("203.0.113.1", timeout=1.0) is False
        assert time.monotonic() - started < 10

    def test_invalid_port_too_large_returns_false(self):
        assert _smb_port_open("127.0.0.1", port=99999) is False

    def test_negative_port_returns_false(self):
        assert _smb_port_open("127.0.0.1", port=-5) is False

    def test_non_int_port_returns_false(self):
        assert _smb_port_open("127.0.0.1", port="445") is False

    def test_custom_port_and_timeout(self, tcp_listener):
        ip, port = tcp_listener
        assert _smb_port_open(ip, port=port, timeout=10.0) is True


class TestSendMagicPacket:

    MAC = "AA-BB-CC-DD-EE-FF"

    @pytest.fixture
    def udp_magic_receiver(self):
        packets = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 9))
        sock.settimeout(0.2)

        import threading

        def _drain():
            while True:
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                packets.append((data, addr))

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        yield packets
        sock.close()

    def test_packet_actually_received_on_wire(self, udp_magic_receiver):
        _send_magic_packet(self.MAC, "127.0.0.1", repeat=1, interval=1)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not udp_magic_receiver:
            time.sleep(0.05)

        payloads = [data for data, _addr in udp_magic_receiver]
        expected = _magic_payload(self.MAC)
        assert expected in payloads

    def test_repeat_sends_multiple_rounds_with_real_interval(
        self, udp_magic_receiver
    ):
        interval = 2
        started = time.monotonic()
        _send_magic_packet(self.MAC, "127.0.0.1", repeat=3, interval=interval)
        elapsed = time.monotonic() - started

        assert elapsed >= interval * 2

        expected = _magic_payload(self.MAC)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(udp_magic_receiver) < 3:
            time.sleep(0.05)
        hits = [d for d, _a in udp_magic_receiver if d == expected]
        assert len(hits) == 3

    def test_global_only_when_subnet_is_global_broadcast(self, udp_magic_receiver):
        _send_magic_packet(self.MAC, "255.255.255.255", repeat=1, interval=1)
        time.sleep(0.5)
        assert udp_magic_receiver == []

    def test_bad_mac_does_not_raise(self):
        _send_magic_packet("not-a-mac", "127.0.0.1", repeat=1, interval=1)


class TestWaitForServer:

    def test_immediate_success_when_port_live(self, tcp_listener):
        ip, port = tcp_listener
        started = time.monotonic()
        wait_for_server(ip, wake_timeout=30, ping_interval=5, stability_wait=0)
        assert time.monotonic() - started < 5

    def test_retries_refused_port_until_deadline(self):
        started = time.monotonic()
        with pytest.raises(WolTimeout):
            wait_for_server("203.0.113.7", wake_timeout=3, ping_interval=1,
                            stability_wait=0)
        assert time.monotonic() - started >= 3

    def test_timeout_message_contains_ip(self):
        with pytest.raises(WolTimeout, match="192\\.0\\.2\\.123"):
            wait_for_server("192.0.2.123", wake_timeout=3, ping_interval=1,
                            stability_wait=0)

    def test_stability_wait_really_sleeps(self, tcp_listener):
        ip, port = tcp_listener
        started = time.monotonic()
        wait_for_server(ip, wake_timeout=30, ping_interval=5, stability_wait=2)
        assert time.monotonic() - started >= 2.0


def _make_config(server_ip: str, mac: str = "AA-BB-CC-DD-EE-FF",
                 enabled: bool = True, broadcast: str = "") -> AppConfig:
    return AppConfig(
        paths=PathsConfig(
            source_drive=r"C:\BackupData\FY26-27",
            lan_destination=r"\\127.0.0.1\lan_backup\FY26-27",
            gcs_key_path="unused.json",
        ),
        cloud=CloudConfig(enabled=False),
        wol=WolConfig(
            enabled=enabled, server_ip=server_ip, mac_address=mac,
            broadcast_address=broadcast, wake_timeout_seconds=300,
            ping_interval_seconds=15, stability_wait_seconds=30,
        ),
        dashboard=DashboardConfig(auth_enabled=False),
    )


class TestEnsureServerOnline:

    def test_wol_disabled_returns_true_without_probing(self):
        cfg = _make_config("192.0.2.1", enabled=False)
        started = time.monotonic()
        assert ensure_server_online(cfg) is True
        assert time.monotonic() - started < 2

    def test_already_online_local_smb_returns_true(self):
        cfg = _make_config("127.0.0.1")
        started = time.monotonic()
        assert ensure_server_online(cfg) is True
        assert time.monotonic() - started < 10

    def test_offline_unreachable_raises_real_wol_timeout(self):
        cfg = _make_config("198.51.100.77", broadcast="255.255.255.255")
        cfg.wol.wake_timeout_seconds = 3
        cfg.wol.ping_interval_seconds = 1
        cfg.wol.stability_wait_seconds = 0
        cfg.wol.wake_retry_count = 1
        cfg.wol.wake_retry_interval_seconds = 1

        started = time.monotonic()
        with pytest.raises(WolTimeout, match="198\\.51\\.100\\.77"):
            ensure_server_online(cfg)
        assert time.monotonic() - started >= 3
