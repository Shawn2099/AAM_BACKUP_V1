"""cloud_reporter on run_tool (plan 5.1, M3 deferral).

size / manifest / diff run through core.tool_runner: output is kept in the
run's artefact folder, a timeout is reported as a timeout (never parsed as
data), and every rclone flag comes from config - no literal defaults.
"""

import json
from pathlib import Path

import pytest

from core import cloud_reporter
from core.run_artefacts import RunArtefacts
from test_support.fake_tools import fake_run_tool

MOD = "core.cloud_reporter"
DIFF_TUNING = dict(timeout=18000, checkers=4, modify_window_seconds=2,
                   retries=3, retries_sleep_seconds=10)


@pytest.fixture
def art(tmp_path):
    return RunArtefacts(str(tmp_path / "run_logs"), "run-rep")


def test_diff_flags_come_from_the_parameters(art):
    with fake_run_tool(MOD, exit_code=0) as calls:
        cloud_reporter.get_cloud_diff("E:/src", "bkt", "FY26-27", "rc.conf", timeout=77,
                                      checkers=9, modify_window_seconds=3, retries=5,
                                      retries_sleep_seconds=20, artefacts=art)
    cmd = calls[0]["argv"]
    assert cmd[cmd.index("--checkers") + 1] == "9"
    assert cmd[cmd.index("--modify-window") + 1] == "3s"
    assert cmd[cmd.index("--retries") + 1] == "5"
    assert cmd[cmd.index("--retries-sleep") + 1] == "20s"
    assert calls[0]["timeout_s"] == 77


def test_diff_timeout_is_partial_never_no_changes(art):
    with fake_run_tool(MOD, timed_out=True):
        diff = cloud_reporter.get_cloud_diff("E:/src", "bkt", "FY26-27", "rc.conf",
                                             artefacts=art, **DIFF_TUNING)
    assert diff["_partial"] is True and "timed out" in diff["_error"]


def test_size_output_is_kept_in_the_run_folder(art):
    out = json.dumps({"count": 3, "bytes": 30, "sizeless": 0})
    with fake_run_tool(MOD, exit_code=0, stdout=out):
        size = cloud_reporter.get_cloud_size("bkt", "FY26-27", "rc.conf", timeout=60,
                                             artefacts=art)
    assert size["count"] == 3
    kept = list(Path(art.dir).glob("*rclone-size-report.stdout.txt"))
    assert kept and json.loads(kept[0].read_text(encoding="utf-8"))["count"] == 3


def test_size_spawn_failure_is_data_not_a_crash(art):
    # The old code let a non-FileNotFoundError OSError escape the report task.
    with fake_run_tool(MOD, spawn_error="PermissionError: Access is denied"):
        size = cloud_reporter.get_cloud_size("bkt", "FY26-27", "rc.conf", timeout=60,
                                             artefacts=art)
    assert size["count"] == 0 and "Access is denied" in size["_error"]


def test_manifest_timeout_raises_never_returns_empty(art):
    with fake_run_tool(MOD, timed_out=True), \
         pytest.raises(cloud_reporter.CloudReporterError, match="timed out after 45s"):
        cloud_reporter.get_cloud_manifest("bkt", "FY26-27", "rc.conf", timeout=45,
                                          artefacts=art)


def test_manifest_listing_is_kept_in_the_run_folder(art):
    listing = json.dumps([{"Path": "a.txt", "Size": 1, "IsDir": False},
                          {"Path": "d", "IsDir": True}])
    with fake_run_tool(MOD, exit_code=0, stdout=listing):
        files = cloud_reporter.get_cloud_manifest("bkt", "FY26-27", "rc.conf", timeout=45,
                                                  artefacts=art)
    assert [f["Path"] for f in files] == ["a.txt"]
    assert list(Path(art.dir).glob("*rclone-manifest.stdout.txt"))


@pytest.mark.parametrize("call", [
    lambda: cloud_reporter.get_cloud_size("b", "FY26-27", "rc"),
    lambda: cloud_reporter.get_cloud_manifest("b", "FY26-27", "rc"),
    lambda: cloud_reporter.get_cloud_diff("s", "b", "FY26-27", "rc"),
])
def test_tunables_have_no_hidden_defaults(call):
    with pytest.raises(TypeError):
        call()
