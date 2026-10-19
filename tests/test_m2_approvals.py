"""M2 2.3 — approval decision logic (plan §6.2)."""

from hypothesis import given, settings
from hypothesis import strategies as st

from core import approvals as ap

NOW = "2026-10-14T10:00:00+05:30"


def _approval(**over):
    a = {"approval_id": "x", "leg": "lan", "guard": "G3_MASS_DELETE",
         "approved_metrics": {"planned_deletes": 4812, "delete_percent": 96.2},
         "approved_at": "2026-10-14T09:00:00+05:30",
         "expires_at": "2026-10-14T21:00:00+05:30", "used_at": None, "dismissed_at": None}
    a.update(over)
    return a


def _eval(approval, **fresh):
    metrics = {"planned_deletes": 4812, "delete_percent": 96.2}
    metrics.update(fresh)
    return ap.evaluate_approval(approval, leg="lan", guard="G3_MASS_DELETE",
                                fresh_metrics=metrics, now_iso=NOW, tolerance_percent=0)


def test_valid_approval_allows_once():
    assert _eval(_approval()).allowed


def test_rejections():
    assert not _eval(None).allowed
    assert "already used" in _eval(_approval(used_at=NOW)).reason
    assert "dismissed" in _eval(_approval(dismissed_at=NOW)).reason
    assert "not been approved" in _eval(_approval(approved_at=None)).reason
    assert "expired" in _eval(_approval(expires_at="2026-10-14T09:59:59+05:30")).reason
    assert "valid expiry" in _eval(_approval(expires_at="garbage")).reason
    assert "not lan/G3" in _eval(_approval(leg="cloud")).reason


def test_worse_than_approved_needs_new_approval():
    r = _eval(_approval(), planned_deletes=4813)
    assert not r.allowed and "worse than approved" in r.reason
    assert _eval(_approval(), planned_deletes=10, delete_percent=0.2).allowed  # better is fine


def test_destination_must_match_for_g4():
    a = _approval(guard="G4_DESTINATION_IDENTITY",
                  approved_metrics={"destination": "\\\\nas\\NEW\\FY26-27"})
    ok = ap.evaluate_approval(a, leg="lan", guard="G4_DESTINATION_IDENTITY",
                              fresh_metrics={"destination": "\\\\NAS\\new\\fy26-27"},
                              now_iso=NOW, tolerance_percent=0)
    bad = ap.evaluate_approval(a, leg="lan", guard="G4_DESTINATION_IDENTITY",
                               fresh_metrics={"destination": "\\\\other\\x"},
                               now_iso=NOW, tolerance_percent=0)
    assert ok.allowed and not bad.allowed and "situation changed" in bad.reason


def test_one_word_confirms_every_approval():
    """Owner 2026-10-19: typing APPROVE is enough for every guard (the card shows the numbers)."""
    for guard in ("G3_MASS_DELETE", "G2_SOURCE_SHRINK", "G4_DESTINATION_IDENTITY", "G5_CANARY",
                  "G1_SOURCE_HAS_FILES", "G6_FY_ROLLOVER"):
        assert ap.confirmation_phrase(guard, {"planned_deletes": 4812}) == "APPROVE"
    assert ap.request_phrase([{"guard": "G3_MASS_DELETE"}, {"guard": "G2_SOURCE_SHRINK"}]) == "APPROVE"
    assert ap.phrase_matches("APPROVE", "  approve ")
    assert not ap.phrase_matches("APPROVE", "yes")
    assert ap.expires_at(NOW, 12).startswith("2026-10-14T22:00:00")


@given(approved=st.integers(0, 10**6), fresh=st.integers(0, 10**6),
       tol=st.floats(0, 50, allow_nan=False))
@settings(max_examples=300, deadline=None)
def test_property_never_allows_more_deletions_than_approved_plus_tolerance(approved, fresh, tol):
    a = _approval(approved_metrics={"planned_deletes": approved, "delete_percent": 1.0})
    r = ap.evaluate_approval(a, leg="lan", guard="G3_MASS_DELETE",
                             fresh_metrics={"planned_deletes": fresh, "delete_percent": 1.0},
                             now_iso=NOW, tolerance_percent=tol)
    if r.allowed:
        assert fresh <= approved * (1 + tol / 100) + 1e-9
