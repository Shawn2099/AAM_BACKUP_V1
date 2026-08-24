"""P1-EXIT9 - RED tests: exit 9 must not mask fatal rclone errors.

Evidence: CLOUD-06/07 ledger rows - nonexistent bucket produced exit 9 which
was mapped to CLOUD_NO_CHANGES_COMPLETE even though stderr carried fatal
errors. Fix: cross-check the captured --use-json-log stream before trusting
exit 9 (plan v1.2/R1: also flag non-JSON 'Failed to' lines from log.Fatalf
paths).
"""
from core.cloud_sync import classify_rclone_exit, scan_rclone_log_for_errors


INFO_ONLY_LOG = "\n".join([
    '{"level":"INFO","msg":"Start sync","time":"2026-08-24T10:00:00Z"}',
    '{"level":"INFO","msg":"There was nothing to transfer","time":"2026-08-24T10:00:01Z"}',
]) + "\n"

ERROR_JSON_LOG = INFO_ONLY_LOG.replace(
    '"level":"INFO","msg":"There was nothing to transfer"',
    '"level":"ERROR","msg":"Failed to list bucket: bucket doesn\'t exist"',
)

FATAL_PLAINTEXT_LOG = INFO_ONLY_LOG + (
    "Failed to create file system for \"aam_gcs:typo-bucket/FY26-27\": "
    "couldn't find bucket\n"
)


def test_scan_clean_info_log_has_no_error():
    has_err, _tail = scan_rclone_log_for_errors(INFO_ONLY_LOG)
    assert has_err is False


def test_scan_json_error_level_is_flagged():
    has_err, tail = scan_rclone_log_for_errors(ERROR_JSON_LOG)
    assert has_err is True
    assert "bucket" in tail.lower()


def test_scan_nonjson_fatal_line_is_flagged():
    """R1 hardening: some fatal errors bypass the JSON logger entirely."""
    has_err, tail = scan_rclone_log_for_errors(FATAL_PLAINTEXT_LOG)
    assert has_err is True
    assert "Failed to create file system" in tail


def test_scan_empty_and_garbage_are_not_errors():
    has_err, _ = scan_rclone_log_for_errors("")
    assert has_err is False
    # garbage that is neither JSON nor a known fatal pattern is NOT flagged
    has_err, _ = scan_rclone_log_for_errors("some random notice text\n")
    assert has_err is False


def test_classifier_mapping_unchanged():
    assert classify_rclone_exit(9) == "CLOUD_NO_CHANGES_COMPLETE"
    assert classify_rclone_exit(0) == "CLOUD_COMPLETE"
