"""B1 (final audit 2026-10-20): G3 and the cloud --max-delete backstop use the SAME deletion allowance.

G3 compared with a float percent while the backstop used Fraction(float), i.e. the BINARY value of the percent
(0.3 is stored as 0.29999999999999998...). Reproduced: 1 984 (percent, destination, planned) cases where G3 passed
but the backstop allowed one deletion fewer (0.3 %, 1000 files, 3 planned -> backstop 2): rclone deleted 2, stopped,
and the run became a backstop BLOCK after files were already deleted - a spurious block. One function decides both,
from the decimal the owner typed."""

from decimal import Decimal
from fractions import Fraction
from math import floor

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core import guards

LIMITS = 10**9      # the file-count limit out of the way: the percent decides


def test_the_reported_disagreement_is_gone():
    assert guards.max_delete_allowance(1000, LIMITS, 0.3) == 3
    assert guards.g3_mass_delete(3, 1000, LIMITS, 0.3).passed is True
    assert guards.g3_mass_delete(4, 1000, LIMITS, 0.3).passed is False


@pytest.mark.parametrize("percent,dest,allowed", [
    (0.7, 1000, 7), (14.1, 1000, 141), (33.3, 1000, 333), (10, 5, 0), (10, 100, 10), (2.5, 80, 2), (0, 1000, 0),
])
def test_allowance_is_the_decimal_the_owner_typed(percent, dest, allowed):
    assert guards.max_delete_allowance(dest, LIMITS, percent) == allowed


def test_the_file_count_limit_still_applies():
    assert guards.max_delete_allowance(100_000, 500, 10) == 500
    assert guards.g3_mass_delete(501, 100_000, 500, 10).passed is False
    assert guards.g3_mass_delete(500, 100_000, 500, 10).passed is True


@settings(max_examples=3000, deadline=None)
@given(dest=st.integers(min_value=0, max_value=5000),
       tenths=st.integers(min_value=0, max_value=1000),
       max_files=st.integers(min_value=0, max_value=6000),
       planned=st.integers(min_value=0, max_value=6000),
       earlier=st.integers(min_value=0, max_value=50))
def test_g3_passes_exactly_when_planned_is_within_the_allowance(dest, tenths, max_files, planned, earlier):
    percent = tenths / 10                       # what the Settings page stores: one decimal
    # independent exact computation (decimal arithmetic, not the production code)
    exact = min(max_files, floor(Decimal(dest + earlier) * Decimal(str(percent)) / 100))
    allowance = guards.max_delete_allowance(dest + earlier, max_files, percent)
    assert allowance == max(0, exact)
    verdict = guards.g3_mass_delete(planned, dest, max_files, percent, already_deleted=earlier)
    assert verdict.passed is (planned + earlier <= allowance)


@settings(max_examples=1000, deadline=None)
@given(dest=st.integers(min_value=1, max_value=5000), tenths=st.integers(min_value=0, max_value=1000),
       done=st.integers(min_value=0, max_value=60))
def test_a_retry_gets_exactly_what_is_left_of_the_runs_allowance(dest, tenths, done):
    percent = tenths / 10
    total = guards.max_delete_allowance(dest + done, LIMITS, percent)
    assert guards.remaining_delete_allowance(dest, done, LIMITS, percent) == max(0, total - done)
    assert isinstance(Fraction(guards.max_delete_allowance(dest, LIMITS, percent)), Fraction)
