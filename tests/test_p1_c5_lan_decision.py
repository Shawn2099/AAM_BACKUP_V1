"""Plan P1 chunk C5: LAN exit decision, core/lan_sync.decide_lan_rclone_result.

Matrix rows D1-D16 (docs/PLAN_P1_RCLONE_LAN_2026-11-25.md, "D decision (U)" and
§2.2). The rules table of §2.2 is applied in order; each row is tested.

Real fixtures (test_support/rclone_logs): lan_clean.jsonl, lan_nochange.jsonl,
lan_maxdelete.jsonl (exit 7), locked_file_exit1.jsonl, recovered_retry_exit0.jsonl,
max_duration_exit10.jsonl (exit 10). Everything else is a small synthetic
JSON-lines log built below, in the shape rclone --use-json-log writes.
"""

import json
from pathlib import Path

import pytest

import core.lan_sync as lan_sync

FIXTURES = Path(__file__).resolve().parents[1] / "test_support" / "rclone_logs"
CONFIG_NOTICE = {"level": "notice",
                 "msg": "Config file \"X:/rclone.conf\" not found - using defaults",
                 "source": "config/config.go:373"}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _log(*entries: dict) -> str:
    return "\n".join(json.dumps(e) for e in entries)


def _stats(**overrides) -> dict:
    """rclone's final stats line (the LAST JSON line carrying "stats")."""
    stats = {"bytes": 0, "checks": 0, "deletedDirs": 0, "deletes": 0, "errors": 0,
             "fatalError": False, "renames": 0, "retryError": False, "transfers": 0}
    stats.update(overrides)
    return {"level": "info", "msg": "\nTransferred:   0 B", "stats": stats}


def _error(msg: str, **extra) -> dict:
    return {"level": "error", "msg": msg, **extra}


def _decide(exit_code, text, **kwargs):
    return lan_sync.decide_lan_rclone_result(exit_code, text, **kwargs)


# ── D1-D2: exit 0, clean ────────────────────────────────────────────

class TestD1CleanComplete:
    def test_clean_fixture_is_complete_and_counts_map_through(self):
        result = _decide(0, _fixture("lan_clean.jsonl"))
        assert result["status"] == "LAN_COMPLETE"
        assert result["stats"]["transfers"] == 12
        assert result["stats"]["bytes"] == 13062
        assert result["stats"]["deletes"] == 0
        assert result["abnormal"] is False
        assert result["retryable"] is False
        assert result["max_delete_reached"] is False
        assert result["time_limit_stopped"] is False
        assert result["files_failed"] == 0
        assert result["failed_paths"] == []
        assert result["deletes_held"] is False


class TestD2NoChangeComplete:
    def test_nochange_fixture_is_complete_with_no_changes_reason(self):
        result = _decide(0, _fixture("lan_nochange.jsonl"))
        assert result["status"] == "LAN_COMPLETE"
        assert "no changes" in result["reason"].lower()
        assert result["stats"]["transfers"] == 0
        assert result["stats"]["deletes"] == 0
        assert result["stats"]["renames"] == 0


# ── D3-D4: exit 0 contradicted ──────────────────────────────────────

class TestD3ExitZeroWithoutStats:
    def test_exit_zero_without_final_stats_is_suspect(self):
        text = _log(CONFIG_NOTICE, {"level": "info", "msg": "Checks finished"})
        result = _decide(0, text)
        assert result["status"] == "LAN_SUSPECT"
        assert "no final stats" in result["reason"].lower()
        assert result["stats"] is None


class TestD4ExitZeroWithErrors:
    def test_error_line_on_exit_zero_is_suspect(self):
        text = _log(CONFIG_NOTICE,
                    _error("something went wrong while copying"),
                    _stats())
        result = _decide(0, text)
        assert result["status"] == "LAN_SUSPECT"

    def test_nonzero_stats_errors_on_exit_zero_is_suspect(self):
        text = _log(CONFIG_NOTICE, _stats(errors=2))
        result = _decide(0, text)
        assert result["status"] == "LAN_SUSPECT"


# ── D5: earlier attempt failed, final attempt succeeded ─────────────

class TestD5RecoveredRetryComplete:
    def test_attempt_failed_then_succeeded_is_complete(self):
        result = _decide(0, _fixture("recovered_retry_exit0.jsonl"))
        assert result["status"] == "LAN_COMPLETE"
        assert result["files_failed"] == 0
        assert result["stats"]["errors"] == 0


# ── D6: locked file, exit 1 with stats ──────────────────────────────

class TestD6LockedFileIsPartial:
    def test_locked_fixture_is_partial_with_one_failed_path(self):
        result = _decide(1, _fixture("locked_file_exit1.jsonl"))
        assert result["status"] == "LAN_PARTIAL"
        assert result["files_failed"] == 1
        assert result["failed_paths"] == ["locked"]
        assert result["deletes_held"] is True


# ── D7-D8: fatal and no-stats failures ──────────────────────────────

class TestD7FatalErrorIsFailed:
    def test_fatal_error_flag_is_failed_and_not_retryable(self):
        text = _log(CONFIG_NOTICE, _error("Failed to sync: fatal"),
                    _stats(errors=1, fatalError=True, lastError="fatal"))
        result = _decide(1, text)
        assert result["status"] == "LAN_FAILED"
        assert result["retryable"] is False


class TestD8NonzeroWithoutStatsIsFailedNotRetryable:
    def test_nonzero_exit_without_stats_is_failed_abnormal(self):
        text = _log(CONFIG_NOTICE, _error("Failed to sync: something"))
        result = _decide(1, text)
        assert result["status"] == "LAN_FAILED"
        assert result["abnormal"] is True
        assert result["retryable"] is False


# ── D9: exit 5 retryable ────────────────────────────────────────────

class TestD9ExitFiveIsRetryableFailed:
    def test_exit_five_with_stats_is_failed_and_retryable(self):
        text = _log(CONFIG_NOTICE, _error("temporary network error"),
                    _stats(errors=1, retryError=True, fatalError=False))
        result = _decide(5, text)
        assert result["status"] == "LAN_FAILED"
        assert result["retryable"] is True


# ── D10-D11: --max-delete backstop, and exit 7 otherwise ────────────

class TestD10MaxDeleteBackstop:
    def test_max_delete_fixture_is_failed_with_backstop_flag(self):
        result = _decide(7, _fixture("lan_maxdelete.jsonl"))
        assert result["status"] == "LAN_FAILED"
        assert result["max_delete_reached"] is True
        assert result["retryable"] is False

    def test_max_delete_exit_seven_capture_is_failed_with_backstop_flag(self):
        result = _decide(7, _fixture("max_delete_exit7.jsonl"))
        assert result["status"] == "LAN_FAILED"
        assert result["max_delete_reached"] is True
        assert result["retryable"] is False


class TestD11ExitSevenWithoutBackstopIsFailed:
    def test_exit_seven_without_max_delete_message_is_failed_not_backstop(self):
        text = _log(CONFIG_NOTICE,
                    _error("Got fatal error on delete: something else", fatalError=True),
                    _stats(deletes=0, errors=1, fatalError=True, lastError="other fatal"))
        result = _decide(7, text)
        assert result["status"] == "LAN_FAILED"
        assert result["max_delete_reached"] is False


# ── D12: exits 2, 3, 8, 9 ───────────────────────────────────────────

class TestD12UnexpectedExitsAreFailed:
    @pytest.mark.parametrize("code", [2, 3, 8, 9])
    def test_unexpected_exit_codes_are_failed(self, code):
        text = _log(CONFIG_NOTICE, _stats())
        result = _decide(code, text)
        assert result["status"] == "LAN_FAILED"
        assert result["retryable"] is False

    def test_exit_nine_reason_says_unexpected_on_lan(self):
        result = _decide(9, _log(CONFIG_NOTICE, _stats()))
        assert "unexpected" in result["reason"].lower()


# ── D13: abnormal exit values ───────────────────────────────────────

class TestD13AbnormalExitValues:
    @pytest.mark.parametrize("code", [None, -1, True, False])
    def test_non_int_or_negative_exit_is_failed_abnormal(self, code):
        result = _decide(code, _fixture("lan_clean.jsonl"))
        assert result["status"] == "LAN_FAILED"
        assert result["abnormal"] is True


# ── D14: duration limit ─────────────────────────────────────────────

class TestD14DurationLimitIsPartial:
    def test_exit_ten_is_partial_with_time_limit_stopped(self):
        result = _decide(10, _fixture("max_duration_exit10.jsonl"))
        assert result["status"] == "LAN_PARTIAL"
        assert result["time_limit_stopped"] is True


# ── D15-D16: locked or read errors, PARTIAL vs deletes held ─────────

class TestD15NoSpaceIsPartial:
    def test_exit_one_with_not_enough_space_is_partial(self):
        text = _log(CONFIG_NOTICE,
                    _error("Failed to copy: write big.bin: There is not enough space on the disk.",
                           object="big.bin"),
                    _stats(errors=1, fatalError=False))
        result = _decide(1, text)
        assert result["status"] == "LAN_PARTIAL"


class TestD16DeletesHeldFlag:
    def test_exit_one_without_not_deleting_marker_has_deletes_held_false(self):
        text = _log(CONFIG_NOTICE,
                    _error("Failed to copy: read x: access denied", object="x"),
                    _stats(errors=1, fatalError=False))
        result = _decide(1, text)
        assert result["status"] == "LAN_PARTIAL"
        assert result["deletes_held"] is False
