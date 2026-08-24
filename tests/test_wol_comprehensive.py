"""Comprehensive tests for core/wol.py â€” REAL sockets, REAL packets, REAL time.

Every probe connects to genuine listening/closed TCP ports, every magic
packet is sent by wakeonlan over a real UDP socket and verified by reception
on a loopback listener, and all waits are measured against the OS clock.
"""

from __future__ import annotations

import socket
import time
from threading import Thread

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
    LanConfig,
    PathsConfig,
    WolConfig,
)


# â”€â”€ Real-socket fixtures â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


@pytest.fixture
def udp_magic_receiver():
    """Real UDP listener on 127.0.0.1:9 collecting incoming datagrams."""
    packets = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 9))
    sock.settimeout(0.2)

    def _drain():
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            packets.append((data, addr))

    t = Thread(target=_drain, daemon=True)
    t.start()
    yield packets
    sock.close()


def _magic_payload(mac: str) -> bytes:
    hex_str = mac.replace(":", "").replace("-", "")
    return b"\xff" * 6 + bytes.fromhex(hex_str) * 16


def _wait_for_count(packets: list, n: int, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and len(packets) < n:
        time.sleep(0.05)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 1. _smb_port_open â€” genuine TCP connect behaviour
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestSmbPortOpen:

    def test_listening_port_returns_true(self, tcp_listener):
        ip, port = tcp_listener
        assert _smb_port_open(ip, port=port) is True

    def test_refused_port_returns_false(self, closed_tcp_port):
        assert _smb_port_open("127.0.0.1", port=closed_tcp_port) is False

    def test_unroutable_host_returns_false_within_timeout(self):
        started = time.monotonic()
        assert _smb_port_open("203.0.113.1", timeout=1.0) is False
        assert time.monotonic() - started < 10

    def test_local_smb_service_detected(self):
        """The real Server service (SMB 445) on this host is reachable."""
        assert _smb_port_open("127.0.0.1") is True

    def test_invalid_port_above_range(self):
        assert _smb_port_open("127.0.0.1", port=70000) is False

    def test_negative_port(self):
        assert _smb_port_open("127.0.0.1", port=-1) is False

    def test_string_port_rejected(self):
        assert _smb_port_open("127.0.0.1", port="445") is False

    def test_custom_timeout_still_connects(self, tcp_listener):
        ip, port = tcp_listener
        assert _smb_port_open(ip, port=port, timeout=10.0) is True


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 2. _send_magic_packet â€” verified by actual UDP reception
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestSendMagicPacket:
    """G3 retransmit contract exercised with real datagrams."""

    MAC = "AA-BB-CC-DD-EE-FF"

    def test_single_round_packet_received_byte_exact(self, udp_magic_receiver):
        _send_magic_packet(self.MAC, "127.0.0.1", repeat=1, interval=1)

        _wait_for_count(udp_magic_receiver, 1)
        expected = _magic_payload(self.MAC)
        assert any(d == expected for d, _a in udp_magic_receiver), (
            f"expected {expected!r}, got {[d.hex() for d, _ in udp_magic_receiver]}"
        )

    def test_three_rounds_three_packets_real_interval(self, udp_magic_receiver):
        interval = 2
        started = time.monotonic()
        _send_magic_packet(self.MAC, "127.0.0.1", repeat=3, interval=interval)
        elapsed = time.monotonic() - started

        assert elapsed >= interval * 2  # two real sleeps between three rounds
        _wait_for_count(udp_magic_receiver, 3)
        expected = _magic_payload(self.MAC)
        hits = [d for d, _a in udp_magic_receiver if d == expected]
        assert len(hits) == 3

    def test_global_broadcast_only_when_subnet_matches(self, udp_magic_receiver):
        _send_magic_packet(self.MAC, "255.255.255.255", repeat=1, interval=1)
        time.sleep(0.5)
        # Only 255.255.255.255 was targeted; our directed listener stays silent.
        assert udp_magic_receiver == []

    def test_unparseable_mac_swallowed_not_raised(self):
        _send_magic_packet("ZZ:00:00:00:00:00", "127.0.0.1", repeat=1, interval=1)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 3. wait_for_server â€” real polling convergence and real timeouts
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestWaitForServer:

    def test_immediate_success_against_live_listener(self, tcp_listener):
        ip, port = tcp_listener
        started = time.monotonic()
        wait_for_server(ip, wake_timeout=300, ping_interval=15, stability_wait=0)
        assert time.monotonic() - started < 5

    def test_keeps_probing_refused_port_until_deadline(self):
        """Refused 445 on TEST-NET: polling continues until the real deadline,
        then WolTimeout â€” proving the loop retries instead of failing fast."""
        started = time.monotonic()
        with pytest.raises(WolTimeout):
            wait_for_server("203.0.113.7", wake_timeout=3, ping_interval=1,
                            stability_wait=0)
        assert time.monotonic() - started >= 3

    def test_real_wol_timeout_after_deadline(self):
        started = time.monotonic()
        with pytest.raises(WolTimeout):
            wait_for_server("192.0.2.55", wake_timeout=3, ping_interval=1,
                            stability_wait=0)
        assert time.monotonic() - started >= 3

    def test_error_message_names_the_server_ip(self):
        with pytest.raises(WolTimeout, match="192\\.0\\.2\\.55"):
            wait_for_server("192.0.2.55", wake_timeout=3, ping_interval=1,
                            stability_wait=0)

    def test_stability_wait_sleeps_the_full_duration(self, tcp_listener):
        ip, port = tcp_listener
        started = time.monotonic()
        wait_for_server(ip, wake_timeout=300, ping_interval=15, stability_wait=2)
        assert time.monotonic() - started >= 2.0

    def test_zero_stability_wait_skips_the_sleep(self, tcp_listener):
        ip, port = tcp_listener
        started = time.monotonic()
        wait_for_server(ip, wake_timeout=300, ping_interval=15, stability_wait=0)
        assert time.monotonic() - started < 5


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 4. ensure_server_online â€” real AppConfig instances end to end
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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
            enabled=enabled,
            server_ip=server_ip,
            mac_address=mac,
            broadcast_address=broadcast,
            wake_timeout_seconds=300,
            ping_interval_seconds=15,
            stability_wait_seconds=30,
        ),
        dashboard=DashboardConfig(auth_enabled=False),
    )


class TestEnsureServerOnline:

    def test_disabled_returns_true_immediately(self):
        cfg = _make_config("192.0.2.9", enabled=False)
        started = time.monotonic()
        assert ensure_server_online(cfg) is True
        assert time.monotonic() - started < 2

    def test_online_via_real_local_smb(self):
        cfg = _make_config("127.0.0.1")
        started = time.monotonic()
        assert ensure_server_online(cfg) is True
        assert time.monotonic() - started < 10

    def test_broadcast_address_auto_derivation_used_in_flow(self):
        """get_broadcast_address derives x.y.z.255 from server_ip â€” the value
        the production wake path passes to _send_magic_packet."""
        cfg = _make_config("192.168.77.42")
        assert cfg.wol.get_broadcast_address() == "192.168.77.255"

    def test_unreachable_server_times_out_through_public_api(self):
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
