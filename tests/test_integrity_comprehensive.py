"""Comprehensive tests for core/integrity.py — audit functions and parsing."""

import pytest

from core.integrity import (
    _SUMMARY_RE,
    audit_cloud,
    audit_lan,
    new_audit_id,
    _is_canary,
    _norm_rel,
    _parse_combined_file,
)


class TestNewAuditId:
    def test_unique_ids(self):
        """Each call returns a unique ID."""
        ids = [new_audit_id("lan", "full") for _ in range(10)]
        assert len(set(ids)) == 10

    def test_id_format(self):
        """ID is a non-empty string embedding mode and scope (traceability)."""
        audit_id = new_audit_id("cloud", "full")
        assert isinstance(audit_id, str)
        assert len(audit_id) > 0
        assert "cloud" in audit_id
        assert "full" in audit_id


class TestNormRel:
    def test_backslash_path(self):
        result = _norm_rel("E:\\SOURCE\\file.txt")
        assert "\\" not in result or "/" in result

    def test_leading_slash(self):
        result = _norm_rel("/path/to/file.txt")
        assert not result.startswith("/")

    def test_double_slash(self):
        result = _norm_rel("\\\\server\\share\\file.txt")
        assert isinstance(result, str)

    def test_dot_path(self):
        result = _norm_rel("./file.txt")
        assert result == "file.txt"

    def test_empty_path(self):
        # PureWindowsPath("") normalizes to "." — the contract is a
        # non-empty posix string, never an exception.
        result = _norm_rel("")
        assert result == "."


class TestIsCanary:
    def test_canary_basename(self):
        assert _is_canary(".AAM_TARGET_MOUNTED") is True

    def test_canary_in_path(self):
        assert _is_canary("dir/.AAM_TARGET_MOUNTED") is True

    def test_non_canary(self):
        assert _is_canary("file.txt") is False


class TestSummaryRegex:
    """_SUMMARY_RE parses rclone's 'N differences found' completion verdict."""

    def test_clean_log(self):
        """Log with no differences → count 0."""
        result = _SUMMARY_RE.search("2026/01/01 00:00:00 INFO  : ... 0 differences found\n")
        assert result is not None
        assert int(result.group(1)) == 0

    def test_diverged_log(self):
        """Diverged run reports its count."""
        result = _SUMMARY_RE.search("NOTICE : Failed to check: 3 differences found")
        assert result is not None
        assert int(result.group(1)) == 3

    def test_missing_summary(self):
        """No completion summary (killed/truncated run) → no match."""
        assert _SUMMARY_RE.search("2026/01/01 00:00:00 INFO : starting check\n") is None

    def test_nonexistent_file_has_no_summary(self):
        """A nonexistent log path can never satisfy the summary requirement."""
        assert _SUMMARY_RE.search("") is None


class TestParseCombinedFile:
    # _parse_combined_file returns a 6-tuple:
    # (added, removed, modified, unchanged, errors, file_present)

    def test_all_markers(self, tmp_path):
        """All 5 marker types parsed."""
        diff_file = tmp_path / "diff.txt"
        diff_file.write_text(
            "+ added.txt\n"
            "- removed.txt\n"
            "* modified.txt\n"
            "= unchanged.txt\n"
            "! error.txt\n"
        )
        added, removed, modified, unchanged, errors, present = _parse_combined_file(str(diff_file))
        assert added == ["added.txt"]
        assert removed == ["removed.txt"]
        assert modified == ["modified.txt"]
        assert unchanged == ["unchanged.txt"]
        assert errors == ["error.txt"]
        assert present is True

    def test_missing_file(self):
        added, removed, modified, unchanged, errors, present = _parse_combined_file("/nonexistent/diff.txt")
        assert present is False
        assert (added, removed, modified, unchanged, errors) == ([], [], [], [], [])

    def test_none_argument(self):
        """None diff_file (never-created) → not present, no exception."""
        assert _parse_combined_file(None)[-1] is False

    def test_dot_leading_normalization(self, tmp_path):
        """./ prefix is normalized."""
        diff_file = tmp_path / "diff.txt"
        diff_file.write_text("+ ./file.txt\n")
        assert _parse_combined_file(str(diff_file))[0] == ["file.txt"]

    def test_too_short_lines(self, tmp_path):
        """Lines shorter than 3 chars are skipped."""
        diff_file = tmp_path / "diff.txt"
        diff_file.write_text("x\ny\nz\n")
        added, _r, _m, _u, _e, present = _parse_combined_file(str(diff_file))
        assert added == []
        assert present is True

    def test_blank_lines(self, tmp_path):
        """Blank lines are skipped."""
        diff_file = tmp_path / "diff.txt"
        diff_file.write_text("\n\n+ valid.txt\n\n")
        assert _parse_combined_file(str(diff_file))[0] == ["valid.txt"]


class TestAuditEntryPoints:
    def test_unavailable_dest_fails_closed(self, tmp_path):
        """Unavailable destination → VERIFICATION_FAILED, never an exception."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")

        result = audit_lan(
            source=str(source),
            dest=str(tmp_path / "nonexistent-dest"),
        )
        assert result is not None
        assert result["status"] == "VERIFICATION_FAILED"

    def test_missing_source(self):
        """Missing source → VERIFICATION_FAILED (fail-closed verdict dict)."""
        result = audit_lan(
            source="/nonexistent/path",
            dest="/nonexistent/dest",
        )
        assert result is not None
        assert result["status"] == "VERIFICATION_FAILED"

    def test_cloud_audit_fails_closed(self):
        """Cloud audit with an unreachable backend → VERIFICATION_FAILED."""
        result = audit_cloud(
            source="/nonexistent/path",
            bucket="no-such-bucket",
            fy_prefix="FY26-26",
            config_path="no-such-rclone.conf",
        )
        assert result is not None
        assert result["status"] == "VERIFICATION_FAILED"
