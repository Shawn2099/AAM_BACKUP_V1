"""Tests for launch.py — startup helpers and health checks."""

from unittest.mock import AsyncMock, MagicMock, patch
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
    @patch("prefect.client.orchestration.get_client")
    def test_no_orphaned_runs(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.read_flow_runs.return_value = []
        
        # Async context manager mock
        mock_get_client.return_value.__aenter__.return_value = mock_client
        
        _cancel_orphaned_runs()
        
        assert mock_client.read_flow_runs.call_count == 2
        mock_client.set_flow_run_state.assert_not_called()

    @patch("prefect.client.orchestration.get_client")
    def test_cancels_pending_and_running_runs(self, mock_get_client):
        mock_client = AsyncMock()
        
        mock_run_pending = MagicMock(id="p-123", name="noble-rook")
        mock_run_running = MagicMock(id="r-456", name="amethyst-condor")
        
        # Return pending runs first, then running runs
        mock_client.read_flow_runs.side_effect = [
            [mock_run_pending],
            [mock_run_running],
        ]
        
        mock_get_client.return_value.__aenter__.return_value = mock_client
        
        _cancel_orphaned_runs()
        
        assert mock_client.read_flow_runs.call_count == 2
        assert mock_client.set_flow_run_state.call_count == 2
        
        # Verify set_flow_run_state was called with the correct IDs
        calls = mock_client.set_flow_run_state.call_args_list
        assert calls[0][1]["flow_run_id"] == "p-123"
        assert calls[1][1]["flow_run_id"] == "r-456"

    @patch("prefect.client.orchestration.get_client")
    def test_handles_cancel_exceptions(self, mock_get_client):
        mock_client = AsyncMock()
        mock_run = MagicMock(id="fail-123", name="crashed-run")
        mock_client.read_flow_runs.return_value = [mock_run]
        
        # Force set_flow_run_state to raise an exception
        mock_client.set_flow_run_state.side_effect = Exception("API Error")
        
        mock_get_client.return_value.__aenter__.return_value = mock_client
        
        # Should not crash, should handle exception gracefully
        _cancel_orphaned_runs()
        
        assert mock_client.read_flow_runs.call_count == 2
        assert mock_client.set_flow_run_state.call_count == 2

    @patch("prefect.client.orchestration.get_client", side_effect=Exception("Connection Failed"))
    def test_handles_client_connection_failure(self, mock_get_client):
        # Should handle connection exception gracefully without throwing
        _cancel_orphaned_runs()
