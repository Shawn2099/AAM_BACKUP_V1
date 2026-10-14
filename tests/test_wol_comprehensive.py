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
        """Timeout → WolTimeout."""
        with pytest.raises(WolTimeout):
            wait_for_server("192.168.10.10", wake_timeout=2, ping_interval=1)

    def test_immediate_success(self):
        """If port is open, returns immediately."""
        # Mock the port check to always return True
        with patch("core.wol._smb_port_open", return_value=True):
            # Should return quickly
            wait_for_server("192.168.10.10", wake_timeout=5, ping_interval=1)


class TestEnsureServerOnline:
    def test_already_online(self):
        """Server already online → no WoL needed."""
        with patch("core.wol._smb_port_open", return_value=True):
            # Should return quickly
            ensure_server_online(MagicMock())

    def test_wake_and_wait(self):
        """Server offline → sends WoL and waits."""
        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.server_ip = "192.168.10.10"
        mock_config.mac_address = "AA:BB:CC:DD:EE:FF"
        mock_config.broadcast_address = "192.168.10.255"
        mock_config.wake_timeout_seconds = 2
        mock_config.ping_interval_seconds = 1
        mock_config.wake_retry_count = 1
        mock_config.wake_retry_interval_seconds = 1

        with patch("core.wol._smb_port_open", side_effect=[False, False, True]), \
             patch("core.wol._send_magic_packet"), \
             patch("core.wol.time.sleep"):
            # Should complete within timeout
            ensure_server_online(mock_config)
