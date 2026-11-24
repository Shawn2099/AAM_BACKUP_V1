"""P1 C1 (plan §4, rows F1-F4): the G3 / --max-delete small-tree floor.

Target API (built by the code agent, not yet present):
  guards.max_delete_allowance(dest, max_files, pct, min_floor_files)
      = max(0, min(max_files, max(min_floor_files, floor(dest * pct / 100))))
  guards.g3_mass_delete(..., already_deleted=0, min_floor_files=...)
  guards.remaining_delete_allowance(..., min_floor_files=...)
  GuardsConfig.min_delete_floor_files: int, default 20, bounds 0..100000
Every test below passes min_floor_files by keyword, so none depends on the default.
"""

from decimal import Decimal
from fractions import Fraction
from math import floor
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

import flow
from core import guards as g
from models.config import GuardsConfig

LIMITS = 10**9   # the file-count limit out of the way unless a test says otherwise


# ── F1: the allowance formula with the floor ───────────────────────────────

@pytest.mark.parametrize("dest", range(10))
def test_f1_small_tree_gets_the_floor(dest):
    assert g.max_delete_allowance(dest, LIMITS, 10, min_floor_files=20) == 20


def test_f1_large_trees_follow_the_percent():
    assert g.max_delete_allowance(1000, LIMITS, 10, min_floor_files=20) == 100
    assert g.max_delete_allowance(100_000, 500, 10, min_floor_files=20) == 500


def test_f1_floor_zero_gives_the_old_values():
    assert g.max_delete_allowance(5, LIMITS, 10, min_floor_files=0) == 0
    assert g.max_delete_allowance(1000, LIMITS, 10, min_floor_files=0) == 100


def test_f1_floor_above_the_file_limit_is_capped_by_it():
    assert g.max_delete_allowance(0, 5, 10, min_floor_files=20) == 5


def test_f1_g3_uses_the_floor():
    # 15 of 100 files (15 %) is within the floor of 20, so G3 passes ...
    assert g.g3_mass_delete(15, 100, LIMITS, 10, min_floor_files=20).passed is True
    assert g.g3_mass_delete(20, 100, LIMITS, 10, min_floor_files=20).passed is True
    # ... and 21 is over it.
    assert g.g3_mass_delete(21, 100, LIMITS, 10, min_floor_files=20).passed is False
    # The same 15 with floor 0 is blocked (10 % of 100 = 10).
    assert g.g3_mass_delete(15, 100, LIMITS, 10, min_floor_files=0).passed is False


def test_f1_retry_allowance_uses_the_floor():
    # Nothing deleted yet on a 0-file tree: the floor is what is left.
    assert g.remaining_delete_allowance(0, 0, LIMITS, 10, min_floor_files=20) == 20
    # 5 already deleted by attempt 1: 20 - 5 is left for attempt 2.
    assert g.remaining_delete_allowance(0, 5, LIMITS, 10, min_floor_files=20) == 15


@settings(max_examples=2000, deadline=None)
@given(dest=st.integers(0, 5000), tenths=st.integers(0, 1000),
       max_files=st.integers(0, 6000), floor_files=st.integers(0, 2000),
       planned=st.integers(0, 6000), earlier=st.integers(0, 50))
def test_f1_formula_and_g3_agree_with_the_floor(dest, tenths, max_files, floor_files, planned, earlier):
    percent = tenths / 10
    exact = max(0, min(max_files, max(floor_files, floor(Decimal(dest + earlier) * Decimal(str(percent)) / 100))))
    allowance = g.max_delete_allowance(dest + earlier, max_files, percent, min_floor_files=floor_files)
    assert allowance == exact
    # F2: G3 passes exactly when the run's deletions fit the same allowance the backstop uses.
    verdict = g.g3_mass_delete(planned, dest, max_files, percent, already_deleted=earlier,
                               min_floor_files=floor_files)
    assert verdict.passed is (planned + earlier <= allowance)


# ── F3: the config key ─────────────────────────────────────────────────────

def test_f3_floor_key_default_and_bounds():
    assert GuardsConfig().min_delete_floor_files == 20
    assert GuardsConfig(min_delete_floor_files=0).min_delete_floor_files == 0
    assert GuardsConfig(min_delete_floor_files=100_000).min_delete_floor_files == 100_000
    for bad in (-1, 100_001):
        with pytest.raises(ValidationError):
            GuardsConfig(min_delete_floor_files=bad)


# ── F4: the cloud guard task passes the configured floor to G3 ─────────────

def _cloud_cfg(tmp_path, floor_files=None):
    src = tmp_path / "csrc"
    src.mkdir(exist_ok=True)
    (src / "ledger.xlsx").write_text("x")
    cfg = MagicMock()
    cfg.paths.source_drive = str(src)
    cfg.cloud.bucket = "bkt"
    cfg.guards = GuardsConfig() if floor_files is None else GuardsConfig(min_delete_floor_files=floor_files)
    return cfg


def _run_cloud_guard(cfg, source_files, dest_files, planned):
    counts = {cfg.paths.source_drive: source_files, "aam_gcs:bkt/FY26-27": dest_files}
    dry = {"error": None, "planned_deletes": planned}
    with patch("flow.ManifestDB") as mdb, \
         patch("flow.temp_rclone_config", MagicMock()), \
         patch("flow.count_objects", side_effect=lambda target, *a, **k: counts[target]), \
         patch("flow.measure_planned_deletes", return_value=dry):
        mdb.from_config.return_value.guard_baseline.return_value = None
        return flow.cloud_guards_task.fn(cfg, "FY26-27", None)


def test_f4_cloud_guard_task_passes_the_configured_floor_to_g3(tmp_path):
    cfg = _cloud_cfg(tmp_path, floor_files=7)
    with patch.object(g, "g3_mass_delete", wraps=g.g3_mass_delete) as g3, \
         patch("flow.ManifestDB") as mdb, \
         patch("flow.temp_rclone_config", MagicMock()), \
         patch("flow.count_objects", return_value=100), \
         patch("flow.measure_planned_deletes", return_value={"error": None, "planned_deletes": 1}):
        mdb.from_config.return_value.guard_baseline.return_value = None
        flow.cloud_guards_task.fn(cfg, "FY26-27", None)
    assert g3.call_args.kwargs.get("min_floor_files") == 7


def test_f4_cloud_guard_small_tree_passes_with_the_default_floor(tmp_path):
    # 15 deletions out of 100 files: over 10 % (10 files) but within the default floor of 20.
    rec = _run_cloud_guard(_cloud_cfg(tmp_path), 100, 100, 15)
    assert rec["max_delete"] == 20
