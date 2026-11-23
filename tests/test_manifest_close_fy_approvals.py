"""ManifestDB.close_fy on the approval rows of the closing FY (the case no calendar test covers any more).

PENDING and APPROVED rows of the closing FY become CLOSED ("FY closed"), SUPERSEDED rows stay SUPERSEDED
(a newer block replaced them long before), rows of every other FY are untouched, and a second call is a no-op."""
from core import approvals as ap
from core.manifest import ManifestDB

OLD, NEW = "FY26-27", "FY27-28"
T0 = "2027-03-30T03:00:00+05:30"
T1 = "2027-03-30T04:00:00+05:30"
EXPIRES = "2027-03-31T03:00:00+05:30"
SWITCH = "2027-04-01T06:00:00+05:30"


def _open(db, leg, fy, run):
    return db.open_approval_request(leg=leg, blocked_run_id=run, fy=fy, guards=[], phrase="APPROVE",
                                    created_at=T0)


def test_close_fy_closes_pending_and_approved_keeps_superseded_and_other_fys(tmp_path):
    db = ManifestDB(tmp_path / "m.db")
    try:
        superseded = _open(db, "lan", OLD, "r1")
        pending = _open(db, "lan", OLD, "r2")              # r1's request is now SUPERSEDED
        approved = _open(db, "cloud", OLD, "r3")
        assert db.approve_request(approved, approved_by="owner", approved_at=T1, expires_at=EXPIRES)
        other_pending = _open(db, "lan", NEW, "r4")
        other_approved = _open(db, "cloud", NEW, "r5")
        assert db.approve_request(other_approved, approved_by="owner", approved_at=T1, expires_at=EXPIRES)
        other_superseded = _open(db, "audit", NEW, "r6")
        _open(db, "audit", NEW, "r7")                       # supersedes r6's request
        assert db.get_approval(superseded)["status"] == ap.SUPERSEDED
        assert db.get_approval(pending)["status"] == ap.PENDING

        result = db.close_fy(OLD, now=SWITCH)

        assert result["approvals"] == 2                     # only the PENDING and the APPROVED row
        closed = db.get_approval(pending)
        assert closed["status"] == ap.CLOSED and closed["dismissed_by"] == "FY closed"
        assert closed["dismissed_at"] == SWITCH
        assert db.get_approval(approved)["status"] == ap.CLOSED
        assert db.get_approval(superseded)["status"] == ap.SUPERSEDED       # stays what it was
        assert db.get_approval(other_pending)["status"] == ap.PENDING        # other FYs untouched
        assert db.get_approval(other_approved)["status"] == ap.APPROVED
        assert db.get_approval(other_superseded)["status"] == ap.SUPERSEDED
        assert db.get_approval(other_pending)["dismissed_by"] is None
        assert db.close_fy(OLD, now=SWITCH)["approvals"] == 0               # idempotent
    finally:
        db.close()
