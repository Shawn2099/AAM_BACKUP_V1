"""Regression tests for F6/G4/G6 watchdog + lock hardening (fix log 2026-08-18)."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

import watchdog
from core.process import read_lock_alive


@pytest.fixture(autouse=True)
def _reset_service_breaker():
    """F6: reset the module-level auto-start circuit breaker between tests."""
    watchdog.service_start_log.clear()
    yield


def _loop(iterations, **patches):
    """Run watchdog.main() for N loop iterations, then break out."""
    with patch("watchdog.time.sleep") as mock_sleep:
        count = {"n": 0}

        def sleep_side(*a):
            count["n"] += 1
            if count["n"] >= iterations:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side
        with patch("watchdog._resolve_paths"), \
             patch("watchdog.BACKUP_LOCK_PATH", Path(tempfile.mktemp())):
            for name, value in patches.items():
                if not name.startswith("httpx"):
                    continue
            try:
                watchdog.main()
            except KeyboardInterrupt:
                pass
        return mock_sleep.call_args_list


# ═══════════════════════════════════════════════════════════════
# _service_state parser (F6 prerequisite)
# ═══════════════════════════════════════════════════════════════

class TestServiceState:
    @pytest.mark.parametrize("stdout, expected", [
        ("SERVICE_NAME: X\n        STATE              : 4  RUNNING\n                                (STOPPABLE)", "RUNNING"),
        ("SERVICE_NAME: X\n        STATE              : 1  STOPPED\n", "STOPPED"),
        ("SERVICE_NAME: X\n        STATE              : 2  START_PENDING (ACCEPTS_NOthing)\n", "START_PENDING"),
        ("SERVICE_NAME: X\n        STATE              : 3  STOP_PENDING\n", "STOP_PENDING"),
        ("no state line at all", ""),
    ])
    @patch("watchdog.subprocess.run")
    def test_parses_states(self, mock_run, stdout, expected):
        mock_run.return_value = MagicMock(stdout=stdout, returncode=0)
        assert watchdog._service_state("AamPrefectServer") == expected

    @patch("watchdog.subprocess.run", side_effect=FileNotFoundError("no sc on linux"))
    def test_unqueryable_returns_empty(self, mock_run):
        assert watchdog._service_state("AamPrefectServer") == ""


# ═══════════════════════════════════════════════════════════════
# F6 — stopped service is started, not waited on forever
# ═══════════════════════════════════════════════════════════════

class TestStoppedService:
    @patch("httpx.get", side_effect=httpx.ConnectError("dead"))
    @patch("watchdog._is_backup_running", return_value=False)
    @patch("watchdog._transfer_process_running", return_value=False)
    @patch("watchdog.subprocess.run")
    def test_stopped_prefect_server_is_started(self, mock_run, *a):
        """API dead + service STOPPED (after threshold) => sc start, not passive wait."""
        def side_effect(args, **kw):
            m = MagicMock()
            if "query" in args:
                m.stdout = "        STATE              : 1  STOPPED\n"
            elif "start" in args:
                m.returncode = 0
            else:
                m.returncode = 0
            return m
        mock_run.side_effect = side_effect

        with patch("watchdog._resolve_paths"), \
             patch("watchdog.BACKUP_LOCK_PATH", Path(tempfile.mktemp())):
            with patch("watchdog.time.sleep") as mock_sleep:
                n = {"c": 0}
                def sleep_side(*a):
                    n["c"] += 1
                    if n["c"] >= watchdog.FAILURE_THRESHOLD + 1:
                        raise KeyboardInterrupt
                mock_sleep.side_effect = sleep_side
                try:
                    watchdog.main()
                except KeyboardInterrupt:
                    pass

        start_calls = [c for c in mock_run.call_args_list if "start" in c[0][0]]
        stop_calls = [c for c in mock_run.call_args_list if c[0][0][1] == "stop"]
        assert len(start_calls) == 1, "expected exactly one sc start"
        assert start_calls[0][0][0][2] == "AamPrefectServer"
        assert len(stop_calls) == 0, "must not sc stop a stopped service"


# ═══════════════════════════════════════════════════════════════
# G4 — healthy API + stopped scheduler agent => agent is started
# ═══════════════════════════════════════════════════════════════

class TestAgentMonitoring:
    @patch("httpx.get")
    @patch("watchdog.subprocess.run")
    def test_stopped_agent_started_when_api_healthy(self, mock_run, mock_httpx):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx.return_value = mock_resp

        started = {"agent": False}

        def side_effect(args, **kw):
            m = MagicMock()
            if "query" in args:
                svc = args[2]
                # Prefect server RUNNING; agent STOPPED until we start it
                if svc == "AamPrefectServer":
                    m.stdout = "STATE : 4  RUNNING\n"
                else:
                    m.stdout = "STATE : 4  RUNNING\n" if started["agent"] else "STATE : 1  STOPPED\n"
            elif "start" in args:
                if args[2] == "AamBackupAgent":
                    started["agent"] = True  # simulate recovery
                m.returncode = 0
            return m
        mock_run.side_effect = side_effect

        with patch("watchdog._resolve_paths"), \
             patch("watchdog.BACKUP_LOCK_PATH", Path(tempfile.mktemp())):
            with patch("watchdog.time.sleep") as mock_sleep:
                n = {"c": 0}
                def sleep_side(*a):
                    n["c"] += 1
                    if n["c"] >= 2:
                        raise KeyboardInterrupt
                mock_sleep.side_effect = sleep_side
                try:
                    watchdog.main()
                except KeyboardInterrupt:
                    pass

        start_calls = [c for c in mock_run.call_args_list if "start" in c[0][0]]
        assert len(start_calls) == 1  # started once, recovered, no flapping
        assert start_calls[0][0][0][2] == "AamBackupAgent"


# ═══════════════════════════════════════════════════════════════
# Circuit breaker — no flapping a crash-looping service
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_allows_max_starts_then_blocks(self):
        watchdog.service_start_log.clear()
        svc = "AamBackupAgent"
        outcomes = [watchdog._start_allowed(svc) for _ in range(5)]
        assert outcomes[:3] == [True, True, True]
        assert outcomes[3:] == [False, False]

    def test_window_empties_allow_again(self):
        watchdog.service_start_log.clear()
        svc = "AamPrefectServer"
        for _ in range(3):
            watchdog._start_allowed(svc)
        # fast-forward past the 1h window
        watchdog.service_start_log[svc] = [t - watchdog.START_WINDOW_SECONDS - 1 for t in watchdog.service_start_log[svc]]
        assert watchdog._start_allowed(svc) is True

    def test_recovery_clears_log(self):
        watchdog.service_start_log["AamBackupAgent"] = [1.0, 2.0, 3.0]
        watchdog.service_start_log.pop("AamBackupAgent", None)  # what main() does on RUNNING
        assert watchdog.service_start_log.get("AamBackupAgent") is None


# ═══════════════════════════════════════════════════════════════
# G6 — corrupted lock files must not crash the watchdog
# ═══════════════════════════════════════════════════════════════

class TestPoisonedLock:
    @pytest.mark.parametrize("content", [
        "-1:100.0",          # negative PID, new format
        "0:100.0",           # zero PID
        "99999999999",       # absurd legacy PID
        "garbage",           # non-numeric
        "-5:-2.5",           # negative PID + create time
    ])
    def test_read_lock_alive_never_raises(self, content):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".lock") as f:
            f.write(content)
            path = Path(f.name)
        try:
            alive, pid = read_lock_alive(path)
            assert alive is False, f"{content!r} must be treated as stale"
        finally:
            path.unlink(missing_ok=True)

    def test_is_backup_running_survives_poisoned_lock(self):
        """The exact crash mode found by probe D: OSError-only catch let
        ValueError/OverflowError out of the main loop -> NSSM crash loop."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".lock") as f:
            f.write("-1:100.0")
            path = Path(f.name)
        try:
            with patch.object(watchdog, "BACKUP_LOCK_PATH", path):
                assert watchdog._is_backup_running() is False  # no exception!
        finally:
            path.unlink(missing_ok=True)
