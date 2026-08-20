"""Tests for Pydantic validation of all config models."""


import pytest

from models.config import (
    AppConfig,
    CloudConfig,
    DashboardConfig,
    LanConfig,
    NotificationConfig,
    PathsConfig,
    ScheduleConfig,
    WolConfig,
    load_config,
)


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

    def test_default_values(self):
        cfg = PathsConfig(
            source_drive="D:\\data",
            lan_destination="\\\\10.0.0.1\\share",
            database_path="C:\\db\\test.db",
            gcs_key_path="C:\\keys\\key.json",
        )
        # Default log_directory is now relative to project root (or AAM_LOG_DIR env var)
        assert cfg.log_directory.endswith("logs")


class TestLanConfig:
    def test_defaults(self):
        cfg = LanConfig()
        assert cfg.enabled is True
        assert cfg.retry_count == 3
        assert cfg.mt_threads == 4  # HDD-optimized: matches 4 hardware threads

    def test_max_attempts_default(self):
        cfg = LanConfig()
        assert cfg.max_attempts == 2
        assert cfg.retry_delay_seconds == 600

    def test_custom_max_attempts(self):
        cfg = LanConfig(max_attempts=5, retry_delay_seconds=120)
        assert cfg.max_attempts == 5
        assert cfg.retry_delay_seconds == 120


class TestWolConfig:
    def test_valid_mac(self):
        cfg = WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="10.0.0.1")
        assert cfg.mac_address == "AA-BB-CC-DD-EE-FF"

    def test_invalid_mac_raises(self):
        with pytest.raises(ValueError, match="MAC"):
            WolConfig(mac_address="not-a-mac", server_ip="10.0.0.1")

    def test_invalid_ip_raises(self):
        with pytest.raises(ValueError, match="IPv4"):
            WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="999.999.999.999")

    def test_mac_with_colons(self):
        cfg = WolConfig(mac_address="aa:bb:cc:dd:ee:ff", server_ip="10.0.0.1")
        assert cfg.mac_address == "aa:bb:cc:dd:ee:ff"


class TestCloudConfig:
    def test_defaults(self):
        # F11: an enabled cloud config must name its bucket; pass one so the
        # other defaults are testable (empty-bucket default is covered in
        # tests/test_batch3_fixes.py::TestConditionalValidation).
        cfg = CloudConfig(bucket="aam-backup-test")
        assert cfg.storage_class == "STANDARD"
        assert cfg.bandwidth_limit == "10M"
        assert cfg.transfers == 2    # HDD-optimized: 2 concurrent upload slots
        assert cfg.checkers == 4    # HDD-optimized: 4 checkers = 4 CPU threads
        assert cfg.verify_timeout_seconds == 14400  # 4h: covers cold HDD reads on 500GB
        assert cfg.max_attempts == 3
        assert cfg.retry_delay_seconds == 300

    def test_invalid_bucket_raises(self):
        with pytest.raises(ValueError, match="bucket"):
            CloudConfig(bucket="Invalid Bucket!")

    def test_invalid_storage_class_raises(self):
        with pytest.raises(ValueError, match="storage_class"):
            CloudConfig(storage_class="GLACIER")

    def test_storage_class_uppercased(self):
        cfg = CloudConfig(bucket="aam-backup-test", storage_class="nearline")
        assert cfg.storage_class == "NEARLINE"

    def test_invalid_bandwidth_raises(self):
        with pytest.raises(ValueError, match="bandwidth"):
            CloudConfig(bandwidth_limit="ten-megs")


class TestScheduleConfig:
    def test_defaults(self):
        cfg = ScheduleConfig()
        assert cfg.cloud_cron == "0 18 * * *"
        assert cfg.lan_cron == "0 1 * * *"
        assert cfg.weekly_cron == "0 8 * * MON"
        assert cfg.monthly_cron == "0 8 1 * *"
        assert cfg.timezone == "Asia/Kolkata"

    def test_custom_schedule(self):
        cfg = ScheduleConfig(
            cloud_cron="30 22 * * *",
            lan_cron="0 3 * * *",
            timezone="America/New_York",
        )
        assert cfg.cloud_cron == "30 22 * * *"
        assert cfg.timezone == "America/New_York"


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


class TestNotificationConfig:
    def test_defaults(self):
        cfg = NotificationConfig()
        assert cfg.smtp_port == 587
        assert cfg.send_on_failure is True
        assert cfg.recipients == []

    def test_invalid_port_raises(self):
        with pytest.raises(ValueError):
            NotificationConfig(smtp_port=99999)


class TestAppConfig:
    def test_load_from_yaml(self, sample_yaml_config, temp_dir):
        yaml_path = temp_dir / "config.yaml"
        yaml_path.write_text(sample_yaml_config)
        cfg = load_config(str(yaml_path))
        assert cfg.firm_name == "TestFirm"
        assert cfg.cloud.bucket == "test-bucket"
        assert cfg.lan.mt_threads == 8  # yaml fixture still sets 8 explicitly
        assert cfg.lan.max_attempts == 2
        assert cfg.cloud.transfers == 4  # yaml fixture still sets 4 explicitly
        assert cfg.cloud.verify_timeout_seconds == 600  # yaml fixture still sets 600
        assert cfg.schedule.cloud_cron == "0 18 * * *"
        assert cfg.dashboard.api_key == "test-key-123"

    def test_lan_disabled_without_unc_ok(self):
        """When LAN is disabled, no UNC validation needed."""
        cfg = AppConfig(
            firm_name="Test",
            paths=PathsConfig(
                source_drive="C:\\data",
                lan_destination="\\\\10.0.0.1\\share",
                database_path="C:\\db\\test.db",
                gcs_key_path="C:\\keys\\key.json",
            ),
            lan=LanConfig(enabled=False),
            cloud=CloudConfig(bucket="aam-backup-test"),
            wol=WolConfig(mac_address="AA-BB-CC-DD-EE-FF", server_ip="10.0.0.1"),
            dashboard=DashboardConfig(auth_enabled=False, api_key=""),
        )
        assert cfg.lan.enabled is False

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


class TestUnknownRootKeyWarning:
    """M8/S2-26: Pydantic's default extra='ignore' silently drops unknown
    keys. The DEPLOYMENT_GUIDE shipped typo'd section names ('notification'
    instead of 'notifications', 'email_from'/'email_to' instead of
    'sender'/'recipients') — a deployment built from the guide had alerts
    silently misconfigured while 05_test_config printed SUCCESS.

    The fix is a LOAD-TIME WARNING on unknown TOP-LEVEL keys: the load must
    still succeed (existing configs with legacy section-level keys like
    notifications.send_on_success must keep loading — that silence is
    deliberate and documented in the model), but the operator sees the
    unknown key named at startup instead of discovering it as a missing
    email three weeks later."""

    _VALID_MINIMAL = (
        'firm_name: "T"\n'
        "paths:\n"
        '  source_drive: "C:\\\\src"\n'
        '  lan_destination: "\\\\\\\\10.0.0.1\\\\share"\n'
        '  database_path: "C:\\\\t\\\\m.db"\n'
        '  log_directory: "C:\\\\t\\\\logs"\n'
        '  gcs_key_path: "C:\\\\t\\\\k.json"\n'
        "cloud:\n"
        "  enabled: true\n"
        '  bucket: "aam-test-bucket"\n'
        "lan:\n"
        "  enabled: false\n"
        "wol:\n"
        '  mac_address: "AA-BB-CC-DD-EE-FF"\n'
        '  server_ip: "10.0.0.1"\n'
        "dashboard:\n"
        "  auth_enabled: false\n"
    )

    def test_unknown_root_key_warns(self, temp_dir, capture_logs):
        text = self._VALID_MINIMAL + (
            "notification:\n"  # ← the guide's typo; real section is 'notifications'
            '  smtp_host: "smtp.example.com"\n'
        )
        p = temp_dir / "config.yaml"
        p.write_text(text, encoding="utf-8")

        # Must still load (no crash) — warnings, not rejections.
        cfg = load_config(str(p))
        assert cfg.firm_name == "T"

        logs = capture_logs.getvalue()
        assert "notification" in logs
        assert "unknown" in logs.lower()
        assert "ignor" in logs.lower()  # the key will be ignored
        assert "notifications" in logs  # suggests the correct name

    def test_unknown_root_key_multiple_warns_all(self, temp_dir, capture_logs):
        text = self._VALID_MINIMAL + (
            "bogus_section:\n  x: 1\n"
            "maintenance2:\n  y: 2\n"
        )
        p = temp_dir / "config.yaml"
        p.write_text(text, encoding="utf-8")
        load_config(str(p))
        logs = capture_logs.getvalue()
        assert "bogus_section" in logs
        assert "maintenance2" in logs

    def test_valid_config_no_unknown_key_warning(self, temp_dir, capture_logs, sample_yaml_config):
        p = temp_dir / "config.yaml"
        p.write_text(sample_yaml_config, encoding="utf-8")
        load_config(str(p))
        assert "unknown top-level" not in capture_logs.getvalue().lower()
