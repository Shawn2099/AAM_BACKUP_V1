"""Tests for launch.py — startup helpers and health checks."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from launch import _cancel_orphaned_runs, _check_prefect_api


class TestCheckPrefectApi:
    @patch("httpx.get")
    def test_api_healthy_returns_true(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        result = _check_prefect_api()
        assert result is True

    @patch("httpx.get", side_effect=Exception("connection refused"))
    def test_network_error_returns_false(self, mock_get):
        result = _check_prefect_api()
        assert result is False

    @patch("httpx.get")
    def test_non_200_returns_false(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_get.return_value = mock_resp
        result = _check_prefect_api()
        assert result is False

    @patch("httpx.get")
    def test_custom_url(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        result = _check_prefect_api("http://other:9999/api")
        assert result is True
        mock_get.assert_called_with("http://other:9999/api/health", timeout=5)


class TestCancelOrphanedRuns:
    @patch("launch.subprocess.run")
    def test_no_orphaned_runs(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")
        _cancel_orphaned_runs()
        assert mock_run.call_count == 2

    @patch("launch.subprocess.run")
    def test_cancels_pending_run(self, mock_run):
        run = [{"id": "abc-123", "name": "noble-rook"}]
        responses = [
            MagicMock(returncode=0, stdout=json.dumps(run)),
            MagicMock(returncode=0, stdout="[]"),
            MagicMock(returncode=0),
        ]
        mock_run.side_effect = responses
        _cancel_orphaned_runs()

    @patch("launch.subprocess.run")
    def test_cancels_running_run(self, mock_run):
        run = [{"id": "xyz-789", "name": "amethyst-condor"}]
        responses = [
            MagicMock(returncode=0, stdout="[]"),
            MagicMock(returncode=0, stdout=json.dumps(run)),
            MagicMock(returncode=0),
        ]
        mock_run.side_effect = responses
        _cancel_orphaned_runs()

    @patch("launch.subprocess.run")
    def test_cli_non_zero_exit_skipped(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="error")
        _cancel_orphaned_runs()

    @patch("launch.subprocess.run")
    def test_invalid_json_handled(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{invalid json")
        _cancel_orphaned_runs()

    @patch("launch.subprocess.run")
    def test_run_without_id_field_skipped(self, mock_run):
        run = [{"name": "no-id-run"}]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(run))
        _cancel_orphaned_runs()

    @patch("launch.subprocess.run")
    def test_cancel_command_raises_timeout(self, mock_run):
        run = [{"id": "timeout-run", "name": "slow-condor"}]
        responses = [
            MagicMock(returncode=0, stdout=json.dumps(run)),
            MagicMock(returncode=0, stdout="[]"),
            subprocess.TimeoutExpired(cmd=["prefect", "flow-run", "cancel", "timeout-run"], timeout=10),
        ]
        mock_run.side_effect = responses
        _cancel_orphaned_runs()

    @patch("launch.subprocess.run")
    def test_empty_stdout_treated_as_no_runs(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        _cancel_orphaned_runs()

    @patch("launch.subprocess.run")
    def test_multiple_runs_across_states_canceled(self, mock_run):
        pending_runs = [{"id": "r1", "name": "run-1"}, {"id": "r2", "name": "run-2"}]
        running_runs = [{"id": "r3", "name": "run-3"}]
        responses = [
            MagicMock(returncode=0, stdout=json.dumps(pending_runs)),
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=json.dumps(running_runs)),
            MagicMock(returncode=0),
        ]
        mock_run.side_effect = responses
        _cancel_orphaned_runs()
        # 2 ls calls + 3 cancel calls = 5 total, all used
        assert mock_run.call_count == 5
