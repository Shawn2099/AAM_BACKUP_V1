"""WAVE 17 — coverage-driven tests for the process/ops layer (part 1).

Found by coverage.py (branch mode) over the full lab suite:
`audit_lab/raw/cov_v1.json`, gap map via `audit_lab/gap_map.py`.

This part covers the two worst-covered modules:
  * core/wol.py  (32%) — magic packets, SMB probe, wake/wait
  * watchdog.py  (41%) — SCM state, service control, circuit breaker, main loop

Part 2 (launch.py, collect_config_data.py, dry-run preflights) is WAVE 18.

Safety: every subprocess, socket and psutil call is faked; nothing real runs.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(300)


class _StopLoop(BaseException):
    """Break out of watchdogs' `while True` (main() catches Exception only)."""


def _cp(returncode=0, stdout="", stderr=""):
    """A subprocess.CompletedProcess stand-in."""
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _breaker(max_sleeps: int):
    """A time.sleep that raises _StopLoop after N calls (breaks the loop)."""
    def fake_sleep(_s):
        if fake_sleep.calls >= max_sleeps:
            raise _StopLoop()
        fake_sleep.calls += 1
    fake_sleep.calls = 0
    return fake_sleep


class _FakeSock:
    """Records connect_ex and returns a scripted result (or raises)."""

    def __init__(self, result=0, exc=None):
        self.result, self.exc, self.addr = result, exc, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        pass

    def connect_ex(self, addr):
        self.addr = addr
        if self.exc is not None:
            raise self.exc
        return self.result


# ══════════════════════════════════════════════════════════════════════════════
# W17.1 — core/wol.py (was 32%)
# ══════════════════════════════════════════════════════════════════════════════

def test_w17_smb_port_rejects_bad_ports(monkeypatch):
    """P1-WOL clamp guard: a bad port never reaches connect_ex, never raises."""
    import core.wol as wol

    made = []
    monkeypatch.setattr(wol.socket, "socket", lambda *a, **k: made.append(1) or _FakeSock())
    for bad in ("445", None, 70000, -1, 1.5, 65536):
        assert wol._smb_port_open("127.0.0.1", port=bad) is False
    assert made == [], "a bad port must not open a socket"
    L.record("W17.smb_port_clamp", "invalid ports are refused before any socket work",
             f"rejected: '445', None, 70000, -1, 1.5, 65536 | sockets opened={len(made)}",
             "OK - never raises, never connects" if not made else "ISSUE - opened a socket")


def test_w17_smb_port_connect_results(monkeypatch):
    import core.wol as wol

    sock = _FakeSock(result=0)
    monkeypatch.setattr(wol.socket, "socket", lambda *a, **k: sock)
    assert wol._smb_port_open("10.0.0.5", port=445, timeout=3) is True
    assert sock.addr == ("10.0.0.5", 445)
    sock.result = 1
    assert wol._smb_port_open("10.0.0.5") is False


def test_w17_smb_port_oserror_returns_false(monkeypatch):
    import core.wol as wol

    monkeypatch.setattr(wol.socket, "socket", lambda *a, **k: _FakeSock(exc=OSError("boom")))
    assert wol._smb_port_open("10.0.0.5") is False


def test_w17_magic_packet_rounds_and_intervals(monkeypatch):
    """G3: repeat rounds, global + subnet targets, interval sleeps between."""
    import core.wol as wol

    sent, slept = [], []
    monkeypatch.setattr(wol, "wol_send", lambda mac, ip_address=None, port=None:
                        sent.append((mac, ip_address, port)))
    monkeypatch.setattr(wol.time, "sleep", lambda s: slept.append(s))
    wol._send_magic_packet("AA:BB:CC:DD:EE:FF", "192.168.1.255", repeat=3, interval=5)
    assert len(sent) == 6, sent                      # 3 rounds x 2 targets
    assert {s[1] for s in sent} == {"255.255.255.255", "192.168.1.255"}
    assert all(s[0] == "AA:BB:CC:DD:EE:FF" and s[2] == 9 for s in sent)
    assert slept == [5, 5]                           # no sleep after the last round
    L.record("W17.magic_packet_contract",
             "magic packets go to global + subnet broadcast, repeat rounds apart",
             f"packets={len(sent)} targets={sorted({s[1] for s in sent})} sleeps={slept}",
             "OK - 3 rounds x 2 targets, 2 intervals")


def test_w17_magic_packet_subnet_equals_global_and_repeat_clamp(monkeypatch):
    import core.wol as wol

    sent, slept = [], []
    monkeypatch.setattr(wol, "wol_send", lambda mac, ip_address=None, port=None:
                        sent.append((mac, ip_address)))
    monkeypatch.setattr(wol.time, "sleep", lambda s: slept.append(s))
    wol._send_magic_packet("AA", "255.255.255.255", repeat=2, interval=1)
    assert len(sent) == 2 and {s[1] for s in sent} == {"255.255.255.255"}
    assert slept == [1]                           # one interval between the two rounds
    sent.clear()
    slept.clear()
    wol._send_magic_packet("AA", "192.168.1.255", repeat=0, interval=9)   # clamp to 1
    wol._send_magic_packet("AA", "192.168.1.255", repeat=-4, interval=9)
    assert len(sent) == 4 and slept == []         # one round each, no sleeping


def test_w17_magic_packet_per_round_failure_continues(monkeypatch):
    """P1-WOL: one bad round must not abort the wake cycle."""
    import core.wol as wol

    calls = []

    def flaky(mac, ip_address=None, port=None):
        calls.append(ip_address)
        if len(calls) % 2 == 1:
            raise ValueError("unparseable MAC")

    monkeypatch.setattr(wol, "wol_send", flaky)
    monkeypatch.setattr(wol.time, "sleep", lambda s: None)
    wol._send_magic_packet("zz", "192.168.1.255", repeat=2, interval=0)
    assert calls == ["255.255.255.255", "192.168.1.255",
                     "255.255.255.255", "192.168.1.255"]


def test_w17_wait_for_server_immediate_with_stability(monkeypatch):
    import core.wol as wol

    slept = []
    monkeypatch.setattr(wol, "_smb_port_open", lambda ip, *a, **k: True)
    monkeypatch.setattr(wol.time, "sleep", lambda s: slept.append(s))
    wol.wait_for_server("10.0.0.9", wake_timeout=60, ping_interval=2, stability_wait=10)
    assert slept == [10], "only the stability wait, no poll backoff"
    slept.clear()
    wol.wait_for_server("10.0.0.9", wake_timeout=60, ping_interval=2, stability_wait=0)
    assert slept == []


def test_w17_wait_for_server_timeout_raises(monkeypatch):
    import core.wol as wol

    monkeypatch.setattr(wol, "_smb_port_open", lambda ip, *a, **k: False)
    monkeypatch.setattr(wol.time, "sleep", lambda s: None)
    with pytest.raises(wol.WolTimeout):
        wol.wait_for_server("10.0.0.9", wake_timeout=1, ping_interval=0, stability_wait=0)


def test_w17_ensure_server_online_paths(monkeypatch, sandbox):
    import core.wol as wol

    cfg = L.build_config(sandbox, source=sandbox / "src", lan_dest=str(sandbox / "nas"),
                         runtime=sandbox / "runtime")
    sent: list = []

def _no_send(*a, **k):
    sent.append((a, k))

    monkeypatch.setattr(wol, "_send_magic_packet", _no_send)
    monkeypatch.setattr(wol, "wait_for_server", lambda *a, **k: sent.append("wait"))

    # WoL disabled -> online by assumption, no packet
    cfg.wol.enabled = False
    monkeypatch.setattr(wol, "_smb_port_open", lambda ip: False)
    assert wol.ensure_server_online(cfg) is True
    assert sent == []

    # enabled + already online -> no packet
    cfg.wol.enabled = True
    monkeypatch.setattr(wol, "_smb_port_open", lambda ip: True)
    assert wol.ensure_server_online(cfg) is True
    assert sent == []

    # enabled + offline -> wake packet then wait
    monkeypatch.setattr(wol, "_smb_port_open", lambda ip: False)
    assert wol.ensure_server_online(cfg) is True
    assert len(sent) == 2 and sent[1] == "wait"
    (mac, bcast), kw = sent[0]
    assert mac == cfg.wol.mac_address and bcast == cfg.wol.get_broadcast_address()
    assert kw == {"repeat": cfg.wol.wake_retry_count,
                  "interval": cfg.wol.wake_retry_interval_seconds}
    L.record("W17.wol_ensure_online",
             "ensure_server_online() is a no-op when disabled or already online "
             "and otherwise wakes + waits",
             f"disabled->no-packet | online->no-packet | offline->packet+wait "
             f"mac={mac} broadcast={bcast}",
             "OK - all three paths behave as documented")


# ══════════════════════════════════════════════════════════════════════════════
# W17.2 — watchdog.py (was 41%)
# ══════════════════════════════════════════════════════════════════════════════

def test_w17_pid_is_alive(monkeypatch):
    pytest.importorskip("psutil")
    import psutil
    import watchdog as wd

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 42)
    assert wd._pid_is_alive(42) is True
    assert wd._pid_is_alive(43) is False


def test_w17_transfer_process_running(monkeypatch):
    pytest.importorskip("psutil")
    import psutil
    import watchdog as wd

    def _iter(attrs=None):
        return [types.SimpleNamespace(info={"name": "Robocopy.exe"})]

    monkeypatch.setattr(psutil, "process_iter", _iter)
    assert wd._transfer_process_running() is True
    monkeypatch.setattr(psutil, "process_iter",
                        lambda attrs=None: [types.SimpleNamespace(info={"name": "chrome.exe"})])
    assert wd._transfer_process_running() is False

    def boom(attrs=None):
        raise RuntimeError("psutil gone")

    monkeypatch.setattr(psutil, "process_iter", boom)
    assert wd._transfer_process_running() is False


def test_w17_is_backup_running_states(monkeypatch, sandbox):
    import watchdog as wd
    from core import process as cp

    lock = sandbox / "backup.lock"
    monkeypatch.setattr(wd, "BACKUP_LOCK_PATH", lock)

    # no lock file at all
    assert wd._is_backup_running() is False

    # live owner
    monkeypatch.setattr(cp, "read_lock_alive", lambda p: (True, 1234))
    lock.write_text("1234:1", encoding="utf-8")
    assert wd._is_backup_running() is True

    # stale owner (PID dead or reused)
    monkeypatch.setattr(cp, "read_lock_alive", lambda p: (False, 4321))
    assert wd._is_backup_running() is False

    # corrupt lock (huge/negative PID) -> never raises, treated as stale
    def boom(p):
        raise OverflowError("Python int too large to convert to C int")

    monkeypatch.setattr(cp, "read_lock_alive", boom)
    assert wd._is_backup_running() is False
    L.record("W17.backup_lock_states",
             "G6: absent / live / stale / corrupt lock are all handled without raising",
             "absent->False | live->True | stale->False | corrupt(OverflowError)->False",
             "OK - the main loop can never be crashed by a bad lock file")


def test_w17_service_state_parsing(monkeypatch):
    import watchdog as wd

    def run_factory(out):
        return lambda *a, **k: _cp(0, out)

    monkeypatch.setattr(wd.subprocess, "run", run_factory("SERVICE_NAME: X\n        STATE              : 4  RUNNING\n"))
    assert wd._service_state("AamPrefectServer") == "RUNNING"
    monkeypatch.setattr(wd.subprocess, "run", run_factory("STATE              : 1  STOPPED\n"))
    assert wd._service_state("x") == "STOPPED"
    monkeypatch.setattr(wd.subprocess, "run", run_factory("STATE              : 4\n"))
    assert wd._service_state("x") == "4"          # one-token fallback
    monkeypatch.setattr(wd.subprocess, "run", run_factory("no state line here"))
    assert wd._service_state("x") == ""

    def tmo(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sc", timeout=15)

    monkeypatch.setattr(wd.subprocess, "run", tmo)
    monkeypatch.setattr(wd.time, "sleep", lambda s: None)   # retry backoff
    assert wd._service_state("x") == ""           # two attempts then give up

    def boom(*a, **k):
        raise OSError("sc.exe not found")

    monkeypatch.setattr(wd.subprocess, "run", boom)
    assert wd._service_state("x") == ""


def test_w17_service_is_running(monkeypatch):
    import watchdog as wd

    monkeypatch.setattr(wd, "_service_state", lambda s: "RUNNING")
    assert wd._service_is_running("svc") is True
    monkeypatch.setattr(wd, "_service_state", lambda s: "START_PENDING")
    assert wd._service_is_running("svc") is False   # F6: transitioning != running


def test_w17_start_service_branches(monkeypatch):
    import watchdog as wd

    cmds = []
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: (cmds.append(a[0]), _cp(0, "ok"))[1])
    assert wd._start_service("AamBackupAgent") is True
    assert cmds[0] == ["sc", "start", "AamBackupAgent"]

    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: _cp(5, "", "access denied"))
    assert wd._start_service("svc") is False

    def tmo(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sc", timeout=30)

    monkeypatch.setattr(wd.subprocess, "run", tmo)
    assert wd._start_service("svc") is False

    def boom(*a, **k):
        raise OSError("no sc")

    monkeypatch.setattr(wd.subprocess, "run", boom)
    assert wd._start_service("svc") is False


def test_w17_start_allowed_breaker(monkeypatch):
    import watchdog as wd

    clock = {"t": 1000.0}
    monkeypatch.setattr(wd.time, "time", lambda: clock["t"])
    monkeypatch.setattr(wd, "service_start_log", {})
    monkeypatch.setattr(wd, "MAX_STARTS_PER_WINDOW", 2)
    monkeypatch.setattr(wd, "START_WINDOW_SECONDS", 3600)

    assert wd._start_allowed("svc") is True
    assert wd._start_allowed("svc") is True
    assert wd._start_allowed("svc") is False      # breaker opens
    clock["t"] += 3601.0                           # window empties -> self-reset
    assert wd._start_allowed("svc") is True


def test_w17_stop_service_never_raises(monkeypatch):
    import watchdog as wd

    cmds = []
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: (cmds.append(a[0]), _cp(0))[1])
    assert wd._stop_service("AamPrefectServer") is None
    assert cmds[0] == ["sc", "stop", "AamPrefectServer"]

    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: _cp(5, "", "denied"))
    assert wd._stop_service("svc") is None

    def tmo(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sc", timeout=30)

    monkeypatch.setattr(wd.subprocess, "run", tmo)
    assert wd._stop_service("svc") is None

    def boom(*a, **k):
        raise OSError("no sc")

    monkeypatch.setattr(wd.subprocess, "run", boom)
    assert wd._stop_service("svc") is None


def test_w17_check_health_branches(monkeypatch):
    import httpx
    import watchdog as wd

    class Ok:
        status_code = 200

    class Bad:
        status_code = 503

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: Ok())
    assert wd._check_health() is True
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: Bad())
    assert wd._check_health() is False

    def boom(url, timeout=None):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    assert wd._check_health() is False


def test_w17_alert_wedged_lock(monkeypatch, sandbox):
    import watchdog as wd
    from core import notify as cn
    import models.config as mc

    sent = []
    monkeypatch.setattr(cn, "notify", lambda cfg, event, db=None: sent.append(event))
    cfg = L.build_config(sandbox, source=sandbox / "src", lan_dest=str(sandbox / "nas"),
                         runtime=sandbox / "runtime")
    monkeypatch.setattr(mc, "load_config", lambda p=None: cfg)

    wd._alert_wedged_lock("reason-x", pid=99)
    assert sent, "the wedged-lock CRITICAL must reach the operator"
    event = sent[0]
    assert "99" in event.message and event.status == "LIVE_LOCK_CAP_EXCEEDED"
    assert event.mode == "watchdog"

    # delivery failure is contained (the monitor must keep running)
    def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(cn, "notify", boom)
    wd._alert_wedged_lock("reason-x", pid=99)   # must not raise
    L.record("W17.wedged_lock_alert",
             "the deferral-cap alert is delivered and its failure is contained",
             f"delivered message pid=99 status={event.status} | "
             "smtp outage -> no exception",
             "OK - alert delivered, outage contained")


# ── main() loop scenarios ─────────────────────────────────────────────────────

def _prep_main(monkeypatch, sandbox, wd):
    monkeypatch.setattr(wd, "_resolve_paths", lambda: None)
    monkeypatch.setattr(wd, "_configure_logging", lambda: None)
    monkeypatch.setattr(wd, "BACKUP_LOCK_PATH", sandbox / "backup.lock")
    monkeypatch.setattr(wd, "LOG_DIR", sandbox / "logs")
    monkeypatch.setattr(wd, "AGENT_SERVICE", "AamBackupAgent")
    monkeypatch.setattr(wd, "WATCHED_SERVICE", "AamPrefectServer")
    monkeypatch.setattr(wd, "FAILURE_THRESHOLD", 3)
    monkeypatch.setattr(wd, "MAX_DEFERRALS", 2)
    monkeypatch.setattr(wd, "MAX_TRANSFER_DEFERRALS", 240)


def test_w17_main_healthy_agent_running(monkeypatch, sandbox):
    import watchdog as wd

    _prep_main(monkeypatch, sandbox, wd)
    monkeypatch.setattr(wd, "_check_health", lambda: True)
    monkeypatch.setattr(wd, "_service_state", lambda s: "RUNNING")
    started = []
    monkeypatch.setattr(wd, "_start_service", lambda s: started.append(s) or True)
    monkeypatch.setattr(wd.time, "sleep", _breaker(1))
    with pytest.raises(_StopLoop):
        wd.main()
    assert started == [], "a RUNNING agent must never be restarted"


def test_w17_main_agent_stopped_starts(monkeypatch, sandbox):
    import watchdog as wd

    _prep_main(monkeypatch, sandbox, wd)
    monkeypatch.setattr(wd, "_check_health", lambda: True)
    monkeypatch.setattr(wd, "_service_state", lambda s: "STOPPED")
    started = []
    monkeypatch.setattr(wd, "_start_allowed", lambda s: True)
    monkeypatch.setattr(wd, "_start_service", lambda s: started.append(s) or True)
    monkeypatch.setattr(wd.time, "sleep", _breaker(1))
    with pytest.raises(_StopLoop):
        wd.main()
    assert started == ["AamBackupAgent"] or set(started) == {"AamBackupAgent"}, \
        "a stopped scheduler must be revived (G4)"


def test_w17_main_agent_stopped_breaker_suppressed(monkeypatch, sandbox):
    import watchdog as wd

    _prep_main(monkeypatch, sandbox, wd)
    monkeypatch.setattr(wd, "_check_health", lambda: True)
    monkeypatch.setattr(wd, "_service_state", lambda s: "STOPPED")
    started = []
    monkeypatch.setattr(wd, "_start_allowed", lambda s: False)
    monkeypatch.setattr(wd, "_start_service", lambda s: started.append(s) or True)
    monkeypatch.setattr(wd.time, "sleep", _breaker(1))
    with pytest.raises(_StopLoop):
        wd.main()
    assert started == [], "the circuit breaker must suppress the start"


def test_w17_main_agent_unknown_state(monkeypatch, sandbox):
    import watchdog as wd

    _prep_main(monkeypatch, sandbox, wd)
    monkeypatch.setattr(wd, "_check_health", lambda: True)
    monkeypatch.setattr(wd, "_service_state", lambda s: "")     # unqueryable
    started = []
    monkeypatch.setattr(wd, "_start_allowed", lambda s: True)
    monkeypatch.setattr(wd, "_start_service", lambda s: started.append(s) or True)
    monkeypatch.setattr(wd.time, "sleep", _breaker(1))
    with pytest.raises(_StopLoop):
        wd.main()
    assert started == [], "an unknown state must not be restarted blindly"


def test_w17_main_unhealthy_below_threshold(monkeypatch, sandbox):
    import watchdog as wd

    _prep_main(monkeypatch, sandbox, wd)
    monkeypatch.setattr(wd, "_check_health", lambda: False)
    transfers = []
    monkeypatch.setattr(wd, "_transfer_process_running", lambda: transfers.append(1) or True)
    stopped, started = [], []
    monkeypatch.setattr(wd, "_stop_service", lambda s: stopped.append(s))
    monkeypatch.setattr(wd, "_start_service", lambda s: started.append(s) or True)
    monkeypatch.setattr(wd.time, "sleep", _breaker(2))
    with pytest.raises(_StopLoop):
        wd.main()
    assert transfers and stopped == [] and started == [], \
        "no restart before the threshold; a probe only counts a deferral"


def test_w17_main_transfer_deferral(monkeypatch, sandbox):
    import watchdog as wd

    _prep_main(monkeypatch, sandbox, wd)
    monkeypatch.setattr(wd, "_check_health", lambda: False)
    monkeypatch.setattr(wd, "FAILURE_THRESHOLD", 1)
    transfers = []
    monkeypatch.setattr(wd, "_transfer_process_running", lambda: transfers.append(1) or True)
    monkeypatch.setattr(wd, "_is_backup_running", lambda: False)
    stopped, started = [], []
    monkeypatch.setattr(wd, "_stop_service", lambda s: stopped.append(s))
    monkeypatch.setattr(wd, "_start_service", lambda s: started.append(s) or True)
    monkeypatch.setattr(wd.time, "sleep", _breaker(1))
    with pytest.raises(_StopLoop):
        wd.main()
    assert len(transfers) >= 1 and stopped == [] and started == []
    L.record("W17.main_transfer_deferral",
             "an in-flight rclone/robocopy defers the restart (8h cap logic)",
             f"transfer probes={len(transfers)} stops={stopped} starts={started}",
             "OK - restart deferred while a real transfer runs")


def test_w17_main_transfer_cap_live_lock(monkeypatch, sandbox):
    import watchdog as wd
    from core import process as cp

    _prep_main(monkeypatch, sandbox, wd)
    monkeypatch.setattr(wd, "_check_health", lambda: False)
    monkeypatch.setattr(wd, "FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(wd, "MAX_TRANSFER_DEFERRALS", 1)
    monkeypatch.setattr(wd, "_transfer_process_running", lambda: True)
    monkeypatch.setattr(wd, "_is_backup_running", lambda: True)      # live owner
    monkeypatch.setattr(cp, "read_lock_alive", lambda p: (True, 77))
    alerts = []
    monkeypatch.setattr(wd, "_alert_wedged_lock", lambda reason, pid=None: alerts.append(pid))
    stopped, started = [], []
    monkeypatch.setattr(wd, "_stop_service", lambda s: stopped.append(s))
    monkeypatch.setattr(wd, "_start_service", lambda s: started.append(s) or True)
    lock = wd.BACKUP_LOCK_PATH
    lock.write_text("77:1", encoding="utf-8")
    monkeypatch.setattr(wd.time, "sleep", _breaker(1))
    with pytest.raises(_StopLoop):
        wd.main()
    assert alerts == [77], "exactly one wedged-lock alert (sentinel dedupes)"
    assert stopped == [] and started == []
    assert lock.exists(), "a PID-live lock is NEVER unlinked"
    L.record("W17.main_transfer_cap_live_lock",
             "at the transfer cap with a live lock owner: CRITICAL + alert, "
             "no unlink, no restart",
             f"alert_pids={alerts} stops={stopped} starts={started} lock_exists={lock.exists()}",
             "OK - fail-closed under a live backup")


def test_w17_main_transfer_cap_lock_released_restarts(monkeypatch, sandbox):
    import watchdog as wd
    from core import process as cp

    _prep_main(monkeypatch, sandbox, wd)
    monkeypatch.setattr(wd, "_check_health", lambda: False)
    monkeypatch.setattr(wd, "FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(wd, "MAX_TRANSFER_DEFERRALS", 1)
    monkeypatch.setattr(wd, "_transfer_process_running", lambda: True)
    monkeypatch.setattr(wd, "_is_backup_running", lambda: False)     # owner dead
    monkeypatch.setattr(cp, "read_lock_alive", lambda p: (False, 999))
    monkeypatch.setattr(wd, "_service_state", lambda s: "STOPPED")
    started = []
    monkeypatch.setattr(wd, "_start_allowed", lambda s: True)
    monkeypatch.setattr(wd, "_start_service", lambda s: started.append(s) or True)
    lock = wd.BACKUP_LOCK_PATH
    lock.write_text("999:1", encoding="utf-8")
    monkeypatch.setattr(wd.time, "sleep", _breaker(1))
    with pytest.raises(_StopLoop):
        wd.main()
    assert not lock.exists(), "a definitely-dead lock owner is collected"
    assert started == ["AamPrefectServer"] or "AamPrefectServer" in started
    L.record("W17.main_transfer_cap_dead_lock",
             "at the transfer cap with a DEAD lock owner: the stale lock is "
             "unlinked and the Prefect service is started",
             f"started={started} lock_exists={lock.exists()}",
             "OK - zombie path recovers")


def test_w17_main_lock_only_deferral_alerts(monkeypatch, sandbox):
    import watchdog as wd
    from core import process as cp

    _prep_main(monkeypatch, sandbox, wd)
    monkeypatch.setattr(wd, "_check_health", lambda: False)
    monkeypatch.setattr(wd, "FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(wd, "MAX_DEFERRALS", 1)
    monkeypatch.setattr(wd, "_transfer_process_running", lambda: False)
    monkeypatch.setattr(wd, "_is_backup_running", lambda: True)
    monkeypatch.setattr(cp, "read_lock_alive", lambda p: (True, 88))
    alerts = []
    monkeypatch.setattr(wd, "_alert_wedged_lock", lambda reason, pid=None: alerts.append(pid))
    stopped, started = [], []
    monkeypatch.setattr(wd, "_stop_service", lambda s: stopped.append(s))
    monkeypatch.setattr(wd, "_start_service", lambda s: started.append(s) or True)
    monkeypatch.setattr(wd.time, "sleep", _breaker(1))
    with pytest.raises(_StopLoop):
        wd.main()
    assert alerts == [88], "lock-only cap alerts once (sentinel dedupes)"
    assert stopped == [] and started == [], "a live lock owner is never restarted under"


def test_w17_main_hung_service_restart(monkeypatch, sandbox):
    import watchdog as wd

    _prep_main(monkeypatch, sandbox, wd)
    monkeypatch.setattr(wd, "_check_health", lambda: False)
    monkeypatch.setattr(wd, "FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(wd, "_transfer_process_running", lambda: False)
    monkeypatch.setattr(wd, "_is_backup_running", lambda: False)
    monkeypatch.setattr(wd, "_service_state", lambda s: "RUNNING")   # RUNNING but API dead
    stopped = []
    monkeypatch.setattr(wd, "_stop_service", lambda s: stopped.append(s))
    monkeypatch.setattr(wd.time, "sleep", _breaker(1))
    with pytest.raises(_StopLoop):
        wd.main()
    assert "AamPrefectServer" in stopped, "a hung-but-RUNNING service is restarted via sc stop"
    L.record("W17.main_hung_restart",
             "service RUNNING but API dead -> sc stop (SCM/NSSM failure actions revive it)",
             f"stopped={stopped}",
             "OK - the genuine hung state triggers the restart")
