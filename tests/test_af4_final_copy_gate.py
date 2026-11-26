"""AF-4: final-copy gate in overlap mode must not accept a stale copy after a newer failure."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core import approvals as ap
from core import fy_overlap as fo
from core import fy_rollover as fr
from core.manifest import ManifestDB
from tests.test_fy_overlap_rollover import FROM, TO, _OverlapRig, _info


def _insert_run(db: ManifestDB, run_id: str, leg: str, started_at: str, status: str, fy: str = FROM) -> None:
    db.insert_run({
        "run_id": run_id,
        "mode": leg,
        "started_at": started_at,
        "status": status,
        "extended_metrics": json.dumps({"guards": {"fy": fy}}),
    })


def test_af4_success_then_later_failure_blocks_rollover():
    """Confirm-first: 1 Apr 01:00 CLOUD_COMPLETE + 10 Apr 01:00 CLOUD_FAILED -> must block."""
    with _OverlapRig(cloud="CLOUD_FAILED", lan="LAN_COMPLETE") as r:
        r.copy("cloud", "2027-04-01T01:00:00+05:30", "CLOUD_COMPLETE")
        _insert_run(r.db, "cloud-fail-apr10", "cloud", "2027-04-10T01:00:00+05:30", "CLOUD_FAILED")
        with pytest.raises(fr.RolloverError):
            r.run()
        r.m["update_config_yaml"].assert_not_called()
        assert r.statuses()[-1] == "ROLLOVER_BLOCKED"
        assert r.metrics("ROLLOVER_BLOCKED")["final_copy"]["cloud"]["outstanding"] is True
        r.m["_open_block_request"].assert_called_once()


def test_af4_success_only_passes(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        _insert_run(db, "c-ok", "cloud", "2027-04-01T01:00:00+05:30", "CLOUD_COMPLETE")
        _insert_run(db, "l-ok", "lan", "2027-04-01T02:00:00+05:30", "LAN_COMPLETE")
        cfg = SimpleNamespace(
            cloud=SimpleNamespace(enabled=True, scheduled=True),
            lan=SimpleNamespace(enabled=True, scheduled=True),
        )
        state = fo.final_copy_state(cfg, db, FROM, {})
        assert state["cloud"]["done"] is True
        assert state["cloud"]["source"] == "earlier"
        assert state["lan"]["done"] is True
        assert fo.outstanding_legs(state) == []
    finally:
        db.close()


def test_af4_failure_then_success_passes(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        _insert_run(db, "c-fail", "cloud", "2027-04-01T01:00:00+05:30", "CLOUD_FAILED")
        _insert_run(db, "c-ok", "cloud", "2027-04-10T01:00:00+05:30", "CLOUD_COMPLETE")
        cfg = SimpleNamespace(
            cloud=SimpleNamespace(enabled=True, scheduled=True),
            lan=SimpleNamespace(enabled=False, scheduled=False),
        )
        state = fo.final_copy_state(cfg, db, FROM, {})
        assert state["cloud"]["done"] is True
        assert state["cloud"]["status"] == "CLOUD_COMPLETE"
        assert state["cloud"]["done_at"] == "2027-04-10T01:00:00+05:30"
    finally:
        db.close()


def test_af4_run_before_closing_moment_never_counts(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        _insert_run(db, "c-mar31", "cloud", "2027-03-31T23:59:00+05:30", "CLOUD_COMPLETE")
        cfg = SimpleNamespace(
            cloud=SimpleNamespace(enabled=True, scheduled=True),
            lan=SimpleNamespace(enabled=False, scheduled=False),
        )
        state = fo.final_copy_state(cfg, db, FROM, {})
        assert state["cloud"]["done"] is False
        assert state["cloud"]["outstanding"] is True
    finally:
        db.close()


def test_af4_approval_lets_forced_switch_through_once():
    with _OverlapRig(cloud="CLOUD_FAILED", lan="LAN_COMPLETE", approved=True) as r:
        r.copy("cloud", "2027-04-01T01:00:00+05:30", "CLOUD_COMPLETE")
        _insert_run(r.db, "cloud-fail-apr10", "cloud", "2027-04-10T01:00:00+05:30", "CLOUD_FAILED")
        assert r.run(approval_id="a1") == "ROLLOVER_COMPLETED"
        r.m["update_config_yaml"].assert_called_once()
        assert r.metrics()["forced_legs"] == ["cloud"]
        assert "Forced:" in _info(r).message
