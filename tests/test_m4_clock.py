"""M4 4.2 - clock tri-state + w32tm fallback (plan WP12, DEC-6).

Finding: the clock check passed whenever Google could not be reached
(Sophos / DNS) - 24 production nights logged "skipping" and went ahead
with an unverified clock, invisibly. Now: OK | SKEWED | UNVERIFIED, a
w32tm fallback before UNVERIFIED, a configurable policy (warn | block),
and the result on the run row + dashboard.

Real w32tm facts (Windows 11, 2026-10-15):
  ok          rc 0, last line `12:10:52, +00.9227616s`
  no answer   rc 0 (!), last line `12:10:52, error: 0x800705B4`
  bad host    rc 2147953401, `The following error occurred: No such host is known. (0x80072AF9)`
"""

import email.utils
import json
import time
from types import SimpleNamespace

from test_support import attention_cfg
from unittest.mock import MagicMock, patch

import pendulum
import pytest
from pydantic import ValidationError

import flow
from core import health
from core.attention import AMBER, compute_attention
from core.manifest import ManifestDB
from core.run_artefacts import RunArtefacts
from core.time_utils import IST, now_iso
from models.config import HealthConfig
from test_support.fake_tools import fake_run_tool

W32_OK = ("Tracking time.windows.com [40.81.94.65:123].\nCollecting 1 samples.\n"
          "The current time is 15-10-2026 12:10:52.\n12:10:52, +00.9227616s\n")
W32_SKEWED = W32_OK.replace("+00.9227616s", "-0900.5000000s")
W32_NO_ANSWER = ("Tracking 192.0.2.1 [192.0.2.1:123].\nCollecting 1 samples.\n"
                 "The current time is 15-10-2026 12:10:52.\n12:10:52, error: 0x800705B4\n")
W32_BAD_HOST = "The following error occurred: No such host is known. (0x80072AF9)\n"


def _google(offset_s=None, exc=None, header="auto"):
    """Fake www.googleapis.com: a Date header `offset_s` away from now."""
    class _Conn:
        def __init__(self, host, timeout=None):
            pass

        def request(self, *a, **k):
            pass

        def getresponse(self):
            if exc is not None:
                raise exc
            value = (email.utils.formatdate(time.time() + offset_s, usegmt=True)
                     if header == "auto" else header)
            return SimpleNamespace(getheader=lambda _n: value)

        def close(self):
            pass

    return patch.object(health.http.client, "HTTPSConnection", _Conn)


def _check(tmp_path, google, w32_stdout="", w32_exit=0, w32_timed_out=False):
    with google, fake_run_tool("core.health", stdout=w32_stdout, exit_code=w32_exit,
                               timed_out=w32_timed_out) as calls:
        result = health.check_clock(
            max_skew_seconds=600, connection_timeout=5, ntp_server="time.windows.com",
            w32tm_timeout_s=30, artefacts=RunArtefacts(tmp_path, "clk", day="d"))
    return result, calls


class TestCheckClock:
    def test_google_within_limit_is_ok_without_w32tm(self, tmp_path):
        r, calls = _check(tmp_path, _google(30))
        assert r.state == health.CLOCK_OK and r.source == "google"
        assert 25 <= r.skew_s <= 35
        assert calls == []

    @pytest.mark.parametrize("offset", [900, -900])
    def test_google_skew_over_limit_is_skewed(self, tmp_path, offset):
        r, calls = _check(tmp_path, _google(offset))
        assert r.state == health.CLOCK_SKEWED
        assert "w32tm /resync" in r.reason
        assert calls == []

    def test_unreachable_google_falls_back_to_w32tm(self, tmp_path):
        r, calls = _check(tmp_path, _google(exc=OSError("getaddrinfo failed")), W32_OK)
        assert r.state == health.CLOCK_OK and r.source == "w32tm:time.windows.com"
        assert r.skew_s == pytest.approx(0.9227616, abs=1e-3)
        assert calls[0]["argv"] == ["w32tm", "/stripchart", "/computer:time.windows.com",
                                    "/samples:1", "/dataonly"]
        assert calls[0]["timeout_s"] == 30

    def test_missing_date_header_falls_back_too(self, tmp_path):
        r, _ = _check(tmp_path, _google(header=None), W32_OK)
        assert r.state == health.CLOCK_OK and r.source.startswith("w32tm")

    def test_w32tm_skew_is_skewed(self, tmp_path):
        r, _ = _check(tmp_path, _google(exc=OSError("x")), W32_SKEWED)
        assert r.state == health.CLOCK_SKEWED and r.skew_s == pytest.approx(900.5)

    def test_w32tm_exit_0_without_a_sample_is_not_trusted(self, tmp_path):
        # regression: w32tm exits 0 when the NTP server never answers
        r, _ = _check(tmp_path, _google(exc=OSError("x")), W32_NO_ANSWER)
        assert r.state == health.CLOCK_UNVERIFIED
        assert "0x800705B4" in r.reason

    def test_both_sources_failing_is_unverified_never_ok(self, tmp_path):
        r, _ = _check(tmp_path, _google(exc=OSError("getaddrinfo failed")), W32_BAD_HOST,
                      w32_exit=2147953401)
        assert r.state == health.CLOCK_UNVERIFIED and r.skew_s is None
        assert "getaddrinfo failed" in r.reason and "No such host" in r.reason

    def test_w32tm_timeout_is_unverified(self, tmp_path):
        r, _ = _check(tmp_path, _google(exc=OSError("x")), w32_timed_out=True)
        assert r.state == health.CLOCK_UNVERIFIED

    def test_metrics_are_json_ready(self, tmp_path):
        r, _ = _check(tmp_path, _google(10))
        m = json.loads(json.dumps(r.to_metrics()))
        assert m["state"] == "OK" and m["source"] == "google"


def _health(tmp_path, clock_state, policy, mode="cloud"):
    result = health.ClockResult(clock_state, None, None, "both sources unreachable")
    with patch.object(health, "source_drive_fault", return_value=(None, "")), \
            patch.object(health, "check_binary_exists", return_value=True), \
            patch.object(health, "check_gcs_key", return_value=(True, "")), \
            patch.object(health, "check_clock", return_value=result) as chk:
        out = health.pre_backup_health(
            str(tmp_path), mode, gcs_key_path="k.json", clock_unverified_policy=policy,
            ntp_server="time.windows.com", w32tm_timeout_seconds=30,
            artefacts=RunArtefacts(tmp_path, "h", day="d"))
    return out, chk


class TestPreBackupHealthPolicy:
    def test_unverified_with_warn_proceeds_and_reports(self, tmp_path):
        out, _ = _health(tmp_path, health.CLOCK_UNVERIFIED, "warn")
        assert out.state == health.CLOCK_UNVERIFIED

    def test_unverified_with_block_fails(self, tmp_path):
        with pytest.raises(health.HealthError, match="could not be verified"):
            _health(tmp_path, health.CLOCK_UNVERIFIED, "block")

    @pytest.mark.parametrize("policy", ["warn", "block"])
    def test_skewed_always_fails(self, tmp_path, policy):
        with pytest.raises(health.HealthError, match="[Cc]lock"):
            _health(tmp_path, health.CLOCK_SKEWED, policy)

    def test_lan_does_not_check_the_clock(self, tmp_path):
        out, chk = _health(tmp_path, health.CLOCK_OK, "warn", mode="lan")
        assert out is None
        chk.assert_not_called()


class TestClockConfig:
    def test_defaults(self):
        h = HealthConfig()
        assert h.clock_unverified_policy == "warn"   # DEC-6
        assert h.ntp_server == "time.windows.com"
        assert h.w32tm_timeout_seconds == 30

    def test_unknown_policy_is_rejected(self):
        with pytest.raises(ValidationError):
            HealthConfig(clock_unverified_policy="ignore")


def test_health_task_passes_the_config_and_returns_the_clock(tmp_path):
    cfg = MagicMock()
    cfg.health.clock_unverified_policy = "warn"
    cfg.health.ntp_server = "ntp.example"
    cfg.health.w32tm_timeout_seconds = 12
    result = health.ClockResult(health.CLOCK_UNVERIFIED, None, None, "r")
    with patch("flow.pre_backup_health", return_value=result) as pbh:
        out = flow.health_check_task.fn(cfg, "cloud", str(tmp_path))
    kw = pbh.call_args.kwargs
    assert kw["clock_unverified_policy"] == "warn" and kw["ntp_server"] == "ntp.example"
    assert kw["w32tm_timeout_seconds"] == 12
    assert out == result.to_metrics()


_CLOUD = ["flow._record_running", "flow._open_run_artefacts", "flow.cloud_publish_artifact_task",
          "flow.cloud_record_task", "flow.cloud_verify_and_report_task", "flow.cloud_sync_task",
          "flow.cloud_access_task", "flow.cloud_guards_task", "flow.cloud_preflight_task",
          "flow.health_check_task", "flow.ManifestDB", "flow._record_run",
          "flow._send_recovery_if_needed", "flow.notify"]


def test_cloud_run_row_records_an_unverified_clock():
    ps = [patch(n) for n in _CLOUD]
    ms = {n.split(".", 1)[1]: p.start() for n, p in zip(_CLOUD, ps)}
    try:
        ms["_open_run_artefacts"].return_value = None
        ms["health_check_task"].return_value = {"state": "UNVERIFIED", "skew_s": None,
                                                "source": None, "reason": "r"}
        ms["cloud_sync_task"].with_options.return_value.side_effect = RuntimeError("stop")
        cfg = MagicMock()
        cfg.paths.database_path = ":memory:"
        cfg.paths.source_drive = "E:\\DATA\\FY26-27"
        cfg.cloud.max_attempts = 1
        with pytest.raises(RuntimeError):
            flow._run_cloud_pipeline(cfg, "run-clk", now_iso())
        em = json.loads(ms["_record_run"].call_args.kwargs["extended_metrics"])
        assert em["clock"]["state"] == "UNVERIFIED"
    finally:
        for p in ps:
            p.stop()


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _att_cfg():
    return SimpleNamespace(
        cloud=SimpleNamespace(enabled=True), lan=SimpleNamespace(enabled=True),
        dashboard=SimpleNamespace(stale_grace_hours=attention_cfg.GRACE_HOURS, alert_failure_window_days=7,
                                  public_url="http://backup-server:8080"),
        notifications=SimpleNamespace(send_on_failure=True),
        schedule=attention_cfg.SCHEDULE, budget=attention_cfg.BUDGET)


def _run(db, run_id, mode, status, em=None):
    ts = pendulum.now(IST).subtract(hours=1).isoformat()
    db.insert_run({"run_id": run_id, "mode": mode, "started_at": ts, "ended_at": ts,
                   "status": status, "extended_metrics": em})


def test_dashboard_flags_an_unverified_clock(db):
    _run(db, "c", "cloud", "CLOUD_COMPLETE",
         json.dumps({"clock": {"state": "UNVERIFIED", "reason": "google: x; w32tm: y"}}))
    _run(db, "l", "lan", "LAN_COMPLETE")
    items = compute_attention(db, _att_cfg())
    assert [(i["level"], i["code"]) for i in items] == [(AMBER, "clock")]
    assert "google: x; w32tm: y" in items[0]["message"]


def test_dashboard_is_quiet_when_the_clock_was_verified(db):
    _run(db, "c", "cloud", "CLOUD_COMPLETE", json.dumps({"clock": {"state": "OK"}}))
    _run(db, "l", "lan", "LAN_COMPLETE")
    assert compute_attention(db, _att_cfg()) == []
