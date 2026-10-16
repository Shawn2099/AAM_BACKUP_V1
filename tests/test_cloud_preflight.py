"""Tests for core/cloud_preflight.py — run_cloud_dry_run().

Covers: Probe A (source drive), Probe B (GCS auth), error paths, timeout,
missing binary, spawn error, empty source, and command construction.
Probe B runs through core.tool_runner.run_tool (plan 5.1): output is kept
in the run's artefact folder and the rclone flags come from config.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.cloud_preflight import run_cloud_dry_run
from core.run_artefacts import RunArtefacts
from test_support.fake_tools import fake_run_tool

MODULE = "core.cloud_preflight"


@pytest.fixture(autouse=True)
def _no_real_rclone_config():
    with patch(f"{MODULE}.temp_rclone_config") as mock_cfg, \
         patch(f"{MODULE}.resolve_binary", return_value="rclone"):
        mock_cfg.return_value.__enter__ = MagicMock(return_value="/tmp/config")
        mock_cfg.return_value.__exit__ = MagicMock(return_value=False)
        yield


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "file.txt").write_text("data")
    return src


def _run(source, tmp_path=None, **overrides):
    kwargs = dict(
        source=str(source), bucket="test-bucket", fy_prefix="FY26-27",
        gcs_key_path="key.json", project_number="123", storage_class="STANDARD",
        location="asia-south1", timeout=300, retries=2, retries_sleep_seconds=5,
    )
    if tmp_path is not None:
        kwargs["artefacts"] = RunArtefacts(str(tmp_path / "run_logs"), "run-pre")
    kwargs.update(overrides)
    return run_cloud_dry_run(**kwargs)


class TestProbeA:
    """Probe A — source drive alive check."""

    def test_source_not_accessible(self, tmp_path):
        with fake_run_tool(MODULE) as calls:
            result = _run(tmp_path / "nonexistent_drive")
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "not accessible" in result["error"]
        assert calls == []          # rclone never started

    def test_source_empty_dir_warns_but_continues(self, tmp_path):
        empty_dir = tmp_path / "empty_source"
        empty_dir.mkdir()
        with fake_run_tool(MODULE, exit_code=0, stdout="[]") as calls:
            result = _run(empty_dir)
        assert result["ok"] is True
        assert len(calls) == 1      # Probe B still ran

    def test_source_read_error(self, source):
        with fake_run_tool(MODULE) as calls, \
             patch.object(Path, "iterdir", side_effect=OSError("Access denied")):
            result = _run(source)
        assert result["ok"] is False
        assert "read error" in result["error"]
        assert calls == []


class TestProbeB:
    """Probe B — GCS auth + bucket probe."""

    def test_success(self, source):
        with fake_run_tool(MODULE, exit_code=0, stdout="[]"):
            result = _run(source)
        assert result == {"ok": True, "exit_code": 0, "error": None}

    def test_gcs_nonzero_exit(self, source):
        with fake_run_tool(MODULE, exit_code=1, stderr="Couldn't find bucket"):
            result = _run(source)
        assert result["ok"] is False
        assert result["exit_code"] == 1
        assert "Couldn't find bucket" in result["error"]

    def test_nonzero_exit_without_stderr(self, source):
        with fake_run_tool(MODULE, exit_code=3):
            result = _run(source)
        assert result["ok"] is False
        assert result["error"] == "Exit 3: no stderr"

    def test_gcs_timeout(self, source):
        with fake_run_tool(MODULE, timed_out=True) as calls:
            result = _run(source, timeout=30)
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert result["error"] == "Timeout after 30s"
        assert calls[0]["timeout_s"] == 30

    def test_rclone_not_found(self, source):
        with fake_run_tool(MODULE, spawn_error="FileNotFoundError: [WinError 2] rclone"):
            result = _run(source)
        assert result["ok"] is False
        assert result["error"] == "rclone not found"

    def test_os_error_launching_rclone(self, source):
        with fake_run_tool(MODULE, spawn_error="PermissionError: Access is denied"):
            result = _run(source)
        assert result["ok"] is False
        assert "OS error" in result["error"]
        assert "Access is denied" in result["error"]


class TestCommandConstruction:
    """Verify the rclone command is built correctly."""

    def test_command_flags(self, source):
        with fake_run_tool(MODULE, exit_code=0) as calls:
            _run(source, bucket="my-bucket")
        cmd = calls[0]["argv"]
        assert cmd[:2] == ["rclone", "lsjson"]
        assert cmd[cmd.index("--max-depth") + 1] == "0"
        assert cmd[cmd.index("--config") + 1] == "/tmp/config"
        # --gcs-no-check-bucket must be ABSENT (forces IAM check)
        assert "--gcs-no-check-bucket" not in cmd

    def test_retry_flags_come_from_the_parameters(self, source):
        with fake_run_tool(MODULE, exit_code=0) as calls:
            _run(source, retries=7, retries_sleep_seconds=12)
        cmd = calls[0]["argv"]
        assert cmd[cmd.index("--retries") + 1] == "7"
        assert cmd[cmd.index("--retries-sleep") + 1] == "12s"

    def test_dest_format(self, source):
        with fake_run_tool(MODULE, exit_code=0) as calls:
            _run(source, bucket="my-bucket")
        assert "aam_gcs:my-bucket/FY26-27" in calls[0]["argv"]

    def test_probe_output_is_kept_in_the_run_folder(self, source, tmp_path):
        with fake_run_tool(MODULE, exit_code=1, stderr="403 Forbidden") as calls:
            _run(source, tmp_path)
        art = calls[0]["artefacts"]
        assert Path(art.dir).is_relative_to(tmp_path / "run_logs")
        stderr_files = list(Path(art.dir).glob("*rclone-preflight-probe.stderr.txt"))
        assert stderr_files and "403 Forbidden" in stderr_files[0].read_text(encoding="utf-8")

    def test_location_and_timeout_have_no_hidden_defaults(self, source):
        # owner rule: tunables come from config, never a literal default
        with pytest.raises(TypeError):
            run_cloud_dry_run(str(source), "b", "FY26-27", "k.json", "1", "STANDARD")
