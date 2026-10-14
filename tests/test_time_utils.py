"""Tests for core/time_utils.py — all functions."""

import pendulum
import pytest

from core.time_utils import (
    IST,
    cron_to_human,
    cutoff_iso,
    get_fy_prefix,
    now_formatted,
    now_iso,
)


class TestGetFyPrefix:
    def test_format(self):
        """FY prefix matches FYxx-xx format."""
        result = get_fy_prefix()
        assert result.startswith("FY")
        assert "-" in result
        assert len(result) == 7  # FY26-27

    def test_current_fy(self):
        """Returns current fiscal year."""
        result = get_fy_prefix()
        now = pendulum.now()
        if now.month >= 4:  # FY starts April
            expected_fy = f"FY{str(now.year % 100).zfill(2)}-{(now.year + 1) % 100:02d}"
        else:
            expected_fy = f"FY{str((now.year - 1) % 100).zfill(2)}-{now.year % 100:02d}"
        assert result == expected_fy


class TestCronToHuman:
    def test_daily_cron(self):
        result = cron_to_human("0 18 * * *", "Asia/Kolkata")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_weekly_cron(self):
        result = cron_to_human("0 8 * * MON", "Asia/Kolkata")
        assert isinstance(result, str)

    def test_timezone_aware(self):
        result = cron_to_human("0 18 * * *", "America/New_York")
        assert isinstance(result, str)


class TestNowIso:
    def test_format(self):
        result = now_iso()
        assert isinstance(result, str)
        assert "T" in result  # ISO format


class TestNowFormatted:
    def test_format(self):
        result = now_formatted()
        assert isinstance(result, str)
        assert len(result) > 0


class TestCutoffIso:
    def test_format(self):
        result = cutoff_iso(7)
        assert isinstance(result, str)
        assert "T" in result
        assert result.endswith("+05:30")  # always IST, explicit offset


class TestISTConstant:
    def test_ist_timezone(self):
        assert IST is not None
        assert str(IST) == "Asia/Kolkata"
