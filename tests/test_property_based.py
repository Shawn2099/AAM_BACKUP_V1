"""Property-based tests using Hypothesis for exhaustive scenario coverage."""

import os
import subprocess
import tempfile
from pathlib import Path, PureWindowsPath
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from core.hashing import PENDING_CHECKSUM, compute_md5, verify_checksum
from core.cloud_reporter import get_cloud_diff
from core.lan_sync import decide_lan_rclone_result
from test_support.fake_tools import SubprocessStyleRunTool


# ── diff_snapshots properties ──────────────────────────────────────────



# ── classify_exit_code properties ──────────────────────────────────────



# ── decide_lan_result properties ───────────────────────────────────────



# ── failed_file_count properties ───────────────────────────────────────



# ── count_failed_lines properties ──────────────────────────────────────




# ── decide_lan_rclone_result properties (plan P1 matrix D) ─────────────

class TestDecideLanRcloneResultProperties:
    @given(st.integers(min_value=-5, max_value=300).filter(lambda c: c not in (0, 1, 4, 5, 6, 10)))
    @settings(max_examples=200)
    def test_codes_outside_table_always_failed(self, code):
        assert decide_lan_rclone_result(code, "")["status"] == "LAN_FAILED"

    @given(st.integers(min_value=0, max_value=20), st.text(max_size=300, alphabet=st.characters(blacklist_categories=("Cs",))))
    @settings(max_examples=200)
    def test_never_complete_without_final_stats(self, code, log_text):
        assert decide_lan_rclone_result(code, log_text)["status"] != "LAN_COMPLETE"

    def test_unusable_exit_codes_failed(self):
        for bad in (None, -1, True):
            assert decide_lan_rclone_result(bad, "")["status"] == "LAN_FAILED"

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

        def _rclone_writes_combined(cmd, **_kw):
            # Emulate rclone: write the diff where --combined points (explicit
            # utf-8 so non-ASCII paths never trip the cp1252 default codec).
            Path(cmd[cmd.index("--combined") + 1]).write_text("\n".join(lines), encoding="utf-8")
            return MagicMock(returncode=0, stdout="", stderr="")

        # get_cloud_diff owns its real temp file (mkstemp + unlink in finally);
        # never patch mkstemp/os.close - both are global and break run_tool.
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.run_tool", new_callable=SubprocessStyleRunTool) as mock_run:
            mock_run.side_effect = _rclone_writes_combined
            result = get_cloud_diff(
                source="E:\\",
                bucket="bucket",
                fy_prefix="FY26-27",
                config_path="/tmp/config", timeout=18000, checkers=4, modify_window_seconds=2,
                retries=3, retries_sleep_seconds=10,
            )
            assert isinstance(result["added"], list)
            assert isinstance(result["removed"], list)
            assert isinstance(result["modified"], list)
            assert isinstance(result["unchanged"], list)
