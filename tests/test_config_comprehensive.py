"""Comprehensive tests for models/config.py — all config models, validation, loading."""

import pytest
from pydantic import ValidationError


@pytest.fixture(scope="session", autouse=True)
def prefect_harness():
    yield


from models.config import (
    AppConfig,
    CloudConfig,
    DashboardConfig,
    HealthConfig,
    LanConfig,
    MaintenanceConfig,
    NotificationConfig,
    PathsConfig,
    ScheduleConfig,
    WolConfig,
    load_config,
)

# ═══════════════════════════════════════════════════════════════════
# PathsConfig
# ═══════════════════════════════════════════════════════════════════


class TestPathsConfig:
    def test_valid_paths(self):
        cfg = PathsConfig(
            source_drive="C:\\data",
            lan_destination="\\\\10.0.0.1\\share",
            database_path="C:\\db\\test.db",
            gcs_key_path="C:\\keys\\key.json",
        )
        assert cfg.source_drive == "C:\\data"

    def test_empty_source_drive_raises(self):
        with pytest.raises(ValueError):
            PathsConfig(
                source_drive="   ",
                lan_destination="\\\\10.0.0.1\\share",
                database_path="C:\\db\\test.db",
                gcs_key_path="C:\\keys\\key.json",
            )

    def test_non_unc_lan_destination_raises(self):
        with pytest.raises(ValueError, match="UNC"):
            PathsConfig(
                source_drive="C:\\data",
                lan_destination="C:\\local\\share",
                database_path="C:\\db\\test.db",
                gcs_key_path="C:\\keys\\key.json",
            )

    def test_db_path_not_ending_in_db_raises(self):
        with pytest.raises(ValueError, match=".db"):
            PathsConfig(
                source_drive="C:\\data",
                lan_destination="\\\\10.0.0.1\\share",
                database_path="C:\\db\\test.txt",
                gcs_key_path="C:\\keys\\key.json",
            )

    def test_strips_whitespace(self):
        cfg = PathsConfig(
            source_drive="  D:\\data  ",
            lan_destination="\\\\10.0.0.1\\share",
            database_path="C:\\db\\test.db",
            gcs_key_path="C:\\keys\\key.json",
        )
        assert cfg.source_drive == "D:\\data"

    def test_default_log_directory(self):
        cfg = PathsConfig(
            source_drive="D:\\data",
            lan_destination="\\\\10.0.0.1\\share",
            database_path="C:\\db\\test.db",
            gcs_key_path="C:\\keys\\key.json",
        )
        assert cfg.log_directory.endswith("logs")

    def test_empty_gcs_key_raises(self):
        with pytest.raises(ValueError):
            PathsConfig(
                source_drive="C:\\data",
                lan_destination="\\\\10.0.0.1\\share",
                database_path="C:\\db\\test.db",
                gcs_key_path="",
            )


# ═══════════════════════════════════════════════════════════════════
# WolConfig
# ═══════════════════════════════════════════════════════════════════


class TestWolConfig:
    def test_valid_mac_with_dashes(self):
        cfg = WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="10.0.0.1")
        assert cfg.mac_address == "AA-BB-CC-DD-EE-FF"

    def test_valid_mac_with_colons(self):
        cfg = WolConfig(mac_address="aa:bb:cc:dd:ee:ff", server_ip="10.0.0.1")
        assert cfg.mac_address == "aa:bb:cc:dd:ee:ff"

    def test_invalid_mac_raises(self):
        with pytest.raises(ValueError, match="MAC"):
            WolConfig(mac_address="not-a-mac", server_ip="10.0.0.1")

    def test_empty_mac_raises(self):
        with pytest.raises(ValueError):
            WolConfig(mac_address="", server_ip="10.0.0.1")

    def test_valid_ip(self):
        cfg = WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="192.168.1.100")
        assert cfg.server_ip == "192.168.1.100"

    def test_invalid_ip_raises(self):
        with pytest.raises(ValueError, match="IPv4"):
            WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="999.999.999.999")

    def test_broadcast_address_auto_derive(self):
        cfg = WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="192.168.10.100")
        assert cfg.get_broadcast_address() == "192.168.10.255"

    def test_broadcast_address_explicit(self):
        cfg = WolConfig(
            mac_address="AA-BB-CC-DD-EE-FF",
            server_ip="192.168.10.100",
            broadcast_address="192.168.20.255",
        )
        assert cfg.get_broadcast_address() == "192.168.20.255"

    def test_broadcast_address_invalid_raises(self):
        with pytest.raises(ValueError, match="broadcast_address"):
            WolConfig(
                mac_address="AA-BB-CC-DD-EE-FF",
                server_ip="10.0.0.1",
                broadcast_address="not-an-ip",
            )

    def test_broadcast_address_empty_auto_derives(self):
        cfg = WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="10.0.0.50")
        assert cfg.get_broadcast_address() == "10.0.0.255"


# ═══════════════════════════════════════════════════════════════════
# CloudConfig
# ═══════════════════════════════════════════════════════════════════


class TestCloudConfig:
    def test_defaults(self):
        cfg = CloudConfig()
        assert cfg.storage_class == "STANDARD"
        assert cfg.bandwidth_limit == "10M"
        assert cfg.transfers == 2
        assert cfg.checkers == 4
        assert cfg.verify_timeout_seconds == 14400
        assert cfg.max_attempts == 3
        assert cfg.retry_delay_seconds == 300

    def test_valid_bucket(self):
        cfg = CloudConfig(bucket="aam-backup-test")
        assert cfg.bucket == "aam-backup-test"

    def test_invalid_bucket_raises(self):
        with pytest.raises(ValueError, match="bucket"):
            CloudConfig(bucket="Invalid Bucket!")

    def test_bucket_starts_with_hyphen_raises(self):
        with pytest.raises(ValueError):
            CloudConfig(bucket="-invalid-bucket")

    def test_storage_class_uppercased(self):
        cfg = CloudConfig(storage_class="nearline")
        assert cfg.storage_class == "NEARLINE"

    def test_invalid_storage_class_raises(self):
        with pytest.raises(ValueError, match="storage_class"):
            CloudConfig(storage_class="GLACIER")

    def test_invalid_bandwidth_raises(self):
        with pytest.raises(ValueError, match="bandwidth"):
            CloudConfig(bandwidth_limit="ten-megs")

    def test_valid_bandwidth_formats(self):
        for bw in ["10M", "500k", "1G"]:
            cfg = CloudConfig(bandwidth_limit=bw)
            assert cfg.bandwidth_limit == bw


# ═══════════════════════════════════════════════════════════════════
# LanConfig
# ═══════════════════════════════════════════════════════════════════


class TestLanConfig:
    def test_defaults(self):
        cfg = LanConfig()
        assert cfg.enabled is True
        assert cfg.retry_count == 3
        assert cfg.mt_threads == 4
        assert cfg.max_attempts == 2
        assert cfg.retry_delay_seconds == 600

    def test_all_fields_custom(self):
        cfg = LanConfig(
            enabled=False,
            retry_count=5,
            retry_wait_seconds=30,
            subprocess_timeout_seconds=7200,
            shutdown_after_backup=False,
            max_attempts=4,
            retry_delay_seconds=300,
            mt_threads=16,
        )
        assert cfg.enabled is False
        assert cfg.retry_count == 5
        assert cfg.max_attempts == 4
        assert cfg.mt_threads == 16

    def test_retry_count_bounds(self):
        assert LanConfig(retry_count=1).retry_count == 1
        assert LanConfig(retry_count=10).retry_count == 10
        with pytest.raises(ValidationError):
            LanConfig(retry_count=0)
        with pytest.raises(ValidationError):
            LanConfig(retry_count=11)


# ═══════════════════════════════════════════════════════════════════
# ScheduleConfig
# ═══════════════════════════════════════════════════════════════════


class TestScheduleConfig:
    def test_defaults(self):
        cfg = ScheduleConfig()
        assert cfg.cloud_cron == "0 18 * * *"
        assert cfg.lan_cron == "0 1 * * *"
        assert cfg.weekly_cron == "0 8 * * MON"
        assert cfg.monthly_cron == "0 8 1 * *"
        assert cfg.timezone == "Asia/Kolkata"

    def test_valid_cron(self):
        cfg = ScheduleConfig(cloud_cron="30 22 * * *")
        assert cfg.cloud_cron == "30 22 * * *"

    def test_invalid_cron_too_few_fields(self):
        with pytest.raises(ValueError, match="cron"):
            ScheduleConfig(cloud_cron="0 18 * *")

    def test_invalid_cron_too_many_fields(self):
        with pytest.raises(ValueError, match="cron"):
            ScheduleConfig(cloud_cron="0 18 * * * *")


# ═══════════════════════════════════════════════════════════════════
# NotificationConfig
# ═══════════════════════════════════════════════════════════════════


class TestNotificationConfig:
    def test_defaults(self):
        cfg = NotificationConfig()
        assert cfg.smtp_port == 587
        assert cfg.send_on_failure is True
        assert cfg.recipients == []

    def test_valid_smtp(self):
        cfg = NotificationConfig(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_username="user@gmail.com",
            smtp_password="pass",
            sender="user@gmail.com",
            recipients=["admin@test.com"],
        )
        assert cfg.smtp_host == "smtp.gmail.com"
        assert len(cfg.recipients) == 1

    def test_invalid_port_raises(self):
        with pytest.raises(ValueError):
            NotificationConfig(smtp_port=99999)

    def test_invalid_port_zero_raises(self):
        with pytest.raises(ValueError):
            NotificationConfig(smtp_port=0)

    def test_repr_hides_password(self):
        cfg = NotificationConfig(smtp_password="secret123")
        assert "secret123" not in repr(cfg)
        assert "***" in repr(cfg)


# ═══════════════════════════════════════════════════════════════════
# MaintenanceConfig
# ═══════════════════════════════════════════════════════════════════


class TestMaintenanceConfig:
    def test_defaults(self):
        cfg = MaintenanceConfig()
        assert cfg.db_retention_days == 90
        assert cfg.log_retention_days == 90
        assert cfg.sqlite_busy_timeout_ms == 30000
        assert cfg.sqlite_vacuum_freelist_threshold == 10000

    def test_valid_custom_values(self):
        cfg = MaintenanceConfig(
            db_retention_days=30,
            log_retention_days=14,
            sqlite_busy_timeout_ms=60000,
            sqlite_vacuum_freelist_threshold=5000,
        )
        assert cfg.db_retention_days == 30

    def test_retention_days_lower_bound(self):
        assert MaintenanceConfig(db_retention_days=7).db_retention_days == 7
        with pytest.raises(ValidationError):
            MaintenanceConfig(db_retention_days=6)

    def test_retention_days_upper_bound(self):
        assert MaintenanceConfig(db_retention_days=3650).db_retention_days == 3650
        with pytest.raises(ValidationError):
            MaintenanceConfig(db_retention_days=3651)


# ═══════════════════════════════════════════════════════════════════
# HealthConfig
# ═══════════════════════════════════════════════════════════════════


class TestHealthConfig:
    def test_defaults(self):
        cfg = HealthConfig()
        assert cfg.max_clock_skew_seconds == 600
        assert cfg.clock_check_timeout_seconds == 10
        assert cfg.min_free_source_gb == 1

    def test_custom_values(self):
        cfg = HealthConfig(max_clock_skew_seconds=300, min_free_source_gb=5)
        assert cfg.max_clock_skew_seconds == 300
        assert cfg.min_free_source_gb == 5


# ═══════════════════════════════════════════════════════════════════
# DashboardConfig
# ═══════════════════════════════════════════════════════════════════


class TestDashboardConfig:
    def test_auth_enabled_requires_api_key(self):
        with pytest.raises(ValueError, match="api_key"):
            DashboardConfig(auth_enabled=True, api_key="")

    def test_auth_disabled_ok_without_key(self):
        cfg = DashboardConfig(auth_enabled=False, api_key="")
        assert cfg.auth_enabled is False

    def test_default_bind_localhost(self):
        cfg = DashboardConfig(api_key="secret")
        assert cfg.bind_address == "127.0.0.1"
        assert cfg.port == 8080

    def test_custom_port(self):
        cfg = DashboardConfig(api_key="secret", port=9090)
        assert cfg.port == 9090

    def test_invalid_port_raises(self):
        with pytest.raises(ValidationError):
            DashboardConfig(api_key="secret", port=80)

    def test_repr_hides_api_key(self):
        cfg = DashboardConfig(api_key="supersecret")
        assert "supersecret" not in repr(cfg)


# ═══════════════════════════════════════════════════════════════════
# AppConfig
# ═══════════════════════════════════════════════════════════════════


class TestAppConfig:
    def test_valid_config(self, sample_yaml_config, temp_dir):
        yaml_path = temp_dir / "config.yaml"
        yaml_path.write_text(sample_yaml_config)
        cfg = load_config(str(yaml_path))
        assert cfg.firm_name == "TestFirm"
        assert cfg.cloud.bucket == "test-bucket"

    def test_neither_enabled_raises(self):
        with pytest.raises(ValueError, match="At least one"):
            AppConfig(
                firm_name="Test",
                paths=PathsConfig(
                    source_drive="C:\\data",
                    lan_destination="\\\\10.0.0.1\\share",
                    database_path="C:\\db\\test.db",
                    gcs_key_path="C:\\keys\\key.json",
                ),
                lan=LanConfig(enabled=False),
                cloud=CloudConfig(enabled=False),
                wol=WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="10.0.0.1"),
                dashboard=DashboardConfig(auth_enabled=False, api_key=""),
            )

    def test_lan_disabled_without_unc_ok(self):
        cfg = AppConfig(
            firm_name="Test",
            paths=PathsConfig(
                source_drive="C:\\data",
                lan_destination="\\\\10.0.0.1\\share",
                database_path="C:\\db\\test.db",
                gcs_key_path="C:\\keys\\key.json",
            ),
            lan=LanConfig(enabled=False),
            wol=WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="10.0.0.1"),
            dashboard=DashboardConfig(auth_enabled=False, api_key=""),
        )
        assert cfg.lan.enabled is False

    def test_fy_mismatch_raises(self):
        with pytest.raises(ValueError, match="FY"):
            AppConfig(
                firm_name="Test",
                paths=PathsConfig(
                    source_drive="E:\\SOURCE\\FY26-27",
                    lan_destination="\\\\server\\share\\FY25-26",
                    database_path="C:\\db\\test.db",
                    gcs_key_path="C:\\keys\\key.json",
                ),
                wol=WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="10.0.0.1"),
                dashboard=DashboardConfig(auth_enabled=False, api_key=""),
            )


# ═══════════════════════════════════════════════════════════════════
# load_config
# ═══════════════════════════════════════════════════════════════════


class TestLoadConfig:
    def test_valid_file(self, sample_yaml_config, temp_dir):
        yaml_path = temp_dir / "config.yaml"
        yaml_path.write_text(sample_yaml_config)
        cfg = load_config(str(yaml_path))
        assert isinstance(cfg, AppConfig)
        assert cfg.firm_name == "TestFirm"

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")

    def test_invalid_yaml_raises(self, tmp_path):
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text("{{invalid yaml: [}")
        with pytest.raises(Exception):
            load_config(str(yaml_path))

    def test_partial_sections_use_defaults(self, tmp_path):
        yaml_path = tmp_path / "minimal.yaml"
        yaml_path.write_text("""
paths:
  source_drive: "E:\\\\SOURCE"
  lan_destination: "\\\\\\\\server\\\\share"
  database_path: "/tmp/test.db"
  gcs_key_path: "/tmp/key.json"
cloud:
  enabled: true
  bucket: "test-bucket"
  project_number: "123"
  storage_class: "STANDARD"
  location: "asia-south1"
lan:
  enabled: false
wol:
  enabled: false
  server_ip: "192.168.1.1"
  mac_address: "AA:BB:CC:DD:EE:FF"
dashboard:
  auth_enabled: false
""")
        cfg = load_config(str(yaml_path))
        assert cfg.maintenance.db_retention_days == 90
        assert cfg.notifications.send_on_failure is True
        assert cfg.notifications.weekly_enabled is True
