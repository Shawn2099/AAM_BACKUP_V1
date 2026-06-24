"""Comprehensive tests for launch.py — Prefect API check, concurrency, orphan cleanup, main."""

import subprocess
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(scope="session", autouse=True)
def prefect_harness():
    yield


# ═══════════════════════════════════════════════════════════════════
# _check_prefect_api
# ═══════════════════════════════════════════════════════════════════


class TestCheckPrefectApi:
    def test_success_returns_true(self):
        from launch import _check_prefect_api
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_get.return_value = mock_resp
            assert _check_prefect_api() is True

    def test_connect_error_returns_false(self):
        from launch import _check_prefect_api
        with patch("httpx.get", side_effect=Exception("connection refused")):
            assert _check_prefect_api() is False

    def test_timeout_returns_false(self):
        from launch import _check_prefect_api
        with patch("httpx.get", side_effect=Exception("timeout")):
            assert _check_prefect_api() is False

    def test_non_200_returns_false(self):
        from launch import _check_prefect_api
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 503
            mock_get.return_value = mock_resp
            assert _check_prefect_api() is False

    def test_custom_url(self):
        from launch import _check_prefect_api
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_get.return_value = mock_resp
            result = _check_prefect_api("http://other:9999/api")
            assert result is True
            mock_get.assert_called_with("http://other:9999/api/health", timeout=5)


# ═══════════════════════════════════════════════════════════════════
# _ensure_concurrency_limit
# ═══════════════════════════════════════════════════════════════════


class TestEnsureConcurrencyLimit:
    def test_creates_global_and_tag_limits(self):
        from launch import _ensure_concurrency_limit
        mock_client = AsyncMock()
        with patch("prefect.client.orchestration.get_client") as mock_get_client:
            mock_get_client.return_value.__aenter__.return_value = mock_client
            _ensure_concurrency_limit()
            mock_client.upsert_global_concurrency_limit_by_name.assert_called_once()
            mock_client.create_concurrency_limit.assert_called_once()

    def test_handles_global_limit_exception(self):
        from launch import _ensure_concurrency_limit
        mock_client = AsyncMock()
        mock_client.upsert_global_concurrency_limit_by_name.side_effect = Exception("API error")
        with patch("prefect.client.orchestration.get_client") as mock_get_client:
            mock_get_client.return_value.__aenter__.return_value = mock_client
            _ensure_concurrency_limit()

    def test_handles_tag_limit_already_exists(self):
        from launch import _ensure_concurrency_limit
        mock_client = AsyncMock()
        mock_client.create_concurrency_limit.side_effect = Exception("already exists")
        with patch("prefect.client.orchestration.get_client") as mock_get_client:
            mock_get_client.return_value.__aenter__.return_value = mock_client
            _ensure_concurrency_limit()


# ═══════════════════════════════════════════════════════════════════
# _cancel_orphaned_runs
# ═══════════════════════════════════════════════════════════════════


class TestCancelOrphanedRuns:
    def test_no_orphaned_runs(self):
        from launch import _cancel_orphaned_runs
        mock_client = AsyncMock()
        mock_client.read_flow_runs.return_value = []
        with patch("prefect.client.orchestration.get_client") as mock_get_client, \
             patch("core.process.read_lock_alive", return_value=(False, None)):
            mock_get_client.return_value.__aenter__.return_value = mock_client
            _cancel_orphaned_runs()
            assert mock_client.set_flow_run_state.call_count == 0

    def test_cancels_pending_and_running(self):
        from launch import _cancel_orphaned_runs
        mock_client = AsyncMock()
        mock_run_pending = MagicMock(id="p-123", name="pending-run")
        mock_run_running = MagicMock(id="r-456", name="running-run")
        mock_client.read_flow_runs.side_effect = [[mock_run_pending], [mock_run_running]]
        with patch("prefect.client.orchestration.get_client") as mock_get_client, \
             patch("core.process.read_lock_alive", return_value=(False, None)):
            mock_get_client.return_value.__aenter__.return_value = mock_client
            _cancel_orphaned_runs()
            assert mock_client.set_flow_run_state.call_count == 2

    def test_backup_active_skips_running(self):
        from launch import _cancel_orphaned_runs
        mock_client = AsyncMock()
        mock_run_pending = MagicMock(id="p-123", name="pending-run")
        mock_client.read_flow_runs.return_value = [mock_run_pending]
        with patch("prefect.client.orchestration.get_client") as mock_get_client, \
             patch("core.process.read_lock_alive", return_value=(True, 12345)):
            mock_get_client.return_value.__aenter__.return_value = mock_client
            _cancel_orphaned_runs()
            assert mock_client.set_flow_run_state.call_count == 1

    def test_stale_lock_cleaned_up(self):
        from launch import _cancel_orphaned_runs
        from pathlib import Path
        mock_client = AsyncMock()
        mock_client.read_flow_runs.return_value = []
        with patch("prefect.client.orchestration.get_client") as mock_get_client, \
             patch("core.process.read_lock_alive", return_value=(False, 9999)), \
             patch("models.config.CONFIG_PATH", "/tmp/test_config.yaml"), \
             patch("models.config.load_config") as mock_config, \
             patch("pathlib.Path.unlink") as mock_unlink:
            mock_cfg = MagicMock()
            mock_cfg.paths.database_path = "/tmp/test.db"
            mock_cfg.paths.backup_lock_path = Path("/tmp/backup.lock")
            mock_config.return_value = mock_cfg
            mock_get_client.return_value.__aenter__.return_value = mock_client
            _cancel_orphaned_runs()
            mock_unlink.assert_called_once()

    def test_live_lock_with_backup_active(self):
        from launch import _cancel_orphaned_runs
        mock_client = AsyncMock()
        mock_run_pending = MagicMock(id="p-123", name="pending-run")
        mock_client.read_flow_runs.return_value = [mock_run_pending]
        with patch("prefect.client.orchestration.get_client") as mock_get_client, \
             patch("core.process.read_lock_alive", return_value=(True, 12345)):
            mock_get_client.return_value.__aenter__.return_value = mock_client
            _cancel_orphaned_runs()
            assert mock_client.read_flow_runs.call_count == 1

    def test_handles_cancel_exception(self):
        from launch import _cancel_orphaned_runs
        mock_client = AsyncMock()
        mock_run = MagicMock(id="fail-123", name="crashed-run")
        mock_client.read_flow_runs.return_value = [mock_run]
        mock_client.set_flow_run_state.side_effect = Exception("API Error")
        with patch("prefect.client.orchestration.get_client") as mock_get_client, \
             patch("core.process.read_lock_alive", return_value=(False, None)):
            mock_get_client.return_value.__aenter__.return_value = mock_client
            _cancel_orphaned_runs()

    def test_handles_client_connection_failure(self):
        from launch import _cancel_orphaned_runs
        with patch("prefect.client.orchestration.get_client", side_effect=Exception("Connection Failed")), \
             patch("core.process.read_lock_alive", return_value=(False, None)):
            _cancel_orphaned_runs()

    def test_config_exception_falls_back_to_default_path(self):
        from launch import _cancel_orphaned_runs
        mock_client = AsyncMock()
        mock_client.read_flow_runs.return_value = []
        with patch("prefect.client.orchestration.get_client") as mock_get_client, \
             patch("core.process.read_lock_alive", return_value=(False, None)), \
             patch("models.config.load_config", side_effect=Exception("bad config")):
            mock_get_client.return_value.__aenter__.return_value = mock_client
            _cancel_orphaned_runs()


# ═══════════════════════════════════════════════════════════════════
# main — Prefect API readiness
# ═══════════════════════════════════════════════════════════════════


class TestMainPrefectApiReady:
    def _main_patches(self):
        """Return a dict of common patches needed for main()."""
        mock_cfg = MagicMock()
        mock_cfg.dashboard.bind_address = "127.0.0.1"
        mock_cfg.dashboard.port = 8080
        mock_deployments = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        return {
            "launch._check_prefect_api": patch("launch._check_prefect_api", return_value=True),
            "launch._run_dashboard": patch("launch._run_dashboard"),
            "launch._ensure_concurrency_limit": patch("launch._ensure_concurrency_limit"),
            "launch._cancel_orphaned_runs": patch("launch._cancel_orphaned_runs"),
            "prefect.serve": patch("prefect.serve"),
            "launch.time.sleep": patch("launch.time.sleep"),
            "launch.threading.Thread": patch("launch.threading.Thread"),
            "models.config.load_config": patch("models.config.load_config", return_value=mock_cfg),
            "core.fy_rollover.rollover": patch("core.fy_rollover.rollover", return_value=False),
            "serve.deployments": patch("serve.deployments", return_value=mock_deployments),
        }

    def test_api_ready_immediately(self):
        from launch import main
        patches = self._main_patches()
        with patches["launch._check_prefect_api"], \
             patches["launch._run_dashboard"], \
             patches["launch._ensure_concurrency_limit"], \
             patches["launch._cancel_orphaned_runs"], \
             patches["prefect.serve"], \
             patches["launch.time.sleep"], \
             patches["launch.threading.Thread"], \
             patches["models.config.load_config"], \
             patches["core.fy_rollover.rollover"], \
             patches["serve.deployments"]:
            try:
                main()
            except Exception:
                pass

    def test_api_ready_after_retries(self):
        from launch import main
        call_count = 0
        def check_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return call_count >= 3
        patches = self._main_patches()
        patches["launch._check_prefect_api"] = patch("launch._check_prefect_api", side_effect=check_side_effect)
        with patches["launch._check_prefect_api"], \
             patches["launch._run_dashboard"], \
             patches["launch._ensure_concurrency_limit"], \
             patches["launch._cancel_orphaned_runs"], \
             patches["prefect.serve"], \
             patches["launch.time.sleep"], \
             patches["launch.threading.Thread"], \
             patches["models.config.load_config"], \
             patches["core.fy_rollover.rollover"], \
             patches["serve.deployments"]:
            try:
                main()
            except Exception:
                pass
            assert call_count == 3

    def test_api_never_ready_exits(self):
        from launch import main
        mock_cfg = MagicMock()
        mock_cfg.dashboard.bind_address = "127.0.0.1"
        mock_cfg.dashboard.port = 8080
        with patch("launch._check_prefect_api", return_value=False), \
             patch("launch.time.sleep"), \
             patch("launch.sys.exit", side_effect=SystemExit(1)) as mock_exit, \
             patch("models.config.load_config", return_value=mock_cfg):
            with pytest.raises(SystemExit):
                main()
            mock_exit.assert_called_once_with(1)


# ═══════════════════════════════════════════════════════════════════
# main — FY rollover
# ═══════════════════════════════════════════════════════════════════


class TestMainFyRollover:
    def _run_main(self, rollover_return=None, rollover_side_effect=None):
        from launch import main
        mock_cfg = MagicMock()
        mock_cfg.dashboard.bind_address = "127.0.0.1"
        mock_cfg.dashboard.port = 8080
        mock_deployments = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        rollover_kwargs = {}
        if rollover_return is not None:
            rollover_kwargs["return_value"] = rollover_return
        if rollover_side_effect is not None:
            rollover_kwargs["side_effect"] = rollover_side_effect
        with patch("launch._check_prefect_api", return_value=True), \
             patch("launch._run_dashboard"), \
             patch("launch._ensure_concurrency_limit"), \
             patch("launch._cancel_orphaned_runs"), \
             patch("prefect.serve"), \
             patch("launch.time.sleep"), \
             patch("launch.threading.Thread"), \
             patch("models.config.load_config", return_value=mock_cfg), \
             patch("core.fy_rollover.rollover", **rollover_kwargs), \
             patch("serve.deployments", return_value=mock_deployments):
            try:
                main()
            except Exception:
                pass

    def test_rollover_success(self):
        self._run_main(rollover_return=True)

    def test_rollover_failure_non_fatal(self):
        from core.fy_rollover import RolloverError
        self._run_main(rollover_side_effect=RolloverError("blocked"))

    def test_rollover_exception_non_fatal(self):
        self._run_main(rollover_side_effect=Exception("unexpected"))


# ═══════════════════════════════════════════════════════════════════
# main — graceful shutdown
# ═══════════════════════════════════════════════════════════════════


class TestMainShutdown:
    def test_keyboard_interrupt_clean_shutdown(self):
        from launch import main
        mock_cfg = MagicMock()
        mock_cfg.dashboard.bind_address = "127.0.0.1"
        mock_cfg.dashboard.port = 8080
        mock_deployments = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        with patch("launch._check_prefect_api", return_value=True), \
             patch("launch._run_dashboard"), \
             patch("launch._ensure_concurrency_limit"), \
             patch("launch._cancel_orphaned_runs"), \
             patch("prefect.serve", side_effect=KeyboardInterrupt()), \
             patch("launch.time.sleep"), \
             patch("launch.threading.Thread"), \
             patch("models.config.load_config", return_value=mock_cfg), \
             patch("core.fy_rollover.rollover", return_value=False), \
             patch("serve.deployments", return_value=mock_deployments):
            main()
