"""Tests for AppConfig cross-field validation and edge cases."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from models.config import AppConfig, CloudConfig, DashboardConfig, LanConfig, PathsConfig, WolConfig


def _quiet_legs(**overrides):
    """Valid config for the legs a cross-field test does not exercise.

    Each sub-model validates on its own, before AppConfig.cross_field_validation
    runs: WolConfig(enabled=True) demands a MAC, CloudConfig(enabled=True)
    demands bucket + project_number, DashboardConfig(auth_enabled=True) demands
    an api_key. A test that only cares about one AppConfig-level rule therefore
    has to hand the other legs a config that passes on its own, otherwise the
    sub-model error fires first and the rule under test never runs.
    """
    legs = dict(
        wol=WolConfig(enabled=False),
        cloud=CloudConfig(enabled=False),
        dashboard=DashboardConfig(auth_enabled=False),
    )
    legs.update(overrides)
    return legs


class TestCrossFieldValidation:
    def test_lan_enabled_without_unc(self):
        """LAN enabled with non-UNC path → error.

        (PathsConfig.is_unc_path rejects this first, unconditionally; the
        AppConfig-level UNC rule is the backstop for a UNC-shaped value that
        still isn't a share.)"""
        with pytest.raises(ValidationError, match="UNC"):
            AppConfig(
                paths=PathsConfig(
                    source_drive="E:\\",
                    lan_destination="D:\\backup",
                ),
                lan=LanConfig(enabled=True),
                **_quiet_legs(),
            )

    def test_cloud_enabled_without_key(self):
        """Cloud enabled without GCS key → error (AppConfig-level rule)."""
        with pytest.raises(ValidationError, match="gcs_key_path"):
            AppConfig(
                paths=PathsConfig(
                    source_drive="E:\\",
                    lan_destination="\\\\nas\\share",
                    gcs_key_path="",
                ),
                lan=LanConfig(enabled=False),
                # the cloud leg must be valid on its own, otherwise
                # CloudConfig._bucket_required_when_enabled fires first
                **_quiet_legs(cloud=CloudConfig(enabled=True, bucket="my-bucket", project_number="123")),
            )

    def test_both_legs_disabled(self):
        """Both LAN and cloud disabled → error."""
        with pytest.raises(ValidationError, match="At least one"):
            AppConfig(
                paths=PathsConfig(
                    source_drive="E:\\",
                    lan_destination="\\\\nas\\share",
                ),
                lan=LanConfig(enabled=False),
                **_quiet_legs(),
            )

    def test_fy_mismatch(self):
        """Source and LAN FY mismatch → error."""
        with pytest.raises(ValidationError, match="FY"):
            AppConfig(
                paths=PathsConfig(
                    source_drive="E:\\SOURCE\\FY25-26",
                    lan_destination="\\\\nas\\share\\FY24-25",
                ),
                lan=LanConfig(enabled=True),
                **_quiet_legs(),
            )

    def test_4digit_fy_rejected(self):
        """4-digit FY name → error."""
        with pytest.raises(ValidationError, match="4-digit"):
            AppConfig(
                paths=PathsConfig(
                    source_drive="E:\\SOURCE\\FY2026-27",
                    lan_destination="\\\\nas\\share\\FY2026-27",
                ),
                lan=LanConfig(enabled=True),
                **_quiet_legs(),
            )

    def test_valid_lan_only_config(self):
        """Valid LAN-only config."""
        config = AppConfig(
            paths=PathsConfig(
                source_drive="E:\\SOURCE\\FY26-27",
                lan_destination="\\\\nas\\share\\FY26-27",
            ),
            lan=LanConfig(enabled=True),
            **_quiet_legs(),
        )
        assert config.lan.enabled is True
        assert config.cloud.enabled is False

    def test_valid_cloud_only_config(self, tmp_path):
        """Valid cloud-only config."""
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')
        config = AppConfig(
            paths=PathsConfig(
                source_drive="E:\\SOURCE\\FY26-27",
                lan_destination="\\\\nas\\share\\FY26-27",
                gcs_key_path=str(key),
            ),
            lan=LanConfig(enabled=False),
            **_quiet_legs(cloud=CloudConfig(enabled=True, bucket="my-bucket", project_number="123")),
        )
        assert config.cloud.enabled is True
        assert config.lan.enabled is False


class TestPathsConfig:
    def test_database_path_must_end_with_db(self):
        """Database path must end with .db."""
        with pytest.raises(ValidationError, match=".db"):
            PathsConfig(
                source_drive="E:\\",
                lan_destination="\\\\nas\\share",
                database_path="C:\\BackupAgent\\manifest.txt",
            )

    def test_auto_derive_database_path(self, tmp_path):
        """Database path is auto-derived from runtime_dir."""
        config = PathsConfig(
            source_drive="E:\\",
            lan_destination="\\\\nas\\share",
            runtime_dir=str(tmp_path),
        )
        assert config.database_path.endswith(".db")
        assert str(tmp_path) in config.database_path

    def test_auto_derive_log_directory(self, tmp_path):
        """Log directory is auto-derived from runtime_dir."""
        config = PathsConfig(
            source_drive="E:\\",
            lan_destination="\\\\nas\\share",
            runtime_dir=str(tmp_path),
        )
        assert "logs" in config.log_directory

    def test_backup_lock_path(self, tmp_path):
        """backup_lock_path is derived from database_path."""
        config = PathsConfig(
            source_drive="E:\\",
            lan_destination="\\\\nas\\share",
            runtime_dir=str(tmp_path),
        )
        assert config.backup_lock_path.name == "backup.lock"

    def test_prefect_home(self, tmp_path):
        """prefect_home is inside runtime_dir."""
        config = PathsConfig(
            source_drive="E:\\",
            lan_destination="\\\\nas\\share",
            runtime_dir=str(tmp_path),
        )
        assert ".prefect" in str(config.prefect_home)


class TestFromYaml:
    def test_load_valid_config(self, tmp_path):
        """Load config from YAML file.

        Legs that are not under test must be explicitly disabled/enumerated:
        DashboardConfig.auth_enabled defaults to True, which then demands an
        api_key (a bare-minimum YAML that omits the dashboard section used to
        load, but that was the pre-hardening default)."""
        config = tmp_path / "config.yaml"
        config.write_text(
            "firm_name: Test Firm\n"
            "paths:\n"
            "  source_drive: E:\\SOURCE\\FY26-27\n"
            "  lan_destination: \\\\nas\\share\\FY26-27\n"
            "  runtime_dir: C:\\BackupAgent\n"
            "  database_path: C:\\BackupAgent\\manifest.db\n"
            "  log_directory: C:\\BackupAgent\\logs\n"
            "  gcs_key_path: key.json\n"
            "lan:\n"
            "  enabled: true\n"
            "cloud:\n"
            "  enabled: false\n"
            "wol:\n"
            "  enabled: false\n"
            "dashboard:\n"
            "  auth_enabled: false\n"
        )
        result = AppConfig.from_yaml(str(config))
        assert result.firm_name == "Test Firm"
        assert result.lan.enabled is True
        assert result.cloud.enabled is False

    def test_malformed_yaml_raises(self, tmp_path):
        """Malformed YAML → exception."""
        config = tmp_path / "config.yaml"
        config.write_text("not: valid: yaml: [")
        with pytest.raises(Exception):
            AppConfig.from_yaml(str(config))
