"""Tests for wol — mock socket and wakeonlan."""

from unittest.mock import patch, MagicMock
import socket

from core.wol import _smb_port_open, _send_magic_packet, wait_for_server, ensure_server_online, WolTimeout
from models.config import AppConfig, WolConfig


class TestSmbPortOpen:
    @patch("core.wol.socket.socket")
    def test_port_open_returns_true(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 0
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_socket_cls.return_value = mock_sock
        assert _smb_port_open("192.168.10.10") is True

    @patch("core.wol.socket.socket")
    def test_port_closed_returns_false(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 111
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_socket_cls.return_value = mock_sock
        assert _smb_port_open("192.168.10.10") is False

    @patch("core.wol.socket.socket")
    def test_os_error_returns_false(self, mock_socket_cls):
        mock_socket_cls.side_effect = OSError("network down")
        assert _smb_port_open("192.168.10.10") is False


class TestSendMagicPacket:
    @patch("core.wol.wol_send")
    def test_sends_packet(self, mock_wol):
        _send_magic_packet("AA:BB:CC:DD:EE:FF")
        mock_wol.assert_called_once_with("AA:BB:CC:DD:EE:FF", ip_address="255.255.255.255", port=9)

    @patch("core.wol.wol_send", side_effect=OSError("send failed"))
    def test_os_error_raises(self, mock_wol):
        import pytest
        with pytest.raises(OSError, match="Failed to send WoL packet"):
            _send_magic_packet("AA:BB:CC:DD:EE:FF")


class TestWaitForServer:
    @patch("core.wol._smb_port_open", return_value=True)
    @patch("core.wol.time.sleep")
    def test_immediate_success(self, mock_sleep, mock_smb):
        wait_for_server("192.168.10.10", wake_timeout=60, ping_interval=5, stability_wait=0)
        mock_smb.assert_called_once()

    @patch("core.wol._smb_port_open", return_value=True)
    @patch("core.wol.time.sleep")
    def test_waits_for_stability(self, mock_sleep, mock_smb):
        wait_for_server("192.168.10.10", wake_timeout=60, ping_interval=5, stability_wait=10)
        mock_sleep.assert_called_with(10)

    @patch("core.wol._smb_port_open", return_value=False)
    @patch("core.wol.time.sleep")
    @patch("core.wol.time.time", side_effect=[0, 10, 20, 300])
    def test_timeout_raises(self, mock_time, mock_sleep, mock_smb):
        import pytest
        with pytest.raises(WolTimeout):
            wait_for_server("192.168.10.10", wake_timeout=60, ping_interval=10, stability_wait=0)


class TestEnsureServerOnline:
    def test_wol_disabled_returns_true(self):
        config = MagicMock()
        config.wol.enabled = False
        assert ensure_server_online(config) is True

    @patch("core.wol._smb_port_open", return_value=True)
    def test_already_online_returns_true(self, mock_smb):
        config = MagicMock()
        config.wol.enabled = True
        config.wol.server_ip = "192.168.10.10"
        assert ensure_server_online(config) is True

    @patch("core.wol.wait_for_server")
    @patch("core.wol._send_magic_packet")
    @patch("core.wol._smb_port_open", return_value=False)
    def test_wakes_and_waits(self, mock_smb, mock_wol, mock_wait):
        config = MagicMock()
        config.wol.enabled = True
        config.wol.server_ip = "192.168.10.10"
        config.wol.mac_address = "AA:BB:CC:DD:EE:FF"
        config.wol.wake_timeout_seconds = 300
        config.wol.ping_interval_seconds = 15
        config.wol.stability_wait_seconds = 30
        result = ensure_server_online(config)
        assert result is True
        mock_wol.assert_called_once()
        mock_wait.assert_called_once()
