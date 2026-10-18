"""M4 4.3 - watchdog restart order, STOP_PENDING escalation, config, own log (plan WP13).

Findings (production watchdog logs): the restart failed with `sc stop` 1051
(AamBackupAgent depends on AamPrefectServer) and logged an EMPTY reason
(sc writes to stdout, the code logged stderr); the agent sat in
STOP_PENDING 14+ min with no escalation; thresholds were hard-coded; the
watchdog's loguru sink and NSSM's stdout capture wrote the same file.

sc.exe output below is verbatim from the trial box (Windows Server 2016).
"""

import json
import sys
from types import SimpleNamespace

import pendulum
import pytest
from pydantic import ValidationError

import watchdog as wd
from core import services
from core.attention import RED, compute_attention
from core.manifest import ManifestDB
from core.time_utils import IST
from core.tool_runner import CaptureResult, run_capture
from models.config import AppConfig, WatchdogConfig

QUERYEX_RUNNING = (
    "\r\nSERVICE_NAME: AamPrefectServer \r\n        TYPE               : 10  WIN32_OWN_PROCESS  \r\n"
    "        STATE              : 4  RUNNING \r\n"
    "                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)\r\n"
    "        WIN32_EXIT_CODE    : 0  (0x0)\r\n        SERVICE_EXIT_CODE  : 0  (0x0)\r\n"
    "        CHECKPOINT         : 0x0\r\n        WAIT_HINT          : 0x0\r\n"
    "        PID                : 1100\r\n        FLAGS              : \r\n")
QUERYEX_STOP_PENDING = QUERYEX_RUNNING.replace("4  RUNNING", "3  STOP_PENDING").replace(
    "1100", "2888")
QUERYEX_STOPPED = QUERYEX_RUNNING.replace("4  RUNNING", "1  STOPPED").replace(": 1100", ": 0")
QUERYEX_MISSING = ("[SC] EnumQueryServicesStatus:OpenService FAILED 1060:\r\n\r\n"
                   "The specified service does not exist as an installed service.\r\n\r\n")
STOP_1051 = ("[SC] ControlService FAILED 1051:\r\n\r\nA stop control has been sent to a service "
             "that other running services are dependent on.\r\n\r\n")


def _sc(script):
    """Fake run_capture for core.services: script maps (verb, service) -> [(rc, out), ...]."""
    calls = []

    def fake(argv, *, timeout_s, max_chars=4000):
        verb, svc = argv[1], argv[2]
        calls.append((verb, svc))
        seq = script.get((verb, svc)) or script.get((verb, "*"))
        rc, out = seq.pop(0) if len(seq) > 1 else seq[0]
        return CaptureResult(argv, rc, False, None, out)
    return fake, calls


class TestServices:
    def test_queryex_running_with_pid(self, monkeypatch):
        fake, _ = _sc({("queryex", "*"): [(0, QUERYEX_RUNNING)]})
        monkeypatch.setattr(services, "run_capture", fake)
        st = services.query("AamPrefectServer", timeout_s=30)
        assert (st.state, st.pid) == ("RUNNING", 1100)

    def test_queryex_stopped_has_no_pid(self, monkeypatch):
        fake, _ = _sc({("queryex", "*"): [(0, QUERYEX_STOPPED)]})
        monkeypatch.setattr(services, "run_capture", fake)
        assert services.query("x", timeout_s=30).pid is None

    def test_missing_service_is_unknown_with_the_sc_text(self, monkeypatch):
        fake, _ = _sc({("queryex", "*"): [(1060, QUERYEX_MISSING)]})
        monkeypatch.setattr(services, "run_capture", fake)
        st = services.query("Nope", timeout_s=30)
        assert st.state == "" and "1060" in st.detail and "does not exist" in st.detail

    def test_query_timeout_is_unknown(self, monkeypatch):
        monkeypatch.setattr(services, "run_capture",
                            lambda argv, **k: CaptureResult(argv, None, True, None, ""))
        st = services.query("x", timeout_s=30)
        assert st.state == "" and "timed out" in st.detail

    def test_stop_1051_reason_comes_from_stdout(self, monkeypatch):
        # regression: the old code logged stderr - empty for sc
        fake, _ = _sc({("stop", "*"): [(1051, STOP_1051)]})
        monkeypatch.setattr(services, "run_capture", fake)
        r = services.stop("AamPrefectServer", timeout_s=30)
        assert not r.ok and r.exit_code == 1051
        assert "dependent on" in r.detail

    def test_stop_of_a_stopped_service_and_start_of_a_running_one_are_ok(self, monkeypatch):
        fake, _ = _sc({("stop", "*"): [(1062, "FAILED 1062")],
                       ("start", "*"): [(1056, "FAILED 1056")]})
        monkeypatch.setattr(services, "run_capture", fake)
        assert services.stop("x", timeout_s=30).ok
        assert services.start("x", timeout_s=30).ok

    def test_wait_for_polls_until_the_target_state(self, monkeypatch):
        fake, calls = _sc({("queryex", "*"): [(0, QUERYEX_STOP_PENDING)] * 2
                           + [(0, QUERYEX_STOPPED)]})
        monkeypatch.setattr(services, "run_capture", fake)
        t = [0.0]
        st = services.wait_for("x", {"STOPPED"}, timeout_s=60, poll_s=5, sc_timeout_s=30,
                               clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
        assert st.state == "STOPPED" and len(calls) == 3

    def test_wait_for_gives_up_after_the_timeout(self, monkeypatch):
        fake, _ = _sc({("queryex", "*"): [(0, QUERYEX_STOP_PENDING)]})
        monkeypatch.setattr(services, "run_capture", fake)
        t = [0.0]
        st = services.wait_for("x", {"STOPPED"}, timeout_s=60, poll_s=5, sc_timeout_s=30,
                               clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
        assert st.state == "STOP_PENDING" and t[0] >= 60


class TestRunCapture:
    def test_real_process_output_and_exit_code(self):
        r = run_capture([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"],
                        timeout_s=60)
        assert r.completed and r.exit_code == 3 and "hi" in r.output

    def test_timeout_kills_and_reports(self):
        r = run_capture([sys.executable, "-c", "import time; time.sleep(60)"], timeout_s=1)
        assert r.timed_out and r.exit_code is None

    def test_spawn_error_is_reported(self):
        r = run_capture(["no-such-binary-aam.exe"], timeout_s=5)
        assert r.spawn_error and r.exit_code is None

    def test_output_is_capped(self):
        r = run_capture([sys.executable, "-c", "print('x' * 10000)"], timeout_s=60, max_chars=100)
        assert len(r.output) == 100


class TestWatchdogConfig:
    def test_defaults_match_the_previous_constants(self):
        w = WatchdogConfig()
        assert (w.check_interval_seconds, w.failure_threshold, w.request_timeout_seconds,
                w.backup_wait_interval_seconds, w.max_deferrals, w.max_transfer_deferrals,
                w.restart_cooldown_seconds, w.max_starts_per_window,
                w.start_window_seconds) == (60, 5, 30, 120, 15, 240, 120, 3, 3600)
        assert w.stop_pending_timeout_seconds == 600

    def test_bounds(self):
        with pytest.raises(ValidationError):
            WatchdogConfig(stop_pending_timeout_seconds=10)

    def test_section_exists_and_applies(self, monkeypatch):
        assert isinstance(AppConfig.model_fields["watchdog"].default_factory(), WatchdogConfig)
        monkeypatch.setattr(wd, "FAILURE_THRESHOLD", wd.FAILURE_THRESHOLD)
        monkeypatch.setattr(wd, "STOP_PENDING_TIMEOUT", wd.STOP_PENDING_TIMEOUT)
        wd._apply_config(WatchdogConfig(failure_threshold=9, stop_pending_timeout_seconds=900))
        assert wd.FAILURE_THRESHOLD == 9 and wd.STOP_PENDING_TIMEOUT == 900
        wd._apply_config(WatchdogConfig())


def test_own_log_file_is_not_the_nssm_file(monkeypatch, tmp_path):
    from loguru import logger
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path)
    wd._configure_logging()
    logger.info("hello watchdog")
    logger.remove()
    assert (tmp_path / "watchdog_app.log").read_text(encoding="utf-8").count("hello watchdog") == 1
    assert not (tmp_path / "watchdog_svc.log").exists()


class FakeStack:
    """Two fake services driven through core.services' functions."""

    def __init__(self, monkeypatch, states, stop_ok=True, stuck=()):
        self.states = dict(states)
        self.log = []
        self.stuck = set(stuck)
        self.stop_ok = stop_ok
        monkeypatch.setattr(wd.services, "query", self.query)
        monkeypatch.setattr(wd.services, "stop", self.stop)
        monkeypatch.setattr(wd.services, "start", self.start)
        monkeypatch.setattr(wd.services, "wait_for", self.wait_for)
        monkeypatch.setattr(wd.services, "kill_process_tree", self.kill)
        monkeypatch.setattr(wd, "_check_health", lambda: self.states[wd.WATCHED_SERVICE] == "RUNNING")
        monkeypatch.setattr(wd.time, "sleep", lambda s: None)

    def query(self, svc, *, timeout_s):
        return services.ServiceStatus(self.states[svc], 4242 if self.states[svc] != "STOPPED" else None, "")

    def stop(self, svc, *, timeout_s):
        self.log.append(("stop", svc))
        if not self.stop_ok:
            return services.ControlResult(False, 1051, STOP_1051.strip())
        self.states[svc] = "STOP_PENDING" if svc in self.stuck else "STOPPED"
        return services.ControlResult(True, 0, "")

    def start(self, svc, *, timeout_s):
        self.log.append(("start", svc))
        self.states[svc] = "RUNNING"
        return services.ControlResult(True, 0, "")

    def wait_for(self, svc, targets, **kw):
        return self.query(svc, timeout_s=1)

    def kill(self, pid):
        self.log.append(("kill", pid))
        for svc, st in self.states.items():
            if st == "STOP_PENDING":
                self.states[svc] = "STOPPED"
        return "killed"


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(wd, "_notify", lambda ev: sent.append(ev))
    monkeypatch.setattr(wd, "_attention_raised", False)
    return sent


class TestRestartStack:
    def test_agent_stops_first_and_starts_last(self, monkeypatch, alerts):
        stack = FakeStack(monkeypatch, {wd.AGENT_SERVICE: "RUNNING", wd.WATCHED_SERVICE: "RUNNING"})
        monkeypatch.setattr(wd, "_is_backup_running", lambda: False)
        assert wd._restart_stack() is True
        assert stack.log == [("stop", wd.AGENT_SERVICE), ("stop", wd.WATCHED_SERVICE),
                             ("start", wd.WATCHED_SERVICE), ("start", wd.AGENT_SERVICE)]
        assert alerts == []

    def test_refused_stop_is_a_critical_with_the_sc_reason(self, monkeypatch, alerts):
        stack = FakeStack(monkeypatch, {wd.AGENT_SERVICE: "RUNNING", wd.WATCHED_SERVICE: "RUNNING"},
                          stop_ok=False)
        assert wd._restart_stack() is False
        assert ("start", wd.WATCHED_SERVICE) not in stack.log
        [ev] = alerts
        assert ev["kind"] == "ACTION_REQUIRED" and "1051" in ev["message"]
        assert "dependent on" in ev["message"]

    def test_stuck_stop_is_killed_when_no_backup_runs(self, monkeypatch, alerts):
        stack = FakeStack(monkeypatch, {wd.AGENT_SERVICE: "RUNNING", wd.WATCHED_SERVICE: "RUNNING"},
                          stuck={wd.AGENT_SERVICE})
        monkeypatch.setattr(wd, "_is_backup_running", lambda: False)
        assert wd._restart_stack() is True
        assert ("kill", 4242) in stack.log
        assert stack.states == {wd.AGENT_SERVICE: "RUNNING", wd.WATCHED_SERVICE: "RUNNING"}

    def test_stuck_stop_is_never_killed_under_a_backup(self, monkeypatch, alerts):
        stack = FakeStack(monkeypatch, {wd.AGENT_SERVICE: "STOP_PENDING",
                                        wd.WATCHED_SERVICE: "RUNNING"})
        monkeypatch.setattr(wd, "_is_backup_running", lambda: True)
        assert wd._resolve_stuck_stop(wd.AGENT_SERVICE) is False
        assert not any(op == "kill" for op, _ in stack.log)
        [ev] = alerts
        assert ev["status"] == "STOP_PENDING_UNDER_BACKUP"


class _StopLoop(BaseException):
    pass


def _prep(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "_resolve_paths", lambda: None)
    monkeypatch.setattr(wd, "_configure_logging", lambda: None)
    monkeypatch.setattr(wd, "BACKUP_LOCK_PATH", tmp_path / "backup.lock")


def test_main_escalates_an_agent_stuck_in_stop_pending(monkeypatch, tmp_path, alerts):
    _prep(monkeypatch, tmp_path)
    monkeypatch.setattr(wd, "_check_health", lambda: True)
    monkeypatch.setattr(wd, "_service_state", lambda s: "STOP_PENDING")
    resolved = []
    monkeypatch.setattr(wd, "_resolve_stuck_stop", lambda s: resolved.append(s) or True)
    t = [0.0]
    monkeypatch.setattr(wd.time, "monotonic", lambda: t[0])
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s
        if len(sleeps) > 12:
            raise _StopLoop()
    monkeypatch.setattr(wd.time, "sleep", sleep)
    monkeypatch.setattr(wd, "STOP_PENDING_TIMEOUT", 600)
    monkeypatch.setattr(wd, "CHECK_INTERVAL_SECONDS", 60)
    with pytest.raises(_StopLoop):
        wd.main()
    # first seen at t=0; escalated at the first check at/after 600 s, once per stuck episode
    assert resolved[:1] == [wd.AGENT_SERVICE]
    assert len(resolved) == 1


def test_main_hung_api_uses_the_ordered_restart(monkeypatch, tmp_path, alerts):
    _prep(monkeypatch, tmp_path)
    monkeypatch.setattr(wd, "_check_health", lambda: False)
    monkeypatch.setattr(wd, "_service_state", lambda s: "RUNNING")
    monkeypatch.setattr(wd, "_is_backup_running", lambda: False)
    monkeypatch.setattr(wd, "_transfer_process_running", lambda: False)
    restarts = []
    monkeypatch.setattr(wd, "_restart_stack", lambda: restarts.append(1) or True)
    monkeypatch.setattr(wd, "FAILURE_THRESHOLD", 2)
    calls = [0]

    def sleep(s):
        calls[0] += 1
        if calls[0] >= 2:   # 1 = below-threshold poll, 2 = the cooldown after the restart
            raise _StopLoop()
    monkeypatch.setattr(wd.time, "sleep", sleep)
    with pytest.raises(_StopLoop):
        wd.main()
    assert restarts == [1]


def test_critical_then_recovery_sends_recovered_once(monkeypatch, tmp_path, alerts):
    _prep(monkeypatch, tmp_path)
    wd._critical("x", "t", "m", "S")
    monkeypatch.setattr(wd, "_check_health", lambda: True)
    monkeypatch.setattr(wd, "_service_state", lambda s: "RUNNING")
    n = [0]

    def sleep(s):
        n[0] += 1
        if n[0] > 2:
            raise _StopLoop()
    monkeypatch.setattr(wd.time, "sleep", sleep)
    with pytest.raises(_StopLoop):
        wd.main()
    assert [e["kind"] for e in alerts] == ["ACTION_REQUIRED", "RECOVERED"]


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _att_cfg():
    return SimpleNamespace(
        cloud=SimpleNamespace(enabled=True), lan=SimpleNamespace(enabled=True),
        dashboard=SimpleNamespace(stale_after_hours=26, alert_failure_window_days=7),
        notifications=SimpleNamespace(send_on_failure=True))


def _ok_runs(db):
    ts = pendulum.now(IST).subtract(hours=1).isoformat()
    for leg in ("cloud", "lan"):
        db.insert_run({"run_id": leg, "mode": leg, "started_at": ts, "ended_at": ts,
                       "status": f"{leg.upper()}_COMPLETE"})


def _notif(db, kind, hours_ago):
    db.record_notification({"event_key": "watchdog:x", "kind": kind, "mode": "watchdog",
                            "subject": "[ACTION REQUIRED] T - WATCHDOG: AamBackupAgent is stopped",
                            "status": "SENT",
                            "created_at": pendulum.now("UTC").subtract(hours=hours_ago).isoformat()})


def test_dashboard_shows_an_open_watchdog_critical(db):
    _ok_runs(db)
    _notif(db, "ACTION_REQUIRED", 1)
    items = compute_attention(db, _att_cfg())
    assert [(i["level"], i["code"]) for i in items] == [(RED, "watchdog")]
    assert "AamBackupAgent is stopped" in items[0]["message"]


def test_dashboard_clears_after_recovered(db):
    _ok_runs(db)
    _notif(db, "ACTION_REQUIRED", 2)
    _notif(db, "RECOVERED", 1)
    assert compute_attention(db, _att_cfg()) == []
