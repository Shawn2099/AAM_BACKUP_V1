"""Wave 1 scenario pack: special days x outages (plan section 4, rows X01-X19).

Every scenario is its own lane. setup/on_day only use rig knobs (never production). Day granularity:
on_day runs before the simulated day's flows, so an injection "from 21:00 the evening before" is applied at
the start of the next day. Knobs the rig does not have yet are called through `_call`, which does nothing
when the knob is absent (the run then simply lacks that injection; see MISSING_KNOBS).

OUTAGES holds the server-off windows for X04-X06 (the Scenario contract has no outage field: pass them to
run_lane with --outage).
"""
from __future__ import annotations

import datetime as dt
from typing import Callable

from tools.livebox import calendar as cal
from tools.livebox.scenarios.base import Scenario

IST = cal.IST
CURRENT, NEXT = "FY90-91", "FY91-92"

# knob name -> scenarios that need it (not in rig.py when this pack was written)
MISSING_KNOBS = {
    "kill_child_after(step)": ("X17",),   # recorded by the rig but not wired in the driver
}

OUTAGES = {
    "X04": [cal.Outage(dt.date(2090, 12, 30), dt.datetime(2091, 1, 2, 9, 0, tzinfo=IST), "latest")],
    "X05": [cal.Outage(dt.date(2091, 3, 30), dt.datetime(2091, 4, 2, 9, 0, tzinfo=IST), "latest")],
    "X06": [cal.Outage(dt.date(2091, 2, 10), dt.date(2091, 2, 13), "latest")],
}


# ---------------------------------------------------------------- helpers
def _call(rig, name, *args, **kw):
    fn = getattr(rig, name, None) if rig is not None else None
    if callable(fn):
        return fn(*args, **kw)
    return None


def _noop(rig) -> None:
    return None


def _schedule(plan: dict) -> Callable:
    """plan: ISO date -> list of callables(rig). Returns an on_day(rig, day)."""
    def on_day(rig, day):
        d = getattr(day, "date", day)
        for fn in plan.get(str(d), ()):
            fn(rig)
    return on_day


def _cloud(variant):
    return lambda rig: _call(rig, "set_cloud", variant)


def _proof(mode, skew=0.0):
    return lambda rig: _call(rig, "set_clock_proof", mode, skew)


def _knob(name, *args):
    return lambda rig: _call(rig, name, *args)


def _g(r, name, default):
    v = getattr(r, name, None)
    return default if v is None else v


def _rows(r):
    return [x for x in _g(r, "rows", []) if isinstance(x, dict)]


def _mails(r, severity=None, code=None, key=None, day=None):
    out = []
    for m in _g(r, "mails", []):
        if not isinstance(m, dict):
            continue
        if severity and str(m.get("severity", "")).upper() != severity:
            continue
        if code and str(m.get("code", "")) != code:
            continue
        if key and key not in str(m.get("key", "")):
            continue
        if day and str(m.get("day", "")) != day:
            continue
        out.append(m)
    return out


def _kind_of(m):
    """The stored notifications.kind; falls back to the subject severity when the mail has no kind."""
    return str(m.get("kind") or m.get("severity") or "").upper()


def _by_kind(r, kind, key=None):
    return [m for m in _g(r, "mails", []) if isinstance(m, dict) and _kind_of(m) == kind
            and (not key or key in str(m.get("key", "")))]


def _lan_bad(rows):
    return [x for x in rows if x.get("leg") == "lan" and x.get("status") not in cal.LAN_OK]


def _cloud_bad(rows):
    return [x for x in rows if x.get("leg") == "cloud" and x.get("status") not in cal.CLOUD_OK]


def _dup_per_key_day(sid, mails, what):
    seen, out = {}, []
    for m in mails:
        k = (m.get("key", ""), m.get("day", ""))
        seen[k] = seen.get(k, 0) + 1
    for (key, day), n in seen.items():
        if n > 1:
            out.append(f"{sid}: {n} {what} mails for key {key!r} on {day} (max 1)")
    return out


def _never_backwards(sid, r):
    fys = [fy for _, fy in sorted(_g(r, "config_fy_by_day", {}).items())]
    out = []
    for a, b in zip(fys, fys[1:]):
        if b < a:
            out.append(f"{sid}: configured FY went backwards ({a} -> {b})")
    return out


def _switch_exactly(sid, r, day):
    sw = list(_g(r, "switched_on", []))
    if sw != [day]:
        return [f"{sid}: configured FY switch expected exactly on {day}, observed {sw}"]
    return []


def _no_switch_on_unproven(sid, r):
    out = []
    proven = _g(r, "clock_proven_by_day", {})
    for d in _g(r, "switched_on", []):
        if proven.get(d) is False:
            out.append(f"{sid}: switch on {d} while the clock was unproven (AAM-ROL-03 must make it wait)")
    return out


def _scn(sid, title, start, days, setup, on_day, expect, expected_red=None, injection_wired=True, overrides=()):
    return Scenario(id=sid, wave=1, title=title, start=start, days=days, lane=f"w1_{sid.lower()}",
                    setup=setup, on_day=on_day, expect=expect, expected_red=expected_red,
                    outages=tuple(OUTAGES.get(sid, ())), injection_wired=injection_wired, overrides=tuple(overrides))


# ---------------------------------------------------------------- X01
def _x01_expect(r):
    s, out = "X01", []
    key = "rollover:activation:FY91-92"
    warn = _by_kind(r, "WARNING", key=key)
    act = _by_kind(r, "ACTION_REQUIRED", key=key)
    info = [m for m in _by_kind(r, "INFO") if "rollover:activated" in str(m.get("key", ""))]
    if len(warn) != 1:
        out.append(f"{s}: expected exactly 1 WARNING for {key}, observed {len(warn)}")
    if len(act) != 1:
        out.append(f"{s}: expected exactly 1 ACTION_REQUIRED for {key}, observed {len(act)} "
                   f"(F-P1-ESCALATION-SUPPRESSED)")
    if len(info) != 1:
        out.append(f"{s}: expected exactly 1 INFO activated mail, observed {len(info)}")
    if list(_g(r, "activated_on", [])) != ["2091-01-02"]:
        out.append(f"{s}: activation expected exactly on 2091-01-02, observed {list(_g(r, 'activated_on', []))}")
    fail = [m for m in _by_kind(r, "FAILURE") if str(m.get("day", "")) in ("2090-12-31", "2091-01-01")]
    if len(fail) > 2:
        out.append(f"{s}: {len(fail)} cloud FAILURE mails over 36 h (max 2)")
    bad = _lan_bad(_rows(r))
    if bad:
        out.append(f"{s}: LAN must stay green all days, observed {len(bad)} non-success LAN rows")
    return out


# ---------------------------------------------------------------- X02
def _x02_expect(r):
    s = "X02"
    out = _no_switch_on_unproven(s, r)
    out += _never_backwards(s, r)
    sw = list(_g(r, "switched_on", []))
    if "2091-04-01" in sw:
        out.append(f"{s}: switch happened on 2091-04-01 while the clock proof was missing (AAM-ROL-03)")
    if sw != ["2091-04-02"]:
        out.append(f"{s}: switch expected exactly on 2091-04-02, observed {sw}")
    ann = [m for m in _g(r, "mails", []) if isinstance(m, dict)
           and (m.get("kind") == cal.MAIL_ANNOUNCE or "announce" in str(m.get("key", "")))]
    if len(ann) > 1:
        out.append(f"{s}: {len(ann)} announce mails (max 1)")
    if _g(r, "config_fy_by_day", {}).get("2091-04-01") not in (None, CURRENT):
        out.append(f"{s}: config on 2091-04-01 must stay {CURRENT}")
    return out


# ---------------------------------------------------------------- X03
def _x03_expect(r):
    s, out = "X03", []
    blocked = [x for x in _rows(r) if x.get("status") == "ROLLOVER_BLOCKED" and x.get("day") == "2091-04-01"]
    if not blocked:
        out.append(f"{s}: expected a ROLLOVER_BLOCKED row on 2091-04-01")
    act = [m for m in _mails(r, "ACTION_REQUIRED", code="AAM-ROL-02", day="2091-04-01")
           if "blocked:rollover" in str(m.get("key", ""))]
    if not act:
        out.append(f"{s}: expected ACTION_REQUIRED blocked:rollover AAM-ROL-02 on 2091-04-01")
    out += _switch_exactly(s, r, "2091-04-02")
    out += _never_backwards(s, r)
    return out


# ---------------------------------------------------------------- X04 / X05 / X06 (outages via OUTAGES)
def _m4(sid, r):
    out = []
    per_key = {}
    for m in _mails(r, code="AAM-SYS-07"):
        per_key[m.get("key", "")] = per_key.get(m.get("key", ""), 0) + 1
    for k, n in per_key.items():
        if n > 1:
            out.append(f"{sid}: {n} missed mails for key {k!r} (max 1 per leg and FY)")
    stale = [m for m in _g(r, "mails", []) if isinstance(m, dict) and str(m.get("key", "")).startswith("stale:")]
    out += _dup_per_key_day(sid, stale, "overdue")
    return out


def _x04_expect(r):
    s = "X04"
    out = _m4(s, r)
    act = list(_g(r, "activated_on", []))
    if len(act) != 1:
        out.append(f"{s}: activation expected exactly once, observed {act}")
    return out


def _x05_expect(r):
    s = "X05"
    out = _never_backwards(s, r) + _m4(s, r)
    sw = list(_g(r, "switched_on", []))
    if len(sw) != 1 or sw[0] < "2091-04-02":
        out.append(f"{s}: expected exactly one switch on or after 2091-04-02, observed {sw}")
    return out


def _x06_expect(r):
    s = "X06"
    stale = [m for m in _g(r, "mails", []) if isinstance(m, dict) and str(m.get("key", "")).startswith("stale:")]
    out = _dup_per_key_day(s, stale, "overdue")
    per_day = {}
    for m in stale:
        per_day[m.get("day", "")] = per_day.get(m.get("day", ""), 0) + 1
    for d, n in per_day.items():
        if n > 4:
            out.append(f"{s}: {n} overdue mails on {d} (max 4: one per leg and FY)")
    return out


# ---------------------------------------------------------------- X07
def _x07_expect(r):
    # record-only scenario: the late 06:00 outcome is observed; only sanity is enforced
    s = "X07"
    out = _never_backwards(s, r)
    if len(_g(r, "switched_on", [])) > 1:
        out.append(f"{s}: more than one switch observed {list(_g(r, 'switched_on', []))}")
    return out


# ---------------------------------------------------------------- X08
def _x08_expect(r):
    s, out = "X08", []
    bad = [x for x in _lan_bad(_rows(r)) if x.get("day") == "2090-12-31"]
    if not bad:
        out.append(f"{s}: expected failing LAN rows on 2090-12-31 (NAS unreachable)")
    lan_mails = [m for m in _g(r, "mails", []) if isinstance(m, dict) and m.get("day") == "2090-12-31"
                 and str(m.get("severity", "")).upper() in ("FAILURE", "WARNING", "ACTION_REQUIRED")
                 and "lan" in str(m.get("key", "")).lower()]
    out += _dup_per_key_day(s, lan_mails, "LAN failure")
    later = [x for x in _rows(r) if x.get("leg") == "lan" and str(x.get("day", "")) >= "2091-01-01"]
    if later and not any(x.get("status") in cal.LAN_OK for x in later):
        out.append(f"{s}: LAN did not recover after the NAS came back")
    return out


# ---------------------------------------------------------------- X09
def _x09_expect(r):
    s, out = "X09", []
    # owner 2026-11-23: switch with cloud only, the NAS is never touched by rollover (F-OWN-1)
    if "2091-04-01" not in list(_g(r, "switched_on", [])):
        out.append(f"{s}: switch must still happen on 2091-04-01 with the NAS unreachable (F-OWN-1)")
    if any(x.get("status") == "ROLLOVER_BLOCKED" for x in _rows(r)):
        out.append(f"{s}: LAN final failure must not block the rollover (F-OWN-1)")
    return out


# ---------------------------------------------------------------- X10
def _x10_expect(r):
    s, out = "X10", []
    if list(_g(r, "junction_target_entries", [])):
        out.append(f"{s}: write through the junction link, target now holds {list(_g(r, 'junction_target_entries', []))}")
    if list(_g(r, "activated_on", [])):
        out.append(f"{s}: next FY activated despite the junction {list(_g(r, 'activated_on', []))}")
    if not _mails(r, "WARNING", code="AAM-ROL-07", day="2090-12-31"):
        out.append(f"{s}: expected ROL-07 WARNING on 2090-12-31")
    if not _mails(r, "ACTION_REQUIRED", code="AAM-ROL-07", day="2091-01-01"):
        out.append(f"{s}: expected ROL-07 ACTION_REQUIRED on 2091-01-01")
    return out


# ---------------------------------------------------------------- X11
def _x11_expect(r):
    s, out = "X11", []
    nrows = [x for x in _rows(r) if x.get("fy") == NEXT and str(x.get("day", "")) >= "2091-01-15"]
    if not any(x.get("fault") == "AAM-SRC-01" for x in nrows):
        out.append(f"{s}: expected AAM-SRC-01 failures for {NEXT} after 2091-01-15")
    crows = [x for x in _rows(r) if x.get("fy") == CURRENT and x.get("status")
             not in cal.CLOUD_OK + cal.LAN_OK]
    if crows:
        out.append(f"{s}: current-FY passes must stay green, {len(crows)} failed")
    out += _dup_per_key_day(s, _mails(r, code="AAM-SRC-01"), "AAM-SRC-01")
    if any(NEXT in str(p) for p in _g(r, "created_paths", [])):
        out.append(f"{s}: the {NEXT} source folder was recreated")
    return out


# ---------------------------------------------------------------- X12
def _x12_expect(r):
    s, out = "X12", []
    if not _mails(r, code="AAM-ROL-07", day="2090-12-31"):
        out.append(f"{s}: expected ROL-07 (FOLDER_FAILED) mail on 2090-12-31")
    if list(_g(r, "junction_target_entries", [])):
        out.append(f"{s}: unexpected entries in the junction target")
    if list(_g(r, "activated_on", [])) != ["2091-01-04"]:
        out.append(f"{s}: activation expected exactly on 2091-01-04, observed {list(_g(r, 'activated_on', []))}")
    return out


# ---------------------------------------------------------------- X13
def _x13_expect(r):
    s, out = "X13", []
    if "2090-12-31" in list(_g(r, "activated_on", [])):
        out.append(f"{s}: activation on a skewed clock (2090-12-31)")
    if _g(r, "clock_proven_by_day", {}).get("2090-12-31") is True and not _g(r, "rows", []):
        out.append(f"{s}: clock reported proven on the skewed day")
    cloud = [x for x in _rows(r) if x.get("leg") == "cloud" and x.get("day") == "2090-12-31"]
    if cloud and not any(x.get("fault") == "AAM-CLK-01" for x in _cloud_bad(cloud)):
        out.append(f"{s}: expected CLOUD_FAILED AAM-CLK-01 on 2090-12-31")
    return out


# ---------------------------------------------------------------- X14
def _x14_expect(r):
    s, out = "X14", []
    apps = [a for a in _g(r, "approvals", []) if isinstance(a, dict)]
    for a in apps:
        if a.get("phrase") != "APPROVE" and (a.get("applied") or a.get("http_status") == 200):
            out.append(f"{s}: wrong approval phrase {a.get('phrase')!r} was accepted (http {a.get('http_status')})")
    good = [a for a in apps if a.get("phrase") == "APPROVE" and a.get("applied")]
    if len(good) > 1:
        out.append(f"{s}: approval applied {len(good)} times (must be used once)")
    if apps and not good:
        out.append(f"{s}: the exact phrase APPROVE was never applied")
    sw = list(_g(r, "switched_on", []))
    if apps and not sw:
        out.append(f"{s}: no switch after the approval")
    return out


# ---------------------------------------------------------------- X15
def _x15_expect(r):
    s, out = "X15", []
    if not _mails(r, code="AAM-ROL-04", day="2091-04-01"):
        out.append(f"{s}: expected ROLLOVER_FAILED AAM-ROL-04 on 2091-04-01")
    out += _switch_exactly(s, r, "2091-04-02")
    if len(_g(r, "archive_calls", [])) > 1:
        out.append(f"{s}: archive ran {len(_g(r, 'archive_calls', []))} times (double archive)")
    return out


# ---------------------------------------------------------------- X16
def _x16_expect(r):
    s, out = "X16", []
    warn = _mails(r, "WARNING", code="AAM-ROL-05")
    act = _mails(r, "ACTION_REQUIRED", code="AAM-ROL-05")
    if not warn:
        out.append(f"{s}: expected daily ROL-05 WARNING mails")
    if not act:
        out.append(f"{s}: expected ROL-05 ACTION_REQUIRED after archive_alert_after_days (3)")
    elif warn and min(m.get("day", "") for m in act) <= min(m.get("day", "") for m in warn):
        out.append(f"{s}: ROL-05 ACTION_REQUIRED must come after the first WARNING")
    out += _switch_exactly(s, r, "2091-04-01")
    if len(_g(r, "archive_calls", [])) < 2:
        out.append(f"{s}: the archive step was not retried (calls: {len(_g(r, 'archive_calls', []))})")
    return out


# ---------------------------------------------------------------- X17
def _x17_expect(r):
    s, out = "X17", []
    if not any(x.get("status") == "ROLLOVER_ABANDONED" for x in _rows(r)):
        out.append(f"{s}: expected a swept ROLLOVER_ABANDONED row after the crash")
    out += _switch_exactly(s, r, "2091-04-02")
    if list(_g(r, "orphan_robocopy", [])):
        out.append(f"{s}: orphan robocopy processes left behind {list(_g(r, 'orphan_robocopy', []))}")
    if len(_g(r, "archive_calls", [])) > 1:
        out.append(f"{s}: archive repeated after the resume ({len(_g(r, 'archive_calls', []))} calls)")
    return out


# ---------------------------------------------------------------- X18
def _x18_expect(r):
    s, out = "X18", []
    # desired behaviour (owner W9/W14, not built): a changed-bytes file must hold the switch back
    if "2091-04-01" in list(_g(r, "switched_on", [])):
        out.append(f"{s}: switched on 2091-04-01 without noticing the changed NAS file (F-OWN-2, hash before close)")
    return out


# ---------------------------------------------------------------- X19
def _x19_expect(r):
    s, out = "X19", []
    n = _g(r, "lan_runs_0600", 0)
    if n:
        out.append(f"{s}: FINDING: {n} LAN run(s) inside the 06:00 check (ARCHITECTURE 5.2 / owner 2026-11-23)")
    for a in _g(r, "approvals", []):
        if isinstance(a, dict) and a.get("applied"):
            out.append(f"{s}: FINDING: a G1 approval was applied (phrase {a.get('phrase')!r})")
    return out


SCENARIOS = [
    _scn("X01", "No internet across activation (cloud + clock proof)", "2090-12-29", 6, _noop,
         _schedule({"2090-12-31": [_cloud("blackhole"), _proof("nointernet")],
                    "2091-01-02": [_cloud("local"), _proof("agree")]}), _x01_expect,
         expected_red="F-P1-ESCALATION-SUPPRESSED",
         overrides=(("2090-12-31", cal.DayOverride(cloud_down=True, clock_unproven=True)),
                    ("2091-01-01", cal.DayOverride(cloud_down=True, clock_unproven=True)))),
    _scn("X02", "No internet on switch day (clock unproven)", "2091-03-30", 5, _noop,
         _schedule({"2091-04-01": [_cloud("blackhole"), _proof("nointernet")],
                    "2091-04-02": [_cloud("local"), _proof("agree")]}), _x02_expect,
         overrides=(("2091-04-01", cal.DayOverride(cloud_down=True, clock_unproven=True)),)),
    _scn("X03", "No internet on switch day, clock proven (G6 blocks)", "2091-03-30", 5, _noop,
         _schedule({"2091-04-01": [_cloud("blackhole")], "2091-04-02": [_cloud("local")]}), _x03_expect,
         overrides=(("2091-04-01", cal.DayOverride(cloud_down=True)),)),
    _scn("X04", "Server off 3 days over activation", "2090-12-29", 7, _noop, None, _x04_expect,
         overrides=(("2091-01-02", cal.DayOverride(
             stale_ok=("cloud",),
             extra_mails=(cal.ExpectedMail(cal.MAIL_PROBLEM, 1, "stale:cloud",
                                           dt.datetime(2091, 1, 2, 23, 30, tzinfo=IST),
                                           severity="ACTION_REQUIRED", code="AAM-SYS-03"),))),)),
    _scn("X05", "Server off 3 days over the switch", "2091-03-29", 6, _noop, None, _x05_expect),
    _scn("X06", "Server off 3 days mid-overlap", "2091-02-08", 8, _noop, None, _x06_expect),
    _scn("X07", "Rollover day missed, late 06:00 run (record)", "2091-03-31", 4, _noop, None, _x07_expect),
    _scn("X08", "NAS unreachable on activation night", "2090-12-30", 4, _noop,
         _schedule({"2090-12-31": [_knob("set_nas", "192.0.2.1")],
                    "2091-01-01": [_knob("set_nas", None)]}), _x08_expect),
    _scn("X09", "NAS unreachable on switch day (F-OWN-1)", "2091-03-31", 4, _noop,
         _schedule({"2091-04-01": [_knob("set_nas", "192.0.2.1")],
                    "2091-04-02": [_knob("set_nas", None)]}), _x09_expect, expected_red="F-OWN-1"),
    _scn("X10", "Junction in place of the next-year folder", "2090-12-29", 5, _noop,
         _schedule({"2090-12-30": [_knob("make_junction", NEXT, "elsewhere")]}), _x10_expect),
    _scn("X11", "Next-year folder deleted after activation", "2091-01-13", 8, _noop,
         _schedule({"2091-01-15": [_knob("delete_source_folder", NEXT)]}), _x11_expect),
    _scn("X12", "Source parent not writable at activation", "2090-12-29", 8, _noop,
         _schedule({"2090-12-31": [_knob("deny_source_parent_write", True)],
                    "2091-01-03": [_knob("deny_source_parent_write", False)]}), _x12_expect),
    _scn("X13", "Clock skew +1 h on activation day", "2090-12-30", 4, _noop,
         _schedule({"2090-12-31": [_proof("skew", 3600.0)], "2091-01-01": [_proof("agree")]}), _x13_expect),
    _scn("X14", "Forced switch with the exact approval phrase", "2091-03-31", 4, _noop,
         _schedule({"2091-04-01": [_knob("set_nas", "192.0.2.1")],
                    "2091-04-02": [_knob("approve", "approve "), _knob("approve", "APPROVE")]}),
         _x14_expect),
    _scn("X15", "Failed switch step (config file locked)", "2091-03-31", 4, _noop,
         _schedule({"2091-04-01": [_knob("deny_config_write", True)],
                    "2091-04-02": [_knob("deny_config_write", False)]}), _x15_expect),
    _scn("X16", "Archive step fails for five days", "2091-03-31", 7, _noop,
         _schedule({"2091-04-01": [_knob("fail_archive", True)],
                    "2091-04-06": [_knob("fail_archive", False)]}), _x16_expect),
    _scn("X17", "Crash in the middle of the rollover", "2091-03-31", 4, _noop,
         _schedule({"2091-04-01": [_knob("kill_child_after", "final_backups")]}), _x17_expect,
         injection_wired=False),
    _scn("X18", "Hash before close (not built, F-OWN-2)", "2091-03-31", 3, _noop,
         _schedule({"2091-04-01": [_knob("tamper_nas_file", CURRENT)]}), _x18_expect, expected_red="F-OWN-2"),
    _scn("X19", "Doc conflicts on the switch day (record)", "2091-03-31", 3, _noop,
         _schedule({"2091-04-01": [_knob("approve", "APPROVE")]}), _x19_expect),
]
