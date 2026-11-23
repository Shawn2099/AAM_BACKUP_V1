"""WAVE 27a - LC1 BEST lab across both boundaries at the real cron instants (plan docs/PLAN_E2E_SIMULATION_2026-11-23.md
section 1c, card LC1).

Real robocopy (LAN leg, loopback admin share = the NAS) and real rclone (a `type = local` remote = the bucket), driven
through the PRODUCTION flows: flow.backup(mode="lan" | "cloud"), flow.rollover_check_flow, flow.integrity_audit_flow.
Each leg runs on its own at its cron instant. The clock moves only through audit_lab/labclock.fake_clock (inside the
Lab helpers); no time-machine inside the Prefect harness.

Events (IST):
    31 Dec 2026 21:00 lan | 1 Jan 02:00 cloud | 1 Jan 21:00 lan | 2 Jan 02:00 cloud | 3 Jan 08:00 audit (Sunday)
    then the clock moves straight to 31 Mar 2027 (no nights in between, no seeding):
    31 Mar 21:00 lan | 1 Apr 02:00 cloud | 1 Apr 06:00 rollover | 1 Apr 21:00 lan | 2 Apr 02:00 cloud

Expected values are hand-written from the card, never read back from the product. Rows and audit state are product
views, labelled `# product-view`.

Run with a clean Prefect home (CLAUDE.md "Environment gotchas"):
    PREFECT_HOME=<empty dir> .venv\\Scripts\\python.exe -m pytest audit_lab/test_w27a_calendar_best.py -q -s -p no:cacheprovider -o addopts=""
"""
from __future__ import annotations

import pendulum
import pytest

from audit_lab import labconf as L
from audit_lab.test_w12_clock import _tree_map
from audit_lab.test_w25_fy_overlap import FY_A, FY_B, Lab, _at, lab  # noqa: F401 - `lab` is the fixture

pytestmark = pytest.mark.timeout(1800)

#: the OK statuses: the _OK set of audit_lab/test_w25_fy_overlap.py, plus the two NO_CHANGES names of plan section 0
_OK = {"LAN_COMPLETE", "CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE", "LAN_NO_CHANGES", "CLOUD_NO_CHANGES"}
_FYS = (FY_A, FY_B)


def _leg_rows(lab: Lab, fy: str, leg: str) -> list[dict]:
    """The run rows of one leg of one FY, NEWEST FIRST (the order Lab.legs returns)."""
    return [r for r in lab.legs(fy) if r["mode"] == leg]


def _run(lab: Lab, at, leg: str, fys: tuple[str, ...]) -> None:
    """One leg at its cron instant: exactly one new row of that leg for each FY in `fys`, none for the other FY,
    and no new row with a failure status (failure = not in _OK and not RUNNING, plan section 0)."""
    before = {fy: len(_leg_rows(lab, fy, leg)) for fy in _FYS}
    label = f"{at.isoformat()} {leg}"
    lab.backup(at, mode=leg)
    for fy in _FYS:
        rows = _leg_rows(lab, fy, leg)
        new = len(rows) - before[fy]
        want = 1 if fy in fys else 0
        assert new == want, f"{label}: {fy} {leg} rows {before[fy]} -> {len(rows)}, expected {want} new"
        for r in rows[:new]:
            assert r["status"] in _OK or r["status"] == "RUNNING", f"{label}: {fy} {leg} ended {r['status']}"


def _work_all(lab: Lab, tag: str) -> None:
    """The accountants' work before a night: every FY folder that exists at that moment."""
    for fy in _FYS:
        if lab.src(fy).exists():
            lab.work(fy, tag)


def test_lc1_best_lab_across_both_boundaries_at_cron_times(lab: Lab):
    # ================================================= 31 Dec 2026 21:00 LAN, 1 Jan 2027 02:00 cloud
    _work_all(lab, "dec31")
    _run(lab, _at(2026, 12, 31, 21), "lan", (FY_A,))
    # FY27-28 is activated by the 1 Jan run; its folder comes into being then, so it is listed in the 02:00 cloud run
    _run(lab, _at(2027, 1, 1, 2), "cloud", _FYS)
    lab.converged(FY_A, "after 1 Jan 02:00 cloud")
    # Not a full pair yet: FY27-28 reaches the bucket at 02:00 but its NAS copy waits for the 21:00 LAN run of 1 Jan,
    # so only the cloud leg can be checked for it here (the first full convergence is after the 2 Jan 02:00 cloud).
    assert _tree_map(lab.cloud(FY_B)) == _tree_map(lab.src(FY_B)), "1 Jan 02:00: cloud FY27-28 differs from its source"

    # ================================================= 1 Jan 2027 21:00 LAN, 2 Jan 02:00 cloud
    _work_all(lab, "jan01")
    _run(lab, _at(2027, 1, 1, 21), "lan", _FYS)
    _run(lab, _at(2027, 1, 2, 2), "cloud", _FYS)
    for fy in _FYS:
        lab.converged(fy, "after the 1 Jan pair (21:00 lan, 02:00 cloud)")

    # ================================================= Sunday 3 Jan 2027 08:00: weekly audit of both FYs
    assert _at(2027, 1, 3, 8).day_of_week == pendulum.SUNDAY
    lab.audit(_at(2027, 1, 3, 8))
    with lab.db() as (_c, m):   # product-view
        for fy in _FYS:
            for leg in ("cloud", "lan"):
                row = m.latest_audit(leg, fy=fy)   # product-view
                assert row is not None and row["status"] == "VERIFIED", f"3 Jan audit {leg} {fy}: {row}"
    for fy in _FYS:
        lab.converged(fy, "after the 3 Jan audit (no change expected)")

    # ================================================= the clock moves straight to 31 March 2027
    _work_all(lab, "mar31")
    _run(lab, _at(2027, 3, 31, 21), "lan", _FYS)
    _run(lab, _at(2027, 4, 1, 2), "cloud", _FYS)
    for fy in _FYS:
        lab.converged(fy, "after the 31 Mar 21:00 lan / 1 Apr 02:00 cloud pair (switch pending)")

    # 1 Apr 06:00 rollover: final backup of FY26-27, config switch to FY27-28, FY26-27 archived and closed
    result = lab.rollover(_at(2027, 4, 1, 6))
    print(f"[LC1] 1 Apr 06:00 rollover returned {result!r}")
    assert lab.archived == [("lab-bucket", FY_A)], f"archive transitions after 1 Apr 06:00: {lab.archived}"
    with lab.db() as (cfg, _m):
        assert cfg.paths.source_drive.endswith(FY_B), f"config not switched to FY27-28: {cfg.paths.source_drive}"
    lab.converged(FY_A, "after the 1 Apr 06:00 rollover (final backup of FY26-27)")
    frozen_cloud, frozen_nas = _tree_map(lab.cloud(FY_A)), _tree_map(lab.nas(FY_A))
    assert frozen_cloud and frozen_nas, "the closed FY26-27 has no files in the cloud or on the NAS"

    # ================================================= 1 Apr 21:00 LAN: FY26-27 is closed, its source is still edited
    _work_all(lab, "apr01")     # FY26-27 gets an edit AFTER its closure: it must not reach the bucket or the NAS
    _run(lab, _at(2027, 4, 1, 21), "lan", (FY_B,))

    # ================================================= 2 Apr 02:00 cloud
    lab.work(FY_A, "apr02")     # FY26-27 edited again on 2 Apr (no FY27-28 edit: its NAS copy is from 21:00)
    _run(lab, _at(2027, 4, 2, 2), "cloud", (FY_B,))
    lab.converged(FY_B, "after the 2 Apr 02:00 cloud")

    # the closed FY26-27 is frozen from 06:00 on 1 Apr, although its source moved on
    assert _tree_map(lab.cloud(FY_A)) == frozen_cloud, "the closed FY26-27 changed in the cloud after the switch"
    assert _tree_map(lab.nas(FY_A)) == frozen_nas, "the closed FY26-27 changed on the NAS after the switch"
    assert (lab.src(FY_A) / "Clients" / "n_apr01.txt").is_file(), "the post-switch edit did not reach the source"
    assert not (lab.cloud(FY_A) / "Clients" / "n_apr01.txt").exists(), "a post-switch FY26-27 edit reached the bucket"
    assert not (lab.nas(FY_A) / "Clients" / "n_apr01.txt").exists(), "a post-switch FY26-27 edit reached the NAS"
    print("[LC1] 31 Mar - 2 Apr: both FY folders converged over the boundary; FY26-27 frozen from 1 Apr 06:00")
