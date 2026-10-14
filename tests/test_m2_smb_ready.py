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
    assert "within 300s" in res.reason and "TimeoutError" in res.reason


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
