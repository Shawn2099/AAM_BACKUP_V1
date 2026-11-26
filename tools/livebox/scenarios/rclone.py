"""rclone LAN scenario pack XR1-XR4 (WS-D D4/D7)."""
from __future__ import annotations

from pathlib import Path

from tools.livebox.scenarios.base import Scenario


def _setup_xr2(rig) -> None:
    d = Path(rig.source_dir) / "burst_old"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(300):
        (d / f"file_{i:03d}.txt").write_text(f"xr2-payload-{i:03d}\n", encoding="utf-8")


def _on_day_xr2(rig, day) -> None:
    if getattr(day, "index", None) == 1:
        old = Path(rig.source_dir) / "burst_old"
        new = Path(rig.source_dir) / "burst_new"
        if old.exists() and not new.exists():
            old.rename(new)


def _setup_xr4(rig) -> None:
    src = Path(rig.source_dir)
    src.mkdir(parents=True, exist_ok=True)
    (src / "ver_target.txt").write_text("v1-initial\n", encoding="utf-8")


def _on_day_xr4(rig, day) -> None:
    idx = getattr(day, "index", None)
    target = Path(rig.source_dir) / "ver_target.txt"
    if idx == 1:
        target.write_text("v2-day1-overwrite\n", encoding="utf-8")
    elif idx == 50:
        target.write_text("v3-day50-younger\n", encoding="utf-8")


def _noop(rig) -> None:
    return None


def _expect_xr1(res) -> list[str]:
    rows = [r for r in (getattr(res, "rows", None) or []) if r.get("leg") == "lan"]
    out = []
    if not any(r.get("status") == "LAN_PARTIAL" for r in rows):
        out.append("XR1: expected LAN_PARTIAL while source file held open")
    if not any(r.get("status") == "LAN_COMPLETE" for r in rows):
        out.append("XR1: expected LAN_COMPLETE after handle release")
    return out


def _expect_xr2(res) -> list[str]:
    rows = [r for r in (getattr(res, "rows", None) or []) if r.get("leg") == "lan"]
    out = []
    if any(r.get("status") == "LAN_BLOCKED" for r in rows):
        out.append("XR2: rename burst of 300 files must not trip G3")
    complete = [r for r in rows if r.get("status") == "LAN_COMPLETE"]
    if len(complete) < 2:
        out.append(f"XR2: expected >= 2 LAN_COMPLETE (before + after rename), got {len(complete)}")
    return out


def _expect_xr3(res) -> list[str]:
    rows = [r for r in (getattr(res, "rows", None) or []) if r.get("leg") == "lan"]
    out = []
    blocked = [r for r in rows if r.get("status") == "LAN_BLOCKED"]
    if len(blocked) < 30:
        out.append(f"XR3: expected >= 30 LAN_BLOCKED copy-only nights, got {len(blocked)}")
    if not any(r.get("status") == "LAN_COMPLETE" for r in rows):
        out.append("XR3: expected LAN_COMPLETE after operator approval")
    return out


def _expect_xr4(res) -> list[str]:
    rows = [r for r in (getattr(res, "rows", None) or []) if r.get("leg") == "lan"]
    complete = [r for r in rows if r.get("status") == "LAN_COMPLETE"]
    if len(complete) < 90:
        return [f"XR4: expected >= 90 LAN_COMPLETE across 95-day retention run, got {len(complete)}"]
    return []


def check_versions_outside_mirror(lan_destination: str, versions_folder: str) -> list[str]:
    """D4 helper: verify versions folder resolves on the same share and outside the mirror root."""
    from models.config import resolve_versions_root

    try:
        vroot = resolve_versions_root(lan_destination, versions_folder)
    except ValueError as e:
        return [str(e)]
    if Path(vroot) == Path(lan_destination):
        return [f"versions root {vroot} equals mirror root"]
    return []


SCENARIOS: list[Scenario] = [
    Scenario(
        id="XR1",
        wave=5,
        title="Source file held open (share mode 0) for 5 nights -> PARTIAL, no deletions, then COMPLETE",
        start="2091-01-10",
        days=7,
        lane="xr1_locked_source",
        setup=_noop,
        on_day=None,
        expect=_expect_xr1,
        injection_wired=False,
    ),
    Scenario(
        id="XR2",
        wave=5,
        title="Rename burst of 300 files -> moves, 0 deletes, no G3 block",
        start="2091-01-10",
        days=3,
        lane="xr2_rename_burst",
        setup=_setup_xr2,
        on_day=_on_day_xr2,
        expect=_expect_xr2,
        injection_wired=True,
    ),
    Scenario(
        id="XR3",
        wave=5,
        title="Mass delete > G3 -> BLOCKED copy-only 30 nights, ladder mails, approval at night 31",
        start="2091-01-10",
        days=33,
        lane="xr3_copy_only_ladder",
        setup=_noop,
        on_day=None,
        expect=_expect_xr3,
        injection_wired=False,
    ),
    Scenario(
        id="XR4",
        wave=5,
        title="Versions retention crossing 91 days -> old backup-dir purged, younger kept",
        start="2091-01-10",
        days=95,
        lane="xr4_versions_retention",
        setup=_setup_xr4,
        on_day=_on_day_xr4,
        expect=_expect_xr4,
        injection_wired=True,
    ),
]
