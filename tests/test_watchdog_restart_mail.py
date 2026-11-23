"""A service the watchdog had to restart is no longer invisible (Wave 2 follow-up, Q2 gap, owner approved).

When the watchdog actually starts / restarts AamPrefectServer or AamBackupAgent it sends ONE WARNING mail
('the backup <name> was down and was restarted automatically, about N minutes, no action needed unless it
repeats') and ONE RECOVERED mail once the service answers again. A flapping service is bounded by
`alerts.repeat_cooldown_hours` (one WARNING/RECOVERED pair per service and window); a mail failure never
breaks the loop; the START_SUPPRESSED mail is unchanged. Only fakes: no real service is touched.
"""
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import watchdog as wd
from core import alert_class, faults
from core import notify as nt


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    for name in ("_down_since", "_restart_mailed", "_pending_recovery"):
        if hasattr(wd, name):
            getattr(wd, name).clear()
    monkeypatch.setattr(wd, "_attention_raised", False)
    monkeypatch.setattr(wd, "_mark_healthy", lambda: None)
    wd.service_start_log.clear()
    yield
    wd.service_start_log.clear()


@pytest.fixture
def sent(monkeypatch):
    """Record every event the watchdog hands to core.notify.notify."""
    events = []

    def fake_notify(config, event, db=None):
        events.append(event)
        return SimpleNamespace(status="SENT")
    monkeypatch.setattr(nt, "notify", fake_notify)
    return events


def _kinds(events):
    return [(e.kind, e.key) for e in events]


def _run_loop(iterations):
    with patch("watchdog.time.sleep") as sleeper, patch("watchdog._resolve_paths"), \
         patch("watchdog._check_stale_legs"), patch("watchdog._send_undelivered_digest"), \
         patch("watchdog._alert_invalid_config"), patch("watchdog._reload_tunables", return_value=None):
        count = 0

        def side(*_a):
            nonlocal count
            count += 1
            if count >= iterations:
                raise KeyboardInterrupt("end")
        sleeper.side_effect = side
        try:
            wd.main()
        except KeyboardInterrupt:
            pass


def _api(down_checks):
    """httpx2.get: `down_checks` failures, then healthy."""
    calls = {"n": 0}

    def get(*_a, **_k):
        calls["n"] += 1
        if calls["n"] <= down_checks:
            raise OSError("dead")
        return MagicMock(status_code=200)
    return get


def test_server_start_sends_one_warning_then_one_recovered(sent):
    def state(service):
        return "STOPPED" if service == wd.WATCHED_SERVICE else "RUNNING"
    with patch("httpx2.get", side_effect=_api(wd.FAILURE_THRESHOLD)), \
         patch("watchdog._is_backup_running", return_value=False), \
         patch("watchdog._transfer_process_running", return_value=False), \
         patch("watchdog._service_state", side_effect=state), \
         patch("watchdog._start_service", return_value=True):
        _run_loop(wd.FAILURE_THRESHOLD + 2)
    assert [e.kind for e in sent] == ["WARNING", "RECOVERED"]
    warning, recovered = sent
    assert warning.mode == "watchdog" and warning.fault_code == "AAM-SVC-05"
    assert warning.status == "SERVICE_RESTARTED"
    assert "backup scheduler" in warning.message and "restarted automatically" in warning.message
    assert "No action is needed unless it repeats" in warning.message
    assert "Prefect" not in warning.message and "API" not in warning.message
    assert recovered.key.startswith("recovered:watchdog:") and recovered.cooldown_hours == 0


def test_agent_start_sends_one_warning_then_one_recovered(sent):
    states = iter(["STOPPED", "RUNNING", "RUNNING"])
    with patch("httpx2.get", return_value=MagicMock(status_code=200)), \
         patch("watchdog._service_state", side_effect=lambda s: next(states)), \
         patch("watchdog._start_service", return_value=True):
        _run_loop(3)
    assert [e.kind for e in sent] == ["WARNING", "RECOVERED"]
    assert "backup program" in sent[0].message


def test_a_start_the_service_control_refused_sends_no_restart_mail(sent):
    states = iter(["STOPPED", "STOPPED"])
    with patch("httpx2.get", return_value=MagicMock(status_code=200)), \
         patch("watchdog._service_state", side_effect=lambda s: next(states)), \
         patch("watchdog._start_service", return_value=False):
        _run_loop(2)
    assert sent == []


def test_stack_restart_mails_once_and_recovers_once(sent):
    with patch("watchdog._stop_and_wait", return_value=True), \
         patch("watchdog._wait_api_healthy", return_value=True), \
         patch.object(wd.services, "start", return_value=SimpleNamespace(ok=True)), \
         patch.object(wd.services, "wait_for", return_value=SimpleNamespace(state="RUNNING")):
        assert wd._restart_stack() is True
    assert [e.kind for e in sent] == ["WARNING"]
    assert "restarted automatically" in sent[0].message
    wd._send_recovered_if_needed()
    wd._send_recovered_if_needed()          # a second healthy check sends nothing more
    assert [e.kind for e in sent] == ["WARNING", "RECOVERED"]


def test_a_failed_stack_restart_sends_only_the_critical_not_the_restart_mail(sent):
    with patch("watchdog._stop_and_wait", return_value=False):
        assert wd._restart_stack() is False
    assert sent == []


def test_flapping_service_is_bounded_to_one_pair_per_window(sent):
    for _ in range(6):
        wd._note_down("AamPrefectServer")
        wd._alert_restarted("AamPrefectServer", "backup scheduler")
        wd._send_recovered_if_needed()
    assert [e.kind for e in sent] == ["WARNING", "RECOVERED"]


def test_each_service_has_its_own_bound(sent):
    wd._alert_restarted("AamPrefectServer", "backup scheduler")
    wd._alert_restarted("AamBackupAgent", "backup program")
    assert [e.kind for e in sent] == ["WARNING", "WARNING"]
    assert sent[0].key != sent[1].key


def test_cooldown_zero_never_suppresses(sent, monkeypatch):
    monkeypatch.setattr(wd, "_restart_cooldown_seconds", lambda: 0.0)
    for _ in range(3):
        wd._alert_restarted("AamPrefectServer", "backup scheduler")
        wd._send_recovered_if_needed()
    assert [e.kind for e in sent] == ["WARNING", "RECOVERED"] * 3


def test_a_mail_failure_never_breaks_the_watchdog(monkeypatch):
    def boom(config, event, db=None):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(nt, "notify", boom)
    wd._alert_restarted("AamPrefectServer", "backup scheduler")      # no exception
    wd._send_recovered_if_needed()
    # and the loop goes on: the server is restarted, the loop reaches the next iteration
    with patch("httpx2.get", side_effect=_api(wd.FAILURE_THRESHOLD)), \
         patch("watchdog._is_backup_running", return_value=False), \
         patch("watchdog._transfer_process_running", return_value=False), \
         patch("watchdog._service_state",
               side_effect=lambda s: "STOPPED" if s == wd.WATCHED_SERVICE else "RUNNING"), \
         patch("watchdog._start_service", return_value=True):
        _run_loop(wd.FAILURE_THRESHOLD + 2)


def test_a_switched_off_class_sends_no_recovered_for_a_mail_that_was_not_sent(monkeypatch):
    events = []

    def off(config, event, db=None):
        events.append(event)
        return SimpleNamespace(status="DISABLED")
    monkeypatch.setattr(nt, "notify", off)
    wd._alert_restarted("AamPrefectServer", "backup scheduler")
    wd._send_recovered_if_needed()
    assert [e.kind for e in events] == ["WARNING"]


def test_start_suppressed_mail_is_unchanged(sent):
    wd.service_start_log[wd.AGENT_SERVICE] = [__import__("time").time()] * wd.MAX_STARTS_PER_WINDOW
    with patch("httpx2.get", return_value=MagicMock(status_code=200)), \
         patch("watchdog._service_state", return_value="STOPPED"), \
         patch("watchdog._start_service") as start:
        _run_loop(1)
    start.assert_not_called()
    assert [(e.kind, e.key) for e in sent] == [
        ("ACTION_REQUIRED", f"watchdog:start_suppressed:{wd.AGENT_SERVICE}")]


def test_the_mail_has_a_fault_code_and_renders_without_the_unexpected_error_fallback(tmp_path):
    fault = faults.get("AAM-SVC-05")
    assert fault.code == "AAM-SVC-05" and fault.severity == faults.WARNING
    from core import email_compose as ec
    from models.config import AppConfig
    cfg = AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": r"\\nas\share\FY26-27",
               "runtime_dir": str(tmp_path / "rt"), "gcs_key_path": str(tmp_path / "k.json")},
        wol={"enabled": False}, cloud={"enabled": False}, dashboard={"public_url": "http://backup-server:8080"})
    message = ("The backup scheduler was down about 7 minutes and was restarted automatically. "
               "No action is needed unless it repeats.")
    event = nt.AlertEvent(key="watchdog:restarted:AamPrefectServer", kind="WARNING", mode="watchdog",
                          title="restarted", message=message, status="SERVICE_RESTARTED",
                          fault_code="AAM-SVC-05")
    subject, html, text = ec.compose(cfg, event)
    assert "Unexpected program error" not in subject + html + text
    assert "AAM-SVC-05" in subject and "WARNING" in subject
    assert "about 7 minutes" in text and "http://backup-server:8080" in text
    assert not re.search(r"prefect|API", text.split("For support", 1)[0], re.IGNORECASE)


def test_classes_reuse_the_existing_switches():
    assert alert_class.classify("WARNING", "watchdog:restarted:AamPrefectServer") == alert_class.FAILURE
    assert alert_class.classify("RECOVERED", "recovered:watchdog:restarted:AamPrefectServer") \
        == alert_class.RESOLVED
