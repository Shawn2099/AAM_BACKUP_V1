"""M4 4.1 - NAS shutdown result verification (plan WP10).

Findings: the shutdown result was never checked (the caller only caught
exceptions; shutdown_server returned a dict nobody read), `1190 already
scheduled` was logged as a failure, and a shutdown cancelled on the NAS
(`shutdown /a`) or refused looked identical to a successful one.

Real shutdown.exe facts (Windows Server 2016, 2026-10-15, plan §4 of the
handoff): exit code = Win32 error; text on stderr as `<host>: <text>(<code>)`;
0 = accepted, 1190 = already scheduled, 5 = access denied, 1707 = bad address.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pendulum
import pytest
from pydantic import ValidationError

import flow
from core import shutdown as sd
from core.attention import AMBER, compute_attention
from core.manifest import ManifestDB
from core.run_artefacts import RunArtefacts
from core.time_utils import IST, now_iso
from models.config import WolConfig
from test_support.fake_tools import fake_run_tool


def _att_cfg():
    return SimpleNamespace(
        cloud=SimpleNamespace(enabled=True), lan=SimpleNamespace(enabled=True),
        dashboard=SimpleNamespace(stale_after_hours=26, alert_failure_window_days=7),
        notifications=SimpleNamespace(send_on_failure=True),
    )


def _run(db, run_id, mode, status, hours_ago, em=None):
    ts = pendulum.now(IST).subtract(hours=hours_ago).isoformat()
    db.insert_run({"run_id": run_id, "mode": mode, "started_at": ts, "ended_at": ts,
                   "status": status, "extended_metrics": em})


def _codes(items, level=None):
    return {i["code"] for i in items if level is None or i["level"] == level}


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _shutdown(tmp_path, port_states, *, exit_code=0, stderr="", timed_out=False,
              spawn_error=None, **kw):
    """Run shutdown_server with a fake shutdown.exe and a scripted port 445."""
    clock = FakeClock()
    states = iter(port_states)
    probes = []

    def tcp(host, port, timeout_s):
        clock.t += 1
        probes.append(clock.t)
        is_open = next(states, False)
        return is_open, "tcp open" if is_open else "tcp 445: ConnectionRefusedError"

    params = dict(delay_s=300, command_timeout_s=120, verify_timeout_s=600,
                  poll_interval_s=15, connect_timeout_s=15, closed_probes=2)
    params.update(kw)
    artefacts = RunArtefacts(tmp_path, "run-sd", day="d")
    with fake_run_tool("core.shutdown", exit_code=exit_code, stderr=stderr,
                       timed_out=timed_out, spawn_error=spawn_error) as calls:
        result = sd.shutdown_server("10.0.0.5", artefacts=artefacts, clock=clock.now,
                                    sleep=clock.sleep, tcp_probe=tcp, **params)
    return result, calls, probes


class TestShutdownServer:
    def test_accepted_and_port_closed_is_verified(self, tmp_path):
        result, calls, _ = _shutdown(tmp_path, [True] * 5 + [False, False])
        assert result.outcome == sd.VERIFIED
        assert result.exit_code == 0
        assert calls[0]["argv"] == ["shutdown", "/s", "/m", "\\\\10.0.0.5", "/t", "300", "/f"]
        assert calls[0]["timeout_s"] == 120

    def test_delay_comes_from_config(self, tmp_path):
        _, calls, _ = _shutdown(tmp_path, [False, False], delay_s=45, verify_timeout_s=300)
        assert calls[0]["argv"][calls[0]["argv"].index("/t") + 1] == "45"

    def test_1190_already_scheduled_is_not_a_failure_and_is_still_verified(self, tmp_path):
        result, _, probes = _shutdown(
            tmp_path, [True, False, False], exit_code=1190,
            stderr="10.0.0.5: A system shutdown has already been scheduled.(1190)\n")
        assert result.outcome == sd.VERIFIED
        assert result.exit_code == 1190
        assert "already scheduled" in result.detail
        assert probes  # verification still ran

    def test_access_denied_is_failed_with_the_tool_text_and_no_polling(self, tmp_path):
        result, _, probes = _shutdown(
            tmp_path, [], exit_code=5, stderr="10.0.0.5: Access is denied.(5)\n")
        assert result.outcome == sd.FAILED
        assert result.exit_code == 5
        assert "Access is denied.(5)" in result.detail
        assert probes == []

    @pytest.mark.parametrize("kw", [{"timed_out": True}, {"spawn_error": "FileNotFoundError: x"}])
    def test_command_that_did_not_complete_is_failed(self, tmp_path, kw):
        result, _, probes = _shutdown(tmp_path, [], **kw)
        assert result.outcome == sd.FAILED
        assert result.exit_code is None
        assert probes == []

    def test_port_still_open_after_the_window_is_not_verified(self, tmp_path):
        # e.g. staff ran `shutdown /a` on the NAS, or the NAS ignored it
        result, _, _ = _shutdown(tmp_path, [True] * 1000)
        assert result.outcome == sd.NOT_VERIFIED
        assert "still reachable" in result.detail and "600" in result.detail

    def test_a_single_closed_probe_is_not_enough(self, tmp_path):
        # one dropped connect (busy NAS) between open probes is not "off"
        result, _, _ = _shutdown(tmp_path, [True, False, True] + [True] * 1000)
        assert result.outcome == sd.NOT_VERIFIED

    def test_metrics_are_json_ready(self, tmp_path):
        result, _, _ = _shutdown(tmp_path, [False, False])
        m = json.loads(json.dumps(result.to_metrics()))
        assert m["outcome"] == sd.VERIFIED and m["server_ip"] == "10.0.0.5"
        assert m["exit_code"] == 0 and "elapsed_s" in m


class TestShutdownConfig:
    def _wol(self, **kw):
        return WolConfig(mac_address="AA:BB:CC:DD:EE:FF", **kw)

    def test_defaults(self):
        w = self._wol()
        assert w.shutdown_delay_seconds == 300
        assert w.shutdown_verify_timeout_seconds == 600
        assert w.shutdown_command_timeout_seconds == 120
        assert w.shutdown_verify_closed_probes == 2

    def test_verify_window_must_outlast_the_delay(self):
        with pytest.raises(ValidationError, match="shutdown_verify_timeout_seconds"):
            self._wol(shutdown_delay_seconds=600, shutdown_verify_timeout_seconds=600)


def _task_cfg(enabled=True):
    cfg = MagicMock()
    cfg.lan.shutdown_after_backup = enabled
    cfg.wol.enabled = True
    cfg.wol.server_ip = "10.0.0.5"
    return cfg


class TestLanShutdownTask:
    def _result(self, outcome, exit_code=0, detail="d"):
        return sd.ShutdownResult(outcome=outcome, server_ip="10.0.0.5", exit_code=exit_code,
                                 detail=detail, elapsed_s=1.0)

    def test_disabled_returns_none_and_sends_nothing(self):
        with patch("flow.shutdown_from_config") as run, patch("flow.notify") as notify:
            assert flow.lan_shutdown_task.fn(_task_cfg(enabled=False)) is None
        run.assert_not_called()
        notify.assert_not_called()

    def test_verified_returns_metrics_without_an_email(self, tmp_path):
        with patch("flow.shutdown_from_config", return_value=self._result(sd.VERIFIED)), \
                patch("flow.notify") as notify:
            out = flow.lan_shutdown_task.fn(_task_cfg(), str(tmp_path))
        assert out["outcome"] == sd.VERIFIED
        notify.assert_not_called()

    @pytest.mark.parametrize("outcome", [sd.FAILED, sd.NOT_VERIFIED])
    def test_unverified_shutdown_sends_one_warning(self, tmp_path, outcome):
        with patch("flow.shutdown_from_config",
                   return_value=self._result(outcome, 5, "Access is denied.(5)")), \
                patch("flow.notify") as notify:
            out = flow.lan_shutdown_task.fn(_task_cfg(), str(tmp_path))
        assert out["outcome"] == outcome
        [(args, _)] = notify.call_args_list
        event = args[1]
        assert event.kind == "WARNING" and event.key == "nas_shutdown:10.0.0.5"
        assert "Access is denied.(5)" in event.message

    def test_unexpected_error_is_recorded_as_failed_never_raised(self, tmp_path):
        with patch("flow.shutdown_from_config", side_effect=RuntimeError("boom")), \
                patch("flow.notify") as notify:
            out = flow.lan_shutdown_task.fn(_task_cfg(), str(tmp_path))
        assert out["outcome"] == sd.FAILED and "boom" in out["detail"]
        notify.assert_called_once()


@patch("flow._send_recovery_if_needed")
@patch("flow.notify")
@patch("flow._record_running")
@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.lan_publish_artifact_task")
@patch("flow.lan_record_task")
@patch("flow.lan_snapshot_after_task", return_value={"a": (1, 1.0)})
@patch("flow.lan_shutdown_task")
@patch("flow.lan_sync_task")
@patch("flow.lan_access_task")
@patch("flow.lan_guards_task")
@patch("flow.lan_snapshot_before_task", return_value={})
@patch("flow.lan_preflight_task")
@patch("flow.wol_check_task")
@patch("flow.health_check_task")
@patch("flow._record_run")
def test_lan_run_row_records_the_shutdown_result_and_status_stays_complete(
    record, _h, _w, _pre, _snap, _g, _acc, sync, shutdown, _after, _rt, _pub, _art, _running,
    _notify, _recov,
):
    cfg = MagicMock()
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.lan.shutdown_on_all_retries_exhausted = False
    sync.with_options.return_value.return_value = {
        "status": "LAN_COMPLETE", "exit_code": 1, "counts": {"copied": 1}, "bytes": {"copied": 1}}
    shutdown.return_value = {"outcome": sd.FAILED, "server_ip": "10.0.0.5", "exit_code": 5,
                             "detail": "Access is denied.(5)", "elapsed_s": 0.1}
    flow._run_lan_pipeline(cfg, "run-sd", now_iso())
    args, kwargs = record.call_args
    assert args[4] == "LAN_COMPLETE"
    em = json.loads(kwargs["extended_metrics"])
    assert em["nas_shutdown"]["outcome"] == sd.FAILED


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


@pytest.mark.parametrize("outcome", [sd.FAILED, sd.NOT_VERIFIED])
def test_dashboard_flags_an_unverified_nas_shutdown(db, outcome):
    _run(db, "c", "cloud", "CLOUD_COMPLETE", 1)
    _run(db, "l", "lan", "LAN_COMPLETE", 1,
         em=json.dumps({"nas_shutdown": {"outcome": outcome, "detail": "Access is denied.(5)"}}))
    items = compute_attention(db, _att_cfg())
    assert _codes(items, AMBER) == {"nas_shutdown"}
    assert "Access is denied.(5)" in " ".join(i["message"] for i in items)


def test_dashboard_is_quiet_after_a_verified_shutdown(db):
    _run(db, "c", "cloud", "CLOUD_COMPLETE", 1)
    _run(db, "l", "lan", "LAN_COMPLETE", 1,
         em=json.dumps({"nas_shutdown": {"outcome": sd.VERIFIED}}))
    assert compute_attention(db, _att_cfg()) == []
