"""H4 — watchdog deferral counter isolation and same-iteration force restart.

Regression tests for the shared-counter defect: a long run of
transfer-deferrals used to pre-satisfy the stale-lock cap (MAX_DEFERRALS=15)
so the watchdog force-deleted a LIVE lock the moment the transfer ended,
restarting under an active backup. The transfer branch also fell through
into the stale-lock branch using stale state, burning one extra cycle.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

import watchdog


@pytest.fixture
def mock_sc_run():
    with patch("watchdog.subprocess.run") as m:
        def side_effect(args, **kwargs):
            mock_result = MagicMock()
            if "query" in args:
                mock_result.stdout = "STATE : 4  RUNNING"
                mock_result.returncode = 0
            elif "stop" in args:
                mock_result.returncode = 0
            return mock_result
        m.side_effect = side_effect
        yield m


@pytest.fixture
def temp_lock_file():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


class _Sequences:
    """Callable side_effects backed by per-call queues with fallbacks."""

    def __init__(self, transfer, lock):
        self._transfer = list(transfer)
        self._lock = list(lock)

    def transferring(self):
        if self._transfer:
            return self._transfer.pop(0)
        return False

    def lock_held(self):
        if self._lock:
            return self._lock.pop(0)
        return False


def _run_loop(max_sleeps, events, lock_path):
    """Drive watchdog.main() until max_sleeps sleep() calls have happened."""
    with patch("watchdog.time.sleep") as m_sleep:
        counter = {"n": 0}

        def sleep_side_effect(seconds):
            events.append(("sleep", seconds))
            counter["n"] += 1
            if counter["n"] >= max_sleeps:
                raise KeyboardInterrupt("end of simulation")

        m_sleep.side_effect = sleep_side_effect

        def sc_wrapper(cmd, **kwargs):
            # NOTE: with a function side_effect, the mock forwards positional
            # args AS-IS — cmd here IS the command list ["sc", "stop", ...].
            events.append(("sc", list(cmd)))
            result = MagicMock()
            if "query" in cmd:
                result.stdout = "STATE : 4  RUNNING"
                result.returncode = 0
            else:
                result.returncode = 0
            return result

        try:
            with patch("watchdog._resolve_paths"), \
                 patch("watchdog.BACKUP_LOCK_PATH", lock_path), \
                 patch("watchdog.subprocess.run", side_effect=sc_wrapper):
                watchdog.main()
        except KeyboardInterrupt:
            pass


def _unhealthy_httpx():
    # FAILURE_THRESHOLD-1 pre-threshold cycles each sleep CHECK_INTERVAL_SECONDS;
    # every cycle afterwards reaches the transfer/lock decision logic.
    with patch("httpx.get", side_effect=httpx.ConnectError("dead")):
        yield


def test_transfer_deferrals_do_not_presatisfy_lock_cap(mock_sc_run, temp_lock_file):
    """20 transfer-deferrals (< 240 cap) followed by a held lock must still
    get its full 15-cycle grace — the old shared counter forced immediately."""
    seq = _Sequences(transfer=[True] * 20, lock=[False] * 20 + [True] * 30)
    events: list = []

    with patch("watchdog._is_backup_running", side_effect=seq.lock_held), \
         patch("watchdog._transfer_process_running", side_effect=seq.transferring), \
         patch("httpx.get", side_effect=httpx.ConnectError("dead")):
        # sleeps: 4 threshold + 20 transfer-deferral + 14 lock-deferral grace,
        # then the 15th lock deferral forces -> stop. Budget covers it all.
        _run_loop(60, events, temp_lock_file)

    stops = [i for i, e in enumerate(events) if e[0] == "sc" and "stop" in e[1]]
    assert stops, "forced restart never triggered"
    first_stop = stops[0]
    sleeps_before = sum(
        1 for e in events[:first_stop] if e[0] == "sleep"
    )
    # 4 (failure threshold) + 20 (transfer deferrals) + 14 (lock grace) = 38.
    # The old shared counter forced at sleeps_before == 24 (lock cycle 1).
    assert sleeps_before == 38, (
        "forced restart must happen only after the FULL 15-cycle lock grace; "
        f"restart fired after {sleeps_before} sleeps"
    )
    # The live lock was removed by the watchdog itself at force time.
    assert not temp_lock_file.exists()


def test_transfer_cap_force_restarts_in_same_iteration(mock_sc_run, temp_lock_file):
    """When the 8 h transfer cap fires, the restart must proceed immediately —
    no wasted BACKUP_WAIT_INTERVAL cycle on stale lock_held state."""
    seq = _Sequences(transfer=[True] * 240 + [False] * 30,
                     lock=[True] * 240 + [False] * 30)
    events: list = []

    with patch("watchdog._is_backup_running", side_effect=seq.lock_held), \
         patch("watchdog._transfer_process_running", side_effect=seq.transferring), \
         patch("httpx.get", side_effect=httpx.ConnectError("dead")):
        _run_loop(400, events, temp_lock_file)

    stops = [i for i, e in enumerate(events) if e[0] == "sc" and "stop" in e[1]]
    assert len(stops) >= 1, "cap reached but no restart was triggered"
    first_stop = stops[0]
    sleeps_before = [e for e in events[:first_stop] if e[0] == "sleep"]
    waits = [s for s in sleeps_before if s[1] == watchdog.BACKUP_WAIT_INTERVAL]
    checks = [s for s in sleeps_before if s[1] == watchdog.CHECK_INTERVAL_SECONDS]
    # 4 threshold sleeps + 239 deferral sleeps (the 240th forces without sleeping).
    assert len(checks) == 4
    assert len(waits) == watchdog.MAX_TRANSFER_DEFERRALS - 1, (
        "stale fall-through detected: an extra BACKUP_WAIT cycle burned "
        "between the forced unlink and the restart"
    )


def test_healthy_cycle_resets_both_counters(mock_sc_run, temp_lock_file):
    """A healthy API response resets both deferral counters. Budget of 1
    sleep = exactly one full healthy cycle (check -> agent check -> sleep)."""
    with patch("httpx.get") as m_httpx:
        resp = MagicMock()
        resp.status_code = 200
        m_httpx.return_value = resp
        events: list = []
        with patch("watchdog.BACKUP_LOCK_PATH", temp_lock_file), \
             patch("watchdog.AGENT_SERVICE", "NoSuchService"):
            _run_loop(1, events, temp_lock_file)
        assert m_httpx.call_count == 1
