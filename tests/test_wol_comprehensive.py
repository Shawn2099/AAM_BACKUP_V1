"""Comprehensive tests for core/wol.py — all functions."""

import socket
import time
from unittest.mock import MagicMock, patch

import pytest

from core.wol import (
    _smb_port_open,
    _send_magic_packet,
    ensure_server_online,
    wait_for_server,
    WolTimeout,
)


class TestSmbPortOpen:
    def test_port_closed_returns_false(self):
        """Non-zero connect_ex on port 445 -> False."""
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect_ex.return_value = 10061
        with patch("core.wol.socket.socket", return_value=mock_sock):
            assert _smb_port_open("127.0.0.1", timeout=1) is False

    def test_port_open_returns_true(self):
        """connect_ex == 0 on port 445 -> True."""
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect_ex.return_value = 0
        with patch("core.wol.socket.socket", return_value=mock_sock):
            assert _smb_port_open("127.0.0.1", timeout=1) is True


class TestSendMagicPacket:
    def test_sends_without_error(self):
        """Magic packet sends both global and subnet-directed WoL broadcasts."""
        mac = "AA:BB:CC:DD:EE:FF"
        with patch("core.wol.wol_send") as mock_wol, patch("core.wol.time.sleep"):
            _send_magic_packet(mac, "192.168.10.255", repeat=2, interval=1)
        assert mock_wol.call_count == 4
        mock_wol.assert_any_call(mac, ip_address="255.255.255.255", port=9)
        mock_wol.assert_any_call(mac, ip_address="192.168.10.255", port=9)


class TestWaitForServer:
    def test_timeout_raises(self):
        """Port never opens -> WolTimeout (no real network traffic)."""
        with patch("core.wol._smb_port_open", return_value=False),              patch("core.wol.time.sleep"):
            with pytest.raises(WolTimeout):
                wait_for_server("192.168.10.10", wake_timeout=0, ping_interval=1,
                                stability_wait=0)

    def test_immediate_success(self):
        """Port open on first probe -> returns without sleeping the interval."""
        with patch("core.wol._smb_port_open", return_value=True),              patch("core.wol.time.sleep") as mock_sleep:
            wait_for_server("192.168.10.10", wake_timeout=5, ping_interval=1,
                            stability_wait=0)
            mock_sleep.assert_not_called()


class TestEnsureServerOnline:
    def test_already_online(self):
        """Server already online → no WoL needed."""
        with patch("core.wol._smb_port_open", return_value=True):
            # Should return quickly
            ensure_server_online(MagicMock())

    def test_wake_and_wait(self):
        """Server offline -> sends WoL and waits (reads config.wol.*)."""
        mock_config = MagicMock()
        mock_config.wol.enabled = True
        mock_config.wol.server_ip = "192.168.10.10"
        mock_config.wol.mac_address = "AA:BB:CC:DD:EE:FF"
        mock_config.wol.get_broadcast_address.return_value = "192.168.10.255"
        mock_config.wol.wake_timeout_seconds = 60
        mock_config.wol.ping_interval_seconds = 1
        mock_config.wol.stability_wait_seconds = 0
        mock_config.wol.wake_retry_count = 1
        mock_config.wol.wake_retry_interval_seconds = 1

        with patch("core.wol._smb_port_open", side_effect=[False, False, True]),              patch("core.wol._send_magic_packet") as mock_send,              patch("core.wol.time.sleep"):
            assert ensure_server_online(mock_config) is True
            mock_send.assert_called_once_with(
                "AA:BB:CC:DD:EE:FF", "192.168.10.255", repeat=1, interval=1
            )
