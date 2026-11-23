"""Weekly integrity audit on run_tool (plan 5.1, M3 deferral).

rclone check runs through core.tool_runner: its output is kept in the
audit's run folder, a timeout / spawn failure is AUDIT_INCOMPLETE (never a
verdict), --modify-window is config (audit.modify_window_seconds), and the
fallback defaults for direct callers are read from the config model - there
is no second copy of any default in core/integrity.py.
"""

import inspect
from pathlib import Path

import pytest

from core import integrity
from core.run_artefacts import RunArtefacts
from models.config import AuditConfig, CloudConfig, LanConfig
from test_support.fake_tools import fake_run_tool

CLEAN = "2026/10/04 03:05:12 NOTICE: Local file system at //?/D:/dst: 0 differences found\n"


@pytest.fixture(autouse=True)
def _rclone_name(monkeypatch):
    monkeypatch.setattr(integrity, "resolve_binary", lambda _n: "rclone")


def test_modify_window_comes_from_the_parameter():
    with fake_run_tool("core.integrity", exit_code=0, stderr=CLEAN) as calls:
        integrity._run_rclone_check("S", "D", "lan/full", 60, modify_window_seconds=7)
    cmd = calls[0]["argv"]
    assert cmd[cmd.index("--modify-window") + 1] == "7s"


def test_audit_output_is_kept_in_the_run_folder(tmp_path):
    art = RunArtefacts(str(tmp_path / "run_logs"), "audit-1")
    with fake_run_tool("core.integrity", exit_code=0, stderr=CLEAN):
        res = integrity.audit_lan("S", "D", timeout=60, artefacts=art)
    assert res["status"] == "VERIFIED"
    kept = list(Path(art.dir).glob("*rclone-audit-lan.stderr.txt"))
    assert kept and "0 differences found" in kept[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("outcome", [dict(timed_out=True),
                                     dict(spawn_error="FileNotFoundError: rclone")])
def test_process_not_completed_is_incomplete_never_a_verdict(outcome):
    with fake_run_tool("core.integrity", **outcome):
        res = integrity._run_rclone_check("S", "D", "lan/full", 60)
    assert res["status"] == "AUDIT_INCOMPLETE" and res["mismatches"] == -1
    assert "could not run" in res["detail"]


def test_fallback_defaults_are_the_config_model_defaults():
    params = inspect.signature(integrity._run_rclone_check).parameters
    for key in ("checkers", "retries", "retries_sleep_seconds", "detail_max_paths",
                "modify_window_seconds"):
        assert params[key].default == AuditConfig.model_fields[key].default, key
    assert (inspect.signature(integrity.audit_lan).parameters["timeout"].default
            == LanConfig.model_fields["subprocess_timeout_seconds"].default)
    assert (inspect.signature(integrity.audit_cloud).parameters["timeout"].default
            == CloudConfig.model_fields["verify_timeout_seconds"].default)


def test_no_literal_defaults_in_the_module_source():
    src = inspect.getsource(integrity._run_rclone_check).split('"""')[0]
    assert "= 1," not in src and "= 3," not in src and "= 10," not in src
    assert '"2s"' not in inspect.getsource(integrity._run_rclone_check)
