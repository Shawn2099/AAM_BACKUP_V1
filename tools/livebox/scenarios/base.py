"""Shared contract for scenario packs (waves 1-4). Plan section 4 (X01-X48).

A scenario is data plus three callables. The driver (run_lane.py) runs the
lane described by (start, days, lane), calling setup(rig) once before day 1,
on_day(rig, day) before each simulated day's flows (knobs: rig.set_cloud,
rig.set_mail, rig.set_clock_proof, outages), and expect(results) at the end.
expect returns a list of failure strings; empty list = PASS.
expected_red names a known finding (e.g. "F-OWN-1") that is EXPECTED to fail:
the report records it as FINDING, not as a harness bug.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional


@dataclass(frozen=True)
class Injection:
    """One granular injection: fn(rig) runs once, when the first event at or after date + time (IST) starts."""
    date: str                    # "YYYY-MM-DD"
    time: str                    # "HH:MM" IST
    fn: Callable[[Any], None]


@dataclass(frozen=True)
class Scenario:
    id: str                      # "X01".."X48"
    wave: int                    # 1..4
    title: str
    start: str                   # ISO date, e.g. "2090-12-29"
    days: int
    lane: str                    # lane name, unique per scenario
    setup: Callable[[Any], None]
    on_day: Optional[Callable[[Any, Any], None]]
    expect: Callable[[Any], list]
    expected_red: Optional[str] = None
    outages: tuple = ()                                   # cal.Outage items (server-off windows)
    lateness: Mapping[str, Any] = field(default_factory=dict)   # "YYYY-MM-DD <event kind>" -> timedelta (late start)
    injections: tuple = ()                                # Injection items
    requires: tuple = ()                                  # "knob:<name>" | "fact:<name>" | "file:<rel>"; unmet -> NOT_RUN
    overrides: tuple = ()                                 # cal.DayOverride pairs / mapping (see cal.overrides_map)
    injection_wired: bool = True                          # False: the driver cannot inject it yet -> always NOT_RUN
    n_deletes: bool = False                               # True: churn may delete files even in a tiny tree (F-NEW-G3-SMALL-N)
