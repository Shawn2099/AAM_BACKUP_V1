"""OV-5.1 - small folders (plan D-11, 5.5; R-14, A-1): below `guards.small_folder_files` the percentage
limits are replaced by `guards.small_folder_max_deletes`; G3 and the rclone --max-delete backstop use the
same function, G2 has the matching rule."""
import pytest
from pydantic import ValidationError

from core import guards
from models.config import GuardsConfig

SMALL_N, SMALL_DEL = 100, 10


def g3(planned, dest, *, already=0, max_files=500, pct=10):
    return guards.g3_mass_delete(planned, dest, max_files, pct, already_deleted=already,
                                 small_folder_files=SMALL_N, small_folder_max_deletes=SMALL_DEL)


def g2(current, baseline, pct=20):
    return guards.g2_source_shrink(current, baseline, pct, small_folder_files=SMALL_N,
                                   small_folder_max_deletes=SMALL_DEL)


def test_config_keys_and_bounds():
    c = GuardsConfig()
    assert (c.small_folder_files, c.small_folder_max_deletes) == (100, 10)
    for bad in ({"small_folder_files": -1}, {"small_folder_max_deletes": -1},
                {"small_folder_files": 10_000_001}, {"small_folder_max_deletes": 100_001}):
        with pytest.raises(ValidationError):
            GuardsConfig(**bad)
    assert GuardsConfig(small_folder_files=0).small_folder_files == 0      # 0 = rule off


def test_function_defaults_come_from_the_config_defaults():
    import inspect
    sig = inspect.signature(guards.g3_mass_delete)
    assert sig.parameters["small_folder_files"].default == GuardsConfig().small_folder_files
    assert sig.parameters["small_folder_max_deletes"].default == GuardsConfig().small_folder_max_deletes


# ── G3 ────────────────────────────────────────────────────────────────────────

def test_nine_files_one_rename_passes():
    # 1 rename = 1 delete + 1 copy. Old rule: floor(9 * 10%) = 0 allowed -> blocked a rename in a new folder.
    assert g3(1, 9).passed


def test_ninety_nine_files_eleven_deletes_blocks():
    assert g3(10, 99).passed
    r = g3(11, 99)
    assert not r.passed and r.fault_code == "AAM-DEL-01"


def test_hundred_file_boundary_has_no_jump():
    assert g3(10, 100).passed          # 10 % of 100 = 10 (normal rule)
    assert not g3(11, 100).passed
    assert guards.max_delete_allowance(100, 500, 10, SMALL_N, SMALL_DEL) == 10
    assert guards.max_delete_allowance(99, 500, 10, SMALL_N, SMALL_DEL) == 10
    assert guards.max_delete_allowance(101, 500, 10, SMALL_N, SMALL_DEL) == 10    # floor(10.1)


def test_large_folder_keeps_todays_rule():
    assert guards.max_delete_allowance(10_000, 500, 10, SMALL_N, SMALL_DEL) == 500
    assert guards.max_delete_allowance(1_000, 500, 10, SMALL_N, SMALL_DEL) == 100


def test_small_rule_never_exceeds_max_delete_files():
    assert guards.max_delete_allowance(50, 3, 10, SMALL_N, SMALL_DEL) == 3


def test_an_explicit_zero_percent_still_means_never_delete():
    """config says 0 = never delete: the small-folder allowance must not override that."""
    assert guards.max_delete_allowance(9, 500, 0, SMALL_N, SMALL_DEL) == 0
    assert not guards.g3_mass_delete(1, 9, 500, 0, small_folder_files=SMALL_N,
                                     small_folder_max_deletes=SMALL_DEL).passed
    assert not guards.g2_source_shrink(8, 9, 0, small_folder_files=SMALL_N,
                                       small_folder_max_deletes=SMALL_DEL).passed


def test_rule_off_with_zero():
    assert guards.max_delete_allowance(9, 500, 10, 0, SMALL_DEL) == 0         # old behaviour


def test_already_deleted_counts_toward_the_size_the_run_started_from():
    # 2 files were already deleted by an earlier attempt: the run started with 98 + 2 = 100 files
    # (normal rule: 10 % = 10 deletions for the whole run), so 8 more are fine and 9 more are not
    assert g3(8, 98, already=2).passed
    assert not g3(9, 98, already=2).passed


def test_backstop_agrees_with_g3():
    """The rclone --max-delete is the SAME number G3 passes."""
    for dest in (0, 1, 9, 50, 99, 100, 101, 1000):
        allowance = guards.max_delete_allowance(dest, 500, 10, SMALL_N, SMALL_DEL)
        assert g3(allowance, dest).passed
        assert not g3(allowance + 1, dest).passed
        assert guards.remaining_delete_allowance(dest, 0, 500, 10, SMALL_N, SMALL_DEL) == allowance


def test_remaining_allowance_after_an_earlier_attempt():
    # started at 99 files, 4 deleted already, 95 left: allowance is on 99 (small) = 10, minus 4 = 6
    assert guards.remaining_delete_allowance(95, 4, 500, 10, SMALL_N, SMALL_DEL) == 6


# ── G2 ────────────────────────────────────────────────────────────────────────

def test_g2_small_baseline_blocks_only_above_the_delete_limit():
    assert g2(current=90, baseline=99).passed            # drop 9
    assert g2(current=89, baseline=99).passed            # drop 10
    r = g2(current=88, baseline=99)                      # drop 11
    assert not r.passed and r.fault_code == "AAM-SRC-04"


def test_g2_small_baseline_ignores_the_percentage():
    # 20 % rule would allow a drop of 19 of 99; the small rule allows 10
    assert not g2(current=80, baseline=99).passed


def test_g2_large_baseline_keeps_the_percentage_rule():
    assert g2(current=800, baseline=1000).passed
    assert not g2(current=799, baseline=1000).passed
    assert g2(current=80, baseline=100).passed           # at 100 files the 20 % rule applies (drop 20)


def test_g2_first_run_and_unmeasured_unchanged():
    assert g2(current=5, baseline=None).passed
    assert not g2(current=None, baseline=5).passed


def test_g1_empty_source_still_blocks_regardless_of_size():
    assert not guards.g1_source_has_files(None, "D:\\FY26-27").passed
