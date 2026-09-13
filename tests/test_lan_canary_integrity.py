"""LAN canary/control-plane contract regression suite (v1.1.3 defect).

Encodes the contract established in Phase 0
(C:\\ChaosTest\\evidence\\LAN_CANARY_CONTRACT_ANALYSIS.md):

  * `<destination-root>/.AAM_TARGET_MOUNTED` is a CONTROL-PLANE mount
    sentinel. Pre-flight (core/lan_preflight.py) requires it at the
    destination ROOT and refuses to mirror without it.
  * The sentinel is NOT backup content. Every `/MIR` transfer excludes the
    bare basename via robocopy `/XF .AAM_TARGET_MOUNTED`
    (core/lan_sync.py:build_robocopy_command + the `/L` dry run), and
    robocopy `/XF` matches a filename in EVERY directory. The transfer
    therefore never copies, updates, or purges this basename at any depth,
    on either side.
  * The weekly integrity audit (core/integrity.py) must mirror that
    exclusion, or it demands from the content comparison exactly what the
    transfer is forbidden to deliver — a permanently unactionable failure.

Two-part semantics these tests pin down (they are NOT the same thing):

  1. CONTROL-PLANE validity is ROOT-ONLY. A nested
     `<dest>/sub/.AAM_TARGET_MOUNTED` does NOT satisfy pre-flight.
  2. VERDICT exclusion is BASENAME-ANY-DEPTH, because that is what the
     transfer's `/XF` actually does. A nested marker is excluded from the
     content verdict as non-convergent operational layout — it is never
     promoted to "valid control marker".

Anti-suppression is a first-class requirement here: every test that mixes a
canary with a REAL divergence must prove the real divergence still fails
the audit and is still visible in the evidence.
"""

import shutil
from pathlib import Path

import pytest

from core import integrity
from core.health import HealthError

MARKER = ".AAM_TARGET_MOUNTED"


# ─────────────────────────────────────────────────────────────────
# Harness — drive _run_rclone_check with a canned rclone result
# ─────────────────────────────────────────────────────────────────

class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=1, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _invoke_check(monkeypatch, diff_rows, *, returncode=None, summary=None):
    """Run core.integrity._run_rclone_check against a canned combined diff.

    `diff_rows` is a list of (marker, relative_path) with marker in
    {+, -, *, =, !} exactly as `rclone check --combined` emits them
    ("<marker> <path>").

    The fake subprocess writes the combined file into the SAME temp path the
    production code created via mkstemp (read out of the `--combined`
    argument), so the real parse/filter path is exercised end to end and no
    tempfile internals are patched.
    """
    body = "".join(f"{marker} {rel}\n" for marker, rel in diff_rows)
    raw_count = sum(1 for marker, _ in diff_rows if marker in ("+", "-", "*"))

    if returncode is None:
        returncode = 1 if raw_count else 0
    if summary is None:
        # rclone's own summary counts every difference it saw, canary included.
        summary = raw_count

    def fake_run(cmd, **kwargs):
        idx = cmd.index("--combined")
        Path(cmd[idx + 1]).write_text(body, encoding="utf-8")
        return _FakeCompleted(returncode, "", f"{summary} differences found")

    monkeypatch.setattr(integrity, "resolve_binary", lambda _name: "rclone")
    monkeypatch.setattr(integrity.subprocess, "run", fake_run)
    return integrity._run_rclone_check("SRC", "DST", "lan/full", 60)


def _preflight(monkeypatch, dest: Path, source: Path | None = None) -> dict:
    """Run LAN pre-flight against a LOCAL destination, stubbing robocopy.

    Local (non-UNC) paths skip the SMB probe, so this exercises the canary
    gate directly. Returns the robocopy invocations that were attempted, so
    tests can assert the gate refused BEFORE any mirror was built.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted(0, "", "")

    monkeypatch.setattr("core.lan_preflight.subprocess.run", fake_run)
    from core.lan_preflight import run_lan_dry_run

    src = source or dest
    try:
        result = run_lan_dry_run(str(src), str(dest))
    except HealthError as exc:
        return {"health_error": exc, "calls": calls}
    return {"result": result, "calls": calls}


# ─────────────────────────────────────────────────────────────────
# TEST 1 — Valid destination-root marker
# ─────────────────────────────────────────────────────────────────

def test_1a_preflight_passes_with_destination_root_marker(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / MARKER).touch()

    out = _preflight(monkeypatch, dest)
    assert "health_error" not in out, f"pre-flight unexpectedly failed: {out}"
    assert out["result"]["ok"] is True
    assert len(out["calls"]) == 1, "robocopy dry-run should have been attempted"


def test_1b_dest_root_marker_is_not_reported_as_backup_content(monkeypatch):
    """The production-shaped case: source has data, dest has data + the
    root sentinel (which /XF never copies). The sentinel shows up as an
    extra-in-dest row and must NOT fail the content comparison."""
    result = _invoke_check(
        monkeypatch,
        [("=", "Accounting/Q1.xlsx"), ("-", MARKER)],
    )
    assert result["status"] == "VERIFIED", result["detail"]
    assert result["mismatches"] == 0
    assert result["extra"] == []
    assert result["canary_excluded"] == 1
    assert result["raw_extra"] == 1
    assert result["raw_mismatches"] == 1


def test_1c_canary_does_not_trip_the_summary_consistency_gate(monkeypatch):
    """rclone's own summary counts the sentinel; the gate must compare it
    against the RAW count, never the filtered one."""
    result = _invoke_check(monkeypatch, [("-", MARKER)], summary=1, returncode=1)
    assert result["status"] == "VERIFIED", result["detail"]
    assert "contradictory" not in result["detail"]


# ─────────────────────────────────────────────────────────────────
# TEST 2 — Missing destination-root marker
# ─────────────────────────────────────────────────────────────────

def test_2_missing_root_marker_fails_preflight(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "Accounting").mkdir()
    (dest / "Accounting" / "Q1.xlsx").write_text("data")

    out = _preflight(monkeypatch, dest)
    assert "health_error" in out, "pre-flight MUST fail without the root marker"
    assert isinstance(out["health_error"], HealthError)
    assert out["calls"] == [], "no mirror may be attempted without the canary"


def test_2b_integrity_fix_does_not_weaken_preflight(tmp_path, monkeypatch):
    """Regression guard for the fix itself: a missing root marker is still a
    hard pre-flight failure even though the audit now ignores the basename."""
    dest = tmp_path / "dest"
    dest.mkdir()
    out = _preflight(monkeypatch, dest)
    assert "health_error" in out
    assert out["calls"] == []


# ─────────────────────────────────────────────────────────────────
# TESTS 3-5 — Genuine divergences must still fail (no suppression)
# ─────────────────────────────────────────────────────────────────

def test_3_real_missing_backup_file_fails(monkeypatch):
    result = _invoke_check(monkeypatch, [("=", "a.txt"), ("+", "missing.txt")])
    assert result["status"] == "VERIFICATION_FAILED"
    assert result["mismatches"] == 1
    assert "missing.txt" in result["missing"]
    assert result["canary_excluded"] == 0


def test_4_real_extra_destination_file_fails(monkeypatch):
    result = _invoke_check(monkeypatch, [("=", "a.txt"), ("-", "unexpected.txt")])
    assert result["status"] == "VERIFICATION_FAILED"
    assert result["mismatches"] == 1
    assert "unexpected.txt" in result["extra"]
    assert result["canary_excluded"] == 0


def test_5_real_content_modification_fails(monkeypatch):
    result = _invoke_check(monkeypatch, [("=", "a.txt"), ("*", "changed.txt")])
    assert result["status"] == "VERIFICATION_FAILED"
    assert result["mismatches"] == 1
    assert "changed.txt" in result["modified"]
    assert result["canary_excluded"] == 0


# ─────────────────────────────────────────────────────────────────
# TESTS 6-8 — Canary + genuine divergence (REQUIRED anti-suppression)
# ─────────────────────────────────────────────────────────────────

def test_6_canary_plus_genuine_missing_file_still_fails(monkeypatch):
    result = _invoke_check(
        monkeypatch,
        [("-", MARKER), ("+", "missing.txt")],
    )
    assert result["status"] == "VERIFICATION_FAILED", result["detail"]
    # The real missing file must remain visible in the mismatch evidence.
    assert "missing.txt" in result["missing"]
    assert MARKER not in result["missing"]
    assert result["canary_excluded"] == 1
    # raw_* are per-direction raw counts: the canary here is a `-` row, so
    # the raw extra count is 2 (canary + real extra) while raw_missing is 1.
    assert result["raw_missing"] == 1
    assert result["raw_extra"] == 1
    assert result["raw_mismatches"] == 2
    assert result["mismatches"] == 1
    assert "canary_excluded=1" in result["detail"]


def test_7_canary_plus_genuine_extra_file_still_fails(monkeypatch):
    result = _invoke_check(
        monkeypatch,
        [("-", MARKER), ("-", "unexpected.txt")],
    )
    assert result["status"] == "VERIFICATION_FAILED", result["detail"]
    assert "unexpected.txt" in result["extra"]
    assert MARKER not in result["extra"]
    assert result["canary_excluded"] == 1
    assert result["raw_extra"] == 2
    assert result["mismatches"] == 1


def test_8_canary_plus_genuine_content_change_still_fails(monkeypatch):
    result = _invoke_check(
        monkeypatch,
        [("+", f"FY26-27/{MARKER}"), ("*", "Accounting/changed.xlsx")],
    )
    assert result["status"] == "VERIFICATION_FAILED", result["detail"]
    assert "Accounting/changed.xlsx" in result["modified"]
    assert result["canary_excluded"] == 1
    assert result["raw_changed"] == 1
    assert result["raw_missing"] == 1
    assert result["mismatches"] == 1


# ─────────────────────────────────────────────────────────────────
# TEST 9 — Nested canary (two-part contract)
# ─────────────────────────────────────────────────────────────────

def test_9a_nested_canary_is_excluded_from_the_content_verdict(monkeypatch):
    """Excluded as non-convergent operational layout — the transfer's /XF is
    basename-global, so /MIR can never bring this row into agreement."""
    nested = f"some/subdirectory/{MARKER}"
    result = _invoke_check(monkeypatch, [("=", "a.txt"), ("+", nested)])
    assert result["status"] == "VERIFIED", result["detail"]
    assert result["mismatches"] == 0
    assert result["missing"] == []
    assert result["canary_excluded"] == 1
    assert result["canary_excluded_paths"] == [nested]
    assert result["raw_missing"] == 1


def test_9b_nested_canary_does_NOT_satisfy_preflight(tmp_path, monkeypatch):
    """Control-plane validity is ROOT-ONLY: a nested file with the same
    basename is never accepted as the mount sentinel."""
    dest = tmp_path / "dest"
    (dest / "some" / "subdirectory").mkdir(parents=True)
    (dest / "some" / "subdirectory" / MARKER).touch()
    assert not (dest / MARKER).exists()

    out = _preflight(monkeypatch, dest)
    assert "health_error" in out, "nested marker must NOT satisfy pre-flight"
    assert out["calls"] == []


# ─────────────────────────────────────────────────────────────────
# TEST 10 — Source-side canary
# ─────────────────────────────────────────────────────────────────

def test_10_source_side_root_canary_is_excluded_from_verdict(monkeypatch):
    """A source-side sentinel can never be delivered (/XF blocks it), so the
    audit must not report it as missing-from-dest backup content. This is the
    exact shape of the reported false positive."""
    result = _invoke_check(monkeypatch, [("=", "a.txt"), ("+", MARKER)])
    assert result["status"] == "VERIFIED", result["detail"]
    assert result["mismatches"] == 0
    assert result["missing"] == []
    assert result["canary_excluded"] == 1
    assert result["raw_missing"] == 1


def test_10b_source_side_nested_canary_is_excluded_from_verdict(monkeypatch):
    """The originally reported failure: paths=['E2E_TEST_FY/.AAM_TARGET_MOUNTED']
    with missing-from-dest=1. Must now be VERIFIED."""
    reported = f"E2E_TEST_FY/{MARKER}"
    result = _invoke_check(monkeypatch, [("+", reported)], summary=1, returncode=1)
    assert result["status"] == "VERIFIED", result["detail"]
    assert result["mismatches"] == 0
    assert result["canary_excluded_paths"] == [reported]
    assert "content mismatch detected" not in result["detail"]


def test_10c_source_side_canary_does_not_provision_the_destination(tmp_path, monkeypatch):
    """A source-side marker is not backup content AND is not a destination
    marker: pre-flight still fails when the destination root lacks one."""
    dest = tmp_path / "dest"
    dest.mkdir()
    source = tmp_path / "src"
    source.mkdir()
    (source / MARKER).touch()

    out = _preflight(monkeypatch, dest, source=source)
    assert "health_error" in out
    assert out["calls"] == []


# ─────────────────────────────────────────────────────────────────
# TEST 11 — Multiple canaries
# ─────────────────────────────────────────────────────────────────

def test_11a_multiple_canaries_alone_verify(monkeypatch):
    result = _invoke_check(
        monkeypatch,
        [
            ("-", MARKER),
            ("+", f"FY26-27/{MARKER}"),
            ("*", f"Accounting/{MARKER}"),
        ],
    )
    assert result["status"] == "VERIFIED", result["detail"]
    assert result["mismatches"] == 0
    assert result["canary_excluded"] == 3
    assert result["raw_mismatches"] == 3
    assert sorted(result["canary_excluded_paths"]) == sorted(
        [MARKER, f"FY26-27/{MARKER}", f"Accounting/{MARKER}"]
    )


def test_11b_multiple_canaries_plus_genuine_missing_still_fail(monkeypatch):
    result = _invoke_check(
        monkeypatch,
        [
            ("-", MARKER),
            ("+", f"FY26-27/{MARKER}"),
            ("*", f"Accounting/{MARKER}"),
            ("+", "missing.txt"),
        ],
    )
    assert result["status"] == "VERIFICATION_FAILED", result["detail"]
    assert result["missing"] == ["missing.txt"]
    assert result["canary_excluded"] == 3
    assert result["raw_missing"] == 2  # nested canary + the real missing file
    assert result["mismatches"] == 1


# ─────────────────────────────────────────────────────────────────
# Read/check errors are NEVER suppressed by canary handling
# ─────────────────────────────────────────────────────────────────

def test_error_rows_are_never_treated_as_canary(monkeypatch):
    result = _invoke_check(
        monkeypatch,
        [("=", "a.txt"), ("!", f"Accounting/{MARKER}")],
    )
    assert result["status"] == "VERIFICATION_FAILED", result["detail"]
    assert result["errors"] == [f"Accounting/{MARKER}"]
    assert result["canary_excluded"] == 0


def test_only_the_exact_basename_is_excluded(monkeypatch):
    """No basename sweep and no dotfile sweep: near-miss names stay content."""
    near_misses = [
        "a.txt",
        ".AAM_TARGET_MOUNTED.txt",
        "AAM_TARGET_MOUNTED",
        ".aam_target_mounted",
        ".AAM_TARGET_MOUNTED.bak",
        ".other_dotfile",
    ]
    rows = [("+", p) for p in near_misses]
    result = _invoke_check(monkeypatch, rows)
    assert result["status"] == "VERIFICATION_FAILED"
    assert result["mismatches"] == len(near_misses)
    assert result["canary_excluded"] == 0
    assert sorted(result["missing"]) == sorted(near_misses)


# ─────────────────────────────────────────────────────────────────
# Result schema / evidence preservation (Phase 3)
# ─────────────────────────────────────────────────────────────────

def test_evidence_keys_exist_on_every_result(monkeypatch):
    ok = _invoke_check(monkeypatch, [("=", "a.txt")])
    for key in (
        "raw_missing", "raw_extra", "raw_changed", "raw_mismatches",
        "canary_excluded", "canary_excluded_paths", "modified",
    ):
        assert key in ok, f"missing evidence key {key} on the success path"

    bad = _invoke_check(monkeypatch, [("+", "missing.txt")])
    for key in ("raw_missing", "raw_mismatches", "canary_excluded", "modified"):
        assert key in bad, f"missing evidence key {key} on the mismatch path"


def test_raw_counts_are_never_replaced_by_filtered_counts(monkeypatch):
    result = _invoke_check(
        monkeypatch,
        [("+", f"FY26-27/{MARKER}"), ("+", "missing.txt")],
    )
    # effective (verdict) vs raw (forensic) are both retained and distinct
    assert result["mismatches"] == 1
    assert result["raw_mismatches"] == 2
    assert result["canary_excluded"] == 1
    assert result["raw_missing"] == 2
    assert len(result["missing"]) == 1
    assert "raw_mismatches=2" in result["detail"]


# ─────────────────────────────────────────────────────────────────
# Rollover creates the sentinel at the NEW destination FY ROOT
# ─────────────────────────────────────────────────────────────────

def test_rollover_creates_marker_at_new_lan_fy_root(tmp_path):
    from core.fy_rollover import create_new_fy_folders

    src_root = tmp_path / "src"
    lan_root = tmp_path / "lan"
    src_root.mkdir()
    lan_root.mkdir()

    created = create_new_fy_folders(str(src_root), str(lan_root), "FY27-28")

    marker = lan_root / "FY27-28" / MARKER
    assert marker.is_file(), "rollover must create the sentinel at the new FY root"
    assert marker.parent == created["lan"]
    # Source folder is data-only: rollover must not seed a sentinel there.
    assert not (src_root / "FY27-28" / MARKER).exists()


# ─────────────────────────────────────────────────────────────────
# Real-rclone integration reproduction (Phase 5)
# ─────────────────────────────────────────────────────────────────

def _real_rclone() -> str | None:
    bundled = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "rclone.exe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("rclone")


RCLONE = _real_rclone()
requires_rclone = pytest.mark.skipif(not RCLONE, reason="rclone binary not available")


@requires_rclone
def test_phase5_real_rclone_canary_only_discrepancy_verifies(tmp_path, monkeypatch):
    """End-to-end: identical content + dest-root sentinel -> VERIFIED."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    (dst / "a.txt").write_text("hello", encoding="utf-8")
    (dst / MARKER).touch()

    monkeypatch.setattr(integrity, "resolve_binary", lambda _n: RCLONE)
    result = integrity.audit_lan(str(src), str(dst), timeout=300)

    assert result["status"] == "VERIFIED", result["detail"]
    assert result["canary_excluded"] == 1
    assert result["raw_extra"] == 1
    assert result["mismatches"] == 0


@requires_rclone
def test_phase5_real_rclone_genuine_missing_still_fails(tmp_path, monkeypatch):
    """End-to-end: dest-root sentinel present AND a real file missing."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    (src / "missing.txt").write_text("gone", encoding="utf-8")
    (dst / "a.txt").write_text("hello", encoding="utf-8")
    (dst / MARKER).touch()

    monkeypatch.setattr(integrity, "resolve_binary", lambda _n: RCLONE)
    result = integrity.audit_lan(str(src), str(dst), timeout=300)

    assert result["status"] == "VERIFICATION_FAILED", result["detail"]
    assert "missing.txt" in result["missing"]
    assert result["canary_excluded"] == 1
