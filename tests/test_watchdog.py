import pytest
from unittest.mock import MagicMock, patch

from watchdog import _transfer_process_running

class TestTransferProcessRunning:
    @patch("psutil.process_iter")
    def test_detects_lowercase_rclone(self, mock_iter):
        proc = MagicMock()
        proc.info = {"name": "rclone.exe"}
        mock_iter.return_value = [proc]
        assert _transfer_process_running() is True

    @patch("psutil.process_iter")
    def test_detects_uppercase_robocopy(self, mock_iter):
        proc = MagicMock()
        proc.info = {"name": "robocopy.EXE"}
        mock_iter.return_value = [proc]
        assert _transfer_process_running() is True

    @patch("psutil.process_iter")
    def test_detects_mixed_case(self, mock_iter):
        proc = MagicMock()
        proc.info = {"name": "Rclone.exe"}
        mock_iter.return_value = [proc]
        assert _transfer_process_running() is True

    @patch("psutil.process_iter")
    def test_handles_none_name_safely(self, mock_iter):
        proc1 = MagicMock()
        proc1.info = {"name": None}  # Windows system processes
        proc2 = MagicMock()
        proc2.info = {"name": "System Idle Process"}
        mock_iter.return_value = [proc1, proc2]
        assert _transfer_process_running() is False

    @patch("psutil.process_iter")
    def test_returns_false_when_no_match(self, mock_iter):
        proc = MagicMock()
        proc.info = {"name": "explorer.exe"}
        mock_iter.return_value = [proc]
        assert _transfer_process_running() is False
