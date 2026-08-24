"""P0-DATA 0.1e - RED tests for the sandbox-outside-production guard.

Contract (IMPLEMENTATION_FIX_PLAN.md v1.1 P0 hardening):
  * Test sandboxes must never equal, contain, or sit inside the production
    source/destination trees - checked RESOLVED and CASEFOLDED, both
    directions, junction-proof.
  * tests/e2e_helpers.py exposes assert_sandbox_safe(test_root, prod_root).
  * tests/test_e2e_real_hardware._compute_test_paths returns paths strictly
    outside the production roots.
"""
from pathlib import Path

import pytest

from models.config import CONFIG_PATH, load_config
from tests.e2e_helpers import assert_sandbox_safe


def _prod_roots():
    cfg = load_config(str(CONFIG_PATH))
    return Path(cfg.paths.source_drive), Path(cfg.paths.lan_destination)


def test_guard_rejects_identity(tmp_path):
    with pytest.raises(AssertionError):
        assert_sandbox_safe(tmp_path, tmp_path)


def test_guard_rejects_sandbox_inside_prod_mixed_case():
    prod, _ = _prod_roots()
    sneaky = Path(str(prod / "E2E_TEST_FY").upper())  # mixed-case bypass attempt
    assert str(sneaky).casefold() == str(prod / "E2E_TEST_FY").casefold()
    with pytest.raises(AssertionError):
        assert_sandbox_safe(sneaky, prod)


def test_guard_rejects_prod_inside_sandbox():
    prod, _ = _prod_roots()
    wide = prod.parent.parent  # an ancestor of the production tree
    with pytest.raises(AssertionError):
        assert_sandbox_safe(wide, prod)


def test_guard_resolves_junction_into_prod(tmp_path):
    prod, _ = _prod_roots()
    link = tmp_path / "junction_into_prod"
    import subprocess
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(prod)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"junction creation unavailable: {r.stderr.strip()}")
    try:
        with pytest.raises(AssertionError):
            assert_sandbox_safe(link, prod)
    finally:
        link.rmdir()


def test_e2e_helpers_dirs_are_outside_prod():
    from tests.e2e_helpers import nas_test_dir, source_test_dir
    src_root, dest_root = _prod_roots()
    # must not raise
    assert_sandbox_safe(source_test_dir(), src_root)
    assert_sandbox_safe(nas_test_dir(), dest_root)


def test_real_hardware_compute_paths_outside_prod():
    from tests.test_e2e_real_hardware import _compute_test_paths
    cfg = load_config(str(CONFIG_PATH))
    test_source, test_dest = _compute_test_paths(cfg)
    assert_sandbox_safe(test_source, Path(cfg.paths.source_drive))
    assert_sandbox_safe(test_dest, Path(cfg.paths.lan_destination))
