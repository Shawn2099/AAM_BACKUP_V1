"""AF-9: runtime validation of shutdown target (no loopback, local host, or UNC mismatch)."""
from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from core import shutdown as sd
from core.run_artefacts import RunArtefacts
import flow
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, WolConfig


@pytest.mark.parametrize("bad_ip", ["127.0.0.1", "::1", "localhost", "0.0.0.0", ""])
def test_af9_shutdown_server_refuses_local_or_loopback(tmp_path, bad_ip):
    with patch("core.shutdown.run_tool") as rt:
        res = sd.shutdown_server(
            bad_ip, delay_s=30, command_timeout_s=10, verify_timeout_s=10,
            poll_interval_s=1, connect_timeout_s=1, closed_probes=1,
            artefacts=RunArtefacts.open(str(tmp_path)),
        )
    assert res.outcome == sd.FAILED
    assert not res.verified
    rt.assert_not_called()


def test_af9_shutdown_server_refuses_local_hostname(tmp_path):
    local_host = socket.gethostname()
    with patch("core.shutdown.run_tool") as rt:
        res = sd.shutdown_server(
            local_host, delay_s=30, command_timeout_s=10, verify_timeout_s=10,
            poll_interval_s=1, connect_timeout_s=1, closed_probes=1,
            artefacts=RunArtefacts.open(str(tmp_path)),
        )
    assert res.outcome == sd.FAILED
    rt.assert_not_called()


def test_af9_lan_shutdown_task_refuses_unc_ip_mismatch(tmp_path):
    src = tmp_path / "FY26-27"
    src.mkdir()
    cfg = AppConfig(
        paths=PathsConfig(
            source_drive=str(src),
            lan_destination=r"\\192.168.1.50\backup\FY26-27",
            database_path=str(tmp_path / "m.db"),
        ),
        cloud=CloudConfig(enabled=False, bucket="test-bucket", project_number="1"),
        lan=LanConfig(enabled=True, shutdown_after_backup=True),
        wol=WolConfig(enabled=True, mac_address="11:22:33:44:55:66",
                      server_ip="192.168.1.99"),
    )
    with patch("flow.shutdown_from_config") as sfc, patch("flow.notify"):
        res = flow.lan_shutdown_task.fn(cfg)
    sfc.assert_not_called()
    assert res["outcome"] == sd.FAILED
    assert "does not match" in res["detail"]
