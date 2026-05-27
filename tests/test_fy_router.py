"""Tests for fy_router — fiscal year prefix calculation."""

from datetime import date

from core.fy_router import get_fy_prefix


class TestFyPrefix:
    def test_april_is_new_fy(self):
        prefix = get_fy_prefix(date(2026, 4, 1))
        assert prefix == "FY26-27"

    def test_march_is_previous_fy(self):
        prefix = get_fy_prefix(date(2026, 3, 31))
        assert prefix == "FY25-26"

    def test_midyear_july(self):
        prefix = get_fy_prefix(date(2026, 7, 15))
        assert prefix == "FY26-27"

    def test_january(self):
        prefix = get_fy_prefix(date(2026, 1, 1))
        assert prefix == "FY25-26"

    def test_december(self):
        prefix = get_fy_prefix(date(2026, 12, 31))
        assert prefix == "FY26-27"

    def test_year_2099_rollover(self):
        prefix = get_fy_prefix(date(2099, 4, 1))
        assert prefix == "FY99-00"

    def test_year_2000(self):
        prefix = get_fy_prefix(date(2000, 1, 15))
        assert prefix == "FY99-00"

    def test_september_edge(self):
        prefix = get_fy_prefix(date(2026, 9, 30))
        assert prefix == "FY26-27"

    def test_april_30(self):
        prefix = get_fy_prefix(date(2026, 4, 30))
        assert prefix == "FY26-27"

    def test_without_arg_uses_today(self):
        prefix = get_fy_prefix()
        assert prefix.startswith("FY")
        assert len(prefix) == 7
        assert "-" in prefix
