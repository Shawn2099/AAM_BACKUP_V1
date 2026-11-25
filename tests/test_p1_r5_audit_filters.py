"""Plan P1 review amendment R5 (S4): the LAN integrity audit uses the mirror's filters.

Matrix: docs/TESTMATRIX_P1_REVIEW_2026-11-25.md section 5 (rows AU1-AU4; AU5 is the existing
test_scope_sharding_limits_checked_set in tests/test_integrity_audit.py and is not repeated).

Rule under test: audit_lan passes `--exclude` for every lan_sync.LAN_EXCLUDES entry plus
`--skip-links --ignore-case` (no shards). With scope_prefixes it passes ordered `--filter`
rules: `- <exclude>` for each exclude, then `+ <prefix>/**`, then `- **`. It never mixes
`--include` and `--exclude` (rclone ignores the excludes then, measured as fact X10).

AU1 and AU2 capture the argv of the rclone call with a fake run_tool. AU3 and AU4 run the real
rclone binary (deploy/bin/rclone.exe) local -> local on tmp_path.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core import integrity
from core.integrity import audit_lan
from core.lan_sync import LAN_EXCLUDES
from core.process import resolve_binary

RCLONE = resolve_binary("rclone")
needs_rclone = pytest.mark.skipif(not RCLONE, reason="rclone binary not available")


def _write(path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _capture_argv(source: str, dest: str, **kwargs):
    """Run audit_lan with run_tool faked; return the rclone argv it built."""
    seen = {}

    def fake_run_tool(name, argv, **kw):
        seen["argv"] = [str(a) for a in argv]
        # An incomplete run: the verdict does not matter here, only the argv.
        return SimpleNamespace(completed=False, exit_code=None, stderr_text=lambda: "",
                               describe_failure=lambda: "captured for the argv test")

    with patch.object(integrity, "run_tool", side_effect=fake_run_tool):
        audit_lan(source, dest, timeout=60, **kwargs)
    return seen["argv"]


def _values_after(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


# ── AU1 full audit argv ───────────────────────────────────────────────────

def test_au1_full_audit_excludes_every_lan_exclude_with_skip_links_and_ignore_case(tmp_path):
    argv = _capture_argv(str(tmp_path / "src"), str(tmp_path / "dst"))
    assert _values_after(argv, "--exclude") == list(LAN_EXCLUDES)
    assert "--skip-links" in argv
    assert "--ignore-case" in argv
    assert "--one-way" not in argv
    assert "--size-only" not in argv
    assert "--include" not in argv


# ── AU2 sharded audit argv: ordered filter rules ──────────────────────────

def test_au2_sharded_audit_uses_ordered_filter_rules_and_no_include_or_exclude(tmp_path):
    argv = _capture_argv(str(tmp_path / "src"), str(tmp_path / "dst"),
                         scope_prefixes=["FY26"])
    filters = _values_after(argv, "--filter")
    expected = [f"- {p}" for p in LAN_EXCLUDES] + ["+ FY26/**", "- **"]
    assert filters == expected
    assert "--include" not in argv
    assert "--exclude" not in argv
    assert "--skip-links" in argv
    assert "--ignore-case" in argv


# ── AU3 real rclone: excluded items only on the source ────────────────────

@needs_rclone
def test_au3_real_rclone_excluded_items_on_source_only_verifies(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "data.txt", b"data")
    _write(src / "$RECYCLE.BIN" / "r.txt", b"recycled")
    _write(src / "System Volume Information" / "s.txt", b"sysvol")
    _write(src / ".aam_write_probe_ab12cd", b"probe")
    _write(dst / "data.txt", b"data")
    result = audit_lan(str(src), str(dst), timeout=300)
    assert result["status"] == "VERIFIED", result["detail"]
    assert result["missing"] == []
    assert result["extra"] == []


# ── AU4 real rclone: sharded, excluded items inside the shard ─────────────

@needs_rclone
def test_au4_real_rclone_sharded_excluded_items_inside_shard_verifies(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "FY26" / "x.pdf", b"pdf-content")
    _write(src / "FY26" / "System Volume Information" / "s.txt", b"sysvol")
    _write(src / "FY26" / ".aam_write_probe_1", b"probe")
    _write(dst / "FY26" / "x.pdf", b"pdf-content")
    result = audit_lan(str(src), str(dst), scope_prefixes=["FY26"], timeout=300)
    assert result["status"] == "VERIFIED", result["detail"]


@needs_rclone
def test_au4b_real_rclone_sharded_changed_file_inside_shard_fails(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "FY26" / "x.pdf", b"pdf-content")
    _write(src / "FY26" / "System Volume Information" / "s.txt", b"sysvol")
    _write(src / "FY26" / ".aam_write_probe_1", b"probe")
    _write(dst / "FY26" / "x.pdf", b"CHANGED!!-content")
    result = audit_lan(str(src), str(dst), scope_prefixes=["FY26"], timeout=300)
    assert result["status"] == "VERIFICATION_FAILED", result["detail"]
    assert "FY26/x.pdf" in result["modified"]
