"""Tests for WS-C C4 / C4w: merged cloud verify + diff + size."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import flow
from core.cloud_verify import verify_cloud_and_diff
from core.run_artefacts import RunArtefacts
from test_support.fake_tools import fake_run_tool


def test_verify_cloud_and_diff_merges_check_and_builds_manifest(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello")
    art = RunArtefacts(tmp_path / "art", "r1")
    stderr = "2026/11/25 01:00:00 NOTICE: aam_gcs:b/FY26-27: 0 differences found\n"
    with fake_run_tool("core.cloud_verify", exit_code=0, stderr=stderr, combined="= a.txt\n") as calls:
        res = verify_cloud_and_diff(
            str(tmp_path), "b", "FY26-27", "/c.conf",
            timeout=60, checkers=4, modify_window_seconds=2, artefacts=art,
        )
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[1] == "check"
    assert "--one-way" in argv and "--combined" in argv and "--size-only" in argv
    assert res["verified"] is True
    assert res["diff"] == {"added": [], "removed": [], "modified": [], "unchanged": ["a.txt"]}
    assert res["manifest_error"] is None
    assert len(res["manifest"]) == 1 and res["manifest"][0]["Path"] == "a.txt"
    assert res["manifest"][0]["Size"] == 5


def test_verify_cloud_and_diff_fails_closed_when_combined_missing(tmp_path):
    art = RunArtefacts(tmp_path / "art", "r2")
    stderr = "2026/11/25 01:00:00 NOTICE: aam_gcs:b/FY26-27: 0 differences found\n"
    with fake_run_tool("core.cloud_verify", exit_code=0, stderr=stderr):
        res = verify_cloud_and_diff(
            str(tmp_path), "b", "FY26-27", "/c.conf",
            timeout=60, checkers=4, modify_window_seconds=2, artefacts=art,
        )
    assert res["diff"]["_partial"] is True
    assert res["manifest_error"] is not None


def test_cloud_verify_and_report_task_uses_two_rclone_calls(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"abc")

    @contextmanager
    def fake_cfg(*a, **k):
        yield "/c.conf"

    cfg = SimpleNamespace(
        paths=SimpleNamespace(source_drive=str(tmp_path), gcs_key_path="k"),
        cloud=SimpleNamespace(
            bucket="b", location="l", project_number="p", storage_class="s",
            verify_timeout_seconds=60, cloud_size_timeout_seconds=60,
            checkers=4, modify_window_seconds=2, retry_count=2, retries_sleep_seconds=5,
        ),
    )
    stderr = "2026/11/25 01:00:00 NOTICE: aam_gcs:b/FY26-27: 0 differences found\n"
    with patch("flow.temp_rclone_config", fake_cfg), \
         fake_run_tool("core.cloud_verify", exit_code=0, stderr=stderr, combined="= a.txt\n") as vcalls, \
         fake_run_tool("core.cloud_reporter", exit_code=0, stdout='{"count":1,"bytes":3,"sizeless":0}') as scalls:
        out = flow.cloud_verify_and_report_task.fn(cfg, "FY26-27", artefacts_dir=str(tmp_path / "art"))

    assert len(vcalls) == 1 and len(scalls) == 1
    assert out["verified"] is True
    assert out["size"] == {"count": 1, "bytes": 3, "sizeless": 0}
    assert set(out["stage_seconds"]) == {"verify_s", "size_s"}
    assert out["manifest_error"] is None
