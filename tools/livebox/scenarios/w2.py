"""Wave 2 scenarios X20-X33 (plan section 4: guards, approvals, folder states).

Each scenario is its own lane starting 2090-12-01 (activation 12-31, switch 04-01). setup/on_day use only
rig knobs and plain file operations inside the lane folder (rig.lane_dir, rig.source_dir, rig.nas_dir,
rig.runtime_dir, rig.config_path). expect() reads run_history rows, mails and approvals tolerantly and
returns failure strings that start with the scenario id.
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from tools.livebox import churn
from tools.livebox.scenarios.base import Scenario

C_FY = "FY90-91"
N_FY = "FY91-92"
MARKER = ".AAM_TARGET_MOUNTED"
START = "2090-12-01"


# ------------------------------------------------------------------------------------------ date helpers
def _date(day: Any) -> Optional[dt.date]:
    d = getattr(day, "date", day)
    if callable(d):
        try:
            d = d()
        except Exception:
            return None
    if isinstance(d, dt.datetime):
        return d.date()
    if isinstance(d, dt.date):
        return d
    try:
        return dt.date.fromisoformat(str(d)[:10])
    except Exception:
        return None


def _md(month: int, dom: int) -> dt.date:
    return dt.date(2090 if month >= 12 else 2091, month, dom)


def _iso(month: int, dom: int) -> str:
    return _md(month, dom).isoformat()


def _days_to(month: int, dom: int, extra: int = 2) -> int:
    return (_md(month, dom) - dt.date.fromisoformat(START)).days + extra


def _is(day: Any, month: int, dom: int) -> bool:
    return _date(day) == _md(month, dom)


# ------------------------------------------------------------------------------------------ rig helpers
def _src_root(rig: Any) -> Path:
    return Path(rig.lane_dir) / "src"


def _nas_root(rig: Any) -> Path:
    return Path(rig.lane_dir) / "nas" / "backup"


def _write_files(folder: Path, count: int, tag: str = "office") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (folder / f"{tag}_{i:02d}.txt").write_text(f"{tag} file {i}\n" * 20, encoding="utf-8")


def _edit_config(rig: Any, mutate: Callable[[dict], None]) -> None:
    """Rewrite the lane config file (validated by the production model) and keep rig.config in step."""
    import yaml

    path = Path(rig.config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(raw)
    try:
        from models.config import AppConfig

        AppConfig(**raw)
    except ImportError:
        pass
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)
    try:
        rig.config = raw
    except Exception:
        pass


def _set_rollover(rig: Any, **kv: Any) -> None:
    _edit_config(rig, lambda raw: raw.setdefault("rollover", {}).update(kv))


# ------------------------------------------------------------------------------------------ result helpers
def _g(res: Any, name: str, default: Any) -> Any:
    v = getattr(res, name, None)
    return default if v is None else v


def _rows(res: Any) -> list:
    return [r for r in _g(res, "rows", []) if isinstance(r, dict)]


def _mails(res: Any) -> list:
    return [m for m in _g(res, "mails", []) if isinstance(m, dict)]


def _approvals(res: Any) -> list:
    return [a for a in _g(res, "approvals", []) if isinstance(a, dict)]


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _blocked(r: dict) -> bool:
    return "BLOCKED" in _s(r.get("status")).upper()


def _failed(r: dict) -> bool:
    return "FAILED" in _s(r.get("status")).upper()


def _has_fault(r: dict, code: str) -> bool:
    return code in _s(r.get("fault"))


def _sel(res: Any, *, leg: Optional[str] = None, fy: Optional[str] = None, day: Optional[str] = None,
         since: Optional[str] = None, until: Optional[str] = None) -> list:
    out = []
    for r in _rows(res):
        d = _s(r.get("day"))[:10]
        if leg and r.get("leg") != leg:
            continue
        if fy and r.get("fy") != fy:
            continue
        if day and d != day:
            continue
        if since and d < since:
            continue
        if until and d > until:
            continue
        out.append(r)
    return out


def _mail_text(m: dict) -> str:
    return " ".join(_s(m.get(k)) for k in ("subject", "code", "fault", "key"))


def _mail_has(m: dict, code: str) -> bool:
    return code in _mail_text(m)


def _expect_block(sid: str, res: Any, *, leg: str, fy: str, day: str, code: str, label: str) -> list:
    rows = _sel(res, leg=leg, fy=fy, day=day)
    if not rows:
        return [f"{sid}: no {leg} row for {fy} on {day} ({label})"]
    bad = [r for r in rows if not (_blocked(r) and _has_fault(r, code))]
    if bad:
        return [f"{sid}: {leg} {fy} on {day} expected BLOCKED {code} ({label}), got "
                f"{[(r.get('status'), r.get('fault')) for r in bad]}"]
    return []


def _no_block(sid: str, res: Any, *, fy: str, since: str, until: Optional[str] = None,
              leg: Optional[str] = None, label: str = "") -> list:
    bad = [r for r in _sel(res, leg=leg, fy=fy, since=since, until=until) if _blocked(r)]
    if bad:
        return [f"{sid}: {fy} must not be blocked {label}: "
                f"{[(r.get('day'), r.get('leg'), r.get('status'), r.get('fault')) for r in bad[:3]]}"]
    return []


# ------------------------------------------------------------------------------------------ X20
def _x20_on_day(rig, day):
    if _is(day, 2, 5):
        def mut(raw):
            paths = raw.setdefault("paths", {})
            dest = _s(paths.get("lan_destination"))
            paths["lan_destination"] = dest.replace("\\backup\\" + C_FY, "\\backup2\\" + C_FY)
        (_nas_root(rig).parent / "backup2" / C_FY).mkdir(parents=True, exist_ok=True)
        _edit_config(rig, mut)


def _x20_expect(res):
    out = _expect_block("X20", res, leg="lan", fy=C_FY, day=_iso(2, 5), code="AAM-NAS-08",
                        label="G4 destination changed")
    n_bad = [r for r in _sel(res, leg="lan", fy=N_FY, day=_iso(2, 5)) if _has_fault(r, "AAM-NAS-08")]
    if n_bad:
        out.append("X20: the G4 block must be per FY (only C), N lan row also got AAM-NAS-08")
    return out


# ------------------------------------------------------------------------------------------ X20b
def _x20b_setup(rig):
    _edit_config(rig, lambda raw: raw.setdefault("guards", {}).update(forbid_local_destination=True))


def _x20b_expect(res):
    lan = _sel(res, leg="lan", fy=C_FY)
    if not lan:
        return ["X20b: no lan rows for C"]
    bad = [r for r in lan if not (_blocked(r) and _has_fault(r, "AAM-NAS-07"))]
    if bad:
        return [f"X20b: forbid_local_destination expected LAN_BLOCKED AAM-NAS-07 on every lan row, got "
                f"{[(r.get('day'), r.get('status'), r.get('fault')) for r in bad[:3]]}"]
    return []


# ------------------------------------------------------------------------------------------ X21
def _x21_on_day(rig, day):
    if _is(day, 2, 10):
        m = _nas_root(rig) / N_FY / MARKER
        if m.exists():
            m.unlink()


def _x21_expect(res):
    out = []
    c_bad = [r for r in _sel(res, leg="lan", fy=C_FY)
             if _blocked(r) and _has_fault(r, "AAM-NAS-10")]
    if c_bad:
        out.append("X21: the N marker block must never run or block C (G5 approval fy check): "
                   f"{[(r.get('day'), r.get('status')) for r in c_bad[:3]]}")
    out += _expect_block("X21", res, leg="lan", fy=N_FY, day=_iso(2, 10), code="AAM-NAS-10",
                         label="G5 marker missing on N")
    for a in _approvals(res):
        if a.get("fy") == C_FY and _s(a.get("status")).upper() == "APPROVED":
            out.append("X21: an approval of the N pass ran/approved C")
    return out


# ------------------------------------------------------------------------------------------ X22
def _x22_on_day(rig, day):
    if _is(day, 2, 20):
        n_src = _src_root(rig) / N_FY
        if n_src.is_dir():
            churn.apply_guard_event(n_src, _date(day), "x22", "mass_delete")


def _x22_expect(res):
    out = []
    d = _iso(2, 20)
    for leg in ("cloud", "lan"):
        out += _expect_block("X22", res, leg=leg, fy=N_FY, day=d, code="AAM-DEL-01", label="G3 mass delete on N")
    out += _no_block("X22", res, fy=C_FY, since=d, until=d, label="(G3 hit only N)")
    return out


# ------------------------------------------------------------------------------------------ X23
def _x23_on_day(rig, day):
    if _is(day, 2, 20):
        churn.apply_guard_event(_src_root(rig) / C_FY, _date(day), "x23", "shrink")


def _x23_expect(res):
    out = []
    d = _iso(2, 20)
    rows = _sel(res, fy=C_FY, day=d)
    if not rows:
        return ["X23: no C rows on " + d]
    if not any(_blocked(r) and _has_fault(r, "AAM-SRC-04") for r in rows):
        out.append(f"X23: C shrunk by 25 percent expected BLOCKED AAM-SRC-04, got "
                   f"{[(r.get('leg'), r.get('status'), r.get('fault')) for r in rows]}")
    return out


# ------------------------------------------------------------------------------------------ X24
def _x24_on_day(rig, day):
    if _is(day, 2, 20):
        churn.apply_guard_event(_src_root(rig) / C_FY, _date(day), "x24", "wipe")


def _x24_expect(res):
    out = []
    d = _iso(2, 20)
    for leg in ("cloud", "lan"):
        out += _expect_block("X24", res, leg=leg, fy=C_FY, day=d, code="AAM-SRC-03", label="G1 empty source")
    return out


# ------------------------------------------------------------------------------------------ X25 / X26 / X27 / X28
def _x25_on_day(rig, day):
    if _is(day, 12, 15):
        _write_files(_src_root(rig) / N_FY, 20)


def _x25_expect(res):
    out = []
    alarm = [m for m in _mails(res) if _mail_has(m, "ROL-06")]
    on_16 = [m for m in alarm if _s(m.get("day"))[:10] == _iso(12, 16)]
    if len(on_16) != 1 or _s(on_16[0].get("severity")).upper() != "ACTION_REQUIRED":
        out.append(f"X25: expected exactly 1 ACTION_REQUIRED ROL-06 sibling alarm on {_iso(12, 16)}, got "
                   f"{[(m.get('day'), m.get('severity')) for m in alarm]}")
    if len([m for m in alarm if _s(m.get("severity")).upper() == "ACTION_REQUIRED"]) > 1:
        out.append("X25: sibling alarm sent more than once")
    n_rows = _sel(res, fy=N_FY, since=_iso(12, 31))
    if not n_rows:
        out.append("X25: no N rows after adoption on 12-31")
    elif any(_failed(r) for r in n_rows):
        out.append("X25: N passes failed after adoption")
    return out


def _x26_on_day(rig, day):
    if _is(day, 12, 15):
        _write_files(_src_root(rig) / "FY 91-92", 5)


def _x26_expect(res):
    acts = [m for m in _mails(res)
            if _s(m.get("severity")).upper() == "ACTION_REQUIRED" and _s(m.get("day"))[:10] >= _iso(12, 15)]
    if not acts:
        return ["X26: FINDING if silent: no ACTION_REQUIRED mail for a misnamed 'FY 91-92' folder"]
    return []


def _x27_on_day(rig, day):
    if _is(day, 12, 15):
        (_src_root(rig) / N_FY).mkdir(parents=True, exist_ok=True)


def _x27_expect(res):
    n_rows = _sel(res, fy=N_FY, since=_iso(12, 31))
    if not n_rows:
        return ["X27: empty office-made N folder was not adopted (no N rows after 12-31)"]
    if any(_failed(r) for r in n_rows):
        return ["X27: N passes failed after adopting the empty folder"]
    return []


def _x28_on_day(rig, day):
    if _is(day, 12, 15):
        nas_n = _nas_root(rig) / N_FY
        _write_files(nas_n, 5, "nasfile")
        m = nas_n / MARKER
        if m.exists():
            m.unlink()


def _x28_expect(res):
    out = []
    n_lan = _sel(res, leg="lan", fy=N_FY, since=_iso(12, 31))
    if not any(_blocked(r) and _has_fault(r, "AAM-NAS-10") for r in n_lan):
        out.append("X28: N lan pass should ask (LAN_BLOCKED AAM-NAS-10) for a NAS folder with files and no marker")
    out += _no_block("X28", res, fy=C_FY, since=_iso(12, 1), leg="lan", label="(C unaffected)")
    return out


# ------------------------------------------------------------------------------------------ X29
def _x29_on_day(rig, day):
    if _is(day, 3, 31):
        churn.apply_guard_event(_src_root(rig) / C_FY, _date(day), "x29", "mass_delete")


def _x29_expect(res):
    out = []
    for a in _approvals(res):
        if a.get("fy") == C_FY and (a.get("http") == 200 or _s(a.get("status")).upper() == "APPROVED"):
            out.append("X29: approval of closed FY90-91 after the switch must be refused (409), got "
                       f"{a.get('status')}/{a.get('http')}")
    return out


# ------------------------------------------------------------------------------------------ X30
def _x30_on_day(rig, day):
    if _is(day, 2, 12):
        rt = Path(rig.runtime_dir)
        for p in sorted(rt.glob("manifest.db*")):
            try:
                p.rename(p.with_name(p.name + ".lost"))
            except OSError:
                pass


def _x30_expect(res):
    bad = [r for r in _sel(res, since=_iso(2, 12)) if _failed(r)]
    if bad:
        return ["X30: after the manifest wipe runs failed: "
                f"{[(r.get('day'), r.get('leg'), r.get('fy'), r.get('status')) for r in bad[:3]]}"]
    return []


# ------------------------------------------------------------------------------------------ X31
def _x31_on_day(rig, day):
    if _is(day, 2, 1):
        _set_rollover(rig, overlap_enabled=False)
    elif _is(day, 2, 3):
        _set_rollover(rig, overlap_enabled=True)


def _x31_expect(res):
    out = []
    off = _sel(res, fy=N_FY, since=_iso(2, 1), until=_iso(2, 2))
    if off:
        out.append(f"X31: N passes must stop at once while overlap is off, got {len(off)} rows on 02-01..02-02")
    if not _sel(res, fy=N_FY, since=_iso(2, 3)):
        out.append("X31: N passes did not resume after overlap was switched on again")
    if not _sel(res, fy=N_FY, until=_iso(1, 31)):
        out.append("X31: no N passes before the switch-off")
    return out


# ------------------------------------------------------------------------------------------ X32
def _x32_setup(rig):
    _set_rollover(rig, overlap_enabled=False)


def _x32_on_day(rig, day):
    if _is(day, 12, 20):
        _write_files(_src_root(rig) / N_FY, 5)


def _x32_expect(res):
    out = []
    if _sel(res, fy=N_FY):
        out.append("X32: overlap disabled but N rows exist (activation must not happen)")
    if _g(res, "activated_on", []):
        out.append(f"X32: activated_on must be empty, got {_g(res, 'activated_on', [])}")
    if not [m for m in _mails(res) if _mail_has(m, "ROL-06")]:
        out.append("X32: expected a ROL-06 alarm for the N folder that has files")
    return out


# ------------------------------------------------------------------------------------------ X33
def _x33_on_day(rig, day):
    if _is(day, 1, 5):
        _set_rollover(rig, overlap_activate_on="01-15")


def _x33_expect(res):
    out = []
    act = list(_g(res, "activated_on", []))
    if len(act) > 1:
        out.append(f"X33: activation recorded more than once: {act}")
    if not _sel(res, fy=N_FY, since=_iso(1, 6)):
        out.append("X33: N must stay active after the activation day was edited")
    elif not _sel(res, fy=N_FY, since=_iso(1, 16)):
        out.append("X33: no N passes after the new activation day 01-15")
    return out


SCENARIOS = [
    Scenario("X20", 2, "G4: lan_destination edited to backup2 (Feb 5)", START, _days_to(2, 5, 6),
             "x20_g4_destination", lambda rig: None, _x20_on_day, _x20_expect),
    Scenario("X20b", 2, "G4 local: forbid_local_destination", START, 10,
             "x20b_g4_local", _x20b_setup, None, _x20b_expect),
    Scenario("X21", 2, "G5: marker deleted in NAS FY91-92 (Feb 10)", START, _days_to(2, 10, 4),
             "x21_g5_marker", lambda rig: None, _x21_on_day, _x21_expect),
    Scenario("X22", 2, "G3: 15 percent of N files deleted (Feb 20)", START, _days_to(2, 20, 3),
             "x22_g3_mass_delete", lambda rig: None, _x22_on_day, _x22_expect, n_deletes=True),
    Scenario("X23", 2, "G2: C shrunk by 25 percent (Feb 20)", START, _days_to(2, 20, 3),
             "x23_g2_shrink", lambda rig: None, _x23_on_day, _x23_expect),
    Scenario("X24", 2, "G1: C source wiped (Feb 20)", START, _days_to(2, 20, 3),
             "x24_g1_wipe", lambda rig: None, _x24_on_day, _x24_expect),
    Scenario("X25", 2, "Office-made src FY91-92 with 20 files (Dec 15)", START, _days_to(1, 5, 1),
             "x25_office_next_fy", lambda rig: None, _x25_on_day, _x25_expect),
    Scenario("X26", 2, "Misnamed src 'FY 91-92' with files", START, _days_to(1, 5, 1),
             "x26_misnamed_folder", lambda rig: None, _x26_on_day, _x26_expect),
    Scenario("X27", 2, "Empty office-made src FY91-92", START, _days_to(1, 5, 1),
             "x27_empty_office_folder", lambda rig: None, _x27_on_day, _x27_expect),
    Scenario("X28", 2, "Next-year NAS folder with files, no marker", START, _days_to(1, 5, 1),
             "x28_nas_no_marker", lambda rig: None, _x28_on_day, _x28_expect),
    Scenario("X29", 2, "Approval of a closed FY after the switch", START, _days_to(4, 1, 3),
             "x29_stale_approval", lambda rig: None, _x29_on_day, _x29_expect),
    Scenario("X30", 2, "Manifest wipe (Feb 12)", START, _days_to(2, 12, 4),
             "x30_manifest_wipe", lambda rig: None, _x30_on_day, _x30_expect),
    Scenario("X31", 2, "Overlap off Feb 1, on again Feb 3", START, _days_to(2, 3, 4),
             "x31_overlap_toggle", lambda rig: None, _x31_on_day, _x31_expect),
    Scenario("X32", 2, "Overlap never on", START, _days_to(1, 5, 1),
             "x32_overlap_disabled", _x32_setup, _x32_on_day, _x32_expect),
    Scenario("X33", 2, "overlap_activate_on edited mid-window (Jan 5)", START, _days_to(1, 20, 1),
             "x33_activate_on_edit", lambda rig: None, _x33_on_day, _x33_expect),
]
