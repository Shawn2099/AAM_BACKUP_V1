"""Tests for core/fy_rollover.py — previously untested functions.

Covers: _fy_name, _parent_path, _child_path, update_config_yaml,
run_archive_transition, detect_rollover.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.fy_rollover import (
    RolloverError,
    _child_path,
    _fy_name,
    _parent_path,
    detect_rollover,
    run_archive_transition,
    update_config_yaml,
)


class TestFyName:
    def test_windows_path(self):
        assert _fy_name("E:\\SOURCE\\FY26-27") == "FY26-27"

    def test_unix_path(self):
        assert _fy_name("/mnt/source/FY25-26") == "FY25-26"

    def test_no_fy_suffix(self):
        assert _fy_name("E:\\SOURCE\\DATA") is None

    def test_case_insensitive(self):
        assert _fy_name("E:\\SOURCE\\fy26-27") == "FY26-27"

    def test_4digit_fy_rejected(self):
        """4-digit FY names return None (not matched by FY_PATTERN)."""
        assert _fy_name("E:\\SOURCE\\FY2026-27") is None


class TestParentPath:
    def test_windows(self):
        assert _parent_path("E:\\SOURCE\\FY26-27") == "E:\\SOURCE"

    def test_unix(self):
        assert _parent_path("/mnt/source/FY26-27") == "/mnt/source"

    def test_unc(self):
        result = _parent_path("\\\\server\\share\\FY26-27")
        assert "FY26-27" not in result


class TestChildPath:
    def test_windows(self):
        assert _child_path("E:\\SOURCE", "FY26-27") == "E:\\SOURCE\\FY26-27"

    def test_unix(self):
        assert _child_path("/mnt/source", "FY26-27") == "/mnt/source/FY26-27"

    def test_trailing_separator(self):
        assert _child_path("E:\\SOURCE\\", "FY26-27") == "E:\\SOURCE\\FY26-27"


# the switch now re-loads the written file (plan K): the rest of the file must be valid too
_VALID_REST = ("wol:\n  enabled: false\n"
               "cloud:\n  enabled: false\n"
               "dashboard:\n  auth_enabled: true\n  api_key: secret-key\n")


class TestUpdateConfigYaml:
    def test_updates_paths(self, tmp_path):
        """Config YAML is updated with new FY paths."""
        config = tmp_path / "config.yaml"
        config.write_text(
            "paths:\n"
            "  source_drive: E:\\SOURCE\\FY25-26\n"
            "  lan_destination: \\\\nas\\share\\FY25-26\n"
            "  runtime_dir: C:\\BackupAgent\n"
            "  database_path: C:\\BackupAgent\\manifest.db\n"
            "  log_directory: C:\\BackupAgent\\logs\n"
            "  gcs_key_path: key.json\n"
            + _VALID_REST
        )

        update_config_yaml(
            config_path=str(config),
            source_root="E:\\SOURCE",
            lan_root="\\\\nas\\share",
            new_fy="FY26-27",
        )

        content = config.read_text()
        assert "E:\\SOURCE\\FY26-27" in content
        assert "\\\\nas\\share\\FY26-27" in content

    def test_preserves_other_keys(self, tmp_path):
        """Other config keys are preserved."""
        config = tmp_path / "config.yaml"
        config.write_text(
            "firm_name: Test Firm\n"
            "paths:\n"
            "  source_drive: E:\\SOURCE\\FY25-26\n"
            "  lan_destination: \\\\nas\\share\\FY25-26\n"
            "  runtime_dir: C:\\BackupAgent\n"
            "  database_path: C:\\BackupAgent\\manifest.db\n"
            "  log_directory: C:\\BackupAgent\\logs\n"
            "  gcs_key_path: key.json\n"
            + _VALID_REST
        )

        update_config_yaml(
            config_path=str(config),
            source_root="E:\\SOURCE",
            lan_root="\\\\nas\\share",
            new_fy="FY26-27",
        )

        content = config.read_text()
        assert "Test Firm" in content

    def test_atomic_replacement(self, tmp_path):
        """Original file is replaced atomically (no temp files left behind)."""
        config = tmp_path / "config.yaml"
        config.write_text(
            "paths:\n"
            "  source_drive: E:\\SOURCE\\FY25-26\n"
            "  lan_destination: \\\\nas\\share\\FY25-26\n"
            "  runtime_dir: C:\\BackupAgent\n"
            "  database_path: C:\\BackupAgent\\manifest.db\n"
            "  log_directory: C:\\BackupAgent\\logs\n"
            "  gcs_key_path: key.json\n"
            + _VALID_REST
        )

        update_config_yaml(
            config_path=str(config),
            source_root="E:\\SOURCE",
            lan_root="\\\\nas\\share",
            new_fy="FY26-27",
        )

        # No temp files left in the directory; only the file, its backup and the lock file
        names = sorted(p.name for p in tmp_path.iterdir())
        assert not [n for n in names if n.startswith(".config_")]
        assert "config.yaml" in names
        assert all(n == "config.yaml" or n.startswith("config.yaml.") for n in names), names


class TestRunArchiveTransition:
    """gcloud runs through run_tool (WP11): fake_run_tool returns one result for
    every call in the test."""

    def _run(self, key, **fake):
        from test_support.fake_tools import fake_run_tool
        with patch("core.fy_rollover._resolve_gcloud", return_value="gcloud"), \
             fake_run_tool("core.fy_rollover", **fake) as calls:
            ok = run_archive_transition(bucket="my-bucket", old_fy="FY25-26",
                                        gcs_key_path=str(key), auth_timeout=30,
                                        archive_timeout=600)
        return ok, calls

    def test_gcloud_not_found(self):
        """gcloud not found → False."""
        with patch("core.fy_rollover._resolve_gcloud", return_value=None):
            assert run_archive_transition(bucket="test-bucket", old_fy="FY25-26",
                                          gcs_key_path="key.json") is False

    def test_auth_failure(self, tmp_path):
        """gcloud auth fails → False, archive never attempted."""
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')
        ok, calls = self._run(key, exit_code=1, stderr="auth failed")
        assert ok is False and len(calls) == 1 and calls[0]["name"] == "gcloud-auth"

    def test_archive_success(self, tmp_path):
        """Successful auth + archive → True; key injected via the environment."""
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')
        ok, calls = self._run(key, exit_code=0)
        assert ok is True and [c["name"] for c in calls] == ["gcloud-auth", "gcloud-archive"]
        assert [c["timeout_s"] for c in calls] == [30, 600]

    def test_archive_command_construction(self, tmp_path):
        """Command includes --storage-class=ARCHIVE and --recursive."""
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')
        _, calls = self._run(key, exit_code=0)
        cmd = calls[-1]["argv"]
        assert cmd[1:4] == ["storage", "objects", "update"]
        assert "gs://my-bucket/FY25-26/" in cmd
        assert "--storage-class=ARCHIVE" in cmd and "--recursive" in cmd

    def test_timeout(self, tmp_path):
        """Timeout → False (run_tool killed the process tree)."""
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')
        ok, _ = self._run(key, timed_out=True)
        assert ok is False

    def test_no_key_file_uses_ambient_auth(self, tmp_path):
        """No key file → skips auth, goes straight to archive."""
        ok, calls = self._run(tmp_path / "nonexistent.json", exit_code=0)
        assert ok is True and [c["name"] for c in calls] == ["gcloud-archive"]


class TestDetectRollover:
    def test_no_fy_in_paths(self):
        """No FY suffix → False (rollover disabled)."""
        with patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27"):
            assert detect_rollover("E:\\SOURCE\\DATA", "\\\\nas\\share\\DATA") is False

    def test_matching_fy(self):
        """FY matches computed → False (no rollover needed)."""
        with patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27"):
            assert detect_rollover("E:\\SOURCE\\FY26-27", "\\\\nas\\share\\FY26-27") is False

    def test_mismatched_fy(self):
        """FY doesn't match computed → True (rollover needed)."""
        with patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27"):
            assert detect_rollover("E:\\SOURCE\\FY25-26", "\\\\nas\\share\\FY25-26") is True
