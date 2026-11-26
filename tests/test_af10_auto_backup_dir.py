"""AF-10: _auto_backup_dir must fail loud when versions_enabled=True and resolution fails."""
from __future__ import annotations

from unittest.mock import patch

import pytest

import flow
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, WolConfig


def test_af10_auto_backup_dir_raises_when_versions_enabled_and_resolution_fails(tmp_path):
    src = tmp_path / "FY26-27"
    src.mkdir()
    cfg = AppConfig(
        paths=PathsConfig(
            source_drive=str(src),
            lan_destination=r"\\192.168.1.50\backup\FY26-27",
            database_path=str(tmp_path / "m.db"),
        ),
        cloud=CloudConfig(enabled=False, bucket="test-bucket", project_number="1"),
        lan=LanConfig(enabled=True, versions_enabled=True),
        wol=WolConfig(enabled=False),
    )
    with patch("flow._effective_fy_prefix", side_effect=RuntimeError("fy parse failed")):
        with pytest.raises(RuntimeError, match="fy parse failed"):
            flow._auto_backup_dir(cfg)
