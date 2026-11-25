"""Tests for RF-5: rclone version check in health_check_task."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import flow
from core import config_help, faults, health
from models.config import ToolsConfig, load_config


def test_rf5_1_tools_config_defaults_and_validation():
    cfg = ToolsConfig()
    assert cfg.rclone_expected_version == "1.74"
    assert cfg.rclone_version_policy == "block"
    assert ToolsConfig(rclone_expected_version="1.75", rclone_version_policy="warn").rclone_version_policy == "warn"

    for bad_ver in ("1.74.3", "v1.74", "1", "abc", ""):
        with pytest.raises(ValidationError):
            ToolsConfig(rclone_expected_version=bad_ver)

    with pytest.raises(ValidationError):
        ToolsConfig(rclone_version_policy="ignore")


def test_rf5_2_rclone_version_fault_matches_and_mismatches():
    health._RCLONE_VERSION_CACHE.clear()
    with patch("core.health.resolve_binary", return_value="C:/bin/rclone.exe"), \
            patch("core.health.run_tool") as rt:
        rt.return_value = SimpleNamespace(
            exit_code=0, spawn_error=None, timed_out=False,
            stdout_text=lambda: "rclone v1.74.3\n- os/version: windows\n",
        )
        assert health.rclone_version_fault("1.74") is None

        health._RCLONE_VERSION_CACHE.clear()
        rt.return_value = SimpleNamespace(
            exit_code=0, spawn_error=None, timed_out=False,
            stdout_text=lambda: "rclone v1.73.1\n- os/version: windows\n",
        )
        fault = health.rclone_version_fault("1.74")
        assert fault is not None
        assert "1.73" in fault and "1.74" in fault

        health._RCLONE_VERSION_CACHE.clear()
        rt.return_value = SimpleNamespace(
            exit_code=1, spawn_error=None, timed_out=False,
            stdout_text=lambda: "",
        )
        assert health.rclone_version_fault("1.74") is not None


def test_rf5_3_rclone_version_fault_cached_per_binary_path():
    health._RCLONE_VERSION_CACHE.clear()
    ok_res = SimpleNamespace(
        exit_code=0, spawn_error=None, timed_out=False,
        stdout_text=lambda: "rclone v1.74.3\n",
    )
    with patch("core.health.resolve_binary", return_value="C:/bin1/rclone.exe"), \
            patch("core.health.run_tool", return_value=ok_res) as rt:
        assert health.rclone_version_fault("1.74") is None
        assert health.rclone_version_fault("1.74") is None
        assert rt.call_count == 1

    with patch("core.health.resolve_binary", return_value="C:/bin2/rclone.exe"), \
            patch("core.health.run_tool", return_value=ok_res) as rt2:
        assert health.rclone_version_fault("1.74") is None
        assert rt2.call_count == 1


def _health_cfg(policy: str = "block"):
    return SimpleNamespace(
        paths=SimpleNamespace(source_drive="C:/src", gcs_key_path="C:/key.json"),
        health=SimpleNamespace(
            min_free_source_gb=1.0,
            max_clock_skew_seconds=300,
            clock_check_timeout_seconds=10,
            clock_unverified_policy="block",
            ntp_server="time.google.com",
            w32tm_timeout_seconds=10,
        ),
        tools=ToolsConfig(rclone_expected_version="1.74", rclone_version_policy=policy),
    )


@pytest.mark.parametrize("mode", ["cloud", "lan"])
def test_rf5_4_health_check_task_block_policy_raises_aam_sys_04(mode):
    cfg = _health_cfg("block")
    with patch("flow.pre_backup_health", return_value=None), \
            patch("flow.health.rclone_version_fault", return_value="rclone 1.73.x != 1.74"), \
            pytest.raises(health.HealthError) as exc_info:
        flow.health_check_task.fn(cfg, mode)
    assert faults.code_of(exc_info.value) == "AAM-SYS-04"


def test_rf5_5_health_check_task_warn_policy_logs_and_notifies_once_per_day():
    cfg = _health_cfg("warn")
    with patch("flow.pre_backup_health", return_value=None), \
            patch("flow.health.rclone_version_fault", return_value="rclone 1.73.x != 1.74"), \
            patch("flow.notify") as notify_mock:
        flow.health_check_task.fn(cfg, "lan")
    assert notify_mock.call_count == 1
    ev = notify_mock.call_args.args[1]
    assert ev.kind == "WARNING"
    assert ev.key == "rclone_version"
    assert ev.fault_code == "AAM-SYS-04"
    assert ev.cooldown_hours == 24


def test_rf5_6_health_check_task_skips_version_check_when_mode_disk():
    cfg = _health_cfg("block")
    with patch("flow.pre_backup_health", return_value=None), \
            patch("flow.health.rclone_version_fault") as ver_mock:
        flow.health_check_task.fn(cfg, "disk")
    ver_mock.assert_not_called()


def test_rf5_7_config_help_and_example_include_rclone_version_keys():
    assert "tools.rclone_expected_version" in config_help.HELP
    assert "tools.rclone_version_policy" in config_help.HELP
    cfg = load_config(Path("config.example.yaml"))
    assert cfg.tools.rclone_expected_version == "1.74"
    assert cfg.tools.rclone_version_policy == "block"
