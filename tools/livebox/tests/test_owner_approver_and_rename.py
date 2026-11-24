"""R1 harness side: the simulated owner approves AAM-DEL-01 (opt in) and a churn rename touches a small share."""
import datetime as dt
import json
from types import SimpleNamespace

from core import approvals
from core.manifest import ManifestDB
from tools.livebox import calendar as cal
from tools.livebox import churn
from tools.livebox.tests.test_review_fixes_safety import _hooks


def _day(d):
    return SimpleNamespace(date=d)


def _rig(db_path):
    cfg = SimpleNamespace(approvals=SimpleNamespace(expiry_hours=12))
    return SimpleNamespace(load_config=lambda: cfg), cfg


def _setup(tmp_path, monkeypatch, n, guards=("G3_MASS_DELETE",), leg="cloud"):
    h = _hooks(tmp_path, monkeypatch, approve_del_after_days=n)
    db = ManifestDB(tmp_path / "m.db")
    rid = db.open_approval_request(leg=leg, blocked_run_id="r1", fy="FY91-92",
                                   guards=[{"guard": g} for g in guards], phrase="APPROVE",
                                   created_at="2091-04-04T02:00:00+05:30")
    h.rig, cfg = _rig(tmp_path / "m.db")
    monkeypatch.setattr(ManifestDB, "from_config", classmethod(lambda cls, c: ManifestDB(tmp_path / "m.db")))
    monkeypatch.setattr("core.time_utils.now_iso", lambda: "2091-04-08T02:00:00+05:30")
    return h, db, rid


def test_approver_is_off_by_default(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    assert h.approve_del_after_days is None
    assert h._owner_approval(_day(dt.date(2091, 5, 1)), "cloud") == {}


def test_owner_approves_after_n_days_and_logs_it(tmp_path, monkeypatch):
    h, db, rid = _setup(tmp_path, monkeypatch, 3)
    assert h._owner_approval(_day(dt.date(2091, 4, 5)), "cloud") == {}            # 1 day old: not yet
    assert db.get_approval(rid)["status"] == approvals.PENDING
    assert h._owner_approval(_day(dt.date(2091, 4, 8)), "cloud") == {"approval_id": rid}
    assert db.get_approval(rid)["status"] == approvals.APPROVED
    rows = [json.loads(x) for x in (h.ev_dir / "events.jsonl").read_text().splitlines()]
    assert rows[-1]["event"] == "owner_approval" and rows[-1]["approval_id"] == rid
    assert h._owner_approval(_day(dt.date(2091, 4, 8)), "lan") == {}              # other leg untouched


def test_owner_never_approves_other_guards(tmp_path, monkeypatch):
    h, db, rid = _setup(tmp_path, monkeypatch, 0, guards=("G3_MASS_DELETE", "G2_SOURCE_SHRINK"))
    assert h._owner_approval(_day(dt.date(2091, 6, 1)), "cloud") == {}
    assert db.get_approval(rid)["status"] == approvals.PENDING


def test_cli_flag_default_off():
    from tools.livebox import run_lane

    a = run_lane.build_parser().parse_args(["--stamp", "s1"])
    assert a.approve_del_after_days is None
    assert run_lane.build_parser().parse_args(["--stamp", "s1", "--approve-del-after-days", "3"]).approve_del_after_days == 3


def _tree(root, per_client):
    for client, n in per_client.items():
        for i in range(n):
            p = root / client / "Invoices" / f"f{i:03d}.pdf"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * (10 + i))


def test_rename_skipped_when_it_would_touch_most_of_the_tree(tmp_path):
    root = tmp_path / "src"
    _tree(root, {"Client_000": 316})          # the lane grown from scratch: one client
    ops = churn.apply_day(root, dt.date(2091, 4, 4), "s", role="C")
    assert not any(o.kind == "rename" for o in ops)
    assert (root / "Client_000").is_dir()


def test_rename_picks_a_small_client(tmp_path):
    root2 = tmp_path / "src2"
    _tree(root2, {f"Client_{i:03d}": 20 for i in range(15)})   # 15 clients of ~6.7 %
    ops = churn.apply_day(root2, dt.date(2091, 4, 4), "s", role="C")
    assert sum(o.kind == "rename" for o in ops) == 1
