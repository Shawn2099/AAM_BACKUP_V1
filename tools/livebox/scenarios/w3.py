"""Scenario pack wave 3 (plan 2026-11-24 section 4, X34-X39): config subsets and concurrency.

expect() reads Results attributes with getattr defaults; a missing attribute means "nothing observed".
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from tools.livebox.scenarios.base import Scenario

SWITCH_START = "2091-03-20"     # spans activation window end and the 04-01 switch
SWITCH_DAYS = 20
CONC_START = "2091-01-10"
CONC_DAYS = 5


def _noop(rig: Any) -> None:
    return None


def _set_flag(rig: Any, section: str, key: str, value: Any) -> None:
    """Best effort: edit the lane config dict and rewrite the YAML. Never raises (knob may be absent)."""
    try:
        cfg = getattr(rig, "config", None)
        if isinstance(cfg, dict) and isinstance(cfg.get(section), dict):
            cfg[section][key] = value
            import yaml
            with open(rig.config_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)
    except Exception:
        pass


def _setup_cloud_only(rig: Any) -> None:
    rig.set_mode("cloud")          # edits the on-disk config (a stale in-memory copy must never revert it)


def _setup_lan_only(rig: Any) -> None:
    rig.set_mode("lan")


def _setup_lan_manual(rig: Any) -> None:
    _set_flag(rig, "lan", "scheduled", False)


def _overlaps(runs: Any) -> list:
    spans = []
    for r in runs or []:
        try:
            s = dt.datetime.fromisoformat(str(r["start"]))
            e = dt.datetime.fromisoformat(str(r["end"]))
        except Exception:
            continue
        spans.append((s, e, str(r.get("argv", ""))))
    spans.sort()
    out = []
    for i in range(1, len(spans)):
        if spans[i][0] < max(x[1] for x in spans[:i]):
            out.append(f"rclone LAN overlap: {spans[i][2]!r} started {spans[i][0].isoformat()} "
                       f"while an earlier LAN rclone was still running")
    return out


def _expect_x34(res: Any) -> list:
    # Cloud-only: rows come from run_lane (--cloud-only); a LAN row means the LAN leg ran despite lan.enabled false.
    return [f"LAN row on {r.get('day')} with lan.enabled false"
            for r in (getattr(res, "rows", None) or []) if r.get("leg") == "lan"]


def _expect_x35(res: Any) -> list:
    out = []
    calls = getattr(res, "archive_calls", None) or []
    if calls:
        out.append(f"archive step called in a LAN-only lane: {calls}")
    out += [f"cloud row on {r.get('day')} with cloud.enabled false"
            for r in (getattr(res, "rows", None) or []) if r.get("leg") == "cloud"]
    return out


def _expect_x36(res: Any) -> list:
    out = []
    n = getattr(res, "lan_runs_0600", 0) or 0
    if n:
        out.append(f"{n} LAN run(s) started at the 06:00 rollover check with lan.scheduled false")
    if getattr(res, "switch_before_manual_lan", False):
        out.append("FY switch happened before the manual LAN press on the closing card (F-OWN-1)")
    return out


def _expect_no_overlap(res: Any) -> list:
    runs = getattr(res, "lan_rclone_runs", None) or getattr(res, "robocopy_runs", None)
    return _overlaps(runs)


def _expect_x39(res: Any) -> list:
    orphans = getattr(res, "orphan_rclone", None) or getattr(res, "orphan_robocopy", None) or []
    out = [f"orphan rclone left running after kill: {a}" for a in orphans]
    runs = getattr(res, "lan_rclone_runs", None) or getattr(res, "robocopy_runs", None)
    out += _overlaps(runs)
    return out


SCENARIOS = [
    Scenario("X34", 3, "Cloud only (lan.enabled false) over activation and switch",
             SWITCH_START, SWITCH_DAYS, "x34_cloud_only", _setup_cloud_only, None, _expect_x34),
    Scenario("X35", 3, "LAN only (cloud.enabled false) over activation and switch: no archive call",
             SWITCH_START, SWITCH_DAYS, "x35_lan_only", _setup_lan_only, None, _expect_x35),
    Scenario("X36", 3, "LAN manual only (lan.scheduled false) over the switch",
             SWITCH_START, SWITCH_DAYS, "x36_lan_manual", _setup_lan_manual, None, _expect_x36,
             expected_red="F-OWN-1"),
    Scenario("X37", 3, "Dashboard /trigger/lan while a LAN flow runs: 409, no second robocopy",
             CONC_START, CONC_DAYS, "x37_double_trigger", _noop, None, _expect_no_overlap, injection_wired=False),
    Scenario("X38", 3, "Two processes start backup(mode=lan) at the same instant: never two robocopy",
             CONC_START, CONC_DAYS, "x38_simultaneous_lan", _noop, None, _expect_no_overlap, injection_wired=False),
    Scenario("X39", 3, "Run killed mid-sync: LAN_ABANDONED next start, no orphan robocopy",
             CONC_START, CONC_DAYS, "x39_kill_mid_sync", _noop, None, _expect_x39, injection_wired=False),
]
