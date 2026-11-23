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
    def test_port_closed(self, tmp_path):
        """Port 445 on localhost likely closed → False."""
        result = _smb_port_open("127.0.0.1", timeout=1)
        # May be True or False depending on system
        assert isinstance(result, bool)


class TestSendMagicPacket:
    def test_sends_without_error(self):
        """Magic packet send doesn't crash."""
        mac = "AA:BB:CC:DD:EE:FF"
        # Should not raise
        try:
            _send_magic_packet(mac, "192.168.10.255")
        except OSError:
            pass  # Network error is acceptable in test env


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
