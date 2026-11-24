"""Scenario pack wave 4 (plan 2026-11-24 section 4, X40-X48: environment and robustness).

expect(results) reads only attributes that may be missing (getattr defaults) and never raises. Rows are
{"day","leg","status","fault"}; mails {"day","kind","fault","subject", optional "severity","key"}; notes list[str];
facts dict. Missing evidence is reported as a string (the report shows it as "no evidence", not as a pass).

Injection knobs available in the driver: rig.set_mail, rig.set_cloud, rig.set_clock_proof, files under the lane.
Not available (reported to the owner): clock travel backwards / forwards (X45, X46), an outage injected by a
scenario (X47, driver option --outage only), a 2 GB VHD (X42, gated), a window around year 2100 (X48).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from tools.livebox.scenarios.base import Scenario

# storm rule M3 (tools.livebox.oracles.check_m3): at most 4 problem mails per sim day
M3_DAY_LIMIT = 4
PROBLEM_SEVERITIES = frozenset({"FAILURE", "ACTION_REQUIRED", "WARNING"})
SUCCESS_WORD = "COMPLETE"


# ---------------------------------------------------------------- helpers
def _list(results: Any, name: str) -> list:
    v = getattr(results, name, None)
    return list(v) if isinstance(v, (list, tuple)) else []


def _facts(results: Any) -> dict:
    v = getattr(results, "facts", None)
    return v if isinstance(v, dict) else {}


def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else default


def _is_problem(mail: Any) -> bool:
    kind = str(_get(mail, "kind", "") or "").lower()
    sev = str(_get(mail, "severity", "") or "").upper()
    return kind == "problem" or sev in PROBLEM_SEVERITIES


def _date(day: Any) -> dt.date | None:
    d = getattr(day, "date", day)
    return d if isinstance(d, dt.date) else None


def _lane_dir(rig: Any) -> Path | None:
    p = getattr(rig, "lane_dir", None)
    return Path(p) if p else None


def _inside(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _noop(rig: Any) -> None:
    return None


def _note(rig: Any, text: str) -> None:
    """Keep injection evidence on the rig object (read by nothing here; the driver may copy it)."""
    try:
        lst = getattr(rig, "w4_events", None)
        if lst is None:
            lst = []
            setattr(rig, "w4_events", lst)
        lst.append(text)
    except Exception:
        pass


def _day_index(day: Any, start: str) -> int | None:
    d = _date(day)
    return (d - dt.date.fromisoformat(start)).days if d else None


# ---------------------------------------------------------------- X40 locked file
X40_START, X40_DAYS = "2091-02-10", 3


def _lock_file(path: Path) -> Any:
    """Exclusive handle (CreateFile share mode 0); returns the handle or None when not possible."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    h = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if h in (None, wintypes.HANDLE(-1).value, -1):
        return None
    return h


def _unlock(handle: Any) -> None:
    if handle is None or sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(handle)
    except Exception:
        pass


def _x40_on_day(rig: Any, day: Any) -> None:
    i = _day_index(day, X40_START)
    if i is None:
        return
    if i == 0 and getattr(rig, "_w4_lock", None) is None:
        src = Path(getattr(rig, "source_dir", ""))
        if not _inside(src, _lane_dir(rig)) or not src.is_dir():
            return
        files = sorted(p for p in src.rglob("*") if p.is_file())
        if files:
            h = _lock_file(files[0])
            setattr(rig, "_w4_lock", h)
            _note(rig, f"X40 lock {'held' if h else 'FAILED'} on {files[0].name}")
    elif i >= X40_DAYS - 1:
        _unlock(getattr(rig, "_w4_lock", None))
        setattr(rig, "_w4_lock", None)


def _x40_expect(results: Any) -> list[str]:
    out: list[str] = []
    rows = _list(results, "rows")
    mails = _list(results, "mails")
    held_days = {(dt.date.fromisoformat(X40_START) + dt.timedelta(days=n)).isoformat() for n in range(X40_DAYS - 1)}
    for r in rows:
        status = str(_get(r, "status", "") or "")
        if _get(r, "day") in held_days and SUCCESS_WORD in status:
            out.append(f"X40 {r.get('day')} {r.get('leg')}: {status} reported while a source file is locked "
                       "(status must be honest)")
    lan = [r for r in rows if _get(r, "leg") == "lan" and _get(r, "day") in held_days]
    if not lan:
        out.append("X40 evidence missing: no LAN row on a lock day")
    elif not any(_get(r, "fault") == "AAM-LAN-02" for r in lan):
        out.append("X40 record: no LAN row carries AAM-LAN-02 (compare with plan; finding if different)")
    per_fault: dict = defaultdict(int)
    for m in mails:
        if _get(m, "day") in held_days and _is_problem(m):
            per_fault[(_get(m, "fault"), str(_get(m, "key", "") or ""))] += 1
    for (fault, key), n in per_fault.items():
        if n > 1:
            out.append(f"X40 {n} mails for fault {fault} key {key or '-'} (expected one each)")
    return out


# ---------------------------------------------------------------- X41 long paths and unicode
X41_START, X41_DAYS = "2091-02-01", 3
_X41_NAMES = ["മലയാളം-ക്ലിയന്റ്.txt",
              "emoji-\U0001f4c1-file.txt", "trailing space .txt"]


def _x41_setup(rig: Any) -> None:
    src = Path(getattr(rig, "source_dir", ""))
    if not _inside(src, _lane_dir(rig)):
        return
    try:
        base = str(src.resolve())
        deep = base
        while len(deep) < 300:
            deep = os.path.join(deep, "long_segment_for_path_test_" + "x" * 20)
        ext = "\\\\?\\" + deep if sys.platform == "win32" else deep
        os.makedirs(ext, exist_ok=True)
        with open(os.path.join(ext, "deep.txt"), "w", encoding="utf-8") as fh:
            fh.write("deep file\n")
        for name in _X41_NAMES:
            p = ("\\\\?\\" + os.path.join(base, name)) if sys.platform == "win32" else os.path.join(base, name)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(name + "\n")
        _note(rig, "X41 long path and unicode files created")
    except Exception as exc:
        _note(rig, f"X41 setup failed: {exc!r}")


def _x41_expect(results: Any) -> list[str]:
    out: list[str] = []
    rows = _list(results, "rows")
    if not rows:
        out.append("X41 evidence missing: no rows")
    for r in rows:
        st = str(_get(r, "status", "") or "")
        if any(w in st for w in ("FAILED", "PARTIAL", "BLOCKED", "ABANDONED")):
            out.append(f"X41 {r.get('day')} {r.get('leg')}: {st} (long path / unicode names must converge)")
    facts = _facts(results)
    if facts.get("audit_verified") is False:
        out.append("X41 audit did not report VERIFIED")
    elif "audit_verified" not in facts:
        out.append("X41 evidence missing: audit verdict not recorded")
    return out


# ---------------------------------------------------------------- X42 disk full (gated)
X42_START, X42_DAYS = "2091-02-05", 3


def _x42_expect(results: Any) -> list[str]:
    facts = _facts(results)
    if not facts.get("x42_vhd_approved"):
        return ["X42 not run: gated (needs owner OK and a 2 GB VHD via diskpart; system change)"]
    out: list[str] = []
    rows = _list(results, "rows")
    if not any(_get(r, "fault") in ("AAM-SYS-05", "AAM-NAS-04") for r in rows):
        out.append("X42 no row with AAM-SYS-05 / AAM-NAS-04 on a full disk")
    for r in rows:
        if SUCCESS_WORD in str(_get(r, "status", "") or "") and _get(r, "fault") is None and \
                facts.get("x42_full_days") and _get(r, "day") in facts["x42_full_days"]:
            out.append(f"X42 {r.get('day')}: success row on a full disk")
    return out


# ---------------------------------------------------------------- X43 DB corruption
X43_START, X43_DAYS = "2091-02-12", 3


def _x43_on_day(rig: Any, day: Any) -> None:
    if getattr(rig, "_w4_corrupted", False):
        return
    rt = getattr(rig, "runtime_dir", None)
    if rt is None or not Path(rt).is_dir():
        return
    for db in sorted(Path(rt).rglob("manifest.db")):
        try:
            if not _inside(db, _lane_dir(rig)) or db.stat().st_size < 8192:
                continue
            with open(db, "r+b") as fh:
                fh.seek(4096)
                fh.write(b"\xa5" * 4096)
            setattr(rig, "_w4_corrupted", True)
            _note(rig, f"X43 corrupted {db.name} bytes 4096-8191 on {_date(day)}")
            return
        except Exception as exc:
            _note(rig, f"X43 corruption failed: {exc!r}")


def _x43_expect(results: Any) -> list[str]:
    out: list[str] = []
    rows = _list(results, "rows")
    corrupt_day = _facts(results).get("x43_corrupt_day") or X43_START
    for r in rows:
        if str(_get(r, "day", "")) >= str(corrupt_day) and SUCCESS_WORD in str(_get(r, "status", "") or "") \
                and _get(r, "fault") != "AAM-SYS-01":
            out.append(f"X43 {r.get('day')} {r.get('leg')}: success row without AAM-SYS-01 after DB corruption "
                       "(no silent success)")
    return out


# ---------------------------------------------------------------- X44 SMTP down then 451 then 550
X44_START, X44_DAYS = "2091-01-20", 6


def _x44_on_day(rig: Any, day: Any) -> None:
    i = _day_index(day, X44_START)
    if i is None:
        return
    try:
        if i == 0:
            rig.set_cloud("blackhole")          # real, repeated failures to alert about
        if i in (0, 1):
            rig.set_mail("stopped")              # server down 2 days
        elif i == 2:
            rig.set_mail("fail", "451 try later")
        elif i == 3:
            rig.set_mail("fail", "550 mailbox unavailable")
        elif i == 4:
            rig.set_mail("normal")
            rig.set_cloud("local")
    except Exception as exc:
        _note(rig, f"X44 knob failed on day {i}: {exc!r}")


def _x44_expect(results: Any) -> list[str]:
    out: list[str] = []
    per: dict = defaultdict(int)
    for m in _list(results, "mails"):
        if _is_problem(m):
            per[_get(m, "day")] += 1
    for d, n in sorted(per.items(), key=lambda kv: str(kv[0])):
        if n > M3_DAY_LIMIT:
            out.append(f"X44 storm: {n} problem mails on {d} (M3 limit {M3_DAY_LIMIT} per day)")
    return out


# ---------------------------------------------------------------- X45 clock backward
X45_START, X45_DAYS = "2091-01-02", 5


def _x45_expect(results: Any) -> list[str]:
    out: list[str] = []
    acts = [m for m in _list(results, "mails") if str(_get(m, "kind", "")).lower() == "activated"]
    if len(acts) > 1:
        out.append(f"X45 {len(acts)} activation mails (second activation after clock back)")
    if len(_list(results, "activated_on")) > 1:
        out.append(f"X45 activated on {len(_list(results, 'activated_on'))} days")
    if len(_list(results, "switched_on")) > 0:
        out.append(f"X45 config switched on {_list(results, 'switched_on')} (no reverse switch allowed)")
    if not _facts(results).get("clock_back_injected"):
        out.append("X45 evidence missing: no clock-back injection (driver has no clock travel knob)")
    return out


# ---------------------------------------------------------------- X46 clock +13 months
X46_START, X46_DAYS = "2091-02-20", 3


def _x46_expect(results: Any) -> list[str]:
    out: list[str] = []
    if _list(results, "switched_on"):
        out.append(f"X46 automatic switch on {_list(results, 'switched_on')} after a +13 month jump")
    if not _facts(results).get("clock_jump_injected"):
        out.append("X46 evidence missing: no clock-jump injection (driver has no clock travel knob)")
    elif not any(_get(m, "fault") == "AAM-ROL-01" for m in _list(results, "mails")) and \
            not any("ROL-01" in str(_get(a, "fault", "") or a) for a in _list(results, "approvals")):
        out.append("X46 no AAM-ROL-01 approval card or mail")
    return out


# ---------------------------------------------------------------- X47 reports during an outage
X47_START, X47_DAYS = "2091-09-28", 6   # includes Monday 2091-10-01 (1st of month)


def _x47_expect(results: Any) -> list[str]:
    out: list[str] = []
    seen: dict = defaultdict(int)
    for m in _list(results, "mails"):
        subj = str(_get(m, "subject", "") or "").lower()
        if "weekly" in subj or "monthly" in subj:
            seen[(_get(m, "day"), "weekly" if "weekly" in subj else "monthly")] += 1
    for (d, kind), n in sorted(seen.items(), key=lambda kv: str(kv[0])):
        if n > 1:
            out.append(f"X47 {n} {kind} report mails on {d} (one period must mail once)")
    if not _facts(results).get("x47_outage_injected"):
        out.append("X47 evidence missing: outage over a Monday/1st comes from the driver --outage option")
    return out


# ---------------------------------------------------------------- X48 century wrap
X48_START, X48_DAYS = "2091-03-30", 4   # placeholder: the real window (2099-12..2100-04) is outside 2090-12-01..


def _x48_expect(results: Any) -> list[str]:
    facts = _facts(results)
    if not facts.get("x48_century_window"):
        return ["X48 not run: needs a lane around 2100-01-01 / 2100-04-01, outside the 728-day window"]
    return [] if not _list(results, "switched_on") else ["X48 record: switch around the century wrap"]


SCENARIOS: list[Scenario] = [
    Scenario("X40", 4, "Locked file: exclusive handle on one source file across 21:01 and 02:01",
             X40_START, X40_DAYS, "x40-locked-file", _noop, _x40_on_day, _x40_expect),
    Scenario("X41", 4, "Long paths (> 260) and Unicode names in the source",
             X41_START, X41_DAYS, "x41-long-unicode", _x41_setup, None, _x41_expect),
    Scenario("X42", 4, "Disk full on runtime or NAS target (gated, owner OK needed)",
             X42_START, X42_DAYS, "x42-disk-full", _noop, None, _x42_expect,
             requires=("fact:x42_injected",)),
    Scenario("X43", 4, "Manifest DB corruption: bytes 4096-8191 overwritten",
             X43_START, X43_DAYS, "x43-db-corrupt", _noop, _x43_on_day, _x43_expect),
    Scenario("X44", 4, "SMTP down 2 days, then 451, then 550: no notification storm on recovery",
             X44_START, X44_DAYS, "x44-smtp-down", _noop, _x44_on_day, _x44_expect),
    Scenario("X45", 4, "Clock backward 2 days: no reverse switch, no second activation",
             X45_START, X45_DAYS, "x45-clock-back", _noop, None, _x45_expect,
             requires=("fact:clock_back_injected",)),
    Scenario("X46", 4, "Clock jump +13 months: approval card, no automatic switch",
             X46_START, X46_DAYS, "x46-clock-jump", _noop, None, _x46_expect,
             requires=("fact:clock_jump_injected",)),
    Scenario("X47", 4, "Report flows during an outage on Monday / the 1st",
             X47_START, X47_DAYS, "x47-report-outage", _noop, None, _x47_expect,
             requires=("fact:x47_outage_injected",)),
    Scenario("X48", 4, "Century wrap FY99-00 to FY00-01 (optional, finding only)",
             X48_START, X48_DAYS, "x48-century-wrap", _noop, None, _x48_expect,
             requires=("fact:x48_century_window",)),
]
