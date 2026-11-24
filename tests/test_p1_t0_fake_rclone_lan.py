"""Plan P1 task T0: the `fake_rclone_lan` test double works before any C6/C7 test relies on it.

Self-test only: it calls core.lan_sync.run_tool directly, which the fake patches. No LAN
product function is used here, so this file must pass on the current code.
"""

import pendulum
import pytest

import core.lan_sync as lan_sync
from core.run_artefacts import RunArtefacts
from core.tool_runner import ToolResult
from test_support.fake_tools import LAN_RCLONE_STEPS, fake_rclone_lan

RUN_ID = "3f2a9c1e-77aa-4c6b-9d11-0a1b2c3d4e5f-lan"
STARTED = pendulum.datetime(2026, 11, 25, 9, 0, tz="Asia/Kolkata")


@pytest.fixture
def artefacts(tmp_path):
    return RunArtefacts(tmp_path, RUN_ID, started=STARTED)


def _sync_argv(log, combined=None):
    argv = ["rclone", "sync", "SRC", "DST", "--log-file", str(log)]
    if combined is not None:
        argv += ["--combined", str(combined)]
    return argv


def _reserve(artefacts, step, ext="log"):
    return artefacts.new_path(step, ext)


def test_steps_are_the_three_lan_step_names():
    assert LAN_RCLONE_STEPS == ("rclone-lan-plan", "rclone-lan-sync", "rclone-lan-check")


def test_dispatch_returns_a_real_toolresult_and_records_the_call(artefacts):
    log = _reserve(artefacts, "rclone-lan-sync")
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0, "log": "LOGTEXT",
                                              "stderr": "ERRTEXT"}}) as fake:
        result = lan_sync.run_tool("rclone-lan-sync", _sync_argv(log), timeout_s=60,
                                   artefacts=artefacts, log_path=log)
    assert isinstance(result, ToolResult)
    assert result.exit_code == 0 and result.completed
    assert result.timed_out is False and result.spawn_error is None
    assert result.log_text() == "LOGTEXT"
    assert result.stderr_text() == "ERRTEXT"
    assert result.timeout_s == 60
    assert fake.steps() == ["rclone-lan-sync"]
    assert fake[0]["argv"] == _sync_argv(log)
    assert fake.calls[0]["name"] == "rclone-lan-sync"


def test_log_goes_to_the_log_file_argv_path_and_combined_to_the_combined_path(artefacts):
    log = _reserve(artefacts, "rclone-lan-plan")
    combined = _reserve(artefacts, "rclone-lan-plan-combined", "txt")
    with fake_rclone_lan({"rclone-lan-plan": {"log": "{}\n", "combined": "+ a.txt\n"}}):
        lan_sync.run_tool("rclone-lan-plan", _sync_argv(log, combined), timeout_s=5,
                          artefacts=artefacts, log_path=log)
    assert log.read_text(encoding="utf-8") == "{}\n"
    assert combined.read_text(encoding="utf-8") == "+ a.txt\n"


def test_combined_none_writes_no_file(artefacts):
    log = _reserve(artefacts, "rclone-lan-plan")
    combined = _reserve(artefacts, "rclone-lan-plan-combined", "txt")
    with fake_rclone_lan({"rclone-lan-plan": {"log": "x"}}):
        lan_sync.run_tool("rclone-lan-plan", _sync_argv(log, combined), timeout_s=5,
                          artefacts=artefacts, log_path=log)
    assert not combined.exists()


def test_check_style_call_without_log_file_writes_no_log(artefacts):
    argv = ["rclone", "check", "SRC", "DST", "--combined", str(_reserve(artefacts, "c", "txt"))]
    with fake_rclone_lan({"rclone-lan-check": {"exit_code": 1, "stderr": "NOTICE: 0 matching files"}}):
        result = lan_sync.run_tool("rclone-lan-check", argv, timeout_s=5, artefacts=artefacts)
    assert result.exit_code == 1
    assert result.log_path is None
    assert result.log_text() == ""
    assert "matching files" in result.stderr_text()


def test_a_list_of_specs_is_consumed_one_per_call_for_retries(artefacts):
    log = _reserve(artefacts, "rclone-lan-sync")
    script = {"rclone-lan-sync": [{"exit_code": 5, "log": "first"},
                                  {"exit_code": 0, "log": "second"}]}
    with fake_rclone_lan(script) as fake:
        first = lan_sync.run_tool("rclone-lan-sync", _sync_argv(log), timeout_s=5,
                                  artefacts=artefacts, log_path=log)
        second = lan_sync.run_tool("rclone-lan-sync", _sync_argv(log), timeout_s=5,
                                   artefacts=artefacts, log_path=log)
    assert (first.exit_code, second.exit_code) == (5, 0)
    assert second.log_text() == "second"
    assert fake.steps() == ["rclone-lan-sync", "rclone-lan-sync"]


def test_a_list_called_beyond_its_length_fails_loudly(artefacts):
    log = _reserve(artefacts, "rclone-lan-sync")
    with fake_rclone_lan({"rclone-lan-sync": [{"exit_code": 0}]}):
        lan_sync.run_tool("rclone-lan-sync", _sync_argv(log), timeout_s=5,
                          artefacts=artefacts, log_path=log)
        with pytest.raises(AssertionError, match="more often"):
            lan_sync.run_tool("rclone-lan-sync", _sync_argv(log), timeout_s=5,
                              artefacts=artefacts, log_path=log)


def test_a_single_spec_repeats_for_every_call(artefacts):
    log = _reserve(artefacts, "rclone-lan-sync")
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0}}) as fake:
        for _ in range(3):
            lan_sync.run_tool("rclone-lan-sync", _sync_argv(log), timeout_s=5,
                              artefacts=artefacts, log_path=log)
    assert len(fake) == 3


def test_timed_out_spec_has_no_exit_code(artefacts):
    log = _reserve(artefacts, "rclone-lan-sync")
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0, "timed_out": True, "log": "partial"}}):
        result = lan_sync.run_tool("rclone-lan-sync", _sync_argv(log), timeout_s=5,
                                   artefacts=artefacts, log_path=log)
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.completed is False
    assert result.log_text() == "partial"


def test_spawn_error_spec_has_no_exit_code_and_writes_no_log(artefacts):
    log = _reserve(artefacts, "rclone-lan-sync")
    with fake_rclone_lan({"rclone-lan-sync": {"spawn_error":
                                              "FileNotFoundError: [WinError 2] missing"}}):
        result = lan_sync.run_tool("rclone-lan-sync", _sync_argv(log), timeout_s=5,
                                   artefacts=artefacts, log_path=log)
    assert result.spawn_error == "FileNotFoundError: [WinError 2] missing"
    assert result.exit_code is None
    assert not log.exists()


def test_raise_spec_is_raised_from_run_tool(artefacts):
    log = _reserve(artefacts, "rclone-lan-sync")
    with fake_rclone_lan({"rclone-lan-sync": {"raise": KeyboardInterrupt()}}) as fake, \
            pytest.raises(KeyboardInterrupt):
        lan_sync.run_tool("rclone-lan-sync", _sync_argv(log), timeout_s=5,
                          artefacts=artefacts, log_path=log)
    assert fake.steps() == ["rclone-lan-sync"]


def test_a_step_without_a_spec_fails_loudly(artefacts):
    log = _reserve(artefacts, "rclone-lan-plan")
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0}}), \
            pytest.raises(AssertionError, match="no spec for step 'rclone-lan-plan'"):
        lan_sync.run_tool("rclone-lan-plan", _sync_argv(log), timeout_s=5,
                          artefacts=artefacts, log_path=log)


def test_a_misspelt_step_name_is_rejected_at_setup():
    with pytest.raises(ValueError, match="unknown step"), \
            fake_rclone_lan({"rclone-lan-sycn": {"exit_code": 0}}):
        pass


def test_an_unknown_spec_key_is_rejected_at_setup():
    with pytest.raises(ValueError, match="unknown spec key"), \
            fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0, "logtext": "typo"}}):
        pass


def test_patch_is_removed_after_the_context(artefacts):
    log = _reserve(artefacts, "rclone-lan-sync")
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0}}):
        pass
    # Outside the context the real run_tool is back: a missing binary is a spawn error, not a fake.
    result = lan_sync.run_tool("rclone-lan-sync", ["no-such-binary-aam-t0"], timeout_s=5,
                               artefacts=artefacts, log_path=log)
    assert result.spawn_error is not None
