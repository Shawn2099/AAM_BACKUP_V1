"""Tests for WS-B B6: versions retention purge (V9)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from core import lan_versions


def test_v9_folders_to_purge_uses_folder_date_and_retention_days(tmp_path):
    vroot = tmp_path / "AAM_versions"
    vroot.mkdir()
    old_d = vroot / "2026-08-01_2101_FY26-27"
    exact_d = vroot / "2026-08-26_2101_FY26-27"
    young_d = vroot / "2026-08-27_2101_FY26-27"
    other = vroot / "not_a_run_folder"
    for d in (old_d, exact_d, young_d, other):
        d.mkdir()

    today = dt.date(2026, 11, 25)  # 2026-08-26 is exactly 91 days ago
    due = lan_versions.folders_to_purge(vroot, retention_days=91, today=today)
    assert due == [old_d, exact_d]


def test_v9_purge_old_versions_runs_rclone_purge_on_due_folders_only(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    mirror = nas / "FY26-27"
    mirror.mkdir(parents=True)
    vroot = nas / "AAM_versions"
    vroot.mkdir()
    due = vroot / "2026-01-01_2101_FY26-27"
    due.mkdir()

    calls = []
    monkeypatch.setattr(
        lan_versions, "run_tool",
        lambda step, cmd, **kw: calls.append((step, cmd)) or SimpleNamespace(exit_code=0, spawn_error=None, timed_out=False),
    )
    res = lan_versions.purge_old_versions(
        str(mirror), "AAM_versions", retention_days=91, today=dt.date(2026, 11, 25),
    )
    assert res["ok"] is True
    assert len(calls) == 1
    assert calls[0][1][1] == "purge" and str(due) in calls[0][1]
    assert "--min-age" not in calls[0][1] and "rmdirs" not in calls[0][1]
    assert str(mirror) not in calls[0][1]
