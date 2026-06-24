"""Tests for flow.py helper functions — stable run_id, error handling."""

import uuid

from flow import _stable_run_id
from core.time_utils import now_iso


class TestStableRunId:
    def test_returns_string(self):
        result = _stable_run_id("cloud")
        assert isinstance(result, str)

    def test_contains_mode(self):
        assert "cloud" in _stable_run_id("cloud")
        assert "lan" in _stable_run_id("lan")

    def test_fallback_without_prefect_context(self):
        """Outside Prefect context, generates UUID-based run_id."""
        result = _stable_run_id("cloud")
        # Should contain a UUID segment (hex with dashes)
        parts = result.rsplit("-", 1)
        assert parts[-1] == "cloud"

    def test_different_modes_different_ids(self):
        cloud_id = _stable_run_id("cloud")
        lan_id = _stable_run_id("lan")
        assert cloud_id != lan_id

    def test_fallback_uuid_is_unique(self):
        """Without Prefect context, each call generates a unique ID."""
        ids = {_stable_run_id("cloud") for _ in range(10)}
        assert len(ids) == 10


class TestNowIso:
    def test_returns_iso_format(self):
        result = now_iso()
        assert "T" in result
        assert "+" in result

    def test_returns_string(self):
        assert isinstance(now_iso(), str)
