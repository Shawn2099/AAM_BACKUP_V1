"""A PENDING guard approval is SUPERSEDED when the same leg + FY later runs successfully (operator fixed the
cause), so /health and the banner clear. Never for failed/partial runs, other legs/FYs, newer requests."""
import pytest

from core import approvals as ap
from core.manifest import ManifestDB

FY = "FY26-27"
T_REQ = "2027-01-05T03:00:00+05:30"
T_RUN = "2027-01-05T04:00:00+05:30"
T_END = "2027-01-05T04:30:00+05:30"


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _open(db, leg="lan", fy=FY, created=T_REQ):
    return db.open_approval_request(leg=leg, blocked_run_id="r0", fy=fy, guards=[],
                                    phrase="p", created_at=created)


def _run(db, status, mode="lan", fy=FY, started=T_RUN, run_id="r1"):
    db.insert_run({"run_id": run_id, "mode": mode, "fy": fy, "started_at": started,
                   "ended_at": T_END, "status": status})


def _status(db, aid):
    return db.get_approval(aid)["status"]


def test_success_supersedes_older_pending_same_leg_and_fy(db):
    a = _open(db)
    _run(db, "LAN_COMPLETE")
    assert _status(db, a) == ap.SUPERSEDED


@pytest.mark.parametrize("status", ["LAN_PARTIAL", "LAN_FAILED", "LAN_BLOCKED", "LAN_RUNNING"])
def test_non_success_leaves_pending(db, status):
    a = _open(db)
    _run(db, status)
    assert _status(db, a) == ap.PENDING


def test_other_leg_and_other_fy_untouched(db):
    other_leg = _open(db, leg="cloud")
    other_fy = _open(db, fy="FY27-28")
    _run(db, "LAN_COMPLETE")
    assert _status(db, other_leg) == ap.PENDING
    assert _status(db, other_fy) == ap.PENDING


def test_approval_created_after_run_start_untouched(db):
    a = _open(db, created="2027-01-05T04:10:00+05:30")
    _run(db, "LAN_COMPLETE")
    assert _status(db, a) == ap.PENDING


def test_already_approved_row_untouched(db):
    a = _open(db)
    assert db.approve_request(a, approved_by="o", approved_at=T_REQ,
                              expires_at="2027-01-06T03:00:00+05:30")
    _run(db, "LAN_COMPLETE")
    assert _status(db, a) == ap.APPROVED


def test_rollover_approval_only_cleared_by_successful_rollover(db):
    a = _open(db, leg="rollover")
    _run(db, "LAN_COMPLETE")
    assert _status(db, a) == ap.PENDING
    _run(db, "ROLLOVER_FAILED", mode="rollover", run_id="r2")
    assert _status(db, a) == ap.PENDING
    _run(db, "ROLLOVER_COMPLETE", mode="rollover", run_id="r3")
    assert _status(db, a) == ap.SUPERSEDED
