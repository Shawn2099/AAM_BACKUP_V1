"""Tests for cloud_sync — rclone command building and exit code classification."""


from core.cloud_sync import build_rclone_sync_command, classify_rclone_exit


class TestClassifyRcloneExit:
    def test_zero_is_complete(self):
        assert classify_rclone_exit(0) == "CLOUD_COMPLETE"

    def test_nine_is_complete(self):
        assert classify_rclone_exit(9) == "CLOUD_COMPLETE"

    def test_one_is_failed(self):
        assert classify_rclone_exit(1) == "CLOUD_FAILED"

    def test_two_is_failed(self):
        assert classify_rclone_exit(2) == "CLOUD_FAILED"

    def test_three_is_failed(self):
        assert classify_rclone_exit(3) == "CLOUD_FAILED"

    def test_four_is_partial(self):
        assert classify_rclone_exit(4) == "CLOUD_PARTIAL"

    def test_five_is_partial(self):
        assert classify_rclone_exit(5) == "CLOUD_PARTIAL"

    def test_six_is_partial(self):
        assert classify_rclone_exit(6) == "CLOUD_PARTIAL"

    def test_seven_is_failed(self):
        assert classify_rclone_exit(7) == "CLOUD_FAILED"

    def test_eight_is_failed(self):
        assert classify_rclone_exit(8) == "CLOUD_FAILED"

    def test_ten_is_partial(self):
        assert classify_rclone_exit(10) == "CLOUD_PARTIAL"

    def test_unknown_code_defaults_to_failed(self):
        assert classify_rclone_exit(99) == "CLOUD_FAILED"

    def test_negative_defaults_to_failed(self):
        assert classify_rclone_exit(-1) == "CLOUD_FAILED"


class TestBuildRcloneSyncCommand:
    def test_basic_structure(self):
        cmd = build_rclone_sync_command(
            source="D:\\data",
            bucket="my-bucket",
            fy_prefix="FY26-27",
            config_path="/tmp/rclone.conf",
        )
        assert cmd[0] == "rclone"
        assert cmd[1] == "sync"
        assert "D:\\data" in cmd
        assert "aam_gcs:my-bucket/FY26-27" in cmd

    def test_config_path_included(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/myconfig.conf",
        )
        assert "--config" in cmd
        cfg_idx = cmd.index("--config")
        assert cmd[cfg_idx + 1] == "/tmp/myconfig.conf"

    def test_custom_transfers(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", transfers=8,
        )
        assert "--transfers" in cmd
        t_idx = cmd.index("--transfers")
        assert cmd[t_idx + 1] == "8"

    def test_custom_checkers(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", checkers=32,
        )
        assert "--checkers" in cmd
        c_idx = cmd.index("--checkers")
        assert cmd[c_idx + 1] == "32"

    def test_custom_storage_class(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="ARCHIVE",
        )
        sc_idx = cmd.index("--gcs-storage-class")
        assert cmd[sc_idx + 1] == "ARCHIVE"

    def test_default_storage_class_is_coldline(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf",
        )
        sc_idx = cmd.index("--gcs-storage-class")
        assert cmd[sc_idx + 1] == "COLDLINE"

    def test_custom_bandwidth(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", bwlimit="50M",
        )
        assert "--bwlimit" in cmd
        b_idx = cmd.index("--bwlimit")
        assert cmd[b_idx + 1] == "50M"

    def test_retry_count(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", retries=5,
        )
        r_idx = cmd.index("--retries")
        assert cmd[r_idx + 1] == "5"

    def test_gcs_no_check_bucket_present(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf",
        )
        assert "--gcs-no-check-bucket" in cmd

    def test_fast_list_present(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf",
        )
        assert "--fast-list" in cmd
