"""Tests for WS-B B2: Phase 1b versions & reminder config keys + V10 validator."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config_help import HELP
from models.config import ApprovalsConfig, LanConfig, load_config, resolve_versions_root


def test_k1b_lan_versions_and_approvals_reminder_defaults_and_bounds():
    lan = LanConfig()
    assert lan.versions_enabled is True
    assert lan.versions_folder == "AAM_versions"
    assert lan.versions_retention_days == 91

    assert LanConfig(versions_retention_days=7).versions_retention_days == 7
    assert LanConfig(versions_retention_days=3650).versions_retention_days == 3650
    with pytest.raises(ValidationError):
        LanConfig(versions_retention_days=6)
    with pytest.raises(ValidationError):
        LanConfig(versions_retention_days=3651)

    app = ApprovalsConfig()
    assert app.reminder_days == [1, 2, 4, 7]
    with pytest.raises(ValidationError):
        ApprovalsConfig(reminder_days=[])
    with pytest.raises(ValidationError):
        ApprovalsConfig(reminder_days=[0, 1])

    for key in ("lan.versions_enabled", "lan.versions_folder",
                "lan.versions_retention_days", "approvals.reminder_days"):
        assert key in HELP, f"missing config_help for {key}"

    cfg = load_config("config.example.yaml")
    assert cfg.lan.versions_enabled is True
    assert cfg.approvals.reminder_days == [1, 2, 4, 7]


def test_v10_resolve_versions_root_and_refuse_inside_mirror_or_other_share():
    dst = r"\\NAS\backup\FY26-27"
    assert resolve_versions_root(dst, "AAM_versions") == r"\\NAS\backup\AAM_versions"
    assert resolve_versions_root(dst, r"\\NAS\backup\custom_versions") == r"\\NAS\backup\custom_versions"

    for bad in (
        r"\\NAS\backup\FY26-27",
        r"\\NAS\backup\FY26-27\AAM_versions",
        r"\\NAS\other_share\AAM_versions",
        r"\\OTHER_NAS\backup\AAM_versions",
        r"..\AAM_versions",
        "",
    ):
        with pytest.raises(ValueError):
            resolve_versions_root(dst, bad)
