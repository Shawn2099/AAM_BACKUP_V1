"""WAVE 27b - LC2 WORST lab data path at the fiscal boundary (plan docs/PLAN_E2E_SIMULATION_2026-11-23.md section 1c,
card LC2). One test per variant.

Real robocopy and real rclone through the production flows, the clock moved only by audit_lab/labclock.fake_clock
(inside the Lab helpers). Variants (a), (b), (c) run from 31 Dec 2026 21:00 to 1 Jan 2027 21:00. Variant (d) starts
from the same 1 Jan state and moves straight to 31 Mar 2027 21:00 (card LC2 (d)).

    (a) src/FY27-28 is a directory junction (mklink /J) to a sandbox folder holding 5 files
    (b) a hand-made FY27-28 folder with 20 files, mtime 20 Dec 2026
    (c) an empty folder created by the app (the placeholder only)
    (d) the LAN run of 31 Mar 21:00: a hook after the first robocopy call rewrites FY26-27 Clients/a_000.txt
        before the second folder runs; then the 1 Apr 02:00 cloud run

Expected values are hand-written from the card. Rows, activation and audit state are product views (`# product-view`).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from audit_lab import labconf as L
from audit_lab.test_w12_clock import _tree_map
from audit_lab.test_w25_fy_overlap import FY_A, FY_B, Lab, _at, lab  # noqa: F401 - `lab` is the fixture

pytestmark = pytest.mark.timeout(1800)

#: the OK statuses: the _OK set of audit_lab/test_w25_fy_overlap.py, plus the two NO_CHANGES names of plan section 0
_OK = {"LAN_COMPLETE", "CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE", "LAN_NO_CHANGES", "CLOUD_NO_CHANGES"}
_FYS = (FY_A, FY_B)
_PLACEHOLDER = "README_FY.txt"


def _leg_rows(lab: Lab, fy: str, leg: str) -> list[dict]:
    """The run rows of one leg of one FY, NEWEST FIRST (product view, Lab.legs order)."""
    return [r for r in lab.legs(fy) if r["mode"] == leg]


def _failures(rows: list[dict]) -> list[dict]:
    """Failure = status not in _OK and not RUNNING (plan section 0)."""
    return [r for r in rows if r["status"] not in _OK and r["status"] != "RUNNING"]


def _converged_ok(lab: Lab, fy: str) -> bool:
    """True when source == NAS == cloud for one FY (the Lab.converged check, as a boolean)."""
    try:
        lab.converged(fy, "probe")
    except AssertionError:
        return False
    return True


def _dec31_to_jan1(lab: Lab) -> None:
    """31 Dec 21:00 lan, 1 Jan 02:00 cloud, 1 Jan 21:00 lan: the window of variants (a), (b), (c)."""
    lab.work(FY_A, "dec31")
    lab.backup(_at(2026, 12, 31, 21), mode="lan")
    lab.backup(_at(2027, 1, 1, 2), mode="cloud")
    lab.backup(_at(2027, 1, 1, 21), mode="lan")


def _rm_junction(link: Path) -> None:
    """Remove a junction WITHOUT touching its target (os.rmdir removes the reparse point only)."""
    if os.path.lexists(link):
        try:
            os.rmdir(link)
        except OSError as e:   # reported, never masks the test's own outcome
            print(f"[LC2a] could not remove the junction {link}: {e}")


def test_lc2a_junction_at_fy27_28_is_never_a_silent_false_success(lab: Lab, sandbox: Path):
    """(a) src/FY27-28 is a junction to a sandbox folder with 5 files.

    Hard rule: no success row for FY27-28 whose destination holds 0 files while the junction target holds 5.
    Acceptable (either one, NOT asserted as a single outcome): adopted + converged, or BLOCKED / failure + a mail.
    The outcome that actually happens is printed and recorded below."""
    link = lab.src(FY_B)
    target = sandbox / "elsewhere" / FY_B
    for i in range(5):
        L.write_file(target / "Clients" / f"j_{i}.txt", f"junction target file {i}\n".encode() * 10)
    assert len(_tree_map(target)) == 5, "the junction target must hold 5 files before the run"

    lab.work(FY_A, "dec31")
    lab.backup(_at(2026, 12, 31, 21), mode="lan")           # FY27-28 does not exist yet
    assert not os.path.lexists(link), "FY27-28 existed before the junction was made"
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        lab.backup(_at(2027, 1, 1, 2), mode="cloud")
        lab.backup(_at(2027, 1, 1, 21), mode="lan")
    finally:
        _rm_junction(link)

    rows = {leg: _leg_rows(lab, FY_B, leg) for leg in ("cloud", "lan")}
    ok = {leg: [r for r in rows[leg] if r["status"] in _OK] for leg in rows}
    dest = {"cloud": _tree_map(lab.cloud(FY_B)), "lan": _tree_map(lab.nas(FY_B))}          # product-view: the destinations
    for leg in ("cloud", "lan"):
        assert not (ok[leg] and not dest[leg]), (
            f"SILENT FALSE SUCCESS on {leg}: FY27-28 row {ok[leg][0]['status']} while its destination holds 0 files "
            f"and the junction target holds 5")

    with lab.db() as (_c, m):   # product-view
        act = m.get_activation(FY_B)
    blocked = [r for leg in rows for r in rows[leg] if "BLOCKED" in r["status"]]
    failed = [r for leg in rows for r in _failures(rows[leg])]
    mails = [a for a in lab.alerts if getattr(a, "fy", None) == FY_B]
    adopted_and_converged = act is not None and bool(ok["cloud"]) and bool(ok["lan"]) and _converged_ok(lab, FY_B)
    blocked_or_failed_with_mail = bool(blocked or failed) and bool(mails)
    # LC2a (fixed): a refused link is not silent any more: no activation, no row for the link, ONE ACTION_REQUIRED
    # mail naming the folder (not an INFO log line only), and the configured FY keeps backing up
    refused_loudly = (act is None and not rows["cloud"] and not rows["lan"]
                      and [getattr(a, "kind", None) for a in mails].count("ACTION_REQUIRED") >= 1
                      and any(str(link) in getattr(a, "message", "") for a in mails))
    if adopted_and_converged:
        outcome = "adopted+converged"
    elif refused_loudly:
        outcome = "refused+ACTION_REQUIRED mail"
    elif blocked_or_failed_with_mail:
        outcome = "blocked/failure+mail"
    else:
        outcome = "UNACCEPTABLE"
    # RECORDED, not asserted as one branch: which of the two acceptable outcomes happened (for the owner)
    print(f"[LC2a] OUTCOME for the FY27-28 junction: {outcome}; statuses cloud={[r['status'] for r in rows['cloud']]} "
          f"lan={[r['status'] for r in rows['lan']]}; activation row={act is not None}; FY27-28 mails={len(mails)}; "
          f"cloud files={len(dest['cloud'])}, NAS files={len(dest['lan'])}")
    assert outcome != "UNACCEPTABLE", f"neither adopted+converged nor blocked/failure+mail: {rows}"


def test_lc2b_hand_made_fy27_28_is_adopted_and_its_source_is_not_changed(lab: Lab):
    """(b) a hand-made FY27-28 with 20 files, mtime 20 Dec 2026: adopted (created_by_app = 0), the source files'
    sha256 unchanged, converged after 1 Jan 21:00."""
    lab.work(FY_A, "dec31")
    lab.backup(_at(2026, 12, 31, 21), mode="lan")           # FY27-28 does not exist yet
    mtime = _at(2026, 12, 20, 12).timestamp()
    for i in range(20):
        L.write_file(lab.src(FY_B) / "Clients" / f"hand_{i:02d}.txt", f"hand made file {i}\n".encode() * 12, mtime=mtime)
    before = _tree_map(lab.src(FY_B))
    assert len(before) == 20, f"the hand-made folder holds {len(before)} files before the run"

    lab.backup(_at(2027, 1, 1, 2), mode="cloud")
    lab.backup(_at(2027, 1, 1, 21), mode="lan")

    with lab.db() as (_c, m):   # product-view
        act = m.get_activation(FY_B)
    assert act is not None, "no activation row for the hand-made FY27-28"
    assert act["created_by_app"] == 0, f"the folder was made by hand but the row says created_by_app={act['created_by_app']}"
    after = _tree_map(lab.src(FY_B))
    changed = sorted(p for p, h in before.items() if after.get(p) != h)
    assert not changed, f"the hand-made source files changed: {changed[:5]}"
    lab.converged(FY_B, "after 1 Jan 21:00 (hand-made FY27-28 adopted)")
    print(f"[LC2b] adopted: activation created_by_app={act['created_by_app']}, 20 source files unchanged, converged")


def test_lc2c_empty_app_created_folder_holds_only_the_placeholder(lab: Lab):
    """(c) an empty folder created by the app: both legs not BLOCKED, the remote prefix and the NAS hold only the
    placeholder."""
    _dec31_to_jan1(lab)
    for leg in ("cloud", "lan"):
        rows = _leg_rows(lab, FY_B, leg)
        assert rows, f"no {leg} run row for FY27-28"
        blocked = [r for r in rows if "BLOCKED" in r["status"]]
        assert not blocked, f"{leg} FY27-28 is BLOCKED: {[r['status'] for r in blocked]}"
    assert set(_tree_map(lab.cloud(FY_B))) == {_PLACEHOLDER}, \
        f"the remote prefix holds {sorted(_tree_map(lab.cloud(FY_B)))}"
    assert set(_tree_map(lab.nas(FY_B))) == {_PLACEHOLDER}, \
        f"the NAS folder holds {sorted(_tree_map(lab.nas(FY_B)))}"
    print("[LC2c] both legs not BLOCKED; remote prefix and NAS hold only README_FY.txt")


def test_lc2d_source_changes_between_the_two_folders_of_the_boundary_lan_run(lab: Lab, monkeypatch):
    """(d) 31 Mar 21:00 LAN: a hook wrapped around core.lan_sync.run_tool rewrites FY26-27 Clients/a_000.txt with new
    bytes after the first robocopy call, i.e. before the second folder runs. Then 1 Apr 02:00 cloud, then the next LAN.

    Expected (card): the cloud holds the new bytes (MD5); the LAN row of 31 Mar is OK or partial; the next LAN converges.
    """
    import core.lan_sync as ls

    _dec31_to_jan1(lab)
    lab.work(FY_A, "mar31")
    lab.work(FY_B, "mar31")

    new_bytes = f"{FY_A} rewritten by the hook between the two folders\n".encode() * 25
    hook = {"fired": 0, "robocopy_calls": 0}
    real_run_tool = ls.run_tool

    def _hooked_run_tool(tool, *args, **kwargs):
        result = real_run_tool(tool, *args, **kwargs)
        if tool == "robocopy":
            hook["robocopy_calls"] += 1
            if hook["fired"] == 0:          # after the FIRST robocopy call of the run
                hook["fired"] = 1
                L.write_file(lab.src(FY_A) / "Clients" / "a_000.txt", new_bytes)
        return result

    monkeypatch.setattr(ls, "run_tool", _hooked_run_tool)
    try:
        lab.backup(_at(2027, 3, 31, 21), mode="lan")
    finally:
        monkeypatch.setattr(ls, "run_tool", real_run_tool)
    assert hook["fired"] == 1, "the hook never fired: no robocopy call in the 31 Mar LAN run"
    assert hook["robocopy_calls"] >= 2, f"the LAN run made {hook['robocopy_calls']} robocopy call(s); two folders expected"
    assert (lab.src(FY_A) / "Clients" / "a_000.txt").read_bytes() == new_bytes, "the hook did not rewrite the source"

    for fy in _FYS:
        newest = _leg_rows(lab, fy, "lan")[0]           # the 31 Mar row (newest first)
        assert newest["status"] in _OK or "PARTIAL" in newest["status"], \
            f"31 Mar LAN row for {fy} ended {newest['status']} (expected OK or partial)"
        print(f"[LC2d] 31 Mar LAN row for {fy}: {newest['status']}")

    lab.backup(_at(2027, 4, 1, 2), mode="cloud")
    cloud_a000 = lab.cloud(FY_A) / "Clients" / "a_000.txt"
    assert cloud_a000.is_file(), "the 1 Apr cloud run did not copy FY26-27 Clients/a_000.txt"
    assert L.md5(cloud_a000) == hashlib.md5(new_bytes).hexdigest(), "the cloud does not hold the hooked bytes (MD5)"

    lab.backup(_at(2027, 4, 1, 21), mode="lan")      # the next LAN run
    for fy in _FYS:
        lab.converged(fy, f"next LAN after the 31 Mar hook ({fy})")
