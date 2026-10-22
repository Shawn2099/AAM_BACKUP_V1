"""M2 2.5 — shared SMB readiness (realistic, configurable NAS wake timing)."""

import socket
from unittest.mock import patch

import pytest

from core import smb_ready as sr


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(tcp_seq, smb_seq=None, **kw):
    clock = FakeClock()
    tcp = iter(tcp_seq)
    smb = iter(smb_seq or [])
    params = dict(probe_path="\\\\nas\\s\\.AAM_TARGET_MOUNTED", connect_timeout_s=15,
                  max_wait_s=900, poll_interval_s=15, stability_wait_s=90)
    params.update(kw)

    def fake_tcp(host, port, timeout_s):
        clock.t += 1  # a probe takes time too
        ok = next(tcp)
        return ok, "tcp open" if ok else "tcp 445: TimeoutError"

    def fake_smb(path):
        return next(smb, (True, "smb stat ok"))

    with patch.object(sr, "tcp_port_open", fake_tcp), patch.object(sr, "smb_path_usable", fake_smb):
        return sr.wait_for_smb_ready("nas", clock=clock.now, sleep=clock.sleep, **params), clock


def test_slow_cold_boot_within_budget_is_ready():
    # NAS answers only after ~6 minutes (24 failed polls x 15s) - old code gave up at ~15s
    res, clock = _run([False] * 24 + [True, True])
    assert res.ready and res.attempts == 25
    assert res.elapsed_s > 360 + 90          # includes the stability wait
    assert any("stability re-check: OK" in t for t in res.timeline)


def test_never_answers_fails_after_max_wait_with_reason():
    res, clock = _run([False] * 1000, max_wait_s=300)
    assert not res.ready
    assert 300 <= res.elapsed_s < 300 + 15 + 2
    assert f"within {res.elapsed_s:.0f}s" in res.reason and "TimeoutError" in res.reason


def test_failure_reason_states_the_time_really_spent():
    # The LAN preflight uses max_wait_s=0 + min_attempts=8 (15s apart): the reason
    # must not claim "within 0s" after ~2 minutes of probing (trial box log 2026-10-15).
    res, _ = _run([False] * 8, max_wait_s=0, min_attempts=8, stability_wait_s=0)
    assert not res.ready and res.attempts == 8
    assert "within 0s" not in res.reason
    assert f"within {res.elapsed_s:.0f}s" in res.reason


def test_tcp_open_but_smb_not_serving_is_not_ready():
    res, _ = _run([True] * 1000, smb_seq=[(False, "smb stat failed: OSError: [WinError 64]")] * 1000,
                  max_wait_s=60)
    assert not res.ready and "smb stat failed" in res.reason


def test_share_dropping_during_stability_wait_keeps_polling():
    res, _ = _run([True, False, True, True])
    assert res.ready
    assert any("stability re-check: no" in t for t in res.timeline)


def test_min_attempts_even_with_zero_budget():
    res, _ = _run([False, False, True, True], max_wait_s=0, min_attempts=2, stability_wait_s=0)
    assert not res.ready and res.attempts == 2


def test_real_tcp_probe_against_local_listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert sr.tcp_port_open("127.0.0.1", port, 2)[0] is True
    finally:
        srv.close()
    assert sr.tcp_port_open("127.0.0.1", port, 1)[0] is False


def test_smb_path_usable_semantics(tmp_path):
    f = tmp_path / ".AAM_TARGET_MOUNTED"
    f.write_text("")
    assert sr.smb_path_usable(str(f))[0] is True
    assert sr.smb_path_usable(str(tmp_path / "absent"))[0] is True   # share answered
    assert sr.smb_path_usable(None)[0] is True


# ── wiring (M2 2.5 part 2) ────────────────────────────────────────────────

def _wol_cfg(tmp_path, **wol):
    from models.config import AppConfig

    w = {"enabled": True, "mac_address": "AA:BB:CC:DD:EE:FF", "server_ip": "10.10.186.231"}
    w.update(wol)
    return AppConfig(
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": "\\\\10.10.186.231\\share\\FY26-27"},
        wol=w, cloud={"enabled": False}, dashboard={})


def test_config_defaults_match_plan(tmp_path):
    cfg = _wol_cfg(tmp_path)
    assert (cfg.wol.wake_timeout_seconds, cfg.wol.stability_wait_seconds,
            cfg.wol.wake_retry_count, cfg.wol.wake_retry_interval_seconds,
            cfg.wol.smb_connect_timeout_seconds) == (900, 90, 5, 10, 15)
    assert (cfg.lan.preflight_smb_attempts, cfg.lan.preflight_smb_wait_seconds) == (8, 15)
    _wol_cfg(tmp_path, wake_timeout_seconds=3600)          # cap raised from 600


def test_already_online_requires_usable_share(tmp_path):
    from core import wol

    cfg = _wol_cfg(tmp_path)
    with patch.object(wol, "_smb_port_open", return_value=True), \
         patch("core.smb_ready.smb_path_usable", return_value=(True, "smb stat ok")) as stat, \
         patch.object(wol, "_send_magic_packet") as send:
        assert wol.ensure_server_online(cfg) is True
    send.assert_not_called()
    assert stat.call_args.args[0].endswith("\\FY26-27\\.AAM_TARGET_MOUNTED")


def test_port_open_but_share_unusable_triggers_wake_with_config_timings(tmp_path):
    from core import wol

    cfg = _wol_cfg(tmp_path, wake_timeout_seconds=1200, ping_interval_seconds=20,
                   stability_wait_seconds=60, wake_retry_count=4,
                   wake_retry_interval_seconds=12, smb_connect_timeout_seconds=9)
    with patch.object(wol, "_smb_port_open", return_value=True), \
         patch("core.smb_ready.smb_path_usable", return_value=(False, "smb stat failed")), \
         patch.object(wol, "_send_magic_packet") as send, \
         patch.object(wol, "wait_for_server") as wait:
        wol.ensure_server_online(cfg)
    send.assert_called_once_with("AA:BB:CC:DD:EE:FF", "10.10.186.255", repeat=4, interval=12)
    args, kwargs = wait.call_args
    assert args == ("10.10.186.231", 1200, 20, 60)
    assert kwargs["connect_timeout"] == 9 and kwargs["probe_path"].endswith(".AAM_TARGET_MOUNTED")


def test_wake_timeout_message_carries_the_reason(tmp_path):
    from core import wol

    with patch.object(wol, "_smb_port_open", return_value=False), \
         patch.object(wol.time, "sleep"):
        with pytest.raises(wol.WolTimeout) as exc:
            wol.wait_for_server("10.10.186.231", 0, 15, 90, probe_path=None, connect_timeout=15)
    assert "did not become usable over SMB" in str(exc.value)


def test_lan_preflight_message_names_attempts(tmp_path):
    from core import lan_preflight as lp
    from core.health import HealthError

    with patch.object(lp, "_is_smb_reachable", return_value=False) as probe:
        with pytest.raises(HealthError) as exc:
            lp.run_lan_dry_run(str(tmp_path), "\\\\10.10.151.53\\share\\FY26-27",
                               smb_attempts=8, smb_wait_seconds=15, smb_connect_timeout_seconds=15)
    assert probe.call_args.kwargs == {"port": 445, "timeout": 15, "attempts": 8, "wait_s": 15}
    assert "after 8 attempt(s) (15s each, 15s apart)" in str(exc.value)
