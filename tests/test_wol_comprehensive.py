"""Comprehensive tests for core/wol.py — WoL magic packets, SMB polling, server online check."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.wol import (
    WolTimeout,
    _send_magic_packet,
    _smb_port_open,
    ensure_server_online,
    wait_for_server,
)

# ═══════════════════════════════════════════════════════════════
# 1. _smb_port_open
# ═══════════════════════════════════════════════════════════════

class TestSmbPortOpen:
    """TCP connect to SMB port."""

    @patch("core.wol.socket.socket")
    def test_port_open(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 0
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        assert _smb_port_open("10.0.0.5") is True

    @patch("core.wol.socket.socket")
    def test_port_closed(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 1
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        assert _smb_port_open("10.0.0.5") is False

    @patch("core.wol.socket.socket")
    def test_oserror_returns_false(self, mock_socket_cls):
        mock_socket_cls.return_value.__enter__.side_effect = OSError("unreachable")

        assert _smb_port_open("10.0.0.5") is False

    @patch("core.wol.socket.socket")
    def test_connection_refused_returns_false(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 111
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        assert _smb_port_open("10.0.0.5") is False

    @patch("core.wol.socket.socket")
    def test_uses_port_445_default(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 0
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        _smb_port_open("10.0.0.5")
        mock_sock.settimeout.assert_called_once_with(5.0)

    @patch("core.wol.socket.socket")
    def test_custom_timeout(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 0
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        _smb_port_open("10.0.0.5", timeout=10.0)
        mock_sock.settimeout.assert_called_once_with(10.0)


# ═══════════════════════════════════════════════════════════════
# 2. _send_magic_packet
# ═══════════════════════════════════════════════════════════════

class TestSendMagicPacket:
    """Send WoL magic packet to global and subnet broadcast."""

    @patch("core.wol.wol_send")
    def test_global_broadcast_sent(self, mock_wol_send):
        _send_magic_packet("AA:BB:CC:DD:EE:FF", "192.168.10.255")

        mock_wol_send.assert_any_call("AA:BB:CC:DD:EE:FF", ip_address="255.255.255.255", port=9)

    @patch("core.wol.wol_send")
    def test_subnet_broadcast_sent(self, mock_wol_send):
        _send_magic_packet("AA:BB:CC:DD:EE:FF", "192.168.10.255")

        mock_wol_send.assert_any_call("AA:BB:CC:DD:EE:FF", ip_address="192.168.10.255", port=9)

    @patch("core.wol.wol_send")
    def test_both_broadcasts_called(self, mock_wol_send):
        _send_magic_packet("AA:BB:CC:DD:EE:FF", "192.168.10.255")

        assert mock_wol_send.call_count == 2

    @patch("core.wol.wol_send")
    def test_subnet_255_255_255_255_only_one_send(self, mock_wol_send):
        """When subnet = 255.255.255.255, only global broadcast sent."""
        _send_magic_packet("AA:BB:CC:DD:EE:FF", "255.255.255.255")

        assert mock_wol_send.call_count == 1
        mock_wol_send.assert_called_once_with("AA:BB:CC:DD:EE:FF", ip_address="255.255.255.255", port=9)

    @patch("core.wol.wol_send")
    def test_oserror_on_global_broadcast_logged(self, mock_wol_send):
        mock_wol_send.side_effect = OSError("network unreachable")

        # Should not raise — errors are logged
        _send_magic_packet("AA:BB:CC:DD:EE:FF", "192.168.10.255")

    @patch("core.wol.wol_send")
    def test_oserror_on_subnet_broadcast_logged(self, mock_wol_send):
        # First call (global) succeeds, second call (subnet) fails
        mock_wol_send.side_effect = [None, OSError("subnet fail")]

        _send_magic_packet("AA:BB:CC:DD:EE:FF", "192.168.10.255")
        assert mock_wol_send.call_count == 2

    @patch("core.wol.wol_send")
    def test_oserror_on_global_still_sends_subnet(self, mock_wol_send):
        mock_wol_send.side_effect = [OSError("global fail"), None]

        _send_magic_packet("AA:BB:CC:DD:EE:FF", "192.168.10.255")
        assert mock_wol_send.call_count == 2


# ═══════════════════════════════════════════════════════════════
# 3. wait_for_server
# ═══════════════════════════════════════════════════════════════

class TestWaitForServer:
    """Poll SMB port until server responds."""

    @patch("core.wol.time.sleep")
    @patch("core.wol._smb_port_open")
    def test_immediate_success(self, mock_smb, mock_sleep):
        mock_smb.return_value = True

        wait_for_server("10.0.0.5", wake_timeout=300, ping_interval=15, stability_wait=0)

        mock_smb.assert_called_once_with("10.0.0.5")

    @patch("core.wol.time.time")
    @patch("core.wol.time.sleep")
    @patch("core.wol._smb_port_open")
    def test_success_after_retries(self, mock_smb, mock_sleep, mock_time):
        # Simulate 3 failed checks then success
        mock_time.side_effect = [0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180, 195, 210, 225, 240, 255, 270, 285, 300]
        mock_smb.side_effect = [False, False, False, True]

        wait_for_server("10.0.0.5", wake_timeout=300, ping_interval=15, stability_wait=0)

        assert mock_smb.call_count == 4

    @patch("core.wol.time.time")
    @patch("core.wol.time.sleep")
    @patch("core.wol._smb_port_open")
    def test_timeout_raises_wol_timeout(self, mock_smb, mock_sleep, mock_time):
        mock_smb.return_value = False
        # Provide enough time values for the loop
        mock_time.side_effect = list(range(0, 400, 15))

        with pytest.raises(WolTimeout):
            wait_for_server("10.0.0.5", wake_timeout=300, ping_interval=15, stability_wait=0)

    @patch("core.wol.time.sleep")
    @patch("core.wol.time.time")
    @patch("core.wol._smb_port_open")
    def test_stability_wait_applied(self, mock_smb, mock_time, mock_sleep):
        mock_smb.return_value = True
        mock_time.side_effect = [0, 1, 2, 3, 4, 5]

        wait_for_server("10.0.0.5", wake_timeout=300, ping_interval=15, stability_wait=30)

        # stability_wait should be called
        mock_sleep.assert_called_with(30)

    @patch("core.wol.time.sleep")
    @patch("core.wol.time.time")
    @patch("core.wol._smb_port_open")
    def test_stability_wait_zero_skipped(self, mock_smb, mock_time, mock_sleep):
        mock_smb.return_value = True
        mock_time.side_effect = [0, 1]

        wait_for_server("10.0.0.5", wake_timeout=300, ping_interval=15, stability_wait=0)

        # sleep should NOT be called for stability_wait=0
        mock_sleep.assert_not_called()

    @patch("core.wol.time.time")
    @patch("core.wol.time.sleep")
    @patch("core.wol._smb_port_open")
    def test_wol_timeout_message(self, mock_smb, mock_sleep, mock_time):
        mock_smb.return_value = False
        mock_time.side_effect = list(range(0, 400, 15))

        with pytest.raises(WolTimeout, match="10.0.0.5"):
            wait_for_server("10.0.0.5", wake_timeout=300, ping_interval=15, stability_wait=0)


# ═══════════════════════════════════════════════════════════════
# 4. ensure_server_online
# ═══════════════════════════════════════════════════════════════

def _make_config(wol_enabled=True, server_ip="10.0.0.5", mac="AA:BB:CC:DD:EE:FF"):
    cfg = MagicMock()
    cfg.wol.enabled = wol_enabled
    cfg.wol.server_ip = server_ip
    cfg.wol.mac_address = mac
    cfg.wol.wake_timeout_seconds = 300
    cfg.wol.ping_interval_seconds = 15
    cfg.wol.stability_wait_seconds = 30
    cfg.wol.get_broadcast_address.return_value = "10.0.0.255"
    return cfg


class TestEnsureServerOnline:
    """High-level WoL orchestrator."""

    @patch("core.wol._smb_port_open")
    def test_wol_disabled_returns_true(self, mock_smb):
        cfg = _make_config(wol_enabled=False)

        result = ensure_server_online(cfg)

        assert result is True
        mock_smb.assert_not_called()

    @patch("core.wol._smb_port_open")
    def test_already_online_returns_true(self, mock_smb):
        mock_smb.return_value = True
        cfg = _make_config()

        result = ensure_server_online(cfg)

        assert result is True

    @patch("core.wol.wait_for_server")
    @patch("core.wol._send_magic_packet")
    @patch("core.wol._smb_port_open")
    def test_sends_wol_and_waits(self, mock_smb, mock_send, mock_wait):
        mock_smb.return_value = False  # Initially offline
        mock_wait.return_value = None
        cfg = _make_config()

        result = ensure_server_online(cfg)

        assert result is True
        mock_send.assert_called_once_with("AA:BB:CC:DD:EE:FF", "10.0.0.255")
        mock_wait.assert_called_once()

    @patch("core.wol.wait_for_server")
    @patch("core.wol._send_magic_packet")
    @patch("core.wol._smb_port_open")
    def test_wol_timeout_propagates(self, mock_smb, mock_send, mock_wait):
        mock_smb.return_value = False
        mock_wait.side_effect = WolTimeout("timeout")
        cfg = _make_config()

        with pytest.raises(WolTimeout):
            ensure_server_online(cfg)

    @patch("core.wol._smb_port_open")
    def test_online_skips_wol(self, mock_smb):
        mock_smb.return_value = True
        cfg = _make_config()

        with patch("core.wol._send_magic_packet") as mock_send:
            ensure_server_online(cfg)
            mock_send.assert_not_called()

    @patch("core.wol.wait_for_server")
    @patch("core.wol._send_magic_packet")
    @patch("core.wol._smb_port_open")
    def test_uses_correct_server_ip(self, mock_smb, mock_send, mock_wait):
        mock_smb.return_value = False
        cfg = _make_config(server_ip="192.168.10.100")

        ensure_server_online(cfg)

        mock_smb.assert_called_with("192.168.10.100")

    @patch("core.wol.wait_for_server")
    @patch("core.wol._send_magic_packet")
    @patch("core.wol._smb_port_open")
    def test_uses_correct_broadcast(self, mock_smb, mock_send, mock_wait):
        mock_smb.return_value = False
        cfg = _make_config()
        cfg.wol.get_broadcast_address.return_value = "192.168.10.255"

        ensure_server_online(cfg)

        mock_send.assert_called_once_with("AA:BB:CC:DD:EE:FF", "192.168.10.255")
