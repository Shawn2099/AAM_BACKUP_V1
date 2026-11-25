"""Tests for churn file mtimes vs scheduled job times (WS-D D0 / WS-E E-R1, E-R2)."""
from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace

from core.audit_pending import PendingContext, classify
from tools.livebox import churn as c


def test_er1_every_churn_op_mtime_is_at_or_before_first_scheduled_job(tmp_path):
    """E-R1: every churn op's mtime <= sim time of the first scheduled job that day."""
    root = tmp_path / "src"
    c.seed_tree(root, seed=7, scale=60)

    # 1. Normal workday + large_add (day >= 15): first job is 02:01 cloud backup
    workday = dt.date(2027, 1, 15)
    first_job_workday = dt.datetime(2027, 1, 15, 2, 1, 0).timestamp()
    ops = c.apply_day(root, workday, seed=7, role="C", large_file_mb=1)
    written_kinds = {"add", "modify", "large_add", "shared_add", "late_edit"}
    written = [op for op in ops if op.kind in written_kinds]
    assert written, "expected at least one file write on workday"
    for op in written:
        st = (root / op.path).stat()
        assert st.st_mtime <= first_job_workday, f"{op}: mtime {st.st_mtime} > {first_job_workday}"

    # 2. Sunday dated event (2027-01-03): first job is 08:00 weekly audit
    sunday = dt.date(2027, 1, 3)
    first_job_sunday = dt.datetime(2027, 1, 3, 8, 0, 0).timestamp()
    sun_ops = c.apply_day(root, sunday, seed=7, role="C")
    assert any(op.kind == "shared_add" for op in sun_ops)
    for op in sun_ops:
        if op.kind in written_kinds:
            st = (root / op.path).stat()
            assert st.st_mtime <= first_job_sunday, f"{op}: mtime {st.st_mtime} > {first_job_sunday}"

    # 3. Closed FY late_edit (2027-04-02): first job is 02:01
    closed_day = dt.date(2027, 4, 2)
    first_job_closed = dt.datetime(2027, 4, 2, 2, 1, 0).timestamp()
    closed_ops = c.apply_day(root, closed_day, seed=7, role=c.ROLE_CLOSED)
    assert [op.kind for op in closed_ops] == ["late_edit"]
    st = (root / closed_ops[0].path).stat()
    assert st.st_mtime <= first_job_closed


def test_er2_sunday_shared_add_before_0800_audit_classifies_as_pending_backup(tmp_path):
    """E-R2: 2027-01-03 shared_add followed by 08:00 audit before 21:01 LAN -> PENDING_BACKUP."""
    src = tmp_path / "src"
    src.mkdir()
    sunday = dt.date(2027, 1, 3)
    ops = c.apply_day(src, sunday, seed=1, role="C")
    assert [op.kind for op in ops] == ["shared_add"]
    rel = ops[0].path
    assert rel == c.SHARED_REL

    # Simulate fake-clocked stat (st_birthtime == st_mtime, parent dir mtime <= 00:30)
    def sim_stat(path: str):
        real = os.stat(path)
        ts = min(real.st_mtime, c.sim_timestamp(sunday))
        return SimpleNamespace(st_mtime=ts, st_birthtime=ts)

    sat_lan = dt.datetime(2027, 1, 2, 21, 1, 0).timestamp()
    sun_audit = dt.datetime(2027, 1, 3, 8, 0, 0).timestamp()
    ctx = PendingContext(
        source_root=str(src),
        dest_root=None,
        reference=sat_lan,
        now=sun_audit,
        modify_window_seconds=2,
        files_checked=100,
        max_delete_files=500,
        max_delete_percent=10.0,
        stat=sim_stat,
    )
    res = classify([rel], [], [], ctx)
    assert res.pending == [("new", rel)]
    assert res.real == []
