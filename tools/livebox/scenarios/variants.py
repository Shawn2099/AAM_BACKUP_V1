"""Scenario variants of the plan 2026-11-24 rows X01, X04, X06, X08, X17, X39 (cheap, small, explicit expectations).

Kept apart from w1..w4 on purpose: those packs have tests that pin their exact id lists.
Run one:  run_lane --scenario tools.livebox.scenarios.variants:<ID> --stamp <s> --root <r> [--cloud-only | --lan-only]
Run all:  run_lane --scenario tools.livebox.scenarios.variants:SCENARIOS

  X01b  X01 with three nights without internet (activation waits one night longer)
  X01c  X01 with DNS down instead of a black-holed token endpoint (rclone remote and clock proof both unreachable)
  X04c  X04 for a cloud-only lane  (run with --cloud-only)
  X04l  X04 for a LAN-only lane    (run with --lan-only; the overdue leg is lan, not cloud)
  X06w  X06 (server off 3 days mid-overlap) plus real watchdog ticks every simulated midnight
  X08b  one night where the NAS wake fails (WoL sent, NAS never ready): LAN fails, cloud runs, mail sent
X05 needs no variant: its expectation is leg-agnostic and the calendar follows --cloud-only / --lan-only
(run_lane --scenario tools.livebox.scenarios.w1:X05 --cloud-only).

NOT WIRED (no variant built, see w1 X17 / w3 X39 which stay injection_wired=False): see NOT_WIRED below.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from tools.livebox import calendar as cal
from tools.livebox.scenarios import w1
from tools.livebox.scenarios.base import Scenario

IST = cal.IST

# Why the process-kill scenarios stay NOT_RUN (the rig has kill_child_after / fire_step, the driver never calls them).
NOT_WIRED = {
    "X17": "flows run in the driver process, so a taskkill can only hit robocopy/rclone children, never 'the flow "
           "process'; firing after rollover step final_backups needs a driver hook on ManifestDB.rollover_step_done",
    "X39": "same: the kill must start while robocopy runs (a run_tool guard thread) and the abandoned-run evidence "
           "needs the flow itself to die; pinned in tests/test_wire_injections.py, not built",
}


def _g(r: Any, name: str, default: Any) -> Any:
    v = getattr(r, name, None)
    return default if v is None else v


# ---------------------------------------------------------------- X01 family (cloud + clock proof down)
def _x01_family_expect(r: Any, sid: str, blackout_days: tuple, act_day: str) -> list:
    out: list = []
    key = "rollover:activation:FY91-92"
    warn = w1._by_kind(r, "WARNING", key=key)
    act = w1._by_kind(r, "ACTION_REQUIRED", key=key)
    info = [m for m in w1._by_kind(r, "INFO") if "rollover:activated" in str(m.get("key", ""))]
    if len(warn) != 1:
        out.append(f"{sid}: expected exactly 1 WARNING for {key}, observed {len(warn)}")
    if len(act) != 1:
        out.append(f"{sid}: expected exactly 1 ACTION_REQUIRED for {key}, observed {len(act)} "
                   f"(F-P1-ESCALATION-SUPPRESSED)")
    if len(info) != 1:
        out.append(f"{sid}: expected exactly 1 INFO activated mail, observed {len(info)}")
    got = list(_g(r, "activated_on", []))
    if got != [act_day]:
        out.append(f"{sid}: activation expected exactly on {act_day}, observed {got}")
    fail = [m for m in w1._by_kind(r, "FAILURE") if str(m.get("day", "")) in blackout_days]
    if len(fail) > len(blackout_days):
        out.append(f"{sid}: {len(fail)} cloud FAILURE mails over {len(blackout_days)} blackout days "
                   f"(max {len(blackout_days)})")
    bad = w1._lan_bad(w1._rows(r))
    if bad:
        out.append(f"{sid}: LAN must stay green all days, observed {len(bad)} non-success LAN rows")
    return out


_X01_BLACKOUT_3 = ("2090-12-31", "2091-01-01", "2091-01-02")


def _x01b_expect(r):
    return _x01_family_expect(r, "X01b", _X01_BLACKOUT_3, "2091-01-03")


def _x01c_expect(r):
    return _x01_family_expect(r, "X01c", ("2090-12-31", "2091-01-01"), "2091-01-02")


def _blackout_ov(days):
    return tuple((d, cal.DayOverride(cloud_down=True, clock_unproven=True)) for d in days)


# ---------------------------------------------------------------- X04 for one leg
def _x04_override(stale_leg: str):
    mails = ()
    if stale_leg == "cloud":          # as X04: the cloud overdue mail of 23:30 is expected
        mails = (cal.ExpectedMail(cal.MAIL_PROBLEM, 1, "stale:cloud", dt.datetime(2091, 1, 2, 23, 30, tzinfo=IST),
                                  severity="ACTION_REQUIRED", code="AAM-SYS-03"),)
    # lan-only: the lan leg may be reported overdue; whether and when is not pinned (record only)
    return (("2091-01-02", cal.DayOverride(stale_ok=(stale_leg,), extra_mails=mails)),)


def _x04_leg_expect(sid: str):
    def expect(r):
        out = w1._x04_expect(r)
        return [f"{sid}{x[3:]}" if x.startswith("X04") else x for x in out]
    return expect


# ---------------------------------------------------------------- X06w: watchdog ticks
TICKS_PER_MIDNIGHT = 3


def _watchdog_ticks(rig: Any, day: Any) -> None:
    """Real watchdog._check_stale_legs (the body of the loop's stale branch) a few times at the start of the
    simulated day (00:01, 00:02, 00:03 IST, never moving the clock back). The loop itself (watchdog.main) is a
    `while True` with sc.exe / HTTP calls and cannot be run; the per-tick stale check is the only part that
    alerts about missed runs, so that is what is called."""
    import watchdog

    log = getattr(rig, "watchdog_ticks", None)
    if log is None:
        log = []
        setattr(rig, "watchdog_ticks", log)
    d = getattr(day, "date", day)
    for n in range(1, TICKS_PER_MIDNIGHT + 1):
        at = dt.datetime.combine(d, dt.time(0, n), tzinfo=IST)
        try:
            now = rig.clock.now
            if now is None or at > now:
                rig.clock.move_to(at)
            stale = watchdog._check_stale_legs(rig.load_config())
            rig.drain_mail(at)
            log.append({"day": str(d), "tick": n, "stale": list(stale)})
        except Exception as exc:  # noqa: BLE001 - recorded; the expectation reports a missing tick
            log.append({"day": str(d), "tick": n, "error": repr(exc)})


def _x06w_expect(r: Any) -> list:
    out: list = []
    stale = [m for m in _g(r, "mails", []) if isinstance(m, dict) and str(m.get("key", "")).startswith("stale:")]
    out += w1._dup_per_key_day("X06w", stale, f"overdue (ticks x{TICKS_PER_MIDNIGHT} per midnight must dedupe)")
    if not stale:
        out.append("X06w: no overdue mail after a 3-day outage (the watchdog ticks reported nothing)")
    days = sorted(str(x.get("day", "")) for x in w1._rows(r))
    if days:
        last = days[-1]
        late = [m for m in stale if m.get("day") == last]
        if late:
            out.append(f"X06w: overdue mail still sent on the last day {last} (a good run must resolve it)")
    if not any(x.get("leg") == "cloud" and x.get("status") in cal.CLOUD_OK for x in w1._rows(r)
               if str(x.get("day", "")) > "2091-02-13"):
        out.append("X06w: no successful cloud run after the outage (nothing resolved the alert)")
    return out


# ---------------------------------------------------------------- X08b: NAS wake fails one night
X08B_START, X08B_DAYS, X08B_FAIL_DAY, X08B_BACK_DAY = "2091-02-02", 5, "2091-02-04", "2091-02-05"
_X08B_LAN_FAIL = ("LAN_FAILED", "LAN_BLOCKED", "LAN_PARTIAL", "LAN_SKIPPED")


def _x08b_on_day(rig: Any, day: Any) -> None:
    d = str(getattr(day, "date", day))
    if d == X08B_FAIL_DAY:
        rig.set_wake_fail(True)
    elif d == X08B_BACK_DAY:
        rig.set_wake_fail(False)


def _x08b_expect(r: Any) -> list:
    out: list = []
    rows = w1._rows(r)
    lan = [x for x in rows if x.get("leg") == "lan" and x.get("day") == X08B_FAIL_DAY]
    if not lan:
        out.append(f"X08b: no LAN row on {X08B_FAIL_DAY} (evidence missing; a failed wake must still leave a row)")
    for x in lan:
        st = str(x.get("status"))
        if st in cal.LAN_OK:
            out.append(f"X08b: LAN {st} on {X08B_FAIL_DAY} although the NAS never woke")
        elif st in ("LAN_ABANDONED", "LAN_RUNNING"):
            out.append(f"X08b: LAN left {st} (crash) instead of a recorded failure")
    if not any(x.get("leg") == "cloud" and x.get("day") == X08B_FAIL_DAY and x.get("status") in cal.CLOUD_OK
               for x in rows):
        out.append(f"X08b: cloud leg did not complete on {X08B_FAIL_DAY} (a NAS wake failure must not touch it)")
    problem = [m for m in _g(r, "mails", []) if isinstance(m, dict) and m.get("day") == X08B_FAIL_DAY
               and w1._kind_of(m) in ("FAILURE", "ACTION_REQUIRED", "WARNING")]
    if not problem:
        out.append(f"X08b: no problem mail on {X08B_FAIL_DAY} for the failed LAN night")
    out += w1._dup_per_key_day("X08b", problem, "LAN wake failure")
    later = [x for x in rows if x.get("leg") == "lan" and str(x.get("day", "")) >= X08B_BACK_DAY]
    if later and not any(x.get("status") in cal.LAN_OK for x in later):
        out.append("X08b: LAN did not recover after the wake worked again")
    return out


# ---------------------------------------------------------------- catalog
def _mk(sid, title, start, days, setup, on_day, expect, *, expected_red=None, overrides=(), outages=(), wave=1):
    return Scenario(id=sid, wave=wave, title=title, start=start, days=days, lane=f"var_{sid.lower()}",
                    setup=setup, on_day=on_day, expect=expect, expected_red=expected_red,
                    overrides=tuple(overrides), outages=tuple(outages))


_noop = w1._noop

SCENARIOS = [
    _mk("X01b", "X01 with three nights without internet", "2090-12-29", 8, _noop,
        w1._schedule({"2090-12-31": [w1._cloud("blackhole"), w1._proof("nointernet")],
                      "2091-01-03": [w1._cloud("local"), w1._proof("agree")]}), _x01b_expect,
        expected_red="F-P1-ESCALATION-SUPPRESSED", overrides=_blackout_ov(_X01_BLACKOUT_3)),
    _mk("X01c", "X01 with DNS down (rclone remote and clock proof)", "2090-12-29", 6, _noop,
        w1._schedule({"2090-12-31": [w1._cloud("dns"), w1._proof("nointernet")],
                      "2091-01-02": [w1._cloud("local"), w1._proof("agree")]}), _x01c_expect,
        expected_red="F-P1-ESCALATION-SUPPRESSED", overrides=_blackout_ov(("2090-12-31", "2091-01-01"))),
    _mk("X04c", "X04 server off 3 days over activation, cloud-only lane (--cloud-only)", "2090-12-29", 7, _noop, None,
        _x04_leg_expect("X04c"), overrides=_x04_override("cloud"), outages=w1.OUTAGES["X04"]),
    _mk("X04l", "X04 server off 3 days over activation, LAN-only lane (--lan-only)", "2090-12-29", 7, _noop, None,
        _x04_leg_expect("X04l"), overrides=_x04_override("lan"), outages=w1.OUTAGES["X04"]),
    _mk("X06w", "X06 server off 3 days mid-overlap, watchdog ticks every midnight", "2091-02-08", 8, _noop,
        _watchdog_ticks, _x06w_expect, outages=w1.OUTAGES["X06"]),
    _mk("X08b", "NAS wake fails one night (WoL timeout): LAN fails, cloud runs, one mail", X08B_START, X08B_DAYS,
        _noop, _x08b_on_day, _x08b_expect,
        overrides=((X08B_FAIL_DAY, cal.DayOverride(
            statuses={"lan": _X08B_LAN_FAIL},
            extra_mails=(cal.ExpectedMail(cal.MAIL_PROBLEM, 1, at=dt.datetime(2091, 2, 4, 21, 30, tzinfo=IST)),))),)),
]
