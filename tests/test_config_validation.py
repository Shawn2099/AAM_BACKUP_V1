"""Tests for models/config.py — previously untested validators.

Covers: diff_timeout_seconds, NotificationConfig.valid_port,
MaintenanceConfig, HealthConfig, DashboardConfig, ScheduleConfig.
"""

import pytest
from pydantic import ValidationError

from models.config import (
    CloudConfig,
    DashboardConfig,
    HealthConfig,
    MaintenanceConfig,
    NotificationConfig,
    ScheduleConfig,
)


class TestDiffTimeoutSeconds:
    def test_default_value(self):
        """Default is 18000."""
        config = CloudConfig(enabled=False)
        assert config.diff_timeout_seconds == 18000

    def test_boundary_min(self):
        """Minimum allowed value is 30."""
        config = CloudConfig(enabled=False, diff_timeout_seconds=30)
        assert config.diff_timeout_seconds == 30

    def test_boundary_max(self):
        """Maximum allowed value is 86400."""
        config = CloudConfig(enabled=False, diff_timeout_seconds=86400)
        assert config.diff_timeout_seconds == 86400

    def test_below_min_rejected(self):
        """Below 30 → ValidationError."""
        with pytest.raises(ValidationError):
            CloudConfig(enabled=False, diff_timeout_seconds=29)

    def test_above_max_rejected(self):
        """Above 86400 → ValidationError."""
        with pytest.raises(ValidationError):
            CloudConfig(enabled=False, diff_timeout_seconds=86401)

    def test_negative_rejected(self):
        """Negative value → ValidationError."""
        with pytest.raises(ValidationError):
            CloudConfig(enabled=False, diff_timeout_seconds=-1)


class TestNotificationConfig:
    def test_valid_port(self):
        config = NotificationConfig(smtp_port=587)
        assert config.smtp_port == 587

    def test_port_too_low(self):
        with pytest.raises(ValidationError):
            NotificationConfig(smtp_port=0)

    def test_port_too_high(self):
        with pytest.raises(ValidationError):
            NotificationConfig(smtp_port=70000)

    def test_default_port(self):
        config = NotificationConfig()
        assert config.smtp_port == 587


class TestMaintenanceConfig:
    def test_defaults(self):
        config = MaintenanceConfig()
        assert config.concurrency_wait_seconds == 3600
        assert config.sqlite_synchronous == "normal"
        assert config.db_retention_days == 90

    def test_concurrency_wait_bounds(self):
        with pytest.raises(ValidationError):
            MaintenanceConfig(concurrency_wait_seconds=0)
        with pytest.raises(ValidationError):
            MaintenanceConfig(concurrency_wait_seconds=86401)

    def test_sqlite_synchronous_pattern(self):
        with pytest.raises(ValidationError):
            MaintenanceConfig(sqlite_synchronous="invalid")


class TestHealthConfig:
    def test_defaults(self):
        config = HealthConfig()
        assert config.max_clock_skew_seconds == 600
        assert config.clock_check_timeout_seconds == 10
        assert config.min_free_source_gb == 1

    def test_clock_skew_bounds(self):
        with pytest.raises(ValidationError):
            HealthConfig(max_clock_skew_seconds=30)  # below min 60
        with pytest.raises(ValidationError):
            HealthConfig(max_clock_skew_seconds=3601)  # above max 3600


class TestDashboardConfig:
    @pytest.mark.parametrize("removed", [{"api_key": "k"}, {"auth_enabled": False}])
    def test_removed_login_keys_are_not_model_fields_any_more(self, removed):
        """Plan E: sign-in is always on and the API key is gone. The model itself is strict; a
        config.yaml from an older release is cleaned by DEPRECATED_KEYS before it gets here
        (code review 2026-10-19, see tests/test_review_2026_10_19.py)."""
        with pytest.raises(ValidationError):
            DashboardConfig(**removed)

    def test_no_password_hash_is_valid_but_nobody_signs_in(self):
        config = DashboardConfig()
        assert config.login_password_hash == "" and config.admin_password_hash == ""

    def test_port_bounds(self):
        with pytest.raises(ValidationError):
            DashboardConfig(port=1023)  # below min 1024
        with pytest.raises(ValidationError):
            DashboardConfig(port=65536)  # above max 65535


class TestScheduleConfig:
    def test_valid_cron(self):
        config = ScheduleConfig(cloud_cron="0 18 * * *")
        assert config.cloud_cron == "0 18 * * *"

    def test_invalid_cron_field_count(self):
        with pytest.raises(ValidationError):
            ScheduleConfig(cloud_cron="0 18 * *")  # only 4 fields

    def test_invalid_cron_values(self):
        with pytest.raises(ValidationError):
            ScheduleConfig(cloud_cron="99 99 * * *")
