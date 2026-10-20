"""M2 2.1 — data-safety guards (plan §6.3): boundaries + properties."""

import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core import guards as g

SYS = frozenset({"system volume information", "$recycle.bin", "desktop.ini", "thumbs.db"})


# ── G1 + first_regular_file ────────────────────────────────────────────────

def test_g1_empty_and_dirs_only_source_block(tmp_path):
    assert not g.g1_source_has_files(g.first_regular_file(str(tmp_path), SYS), str(tmp_path)).passed
    (tmp_path / "Clients" / "2026").mkdir(parents=True)          # probe P6 shape
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "desktop.ini").write_text("x")                    # ignored system file
    res = g.g1_source_has_files(g.first_regular_file(str(tmp_path), SYS), str(tmp_path))
    assert not res.passed and "empty folders" in res.reason


def test_g1_deep_file_passes(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "ledger.xlsx").write_text("x")
    first = g.first_regular_file(str(tmp_path), SYS)
    assert first and first.endswith("ledger.xlsx")
    assert g.g1_source_has_files(first, str(tmp_path)).passed


def test_first_regular_file_unlistable_root_raises(tmp_path):
    with pytest.raises(OSError):
        g.first_regular_file(str(tmp_path / "missing"), SYS)


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_first_regular_file_does_not_follow_junctions(tmp_path):
    import subprocess

    target = tmp_path / "target"
    target.mkdir()
    (target / "only_via_junction.txt").write_text("x")
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(src / "j"), str(target)],
                   check=True, capture_output=True)
    assert g.first_regular_file(str(src), SYS) is None


# ── G2 ─────────────────────────────────────────────────────────────────────

def test_g2_cases():
    assert g.g2_source_shrink(None, 100, 20).verdict == g.BLOCK        # unknown -> block
    assert g.g2_source_shrink(5, None, 20).passed                      # first run
    assert g.g2_source_shrink(80, 100, 20).passed                      # exactly 20% ok
    r = g.g2_source_shrink(79, 100, 20)
    assert not r.passed and r.metrics["drop_percent"] == 21.0
    assert g.g2_source_shrink(150, 100, 20).passed                     # growth fine


@given(cur=st.integers(0, 10**7), base=st.integers(1, 10**7),
       lim=st.floats(0, 100, allow_nan=False))
@settings(max_examples=300, deadline=None)
def test_g2_property_blocks_exactly_when_drop_exceeds_limit(cur, base, lim):
    r = g.g2_source_shrink(cur, base, lim)
    drop_pct = 100.0 * max(base - cur, 0) / base
    assert r.passed == (drop_pct <= lim)


# ── G3 ─────────────────────────────────────────────────────────────────────

def test_g3_cases():
    assert g.g3_mass_delete(None, 10, 500, 10).verdict == g.BLOCK
    assert g.g3_mass_delete(3, None, 500, 10).verdict == g.BLOCK
    assert g.g3_mass_delete(0, 0, 500, 10).passed                      # empty dest
    assert g.g3_mass_delete(10, 100, 500, 10).passed                   # exactly 10%
    assert not g.g3_mass_delete(11, 100, 500, 10).passed
    r = g.g3_mass_delete(501, 1_000_000, 500, 10)
    assert not r.passed and "501" in r.reason


@given(dels=st.integers(0, 10**7), dest=st.integers(0, 10**7),
       max_files=st.integers(0, 10**6), max_pct=st.floats(0, 100, allow_nan=False))
@settings(max_examples=400, deadline=None)
def test_g3_property_never_passes_over_either_limit(dels, dest, max_files, max_pct):
    # B1: the verdict is exact (the decimal the owner typed), one rule shared with the cloud backstop
    from fractions import Fraction
    from math import floor

    r = g.g3_mass_delete(dels, dest, max_files, max_pct)
    allowed = max(0, min(max_files, floor(Fraction(dest) * Fraction(str(max_pct)) / 100)))
    assert r.passed is (dels <= allowed)


def test_g3_blocks_planned_deletions_against_an_empty_destination():
    """Nothing can be deleted from a destination with no files: planned deletions with a destination
    count of 0 mean the measurement is wrong (an unreadable listing) - fail closed, never '0 %'."""
    assert not g.g3_mass_delete(1, 0, 500, 10).passed
    assert g.g3_mass_delete(0, 0, 500, 10).passed


# ── G4 ─────────────────────────────────────────────────────────────────────

LOCAL = {"127.0.0.1", "::1", "10.10.150.20"}


def test_g4_loopback_and_own_ip_block_when_forbidden():
    r = g.g4_destination_identity("\\\\127.0.0.1\\aam_test\\FY26-27", None, {"127.0.0.1"}, LOCAL, True)
    assert not r.passed and "THIS machine" in r.reason
    r = g.g4_destination_identity("\\\\myself\\share", None, {"10.10.150.20"}, LOCAL, True)
    assert not r.passed


def test_g4_unresolvable_host_blocks_when_forbidden():
    r = g.g4_destination_identity("\\\\nas\\share", None, None, LOCAL, True)
    assert not r.passed and "resolve" in r.reason


def test_g4_changed_destination_blocks_case_insensitively_equal_passes():
    r = g.g4_destination_identity("\\\\10.10.151.53\\aam_test\\CHAOS01\\FY26-27",
                                  "\\\\10.10.186.231\\lan_backup\\FY26-27",
                                  {"10.10.151.53"}, LOCAL, True)
    assert not r.passed and "changed" in r.reason
    r = g.g4_destination_identity("\\\\NAS\\Backup\\FY26-27\\", "\\\\nas\\backup\\fy26-27",
                                  {"10.10.186.231"}, LOCAL, True)
    assert r.passed


def test_g4_local_allowed_when_not_forbidden():
    assert g.g4_destination_identity("\\\\127.0.0.1\\s", None, {"127.0.0.1"}, LOCAL, False).passed


def test_unc_host():
    assert g.unc_host("\\\\10.10.186.231\\share\\FY26-27") == "10.10.186.231"
    assert g.unc_host("//nas/share") == "nas"
    assert g.unc_host("E:\\data") is None


def test_resolve_host_and_local_addresses():
    assert "127.0.0.1" in (g.resolve_host("127.0.0.1") or set())
    assert g.resolve_host("no-such-host.invalid") is None
    assert "127.0.0.1" in g.local_addresses()


# ── G5 ─────────────────────────────────────────────────────────────────────

def test_g5_cases():
    assert g.g5_canary(True, True, "\\\\nas\\s").passed
    assert not g.g5_canary(None, True, "\\\\nas\\s").passed
    r = g.g5_canary(False, True, "\\\\nas\\s")
    assert not r.passed and "already holds files" in r.reason
    r = g.g5_canary(False, False, "\\\\nas\\s")
    assert not r.passed and "initialise" in r.reason
