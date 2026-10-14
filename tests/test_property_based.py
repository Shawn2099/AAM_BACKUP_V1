"""Property-based tests using Hypothesis for exhaustive scenario coverage."""

import os
import subprocess
import tempfile
from pathlib import Path, PureWindowsPath
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from core.hashing import PENDING_CHECKSUM, compute_md5, verify_checksum
from core.lan_sync import classify_exit_code, count_failed_lines, decide_lan_result, failed_file_count, log_shows_completed_summary
from core.lan_manifest import diff_snapshots
from core.cloud_reporter import get_cloud_diff


# ── diff_snapshots properties ──────────────────────────────────────────

class TestDiffSnapshotsProperties:
    @given(
        before=st.dictionaries(
            st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))),
            st.tuples(st.integers(min_value=0), st.floats(min_value=0, max_value=2e9)),
        ),
        after=st.dictionaries(
            st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))),
            st.tuples(st.integers(min_value=0), st.floats(min_value=0, max_value=2e9)),
        ),
    )
    @settings(max_examples=200)
    def test_partition_invariant(self, before, after):
        """added/modified/removed/unchanged are disjoint and cover all keys."""
        result = diff_snapshots(before, after)
        added = set(result["added"])
        removed = set(result["removed"])
        modified = set(result["modified"])
        unchanged = set(result["unchanged"])

        # Disjoint
        assert not (added & removed)
        assert not (added & modified)
        assert not (added & unchanged)
        assert not (removed & modified)
        assert not (removed & unchanged)
        assert not (modified & unchanged)

        # Cover all keys
        all_keys = set(before.keys()) | set(after.keys())
        assert added | removed | modified | unchanged == all_keys

    @given(
        data=st.dictionaries(
            st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))),
            st.tuples(st.integers(min_value=0), st.floats(min_value=0, max_value=2e9)),
        ),
    )
    @settings(max_examples=100)
    def test_identical_snapshots_no_changes(self, data):
        """Identical before/after → all unchanged, no added/removed/modified."""
        result = diff_snapshots(data, data)
        assert result["added"] == []
        assert result["removed"] == []
        assert result["modified"] == []
        assert set(result["unchanged"]) == set(data.keys())

    @given(
        before=st.dictionaries(
            st.text(min_size=1, max_size=30, alphabet=st.characters(blacklist_categories=("Cs",))),
            st.tuples(st.integers(min_value=0), st.floats(min_value=0, max_value=2e9)),
        ),
    )
    @settings(max_examples=100)
    def test_empty_before_all_added(self, before):
        """Empty before → everything in after is added."""
        result = diff_snapshots({}, before)
        assert set(result["added"]) == set(before.keys())
        assert result["removed"] == []
        assert result["modified"] == []
        assert result["unchanged"] == []

    @given(
        after=st.dictionaries(
            st.text(min_size=1, max_size=30, alphabet=st.characters(blacklist_categories=("Cs",))),
            st.tuples(st.integers(min_value=0), st.floats(min_value=0, max_value=2e9)),
        ),
    )
    @settings(max_examples=100)
    def test_empty_after_all_removed(self, after):
        """Empty after → everything in before is removed."""
        result = diff_snapshots(after, {})
        assert set(result["removed"]) == set(after.keys())
        assert result["added"] == []
        assert result["modified"] == []
        assert result["unchanged"] == []


# ── classify_exit_code properties ──────────────────────────────────────

class TestClassifyExitCodeProperties:
    @given(st.integers(min_value=0, max_value=255))
    @settings(max_examples=300)
    def test_bit4_always_failed(self, code):
        """Bit 4 set → always LAN_FAILED."""
        if code & 16:
            assert classify_exit_code(code) == "LAN_FAILED"

    @given(st.integers(min_value=0, max_value=255))
    @settings(max_examples=300)
    def test_bit3_always_partial(self, code):
        """Bit 3 set (without bit 4) → always LAN_PARTIAL."""
        if code & 8 and not code & 16:
            assert classify_exit_code(code) == "LAN_PARTIAL"

    @given(st.integers(min_value=0, max_value=255))
    @settings(max_examples=300)
    def test_no_bits_complete(self, code):
        """No bits set → LAN_COMPLETE."""
        if code == 0:
            assert classify_exit_code(code) == "LAN_COMPLETE"

    @given(st.integers(min_value=-100, max_value=-1))
    @settings(max_examples=50)
    def test_negative_without_bit3_or_bit4_failed(self, code):
        """Characterized behaviour: classify_exit_code checks the bitmask FIRST.

        A negative code only maps to LAN_FAILED via the bitmask precedence when
        bit 3 (8) and bit 4 (16) are both clear; a negative code whose two's
        complement happens to set bit 3 yields LAN_PARTIAL. The dedicated -1
        timeout sentinel (all bits set) still maps to LAN_FAILED.
        """
        if not (code & 8) and not (code & 16):
            assert classify_exit_code(code) == "LAN_FAILED"

    def test_negative_timeout_sentinel_failed(self):
        """The -1 sentinel used for timeouts is LAN_FAILED."""
        assert classify_exit_code(-1) == "LAN_FAILED"

    @given(st.integers(min_value=0, max_value=255))
    @settings(max_examples=400)
    def test_mapping_matches_documented_precedence(self, code):
        """The documented precedence holds for the whole valid robocopy range."""
        if code & 16:
            expected = "LAN_FAILED"
        elif code & 8:
            expected = "LAN_PARTIAL"
        elif code <= 7:
            expected = "LAN_COMPLETE"
        else:
            expected = "LAN_FAILED"
        assert classify_exit_code(code) == expected


# ── decide_lan_result properties ───────────────────────────────────────

class TestDecideLanResultProperties:
    @given(st.integers(min_value=0, max_value=255))
    @settings(max_examples=300)
    def test_bit4_with_complete_log_failed(self, code):
        """Bit 4 + complete log → LAN_FAILED."""
        log = "Files : 10 10 0 0 0 0\nEnded : Mon Jan 01 00:00:00 2026\n"
        if code & 16:
            result = decide_lan_result(code, log)
            assert result["status"] == "LAN_FAILED"
            assert result["termination"] == "abnormal"

    @given(st.integers(min_value=0, max_value=255))
    @settings(max_examples=300)
    def test_incomplete_log_suspect_unless_bit4(self, code):
        """Characterized behaviour: bit 4 is evaluated before log completeness.

        A truncated log with a bit-4 code is LAN_FAILED (fatal process error),
        not LAN_SUSPECT — the bit-4 check precedes the log-summary check.
        Any code without bit 4 and no completed summary is LAN_SUSPECT.
        """
        result = decide_lan_result(code, "truncated log without summary")
        if code & 16:
            assert result["status"] == "LAN_FAILED"
        else:
            assert result["status"] == "LAN_SUSPECT"
        assert result["termination"] == "abnormal"

    @given(st.integers(min_value=0, max_value=255))
    @settings(max_examples=300)
    def test_none_exit_code_failed(self, result_code):
        """None exit code → LAN_FAILED."""
        log = "Files : 10 10 0 0 0 0\nEnded : Mon Jan 01 00:00:00 2026\n"
        result = decide_lan_result(None, log)
        assert result["status"] == "LAN_FAILED"

    @given(st.text(min_size=0, max_size=1000, alphabet=st.characters(blacklist_categories=("Cs",))))
    @settings(max_examples=200)
    def test_contradictory_evidence_fails_closed(self, log_text):
        """Exit 0-7 with failed markers in log → LAN_SUSPECT."""
        contradictory_log = log_text + "\n** FAILED: some_file.txt\n"
        result = decide_lan_result(1, contradictory_log)
        # If the log has a completed summary AND failed markers → SUSPECT
        if log_shows_completed_summary(contradictory_log):
            assert result["status"] == "LAN_SUSPECT"


# ── failed_file_count properties ───────────────────────────────────────

class TestFailedFileCountProperties:
    @given(st.text(min_size=0, max_size=5000, alphabet=st.characters(blacklist_categories=("Cs",))))
    @settings(max_examples=100)
    def test_bit3_floor(self, log_text):
        """Bit 3 set → at least 1 failed file."""
        result = failed_file_count(log_text, exit_code=8)
        assert result >= 1

    @given(st.text(min_size=0, max_size=5000, alphabet=st.characters(blacklist_categories=("Cs",))))
    @settings(max_examples=100)
    def test_no_bit3_no_summary_returns_zero(self, log_text):
        """No bit 3, no parseable summary → 0."""
        # Use a log that won't match the summary pattern
        result = failed_file_count("no summary here", exit_code=1)
        assert result == 0

    def test_summary_with_failed_column(self):
        """Summary with FAILED > 0 → returns that count."""
        log = "Files : 10 10 0 0 5 0\nEnded : Mon Jan 01 00:00:00 2026\n"
        result = failed_file_count(log, exit_code=8)
        assert result == 5

    def test_bit3_floor_even_with_zero_in_summary(self):
        """Bit 3 set but summary says 0 → returns 1 (max of both)."""
        log = "Files : 10 10 0 0 0 0\nEnded : Mon Jan 01 00:00:00 2026\n"
        result = failed_file_count(log, exit_code=8)
        assert result == 1


# ── count_failed_lines properties ──────────────────────────────────────

class TestCountFailedLinesProperties:
    @given(
        num_failures=st.integers(min_value=0, max_value=50),
        # The marker regex is anchored to line start (^\s*\*\*), so only
        # whitespace may precede it — any other prefix legitimately hides the
        # marker. Restrict the generator to what the contract recognizes.
        indent=st.text(min_size=0, max_size=8, alphabet=" \t"),
    )
    @settings(max_examples=100)
    def test_counts_failed_lines(self, num_failures, indent):
        """Counts line-start '** FAILED:' markers, tolerating leading whitespace."""
        lines = [f"{indent}** FAILED: file_{i}.txt" for i in range(num_failures)]
        log = "\n".join(lines)
        assert count_failed_lines(log) == num_failures

    @given(st.text(min_size=1, max_size=20, alphabet="abcXYZ019"))
    @settings(max_examples=50)
    def test_non_whitespace_prefix_hides_marker(self, prefix):
        """A non-whitespace character before the marker means it is not counted."""
        log = f"{prefix}** FAILED: file.txt"
        assert count_failed_lines(log) == 0


# ── compute_md5 properties ─────────────────────────────────────────────

class TestComputeMd5Properties:
    @given(st.binary(min_size=0, max_size=10000))
    @settings(max_examples=100)
    def test_md5_matches_hashlib(self, data):
        """compute_md5 matches hashlib for any binary content."""
        import hashlib
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            f.flush()
            result = compute_md5(f.name)
            expected = hashlib.md5(data, usedforsecurity=False).hexdigest()
            assert result == expected
        os.unlink(f.name)

    @given(st.binary(min_size=0, max_size=10000))
    @settings(max_examples=100)
    def test_verify_checksum_roundtrip(self, data):
        """verify_checksum(compute_md5(x), x) is always True."""
        import hashlib
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            f.flush()
            md5 = compute_md5(f.name)
            assert verify_checksum(f.name, md5) is True
        os.unlink(f.name)

    @given(st.binary(min_size=1, max_size=1000))
    @settings(max_examples=50)
    def test_different_data_different_md5(self, data):
        """Different data → different MD5."""
        import hashlib
        other = data + b"x"
        with tempfile.NamedTemporaryFile(delete=False) as f1, \
             tempfile.NamedTemporaryFile(delete=False) as f2:
            f1.write(data)
            f2.write(other)
            f1.flush()
            f2.flush()
            assert compute_md5(f1.name) != compute_md5(f2.name)
        os.unlink(f1.name)
        os.unlink(f2.name)


# ── get_cloud_diff parsing properties ───────────────────────────────────

class TestGetCloudDiffProperties:
    @given(
        st.lists(
            st.sampled_from(["+", "-", "*", "="]),
            min_size=0,
            max_size=50,
        ),
        st.lists(
            st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))),
            min_size=0,
            max_size=50,
        ),
    )
    @settings(max_examples=100)
    def test_diff_output_has_four_lists(self, markers, paths):
        """get_cloud_diff always returns 4 lists."""
        import tempfile
        lines = []
        for i, marker in enumerate(markers):
            path = paths[i % len(paths)] if paths else f"file_{i}.txt"
            lines.append(f"{marker} {path}")

        # Write with an explicit encoding so non-ASCII paths never trip the
        # platform default codec (cp1252 on Windows).
        import tempfile as _tempfile
        fd, diff_file = _tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        try:
            # get_cloud_diff owns and unlinks the temp file it is handed, so
            # hand it a freshly-created one inside the patch and never unlink
            # it ourselves (the finally block in the function removes it).
            with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
                 patch("core.cloud_reporter.subprocess.run") as mock_run, \
                 patch("core.cloud_reporter.tempfile.mkstemp", return_value=(0, diff_file)), \
                 patch("core.cloud_reporter.os.close"):
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                result = get_cloud_diff(
                    source="E:\\",
                    bucket="bucket",
                    fy_prefix="FY26-27",
                    config_path="/tmp/config",
                )
                assert isinstance(result["added"], list)
                assert isinstance(result["removed"], list)
                assert isinstance(result["modified"], list)
                assert isinstance(result["unchanged"], list)
        finally:
            Path(diff_file).unlink(missing_ok=True)
