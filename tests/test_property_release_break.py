"""Release break-it properties (Hypothesis): invariants that must hold for ALL inputs.

Targets: FY overlap name arithmetic, the single terminal-status decision, the cloud-verify and LAN
verdict functions on hostile logs, status helpers on garbage, SMTP error classification.
"""
import smtplib
from datetime import date, datetime, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from core import statuses
from core.cloud_verify import decide_cloud_verify_result
from core.fy_overlap import calendar_fy, next_fy, run_date
from core.lan_sync import decide_lan_result
from core.notify import _is_permanent_smtp_error
from core.time_utils import get_fy_prefix

SET = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])

LEGS = st.sampled_from(["cloud", "lan"])
PHASES = st.sampled_from(["pre", "sync", "verify", "post", "weird", ""])
EXC = st.sampled_from([None, statuses.EXC_BLOCKED, statuses.EXC_PARTIAL, statuses.EXC_INTERRUPTED, "other", "boom"])
ALL_STATUSES = sorted(
    set(statuses.SUCCESS_STATUSES) | set(statuses.FAILED_STATUSES) | set(statuses.PARTIAL_STATUSES)
    | set(statuses.BLOCKED_STATUSES) | set(statuses.RUNNING_STATUSES) | set(statuses.SKIPPED_STATUSES)
    | set(statuses.NO_CHANGES_STATUSES))


@SET
@given(start=st.integers(0, 99))
def test_next_fy_is_consecutive_and_cycles(start):
    name = f"FY{start:02d}-{(start + 1) % 100:02d}"
    nxt = next_fy(name)
    n = int(nxt[2:4])
    assert n == (start + 1) % 100 and int(nxt[5:7]) == (n + 1) % 100
    # 100 steps return to the start
    cur = name
    for _ in range(100):
        cur = next_fy(cur)
    assert cur == name


@SET
@given(s=st.text(max_size=20))
def test_next_fy_only_raises_valueerror(s):
    try:
        out = next_fy(s)
    except ValueError:
        return
    assert out.startswith("FY") and len(out) == 7


@SET
@given(s=st.one_of(st.none(), st.integers(), st.binary(max_size=5), st.lists(st.integers(), max_size=2)))
def test_next_fy_non_string_is_valueerror(s):
    with pytest.raises(ValueError):
        next_fy(s)


@SET
@given(d=st.dates(min_value=date(1990, 1, 1), max_value=date(2098, 12, 31)))
def test_calendar_fy_contains_date_and_next_follows(d):
    fy = calendar_fy(d)
    start = d.year if d.month >= 4 else d.year - 1
    assert fy == f"FY{start % 100:02d}-{(start + 1) % 100:02d}"
    assert fy == get_fy_prefix(d)
    assert next_fy(fy) == calendar_fy(date(start + 1, 4, 1))


@SET
@given(dt=st.datetimes(min_value=datetime(1990, 1, 1), max_value=datetime(2090, 1, 1)),
       tz=st.sampled_from([None, timezone.utc]))
def test_run_date_never_crashes_and_is_within_a_day(dt, tz):
    if tz:
        dt = dt.replace(tzinfo=tz)
    rd = run_date(dt)
    assert abs((date(rd.year, rd.month, rd.day) - dt.date()).days) <= 1


@SET
@given(leg=LEGS, phase=PHASES, current=st.one_of(st.sampled_from(ALL_STATUSES), st.text(max_size=12)), exc=EXC)
def test_terminal_status_exception_never_leaves_success(leg, phase, current, exc):
    out = statuses.decide_terminal_status(leg, phase, current, exc)
    assert isinstance(out, str)
    if exc is None:
        assert out == current
    elif exc != statuses.EXC_BLOCKED:
        # a raised exception must never end as success / skipped / running
        assert out
        assert statuses.category(out) not in (statuses.CATEGORY_SUCCESS, statuses.CATEGORY_NO_CHANGES,
                                              statuses.CATEGORY_RUNNING)
    else:
        assert out == f"{leg.upper()}_BLOCKED"


@SET
@given(x=st.one_of(st.none(), st.text(max_size=30), st.integers(), st.binary(max_size=4)),
       fc=st.one_of(st.none(), st.integers(-5, 10**9)), ff=st.one_of(st.none(), st.integers(-5, 10**9)),
       att=st.booleans())
def test_status_helpers_total_on_garbage(x, fc, ff, att):
    statuses.is_success(x)
    assert statuses.category(x)
    assert isinstance(statuses.label(x), str)
    assert isinstance(statuses.describe(x, fc, ff, att), str)


@SET
@given(st.sampled_from(ALL_STATUSES))
def test_every_known_status_has_one_category_and_text(s):
    assert statuses.category(s) != statuses.CATEGORY_UNKNOWN
    assert statuses.label(s) and statuses.describe(s)
    if statuses.is_success(s):
        assert statuses.category(s) in (statuses.CATEGORY_SUCCESS, statuses.CATEGORY_NO_CHANGES)


NOISE = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200)
SUMMARY = st.sampled_from(["0 differences found", "3 differences found", "-1 differences found", "x differences found",
                           "Failed to check", "2026/09/06 18:18:52 ERROR : a: boom", "", "0 differences found\n0 differences found"])


@SET
@given(code=st.one_of(st.integers(-300, 300), st.booleans(), st.none(), st.floats(allow_nan=True), st.text(max_size=3)),
       a=NOISE, b=NOISE, s1=SUMMARY, s2=SUMMARY)
def test_cloud_verify_never_verifies_on_bad_evidence(code, a, b, s1, s2):
    out = decide_cloud_verify_result(code, a + "\n" + s1, b + "\n" + s2)
    assert set(out) >= {"verified", "termination", "completion", "differences", "reason"}
    if out["verified"]:
        assert type(code) is int and code == 0
        assert out["differences"] == 0 and out["completion"] and out["termination"] == "normal"
        text = a + "\n" + s1 + "\n" + b + "\n" + s2
        assert "Failed to check" not in text.replace("failed to check", "Failed to check") or False is True
    else:
        assert out["reason"]


@SET
@given(code=st.one_of(st.integers(-300, 300), st.booleans(), st.none()), log=NOISE,
       tail=st.sampled_from(["", "\n   Total    Copied   Skipped  Mismatch    FAILED    Extras\n    Dirs :  1  0  1  0  0  0\n"
                             "   Files :  5  5  0  0  0  0\n   Ended : Monday\n", "FAILED", "ERROR 5 (0x5)"]))
def test_lan_result_fails_closed(code, log, tail):
    out = decide_lan_result(code, log + tail)
    assert out["status"] in ("LAN_COMPLETE", "LAN_PARTIAL", "LAN_SUSPECT", "LAN_FAILED")
    if type(code) is not int or code < 0 or code & 16:
        assert out["status"] == "LAN_FAILED"
    if out["status"] == "LAN_COMPLETE":
        assert out["log_complete"] and code in (0, 1, 2, 3)


class _E(Exception):
    pass


@SET
@given(code=st.one_of(st.integers(-10, 999), st.none(), st.text(max_size=3)))
def test_smtp_classification_by_code(code):
    exc = smtplib.SMTPResponseException(code if isinstance(code, int) else 0, b"msg") if isinstance(code, int) else _E()
    if not isinstance(code, int):
        exc.smtp_code = code
    got = _is_permanent_smtp_error(exc)
    assert got == (isinstance(code, int) and 500 <= code < 600)
