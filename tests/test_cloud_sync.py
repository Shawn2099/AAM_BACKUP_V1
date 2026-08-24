"""Tests for cloud_sync — REAL rclone.exe against the production GCS bucket."""

import os
import time
from pathlib import Path

import pytest

from core.cloud_sync import (
    build_rclone_sync_command,
    classify_rclone_exit,
    resolve_max_duration_seconds,
    run_cloud_sync,
    scan_rclone_log_for_errors,
)
from core.rclone_config import temp_rclone_config


@pytest.fixture
def gcs(gcs_sandbox, tmp_path):
    fy = f"{gcs_sandbox['fy_prefix']}/{int(time.time() * 1000)}_{os.getpid()}"
    src = tmp_path / "src"
    (src / "docs").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "docs" / "n.bin").write_bytes(os.urandom(2048))
    return gcs_sandbox | {"fy_prefix": fy}, src


def _sync(gcs_env, src, **overrides):
    params = dict(
        source=str(src), bucket=gcs_env["bucket"], fy_prefix=gcs_env["fy_prefix"],
        gcs_key_path=gcs_env["gcs_key_path"], project_number=gcs_env["project_number"],
        storage_class=gcs_env["storage_class"], location=gcs_env["location"],
        timeout=300,
    )
    params.update(overrides)
    return run_cloud_sync(**params)


class TestClassifyRcloneExit:

    @pytest.mark.parametrize("code,expected", [
        (0, "CLOUD_COMPLETE"),
        (1, "CLOUD_FAILED"), (2, "CLOUD_FAILED"), (3, "CLOUD_FAILED"),
        (4, "CLOUD_PARTIAL"), (5, "CLOUD_PARTIAL"),
        (6, "CLOUD_FAILED"), (7, "CLOUD_FAILED"), (8, "CLOUD_FAILED"),
        (9, "CLOUD_NO_CHANGES_COMPLETE"), (10, "CLOUD_PARTIAL"),
        (99, "CLOUD_FAILED"), (-1, "CLOUD_FAILED"),
    ])
    def test_matrix(self, code, expected):
        assert classify_rclone_exit(code) == expected


class TestBuildRcloneSyncCommand:

    def test_structure(self):
        cmd = build_rclone_sync_command(
            source="D:\\data", bucket="my-bucket", fy_prefix="FY26-27",
            config_path="C:\\temp\\r.conf", storage_class="COLDLINE",
        )
        assert cmd[1] == "sync"
        assert "aam_gcs:my-bucket/FY26-27" in cmd
        assert cmd[cmd.index("--config") + 1] == "C:\\temp\\r.conf"
        for flag in ("--fast-list", "--gcs-no-check-bucket",
                     "--error-on-no-transfer", "--check-first"):
            assert flag in cmd

    def test_resolves_real_binary(self):
        cmd = build_rclone_sync_command(
            source="X:", bucket="b", fy_prefix="FY", config_path="c",
            storage_class="STANDARD",
        )
        assert Path(cmd[0]).exists()


class TestResolveMaxDuration:

    def test_auto_derivation(self):
        assert resolve_max_duration_seconds(21600, None) == 21300

    def test_too_small_disables_cap(self):
        assert resolve_max_duration_seconds(200, None) is None


class TestScanRcloneLog:

    def test_clean_log_has_no_error(self):
        ok, _ = scan_rclone_log_for_errors(
            '{"level":"INFO","msg":"There was nothing to transfer"}\n')
        assert ok is False

    def test_json_error_detected(self):
        ok, tail = scan_rclone_log_for_errors(
            '{"level":"ERROR","msg":"failed to list bucket"}\n')
        assert ok is True and "bucket" in tail


class TestRunCloudSyncReal:

    def test_upload_completes_and_lands_in_bucket(self, gcs):
        env, src = gcs
        result = _sync(env, src)

        assert result["status"] == "CLOUD_COMPLETE", result
        assert result["exit_code"] == 0

        from core.cloud_reporter import get_cloud_manifest
        with temp_rclone_config(
            env["gcs_key_path"], env["location"], env["project_number"],
            env["storage_class"],
        ) as cfg:
            files = get_cloud_manifest(env["bucket"], env["fy_prefix"], cfg,
                                       timeout=120)
        assert {"a.txt", "docs/n.bin"} <= {f["Path"] for f in files}

    def test_second_run_no_changes_is_not_an_error(self, gcs):
        env, src = gcs
        assert _sync(env, src)["status"] == "CLOUD_COMPLETE"

        result = _sync(env, src)
        assert result["status"] == "CLOUD_NO_CHANGES_COMPLETE", result
        assert result["exit_code"] == 9

    def test_missing_source_directory_fails(self, gcs):
        env, _src = gcs
        result = _sync(env, Path("Q:/definitely_missing"))
        assert result["status"] == "CLOUD_FAILED"

    def test_invalid_service_account_key_fails(self, gcs, tmp_path):
        env, src = gcs
        bad_key = tmp_path / "bad.json"
        bad_key.write_text('{"not": "a real key"}')

        result = _sync(env, src, gcs_key_path=str(bad_key))
        assert result["status"] == "CLOUD_FAILED"

    def test_real_timeout_kills_slow_transfer(self, gcs):
        """bwlimit=1 byte/s makes a real 100KB upload take minutes;
        the OS-enforced timeout fires at ~5s."""
        env, src = gcs
        (src / "big.bin").write_bytes(os.urandom(100_000))

        started = time.monotonic()
        result = _sync(env, src, bwlimit="1", timeout=5, max_duration_seconds=0)
        elapsed = time.monotonic() - started

        assert result["status"] == "CLOUD_FAILED"
        assert result["exit_code"] == -1
        assert "Timeout after 5s" in result["error"]
        assert elapsed < 60

    def test_result_contract_keys_exact(self, gcs):
        env, src = gcs
        assert set(_sync(env, src).keys()) == {"status", "exit_code", "error"}
