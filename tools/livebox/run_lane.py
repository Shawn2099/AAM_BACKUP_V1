r"""Livebox lane driver (plan LIVEBOX_TEST_PLAN_2026-11-24.md sections 1.5, 1.8, 1.9, 2, 6; row L8).

Two layers.

Pure loop (unit-tested with fake hooks, tests/test_run_lane.py):
    run_lane(days, *, lane, stamp, agent_runs, hooks, ...) -> LaneResult
        per day: hooks.churn(day) -> per event (time order): hooks.safety_check(ev) [pause = wait + retry, never skip],
        hooks.run_event(day, ev) -> hooks.check_day(day, results) -> state saved. Evidence: events.jsonl,
        checks.jsonl, state.json under evidence_dir(agent_runs, stamp, lane).
    cleanup_lane / resolve_lane_root / parse_args / main

Real hooks (`LaneHooks`): one lane = one process. Builds the LaneRig (derived config, fake SMTP, seams, simulated
clock, lane-private Prefect), seeds and churns the source, runs the real flows at their simulated instants, drains
mail, reads the dashboard /status, runs the oracles and writes per-day evidence. Scenario packs plug in through
`Scenario(inject=..., expect=...)`.

Run (from the code root, so that the stdlib `calendar` is not shadowed by tools/livebox/calendar.py):
    python -m tools.livebox.run_lane --lane A --stamp 20261124_1000 --days 30 --start 2090-12-15 \
        --root <dir> --allow-dev-path
Safety: never powers anything off (rig spies/raisers), no real SMTP, rclone local remote, paths only under the lane
root (S1-S15). A SafetyRefusal aborts the lane (exit 2); S10/S11 only pause.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shutil
import stat
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

if __name__ == "__main__":  # python tools/livebox/run_lane.py: the script dir would shadow the stdlib `calendar`
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
    sys.path.insert(0, os.path.dirname(os.path.dirname(_here)))

from tools.livebox import calendar as cal  # noqa: E402
from tools.livebox import oracles, safety  # noqa: E402
from tools.livebox.safety import SafetyPause, SafetyRefusal  # noqa: E402

CODE_ROOT = Path(__file__).resolve().parents[2]
LIVEBOX_ROOT = Path(safety.LIVEBOX_ROOT)
PRODUCTION_ROOTS = (r"C:\AAMBackup", r"C:\BackupAgent", "E:\\")
IST = cal.IST
DEFAULT_START = dt.date(2090, 12, 1)
DEFAULT_DAYS = 728
DEV_MIN_FREE_GB = 5   # dev PC only; the box keeps the plan's 50 GB (S12)


# ====================================================================================================== pure loop
SMALL_TREE_FILES = 50          # F5: no churn deletes in a tree with fewer regular files than this


def env_api_url_is_prod() -> bool:
    """H1: os.environ PREFECT_API_URL names the production :4200 server (threads without the settings contextvar
    would inherit it, launch.py sets it at import)."""
    url = os.environ.get("PREFECT_API_URL")
    if not url:
        return False
    try:
        return safety._api_parts(url)[1] == safety.PROD_PREFECT_PORT
    except Exception:  # noqa: BLE001 - an unreadable url is not proven safe
        return True


def evidence_dir(agent_runs, stamp: str, lane: str) -> Path:
    """agent_runs/livebox_<stamp>/<lane>; an `agent_runs` that is itself a folder named `evidence` (the box default:
    <lane>/evidence) is the evidence folder."""
    if Path(agent_runs).name == "evidence":
        return Path(agent_runs)
    return Path(agent_runs) / f"livebox_{stamp}" / lane


@dataclass
class LaneResult:
    status: str = "completed"                      # completed | stopped
    days_completed: int = 0                        # days run in THIS call
    fails: list = field(default_factory=list)
    last_day: dt.date | None = None
    counts: dict = field(default_factory=dict)     # verdict -> n
    checks: int = 0
    evidence: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed" and not self.fails


def _jsonl(path: Path, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, default=str, ensure_ascii=False) + "\n")


def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def flow_state(obj: Any) -> tuple[str, str | None]:
    """(flow_state, exc_type) of an event outcome: Completed | Failed (the flow raised) | Crashed (harness error).
    Separate from events.jsonl `ok`, which only says the harness ran the event."""
    err = getattr(obj, "error", None)
    exc = getattr(getattr(obj, "flow", None), "exc", None)
    if getattr(obj, "harness_error", None):
        return "Crashed", (err.get("type") if isinstance(err, dict) else None) or "HarnessError"
    if exc is not None:
        return "Failed", type(exc).__name__
    if isinstance(err, dict):
        return "Failed", err.get("type")
    return "Completed", None


def _write_progress(ev_dir: Path, hooks: Any, **fields: Any) -> None:
    """progress.json: small, atomic (tmp + rename), best effort; read it while the lane runs."""
    real = getattr(hooks, "real", None)
    now = real.now() if real is not None else dt.datetime.now(IST)
    with contextlib.suppress(Exception):
        _write_state(ev_dir / "progress.json", {"real_time": now.isoformat(), **fields})


def run_lane(days, *, lane: str, stamp: str, agent_runs, hooks, stop_on_fail: bool = False, resume: bool = False,
             sleep: Callable[[float], None] = time.sleep, pause_poll_seconds: float = 60.0) -> LaneResult:
    """The per-day loop (plan 1.5). See the module docstring; hooks decide everything that touches the world."""
    ev_dir = evidence_dir(agent_runs, stamp, lane)
    ev_dir.mkdir(parents=True, exist_ok=True)
    events_p, checks_p, state_p, pause_p, timing_p = (
        ev_dir / n for n in ("events.jsonl", "checks.jsonl", "state.json", "pauses.jsonl", "timing.jsonl"))
    last_done: dt.date | None = None
    state: dict = {}
    if resume:
        state = _read_state(state_p)
        if state.get("last_completed_day"):
            last_done = dt.date.fromisoformat(state["last_completed_day"])
        if hasattr(hooks, "load_state"):
            hooks.load_state(state)
    else:
        for p in (events_p, checks_p, state_p, pause_p, timing_p):
            with contextlib.suppress(OSError):
                p.unlink()
    res = LaneResult(evidence=ev_dir, last_day=last_done)
    counts: dict[str, int] = {}
    events_done, last_kind, total_days = 0, None, len(days)

    def record(check: dict, day: cal.Day) -> None:
        row = {"lane": lane, "day": day.date.isoformat(), **check}
        row.setdefault("evidence", [])
        _jsonl(checks_p, row)
        v = str(row.get("verdict", "")).upper()
        counts[v] = counts.get(v, 0) + 1
        res.checks += 1
        if v == "FAIL":
            res.fails.append(row)

    for day in days:
        if last_done is not None and day.date <= last_done:
            continue
        stage = "churn"
        _write_progress(ev_dir, hooks, day_index=day.index if hasattr(day, "index") else None, days_total=total_days,
                        sim_date=day.date.isoformat(), events_done=events_done, last_event=last_kind,
                        status="day_start")
        try:
            hooks.churn(day)
            results: list = []
            for ev in sorted(day.events, key=lambda e: e.at):
                while True:
                    try:
                        hooks.safety_check(ev)
                        break
                    except SafetyPause as p:
                        _jsonl(pause_p, {"day": day.date.isoformat(), "kind": ev.kind, "at": ev.at.isoformat(),
                                         "rule": p.rule, "message": p.message})
                        sleep(pause_poll_seconds)
                    except SafetyRefusal as r:
                        _jsonl(ev_dir / "abort.jsonl", {"day": day.date.isoformat(), "kind": ev.kind,
                                                        "rule": r.rule, "message": r.message})
                        raise
                stage = f"run_event {ev.kind}"
                obj = hooks.run_event(day, ev)
                row = {"lane": lane, "day": day.date.isoformat(), "kind": ev.kind, "at": ev.at.isoformat(),
                       "fy": ev.fy}
                res_d = getattr(obj, "resources", None)
                if isinstance(res_d, dict):
                    row["resources"] = res_d
                err = getattr(obj, "error", None)
                sched = getattr(obj, "scheduled_at", None)
                val = getattr(obj, "value", None)
                row.update({
                    "ok": bool(getattr(obj, "ok", True)) and not err and not getattr(obj, "harness_error", None),
                    "error": err if isinstance(err, dict) else None,
                    "flow_state": flow_state(obj)[0], "exc_type": flow_state(obj)[1],
                    "returned": "" if val is None else str(val)[:500],
                    "scheduled_at": sched.isoformat() if hasattr(sched, "isoformat") else (sched or None),
                    "new_run_ids": [str(x) for x in (getattr(obj, "new_run_ids", None) or [])]})
                _jsonl(events_p, row)
                events_done, last_kind = events_done + 1, ev.kind
                _write_progress(ev_dir, hooks, day_index=day.index if hasattr(day, "index") else None,
                                days_total=total_days, sim_date=day.date.isoformat(), events_done=events_done,
                                last_event=last_kind, status="running")
                rd = res_d if isinstance(res_d, dict) else {}
                _jsonl(timing_p, {"day": day.date.isoformat(), "kind": ev.kind, "at": ev.at.isoformat(),
                                  "seconds": rd.get("seconds"), "peak_rss_bytes": rd.get("peak_rss_bytes")})
                results.append((ev, obj))
            stage = "check_day"
            checks = hooks.check_day(day, results) or []
        except SafetyRefusal:
            raise
        except Exception as e:  # noqa: BLE001 - a scenario knob / oracle blew up: a recorded FAIL, not a lost run
            record({"check_id": "HARNESS_" + stage.split()[0].upper(), "verdict": "FAIL",
                    "expected": "no exception in the harness",
                    "observed": f"{stage}: {type(e).__name__}: {e}"}, day)
            res.status = "stopped"
            break
        for c in checks:
            record(c, day)
        state = {**state, "last_completed_day": day.date.isoformat(), "lane": lane, "stamp": stamp}
        if hasattr(hooks, "get_state"):
            state["hooks"] = hooks.get_state()
        _write_state(state_p, state)
        res.days_completed += 1
        res.last_day = day.date
        if stop_on_fail and any(str(c.get("verdict", "")).upper() == "FAIL" for c in checks):
            res.status = "stopped"
            break
    if res.status == "completed" and hasattr(hooks, "final_checks"):
        last = days[-1] if len(days) else None
        for c in hooks.final_checks() or []:
            if last is not None:
                record(c, last)
    res.counts = counts
    _write_progress(ev_dir, hooks, days_total=total_days, events_done=events_done, last_event=last_kind,
                    status=res.status)
    _write_state(ev_dir / "result.json", {"status": res.status, "days_completed": res.days_completed,
                                          "last_day": res.last_day, "counts": counts, "checks": res.checks,
                                          "fails": [f.get("check_id") + "@" + f.get("day", "") for f in res.fails]})
    return res


# ====================================================================================================== paths / cleanup
def _is_production(path: Any) -> bool:
    return any(safety.is_under(path, r) for r in PRODUCTION_ROOTS)


def resolve_lane_root(root, stamp: str, lane: str, allow_dev_path: bool = False) -> Path:
    """<root>/<stamp>/<lane>; root must be C:\\AAMLivebox (or under it) unless allow_dev_path; never a production path."""
    root = Path(root)
    if _is_production(root):
        raise SafetyRefusal("S3", f"root {root} is a production path")
    if not allow_dev_path and not safety.is_under(root, LIVEBOX_ROOT):
        raise SafetyRefusal("S2", f"root {root} is not under {LIVEBOX_ROOT} (dev PC: --allow-dev-path)")
    if not stamp or not lane or any(c in stamp + lane for c in "\\/:"):
        raise SafetyRefusal("S2", f"bad stamp/lane {stamp!r}/{lane!r}")
    return root / stamp / lane


def _rmtree_safe(path: Path) -> None:
    """Delete a folder without ever following a junction or symlink out of it."""
    for dp, dns, _fns in os.walk(path, topdown=True, followlinks=False):
        for d in list(dns):
            full = os.path.join(dp, d)
            if os.path.islink(full) or (hasattr(os.path, "isjunction") and os.path.isjunction(full)):
                os.rmdir(full)
                dns.remove(d)

    def onerror(fn, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        fn(p)

    shutil.rmtree(path, onerror=onerror)


def strip_lane_denies(lane_root) -> None:
    """Remove the current user's deny ACEs from lane\\config.livebox.yaml and lane\\src (the only two the rig sets)."""
    import subprocess

    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    dom = os.environ.get("USERDOMAIN")
    who = f"{dom}\\{user}" if dom else user
    lane_root = Path(lane_root)
    for target in (lane_root / "config.livebox.yaml", lane_root / "src"):
        if os.path.lexists(target) and not os.path.islink(target):
            with contextlib.suppress(Exception):
                subprocess.run(["icacls", str(target), "/remove:d", who], capture_output=True, text=True,
                               timeout=60)


def _save_evidence_after_crash(lane_root: Path) -> None:
    """A lane without evidence\\result.json crashed: save the product logs, manifest.db and the dataset manifest into
    the evidence folder BEFORE runtime\\ is deleted; when that is not possible refuse and leave runtime\\ intact."""
    ev_dir, runtime = lane_root / "evidence", lane_root / "runtime"
    if (ev_dir / "result.json").exists() or not runtime.exists():
        return
    from tools.livebox import evidence

    try:
        ev_dir.mkdir(parents=True, exist_ok=True)
        evidence.collect_logs(runtime, ev_dir, LaneHooks.LOG_CAP_BYTES)
        db = runtime / "manifest.db"
        if db.is_file():
            saved = evidence.copy_manifest_db(db, ev_dir / "manifest.db")
            if not Path(saved).is_file() or Path(saved).stat().st_size == 0:
                raise OSError("manifest.db could not be copied")
        evidence.write_dataset_manifest(ev_dir)
    except Exception as e:  # noqa: BLE001
        raise SafetyRefusal("B3", f"crashed lane {lane_root}: evidence could not be saved before the cleanup "
                                  f"({type(e).__name__}: {e}); runtime\\ was left in place") from e


LANE_MARKER = safety.LANE_MARKER   # written by LaneRig.build(); cleanup_lane refuses a folder without it (M2)


def cleanup_lane(lane_root, livebox_root, keep_evidence: bool = True) -> None:
    """Delete lane_root, only when it is exactly <livebox_root>/<stamp>/<lane> (plan 1.9). The `evidence` folder of
    the lane is the dataset and is kept unless keep_evidence=False."""
    lane_root, livebox_root = Path(lane_root), Path(livebox_root)
    if _is_production(lane_root) or _is_production(livebox_root):
        raise SafetyRefusal("S3", f"{lane_root} is a production path")
    lr, rr = os.path.realpath(lane_root), os.path.realpath(livebox_root)
    if safety.norm(lr) == safety.norm(rr) or not safety.is_under(lr, rr):
        raise SafetyRefusal("S2", f"{lane_root} is not below {livebox_root}")
    rel = safety.norm(lr)[len(safety.norm(rr)):].strip("\\")
    if len(rel.split("\\")) != 2:
        raise SafetyRefusal("S2", f"{lane_root} is not exactly <root>\\<stamp>\\<lane> below {livebox_root}")
    if not lane_root.exists():
        return
    if not (lane_root / LANE_MARKER).is_file():
        raise SafetyRefusal("S2", f"{lane_root} has no {LANE_MARKER} (not a lane folder made by the rig); nothing deleted")
    strip_lane_denies(lane_root)                     # a crashed process leaves its icacls denies behind
    if not keep_evidence:
        _rmtree_safe(lane_root)
        return
    _save_evidence_after_crash(lane_root)
    for child in lane_root.iterdir():
        if child.name == "evidence":
            continue
        if os.path.islink(child) or (hasattr(os.path, "isjunction") and os.path.isjunction(child)):
            os.rmdir(child)
        elif child.is_dir():
            _rmtree_safe(child)
        else:
            with contextlib.suppress(OSError):
                os.chmod(child, stat.S_IWRITE)
            child.unlink()
    if not (lane_root / "evidence").exists():
        with contextlib.suppress(OSError):
            lane_root.rmdir()


# ====================================================================================================== CLI
def _date_arg(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date: {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="run_lane", description="Livebox lane driver")
    ap.add_argument("--lane")
    ap.add_argument("--stamp", required=True)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="number of simulated days; --days 0 is the box preflight: open the lane (all S checks, "
                         "rig, harness), run no day, close")
    ap.add_argument("--start", type=_date_arg, default=DEFAULT_START)
    ap.add_argument("--root", type=Path, default=LIVEBOX_ROOT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--preflight", action="store_true",
                    help="same as --dry-run: print the plan and the S1,S2,S3,S5-S9 checks, create nothing")
    ap.add_argument("--allow-dev-path", action="store_true")
    ap.add_argument("--cleanup", default=None, metavar="LANE")
    ap.add_argument("--stop-on-fail", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--seed", default="livebox")
    ap.add_argument("--scale", type=int, default=300)
    ap.add_argument("--large-file-mb", type=int, default=50)
    ap.add_argument("--retention-days", type=int, default=None)
    ap.add_argument("--no-overlap", action="store_true")
    ap.add_argument("--n-deletes", action="store_true", help="churn deletes files in the next-FY folder (trips G3)")
    ap.add_argument("--approve-del-after-days", type=int, default=None, metavar="N",
                    help="the simulated owner approves a pending AAM-DEL-01 (G3 mass delete) block once it is "
                         "N simulated days old; default off (lanes that test the block must stay blocked)")
    legs = ap.add_mutually_exclusive_group()
    legs.add_argument("--cloud-only", action="store_true")
    legs.add_argument("--lan-only", action="store_true")
    legs.add_argument("--both-off", action="store_true", help="neither leg runs (cloud.enabled = lan.enabled = false)")
    ap.add_argument("--outage", action="append", default=[], metavar="START:END[:catchup]",
                    help="server-off window (ISO dates, inclusive); catchup = none|latest")
    ap.add_argument("--overlay", type=Path, default=None, help="folder holding the time_machine package")
    ap.add_argument("--expected-commit", default=None, help="S1: the commit the owner named (dev: defaults to HEAD)")
    ap.add_argument("--prod-config", type=Path, default=None, help="production config.yaml (read only): crons, lock")
    ap.add_argument("--prod-api", default=None, help="production Prefect API url (read-only GET, S14)")
    ap.add_argument("--agent-runs", type=Path, default=None)
    ap.add_argument("--pause-poll-seconds", type=float, default=60.0)
    ap.add_argument("--scenario", default=None, help="module:attribute of a Scenario")
    ap.add_argument("--code-root", type=Path, default=CODE_ROOT,
                    help="the BOX checkout used for S1 (git head) and S5 (flow.__file__); when given, the evidence "
                         "defaults to <lane>/evidence")
    ap.add_argument("--export-evidence", default=None, metavar="DEST_ZIP",
                    help="zip the evidence folder of --lane/--stamp (with its dataset manifest) and exit")
    return ap


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    ap = build_parser()
    a = ap.parse_args(list(argv) if argv is not None else None)
    if a.cleanup is None and not a.lane and not load_catalog(a.scenario):
        ap.error("--lane is required (or --cleanup LANE, or a catalog --scenario)")
    if a.days < 0:
        ap.error("--days must be >= 0")
    return a


def _parse_outages(items: list[str]) -> list[cal.Outage]:
    out = []
    for it in items:
        parts = it.split(":")
        if len(parts) not in (2, 3):
            raise SystemExit(f"bad --outage {it!r}")
        out.append(cal.Outage(dt.date.fromisoformat(parts[0]), dt.date.fromisoformat(parts[1]),
                              parts[2] if len(parts) == 3 else "none"))
    return out


def load_catalog(spec: str | None) -> list:
    """Catalog scenarios (scenarios.base.Scenario) named by `module:ID` (looked up in the module's SCENARIOS or as a
    module attribute) or `module:SCENARIOS` (the whole list); [] for None or a non-catalog spec."""
    if not spec or ":" not in spec:
        return []
    import importlib

    from tools.livebox.scenarios.base import Scenario as CatalogScenario

    mod_name, _, attr = spec.partition(":")
    try:
        mod = importlib.import_module(mod_name)
    except ImportError:
        return []
    obj = getattr(mod, attr, None)
    if obj is None:
        obj = next((s for s in (getattr(mod, "SCENARIOS", None) or ()) if getattr(s, "id", None) == attr), None)
    if isinstance(obj, CatalogScenario):
        return [obj]
    if isinstance(obj, (list, tuple)) and obj and all(isinstance(x, CatalogScenario) for x in obj):
        return list(obj)
    return []


def _load_scenario(spec: str | None):
    if not spec:
        return None
    import importlib

    cat = load_catalog(spec)
    if cat:
        from tools.livebox.scenarios import results as _res

        return _res.to_driver_scenario(cat[0])
    mod, _, attr = spec.partition(":")
    return getattr(importlib.import_module(mod), attr or "SCENARIO")


def lane_mode(a) -> str:
    """cloud | lan | off | both from --cloud-only / --lan-only / --both-off."""
    if getattr(a, "both_off", False):
        return "off"
    if getattr(a, "cloud_only", False):
        return "cloud"
    if getattr(a, "lan_only", False):
        return "lan"
    return "both"


def build_days(a: argparse.Namespace) -> list[cal.Day]:
    cat = getattr(a, "scenario_obj", None) or next(iter(load_catalog(getattr(a, "scenario", None))), None)
    if cat is not None:
        start, ndays, scn_outages = dt.date.fromisoformat(cat.start), cat.days, list(cat.outages or ())
    else:
        scenario = _load_scenario(getattr(a, "scenario", None))
        start, ndays, scn_outages = a.start, a.days, list(getattr(scenario, "outages", ()) or ())
    outages = _parse_outages(a.outage) + scn_outages
    kw = {}
    ovr = cal.overrides_map(getattr(cat, "overrides", ()) if cat is not None else ())
    if ovr:
        kw["overrides"] = ovr
    return cal.build_calendar(start, ndays, overlap_enabled=not a.no_overlap,
                              cloud_enabled=lane_mode(a) in ("cloud", "both"),
                              lan_enabled=lane_mode(a) in ("lan", "both"),
                              outages=outages, **kw)


def override_injection_dates(overrides) -> set[dt.date]:
    """Dates with a cloud_down / clock_unproven / stale_ok / extra_mails override: problem mails are expected there
    (storm rule M2)."""
    return {d for d, o in cal.overrides_map(overrides).items()
            if o.cloud_down or o.clock_unproven or o.stale_ok or o.extra_mails}


def unexpected_stale_legs(stale, day) -> list[str]:
    """Stale legs the watchdog reported that the calendar day does not allow (``day.stale_ok``)."""
    ok = set(getattr(day, "stale_ok", ()) or ())
    return [leg for leg in (stale or []) if leg not in ok]


def apply_known_findings(day, checks: list[dict]) -> list[dict]:
    """A FAIL mail check that names the key of an expected ACTION_REQUIRED mail sent 0 times is the known
    P1 finding (the escalation is suppressed by the product): verdict FINDING."""
    keys = [m.key for m in day.mails if m.severity == "ACTION_REQUIRED" and m.key]
    for c in checks:
        obs = str(c.get("observed", ""))
        if c.get("verdict") == "FAIL" and "sent 0" in obs and any(k in obs for k in keys):
            c["verdict"], c["finding"] = "FINDING", "F-P1-ESCALATION-SUPPRESSED"
    return checks


def default_agent_runs(a: argparse.Namespace, lane_root: Path) -> Path:
    """Evidence root: --agent-runs; otherwise <lane>/evidence, and only with --allow-dev-path <code root>/agent_runs."""
    if getattr(a, "agent_runs", None):
        return Path(a.agent_runs)
    cr = getattr(a, "code_root", None)
    separate_code_root = cr is not None and Path(cr) != CODE_ROOT      # a box checkout: evidence stays in the lane
    if not getattr(a, "allow_dev_path", False) or separate_code_root:
        return Path(lane_root) / "evidence"
    return CODE_ROOT / "agent_runs"


def check_evidence_location(a: argparse.Namespace, lane_root: Path, ar) -> None:
    """S2/S3 for the evidence folder: inside the lane and outside the code root (not on the dev path)."""
    if getattr(a, "allow_dev_path", False):
        return
    ev = evidence_dir(ar, a.stamp, a.lane)
    code_root = Path(getattr(a, "code_root", None) or CODE_ROOT)
    if safety.is_under(ev, code_root):
        raise SafetyRefusal("S3", f"evidence dir {ev} is under the code root {code_root}")
    if not safety.is_under(ev, lane_root):
        raise SafetyRefusal("S2", f"evidence dir {ev} is not inside the lane {lane_root}")


def _git_out(code_root, *args: str) -> str | None:
    import subprocess

    try:
        r = subprocess.run(["git", "-C", str(code_root), *args], capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def preflight_rules(a: argparse.Namespace, lane_root: Path, ar) -> list[tuple[str, str, str]]:
    """Dry run / --preflight: S1, S2, S3, S5, S6, S7, S8, S9 as far as they need no flows and no rig.
    Returns (rule, "ok" | "refuse" | "deferred", message); nothing is created."""
    out: list[tuple[str, str, str]] = []
    code_root = Path(getattr(a, "code_root", None) or CODE_ROOT)
    cfg_path = str(Path(lane_root) / "config.livebox.yaml")
    derived: dict = {}

    def add(rule: str, fn: Callable[[], str | None]) -> None:
        try:
            msg = fn()
            deferred = (msg or "").startswith("deferred: ")
            out.append((rule, "deferred" if deferred else "ok", (msg or "")[len("deferred: "):] if deferred else msg or ""))
        except SafetyRefusal as e:
            out.append((rule, "refuse", e.message))
        except Exception as e:  # noqa: BLE001 - fail closed
            out.append((rule, "refuse", f"check crashed: {type(e).__name__}: {e}"))

    def derive() -> dict:
        if not derived:
            from tools.livebox.derive_config import derive_config
            from tools.livebox.rig import dev_prod_raw

            derived.update(derive_config(dev_prod_raw(), Path(lane_root), smtp_port=1, has_backup_privilege=False,
                                         retention_days=a.retention_days, test_fy=cal.fy_of_date(a.start)))
        return derived

    def s1() -> str:
        head = (_git_out(code_root, "rev-parse", "HEAD") or "").strip() or None
        porcelain = _git_out(code_root, "status", "--porcelain", "--untracked-files=no")
        expected = a.expected_commit or (head if a.allow_dev_path else None)
        safety.check_box_head(head, expected, porcelain)
        return f"HEAD {head[:12]} is the expected commit, tracked tree clean"

    def paths() -> dict[str, str]:
        raw = derive()["paths"]
        return {k: raw[k] for k in ("source_drive", "lan_destination", "runtime_dir", "gcs_key_path")}

    def s2() -> str:
        if not a.allow_dev_path:
            safety.check_sandbox_paths(paths(), str(lane_root))
            ev = evidence_dir(ar, a.stamp, a.lane)
            if not safety.is_under(ev, lane_root):
                raise SafetyRefusal("S2", f"evidence dir {ev} is not inside the lane {lane_root}")
        else:
            for name, pth in paths().items():
                if not pth or not safety.is_under(pth, lane_root):
                    raise SafetyRefusal("S2", f"{name} = {pth!r} is not under the lane root {lane_root}")
        return f"sandbox paths and evidence dir are inside the lane {lane_root}"

    def s3() -> str:
        safety.check_not_production_paths(paths(), list(PRODUCTION_ROOTS))
        if not a.allow_dev_path:
            ev = evidence_dir(ar, a.stamp, a.lane)
            if safety.is_under(ev, code_root):
                raise SafetyRefusal("S3", f"evidence dir {ev} is under the code root {code_root}")
        return "no sandbox path is a production path; evidence is outside the code root"

    def s5() -> str:
        safety.check_config_binding(cfg_path, cfg_path, str(code_root / "flow.py"), str(code_root))
        return f"config {cfg_path}, flow.py under {code_root}"

    def s6() -> str:
        # prepare_env clears PREFECT_API_URL for the lane process, so an inherited value is not what the lane uses
        note = " (an inherited PREFECT_API_URL is cleared when the lane starts)" if os.environ.get("PREFECT_API_URL") else ""
        return "deferred: the lane's own loopback Prefect server is checked when the lane opens" + note

    def s7() -> str:
        n = derive()["notifications"]
        safety.check_notifications(n["smtp_host"], n["smtp_auth"], list(n["recipients"]) + list(n["report_recipients"]))
        return "SMTP 127.0.0.1 without auth, recipients on .invalid"

    def s8() -> str:
        text = alias_text(Path(lane_root) / "bucket")
        f = parse_rclone_conf(text)
        safety.check_rclone_config(cfg_path, cfg_path, f.get("type"), f.get("endpoint"),
                                   alias_remote=f.get("remote"), lane_root=str(lane_root))
        return f"rclone remote type {f.get('type')} inside the lane"

    def s9() -> str:
        return "deferred: shutdown / WoL raisers and the shutdown spy are verified when the lane opens"

    for rule, fn in (("S1", s1), ("S2", s2), ("S3", s3), ("S5", s5), ("S6", s6), ("S7", s7), ("S8", s8), ("S9", s9)):
        add(rule, fn)
    return out


def alias_text(bucket_dir) -> str:
    from tools.livebox.rig import alias_config_text

    return alias_config_text(bucket_dir)


def parse_rclone_conf(text: str, remote: str = "aam_gcs") -> dict:
    """{type, endpoint, remote} of the [remote] section only: comments (# ;) and other sections are ignored."""
    import configparser

    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cp.read_string(text)
    except configparser.Error:
        return {"type": None, "endpoint": None, "remote": None}
    sec = cp[remote] if cp.has_section(remote) else {}
    t = sec.get("type") if sec else None
    return {"type": t.strip().lower() if t else None, "endpoint": (sec.get("endpoint") or None) if sec else None,
            "remote": (sec.get("remote") or None) if sec else None}


def plan_text(a: argparse.Namespace, days: list[cal.Day], lane_root: Path) -> str:
    n_ev = sum(len(d.events) for d in days)
    n_rows = sum(len(d.rows) for d in days)
    last = days[-1].date if days else a.start
    t = cal.totals(days)
    return (f"lane {a.lane} stamp {a.stamp}: {len(days)} days {a.start} .. {last}, {n_ev} events, {n_rows} expected "
            f"rows, activations {t['activations']}, switches {t['switches']}, overlap days {t['overlap_days']}, "
            f"weekly {t.get('weekly', 0)}, monthly {t.get('monthly', 0)}; lane root {lane_root}")


def main(argv: Iterable[str] | None = None, *, hooks_factory: Callable[[Path], Any] | None = None,
         agent_runs=None, sleep: Callable[[float], None] = time.sleep) -> int:
    a = parse_args(argv)
    try:
        if a.export_evidence is not None:
            if not a.lane:
                print("--export-evidence needs --lane", file=sys.stderr)
                return 2
            ar0 = Path(agent_runs) if agent_runs is not None else default_agent_runs(
                a, resolve_lane_root(a.root, a.stamp, a.lane, a.allow_dev_path))
            ev0 = evidence_dir(ar0, a.stamp, a.lane)
            if not ev0.is_dir():
                print(f"no evidence folder {ev0}", file=sys.stderr)
                return 1
            from tools.livebox import evidence as evmod

            print(f"exported {evmod.export_evidence(ev0, a.export_evidence)}")
            return 0
        if a.cleanup is not None:
            lane_root = resolve_lane_root(a.root, a.stamp, a.cleanup, a.allow_dev_path)
            cleanup_lane(lane_root, a.root)
            print(f"cleaned {lane_root}")
            return 0
        catalog = load_catalog(a.scenario)
        if not catalog:
            return _run_one(a, hooks_factory, agent_runs, sleep)
        codes = []
        for scn in catalog:                                   # each scenario runs in its own lane
            a2 = argparse.Namespace(**vars(a))
            a2.lane, a2.start, a2.days, a2.scenario_obj = scn.lane, dt.date.fromisoformat(scn.start), scn.days, scn
            codes.append(_run_one(a2, hooks_factory, agent_runs, sleep, catalog_scn=scn))
        return max(codes) if codes else 0
    except SafetyRefusal as e:
        print(f"REFUSED {e.rule}: {e.message}", file=sys.stderr)
        return 2


def _scenario_verdict(scn, ev_dir: Path, last_day: dt.date | None, lane: str) -> dict:
    """Judge one catalog scenario from the lane's evidence: one checks.jsonl row, merged into summary.json."""
    from tools.livebox.scenarios import results as scn_results

    v = scn_results.verdict_for(scn, ev_dir)
    observed = "; ".join(v.get("failures") or []) or v.get("reason", "")
    _jsonl(ev_dir / "checks.jsonl", {"lane": lane, "day": last_day.isoformat() if last_day else None,
                                     "check_id": f"SCENARIO:{scn.id}", "verdict": v["verdict"],
                                     "expected": scn.title, "observed": observed, "evidence": []})
    summ = _read_state(ev_dir / "summary.json")
    summ.update({"scenario_id": scn.id, "verdict": v["verdict"], "failures": list(v.get("failures") or []),
                 "reason": v.get("reason", "")})
    (ev_dir / "summary.json").write_text(json.dumps(summ, indent=1, default=str), encoding="utf-8")
    return v


def _run_one(a: argparse.Namespace, hooks_factory, agent_runs, sleep, catalog_scn=None) -> int:
    """One lane (one process step): dry run, or open + run + close, then judge a catalog scenario."""
    try:
        lane_root = resolve_lane_root(a.root, a.stamp, a.lane, a.allow_dev_path)
        days = build_days(a)
        ar = Path(agent_runs) if agent_runs is not None else default_agent_runs(a, lane_root)
        if a.dry_run or a.preflight:
            print("DRY RUN " + plan_text(a, days, lane_root))
            from tools.livebox import evidence as env_mod
            print(env_mod.env_summary_line(env_mod.env_versions(days[-1].date if days else None)))
            problems = validate_plan(a, days, lane_root)
            for p in problems:
                print("PLAN PROBLEM: " + p)
            print("plan valid" if not problems else f"{len(problems)} plan problem(s)")
            refused = False
            for rule, status, msg in preflight_rules(a, lane_root, ar):
                print(f"{rule} {status}: {msg}")
                if status == "refuse":
                    refused = True
                    print(f"REFUSED {rule}: {msg}", file=sys.stderr)
            return 2 if (problems or refused) else 0
        check_evidence_location(a, lane_root, ar)
        if not a.resume:
            for d in (lane_root, evidence_dir(ar, a.stamp, a.lane)):
                if d.is_dir() and any(d.iterdir()):
                    print(f"REFUSED B2: {d} already exists and is not empty. Pass --resume to continue that lane, "
                          f"or use a new --stamp for a fresh start.", file=sys.stderr)
                    return 2
        hooks = hooks_factory(lane_root) if hooks_factory else None
        if hooks is None:
            prepare_env(lane_root, a)
            hooks = LaneHooks.from_args(a, lane_root, days, ar)
        opener = getattr(hooks, "open", None)
        try:
            if opener:
                opener()
            res = run_lane(days, lane=a.lane, stamp=a.stamp, agent_runs=ar, hooks=hooks,
                           stop_on_fail=a.stop_on_fail, resume=a.resume, sleep=sleep,
                           pause_poll_seconds=a.pause_poll_seconds)
        finally:
            closer = getattr(hooks, "close", None)
            if closer:
                closer()
        print(f"lane {a.lane}: {res.status}, {res.days_completed} days, checks {res.checks}, "
              f"verdicts {res.counts}, evidence {res.evidence}")
        harness = any(str(f.get("check_id", "")).upper().startswith("HARNESS") for f in res.fails)
        if catalog_scn is not None:
            v = _scenario_verdict(catalog_scn, res.evidence, res.last_day, a.lane)
            print(f"scenario {catalog_scn.id}: {v['verdict']}")
            if harness or v["verdict"] == "FAIL":
                return 3
            return 1 if res.status == "stopped" else 0
        if harness:
            return 3
        if res.status == "stopped":
            return 1
        return 3 if res.fails else 0
    except SafetyRefusal as e:
        print(f"REFUSED {e.rule}: {e.message}", file=sys.stderr)
        return 2


# ====================================================================================================== plan validation
def validate_plan(a: argparse.Namespace, days: list[cal.Day], lane_root: Path) -> list[str]:
    """Dry run: calendar sanity, derived config valid and inside the lane (nothing is created or run)."""
    problems: list[str] = []
    if not days:
        problems.append("no days")
    if [d.date for d in days] != [a.start + dt.timedelta(days=i) for i in range(len(days))]:
        problems.append("calendar days are not consecutive")
    if any(d.events != tuple(sorted(d.events, key=lambda e: e.at)) for d in days):
        problems.append("a day's events are not in time order")
    try:
        from models.config import AppConfig
        from tools.livebox.derive_config import derive_config
        from tools.livebox.rig import dev_prod_raw

        test_fy = cal.fy_of_date(days[0].date if days else a.start)
        raw = derive_config(dev_prod_raw(), lane_root, smtp_port=1, has_backup_privilege=False,
                            retention_days=a.retention_days, test_fy=test_fy)
        AppConfig(**raw)
        paths = {"source": raw["paths"]["source_drive"], "nas": raw["paths"]["lan_destination"],
                 "runtime": raw["paths"]["runtime_dir"], "key": raw["paths"]["gcs_key_path"]}
        leaves = [test_fy]
        safety.check_not_production_paths(paths, PRODUCTION_ROOTS)
        safety.check_fy_leaves(leaves, [], century=False, sandbox_leaves=safety.sandbox_leaves_from(test_fy))
        for k, p in paths.items():
            if not safety.is_under(p, lane_root):
                problems.append(f"derived {k} path {p} is not under the lane root")
        safety.check_notifications(raw["notifications"]["smtp_host"], raw["notifications"]["smtp_auth"],
                                   raw["notifications"]["recipients"] + raw["notifications"]["report_recipients"])
    except SafetyRefusal as e:
        problems.append(f"{e.rule}: {e.message}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"config validation crashed: {type(e).__name__}: {e}")
    return problems


# ====================================================================================================== process env
def prepare_env(lane_root: Path, a: argparse.Namespace | None = None) -> None:
    """Lane process environment (plan 1.1), BEFORE any project import: AAM_CONFIG, clean PREFECT_HOME, TEMP/TMP,
    no production Prefect API, bytecode off, BELOW_NORMAL priority (inherited by rclone / robocopy)."""
    import tempfile

    lane_root = Path(lane_root)
    for sub in ("tmp", "prefect_home", "evidence"):
        (lane_root / sub).mkdir(parents=True, exist_ok=True)
    env = os.environ
    env["AAM_CONFIG"] = str(lane_root / "config.livebox.yaml")
    env["PREFECT_HOME"] = str(lane_root / "prefect_home")
    for k in ("PREFECT_API_URL", "PREFECT_API_KEY", "PREFECT_TEST_MODE"):
        env.pop(k, None)
    env["PREFECT_SERVER_ANALYTICS_ENABLED"] = "false"
    env["TEMP"] = env["TMP"] = str(lane_root / "tmp")
    tempfile.tempdir = str(lane_root / "tmp")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    code_root = Path(getattr(a, "code_root", None) or CODE_ROOT)
    sys.path[:] = [p for p in sys.path if os.path.normcase(p) != os.path.normcase(str(code_root))]
    sys.path.insert(0, str(code_root))                 # the box checkout wins over any other copy of the product
    rc_dir = code_root / "deploy" / "bin"
    if rc_dir.is_dir():
        rest = [p for p in env.get("PATH", "").split(os.pathsep)
                if p and os.path.normcase(p) != os.path.normcase(str(rc_dir))]
        env["PATH"] = os.pathsep.join([str(rc_dir), *rest])
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:  # noqa: BLE001 - priority is a courtesy
        pass


# ====================================================================================================== real hooks
class RealTime:
    """The real wall clock, immune to the simulated clock: captured before time_machine starts."""

    def __init__(self) -> None:
        self.w0 = time.time()
        self.m0 = time.monotonic()

    def now(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.w0 + (time.monotonic() - self.m0), IST)

    def timestamp(self) -> float:
        return self.w0 + (time.monotonic() - self.m0)


@dataclass
class Scenario:
    """Plug-in point for scenario packs (plan section 4). All callables optional.

    inject(ctx, day, ev)              before the flow of every event (ctx = LaneHooks)
    expect(ctx, day, ev, outcome)     after it; returns an iterable of check dicts (check_id, verdict, expected,
                                      observed, evidence) or None
    before_day(ctx, day) / end_of_day(ctx, day, results) -> checks
    injection_dates                   dates where problem mails are expected (storm rule M2)
    outages                           cal.Outage items, merged with --outage (calendar and mail oracle)
    lateness                          {"YYYY-MM-DD <event kind>": timedelta}: the scheduled instant is that much earlier
    injections                        scenarios.base.Injection(date, time, fn(rig)), applied once each in time order
    """

    name: str = ""
    inject: Callable | None = None
    expect: Callable | None = None
    before_day: Callable | None = None
    end_of_day: Callable | None = None
    injection_dates: Iterable[dt.date] = ()
    outages: tuple = ()
    lateness: dict = field(default_factory=dict)
    injections: tuple = ()
    overrides: tuple = ()                  # cal.DayOverride pairs / mapping (cloud_down, clock_unproven)
    n_deletes: bool = False                # churn may delete files even in a tiny tree (keeps F-NEW-G3-SMALL-N)
    approve_del_after_days: int | None = None   # the simulated owner approves AAM-DEL-01 after N days (None = never)


def load_prod_facts(prod_config: Path | None) -> dict:
    """Read-only facts of the production config: crons, lock path, log dir, NAS path (nothing is written)."""
    if prod_config is None:
        return {}
    import yaml

    raw = yaml.safe_load(Path(prod_config).read_text(encoding="utf-8")) or {}
    sch, paths = raw.get("schedule", {}) or {}, raw.get("paths", {}) or {}
    crons = {k.removesuffix("_cron"): v for k, v in sch.items() if k.endswith("_cron") and v}
    runtime = paths.get("runtime_dir")
    return {"crons": crons, "runtime_dir": runtime,
            "lock": str(Path(runtime) / "backup.lock") if runtime else None,
            "log_directory": paths.get("log_directory") or (str(Path(runtime) / "logs") if runtime else None),
            "lan_destination": paths.get("lan_destination"), "timezone": sch.get("timezone")}


import http.client as _http_client  # noqa: E402

_REAL_HTTP = _http_client.HTTPConnection      # captured before any rig seam can replace them
_REAL_HTTPS = _http_client.HTTPSConnection
last_error: str = ""      # why the last fetch_prod_slot returned None (shown in the S14 refusal)


def fetch_prod_slot(api_url: str, timeout: float = 5.0) -> dict | None:
    """Read-only GET of the production global concurrency limit `aam-backup` -> {"active": n, "limit": n}.
    None on any failure; the reason is kept in the module-level `last_error`."""
    import urllib.parse

    global last_error
    last_error = ""
    body = b""
    conn = None
    try:
        url = api_url.rstrip("/") + "/v2/concurrency_limits/aam-backup"
        u = urllib.parse.urlsplit(url)
        # the REAL http.client classes captured at import, never the rig's clock-proof fakes
        cls = _REAL_HTTPS if u.scheme == "https" else _REAL_HTTP
        conn = cls(u.hostname, u.port, timeout=timeout)
        conn.request("GET", u.path + (f"?{u.query}" if u.query else ""))
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            raise OSError(f"HTTP {resp.status} {resp.reason}")
        d = json.loads(body)
        active = d.get("active_slots")
        return {"active": len(active) if isinstance(active, list) else int(active or 0), "limit": int(d["limit"])}
    except Exception as e:  # noqa: BLE001
        last_error = f"{type(e).__name__}: {e}"
        if isinstance(e, ValueError) and body:             # JSON decode failed: most likely an HTML page
            last_error += f" (response was not JSON: {body[:60]!r}; --prod-api must end in /api)"
        return None
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


def _s14_slot(prod_api: str | None) -> None:
    """S14 with a readable refusal: why the production slot could not be read."""
    if not prod_api:
        raise SafetyRefusal("S14", "production slot state unreadable: --prod-api was not given (a non-dev run needs "
                                   "e.g. --prod-api http://127.0.0.1:4200/api)")
    slot = fetch_prod_slot(prod_api)
    if slot is None:
        raise SafetyRefusal("S14", f"production slot state unreadable at {prod_api}: {last_error or 'no detail'}")
    safety.check_production_slot(slot)


def tree_summary(root: Path, ignore: Iterable[str] = ()) -> tuple[int, int, float]:
    """(file count, total bytes, newest mtime) - the cheap daily comparison."""
    skip, n, total, newest = set(ignore), 0, 0, 0.0
    if not Path(root).exists():
        return 0, 0, 0.0
    for dp, _dns, fns in os.walk(root):
        for f in fns:
            if f in skip:
                continue
            st = os.stat(os.path.join(dp, f))
            n += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return n, total, newest


def _summary_equal(a, b, tol: float = 2.0) -> bool:
    return a[0] == b[0] and a[1] == b[1] and abs(a[2] - b[2]) <= tol


def audit_spy_expectation(lan_on: bool, config: dict | None) -> tuple[int, int]:
    """(wake, shutdown) the spies must count on an audit day: the flow wakes whenever the LAN leg runs, and shuts
    down only when lan.shutdown_after_backup and wol.enabled are both true in the lane config (flow.py _shut_nas_down)."""
    cfg = config or {}
    shut_on = bool((cfg.get("lan") or {}).get("shutdown_after_backup")) and bool((cfg.get("wol") or {}).get("enabled"))
    return (1, 1 if shut_on else 0) if lan_on else (0, 0)


def _check(check_id: str, verdict: str, expected: Any = "", observed: Any = "", evidence: list | None = None,
           **extra) -> dict:
    return {"check_id": check_id, "verdict": verdict, "expected": expected, "observed": observed,
            "evidence": evidence or [], **extra}


def _viol_checks(check_id: str, violations: list, expected: str, evidence: list | None = None) -> list[dict]:
    if not violations:
        return [_check(check_id, "PASS", expected, "ok", evidence)]
    return [_check(f"{check_id}:{getattr(v, 'rule', check_id)}", "FAIL", expected, str(v), evidence) for v in violations]


@dataclass
class EventOutcome:
    ev: cal.Event
    flow: Any = None                    # rig.FlowOutcome | None
    shared: bool = False                # second FY pass of the same flow call
    real_seconds: float = 0.0
    checks: list = field(default_factory=list)
    status: Any = None                  # oracles.StatusView for status events
    harness_error: str | None = None
    value: Any = None
    new_rows: list = field(default_factory=list)
    resources: dict | None = None       # evidence.ResourceTimer.as_dict() of the event
    ok: bool = True
    error: dict | None = None           # {"type", "message", "traceback"} when the event raised
    scheduled_at: dt.datetime | None = None
    new_run_ids: list = field(default_factory=list)


class LaneHooks:
    """The real hooks of a lane (see module docstring)."""

    MARKER = ".AAM_TARGET_MOUNTED"

    def __init__(self, lane_root: Path, *, lane: str, stamp: str, days: list[cal.Day], agent_runs,
                 allow_dev_path: bool = False, seed: str = "livebox", scale: int = 300, large_file_mb: int = 50,
                 retention_days: int | None = None, overlay=None, expected_commit: str | None = None,
                 prod_config: Path | None = None, prod_api: str | None = None, prod_raw: dict | None = None,
                 outages: Iterable[cal.Outage] = (), scenario: Scenario | None = None,
                 has_backup_privilege: bool | None = False, activate_on: str = "12-31",
                 n_deletes: bool = False, approve_del_after_days: int | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 code_root: Path | None = None, resume: bool = False, mode: str = "both"):
        self.real = RealTime()
        self.resume = bool(resume)
        self.mode = mode
        import re

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", stamp):    # the stamp becomes part of the bucket name
            raise SafetyRefusal("S2", f"stamp {stamp!r} must be lower-case letters, digits or hyphens (bucket name)")
        self.lane_root = Path(lane_root)
        self.lane, self.stamp, self.days = lane, stamp, days
        self.test_fy = cal.fy_of_date(days[0].date) if days else "FY90-91"   # lane FY leaf follows the start date
        self.ev_dir = evidence_dir(agent_runs, stamp, lane)
        self.allow_dev_path = allow_dev_path
        self.seed, self.scale, self.large_file_mb = seed, scale, large_file_mb
        self.retention_days, self.overlay = retention_days, overlay
        self.expected_commit = expected_commit
        self.prod = load_prod_facts(prod_config)
        self.prod_api = prod_api
        self.prod_raw = prod_raw if prod_raw is not None else self._default_prod_raw(prod_config)
        if isinstance(scenario, (list, tuple)):          # catalog list: the first one drives this lane
            scenario = scenario[0] if scenario else None
        if scenario is not None and not isinstance(scenario, Scenario):   # a catalog Scenario: wrap it
            from tools.livebox.scenarios import results as _res

            scenario = _res.to_driver_scenario(scenario)
        self.cli_args: dict | None = None
        self.scenario = scenario or Scenario()
        self.outages = tuple(outages) + tuple(getattr(self.scenario, "outages", ()) or ())
        self.code_root = Path(code_root) if code_root is not None else CODE_ROOT
        self.has_backup_privilege = has_backup_privilege
        self.activate_on = activate_on
        self.n_deletes = bool(n_deletes or getattr(self.scenario, "n_deletes", False))
        self.approve_del_after_days = (approve_del_after_days if approve_del_after_days is not None
                                       else getattr(self.scenario, "approve_del_after_days", None))
        self.sleep = sleep
        self.rig = None
        self._stack = contextlib.ExitStack()
        self._opened = False
        self.argv_violations: list[str] = []
        self.pending_churn: cal.Day | None = None
        self.closed_fys: list[str] = []
        self.ever_activated: set[str] = set()
        self.frozen: dict[str, dict] = {}              # "<kind>:<fy>" -> {"hashes": {...}, "summary": [...]}
        self.last_group: tuple | None = None
        self.last_outcome: EventOutcome | None = None
        self.lan_flows = 0
        self.lan_flows_complete = 0
        self.lan_passes = 0
        self.prev_spies = {"wake": 0, "shutdown": 0, "archive": 0}   # calls of earlier processes (resume)
        self._wake_per_pass_seen = False
        self.audit_wakes = 0
        self.audit_shutdowns = 0
        self.rollover_wakes = 0                       # NAS calls made by rollover events (reported once as F-OWN-1)
        self.rollover_shutdowns = 0
        self.day_seconds: dict[str, float] = {}
        self.event_seconds: list[dict] = []
        self._reported_mail_violations: set[str] = set()
        self._injection_dates: set[dt.date] = set(getattr(self.scenario, "injection_dates", ()) or ()) | override_injection_dates(
            getattr(self.scenario, "overrides", ()))
        self.status_views: dict[tuple, Any] = {}
        self.day_by_date = {d.date: d for d in days}
        self.safety_results: list = []
        self._events_since_resources = 0
        # evidence dataset (tools.livebox.evidence); every failure is logged and never aborts the lane
        self.inputs = None
        self._applied_injections: set[tuple[str, str]] = set()
        self.created_paths: list[str] = []
        self.junction_target_entries: list[str] = []
        self.switch_before_manual_lan: bool = False
        self.evidence_errors: list[dict] = []
        self._loop_started = False
        self._last_logs_day: int | None = None

    @staticmethod
    def _default_prod_raw(prod_config: Path | None) -> dict:
        """Input of derive_config: the production config (read only) on the box; on the dev PC a synthetic one
        with the BOX schedule (cloud 02:01, rollover 06:00, audit Sun 08:00, weekly Mon 08:00, monthly 1st 08:30,
        LAN 21:01) and the box's 90-day retention, so the schedule-aware overdue logic sees the real times."""
        if prod_config is not None:
            import yaml

            raw = yaml.safe_load(Path(prod_config).read_text(encoding="utf-8")) or {}
            try:
                from models.config import _drop_deprecated_keys

                raw = _drop_deprecated_keys(raw) or raw
            except Exception:  # noqa: BLE001
                pass
            return raw
        from tools.livebox.rig import dev_prod_raw

        raw = dev_prod_raw()
        raw["schedule"].update(cloud_cron="1 2 * * *", lan_cron="1 21 * * *", rollover_cron="0 6 * * *",
                               audit_cron="0 8 * * SUN", weekly_cron="0 8 * * MON", monthly_cron="30 8 1 * *")
        raw["maintenance"]["log_retention_days"] = 90
        raw["budget"] = {"cloud_max_hours": 5, "lan_max_hours": 4, "audit_max_hours": 8}   # fits the box schedule
        return raw

    # ------------------------------------------------------------------ factories
    @classmethod
    def from_args(cls, a: argparse.Namespace, lane_root: Path, days: list[cal.Day], agent_runs) -> "LaneHooks":
        scenario = getattr(a, "scenario_obj", None) or _load_scenario(a.scenario)
        hooks = cls(lane_root, lane=a.lane, stamp=a.stamp, days=days, agent_runs=agent_runs,
                    allow_dev_path=a.allow_dev_path, seed=a.seed, scale=a.scale, large_file_mb=a.large_file_mb,
                    retention_days=a.retention_days, overlay=a.overlay, expected_commit=a.expected_commit,
                    prod_config=a.prod_config, prod_api=a.prod_api, outages=_parse_outages(a.outage),
                    scenario=scenario, n_deletes=a.n_deletes, code_root=getattr(a, "code_root", None),
                    approve_del_after_days=getattr(a, "approve_del_after_days", None),
                    resume=getattr(a, "resume", False), mode=lane_mode(a))
        hooks.cli_args = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(a).items()
                          if k != "scenario_obj"}
        return hooks

    # ------------------------------------------------------------------ open / close
    def open(self) -> None:
        from tools.livebox import rig as rigmod

        self.ev_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = self.lane_root / "config.livebox.yaml"
        strip_lane_denies(self.lane_root)                # denies of a crashed earlier process
        resume_cfg = self._resume_config()
        rig = rigmod.LaneRig.create(self.stamp, self.lane, root=self.lane_root.parent.parent,
                                    prod_raw=self.prod_raw, retention_days=self.retention_days,
                                    has_backup_privilege=self.has_backup_privilege, overlay=self.overlay,
                                    use_clock=True, test_fy=self.test_fy)
        if rig.lane_dir != self.lane_root:
            raise SafetyRefusal("S2", f"rig lane dir {rig.lane_dir} is not the lane root {self.lane_root}")
        rig.evidence_dir = self.ev_dir                   # mail evidence goes next to the checks
        self.rig = rig
        self._preflight_paths(rig)
        rig.build()
        if getattr(rig, "inputs", None) is not None:
            self.inputs = rig.inputs                      # one log, one seq counter
        if resume_cfg is not None:                       # resume: keep the config the product may have switched
            import yaml

            resume_cfg["notifications"]["smtp_port"] = rig.smtp.port
            with cfg_path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(resume_cfg, fh, sort_keys=False, allow_unicode=True)
        if self.mode != "both":                          # --cloud-only / --lan-only / --both-off: the on-disk config
            rig.set_mode(self.mode)
        self._stack.callback(rig.remove)
        self._safety_preflight("pre")                    # S1-S5, S7 before anything of the product is patched
        rig.install()
        self._guard_s6_before_concurrency_limit()
        self._stack.enter_context(rig.prefect_harness())
        self._install_run_tool_guard()
        self._opened = True
        self._safety_preflight("post")
        self._seed()
        self._evidence_at_start()

    def _resume_config(self) -> dict | None:
        """The lane config on disk, only when resuming; a fresh start never reuses an old config."""
        cfg_path = self.lane_root / "config.livebox.yaml"
        if not self.resume or not cfg_path.exists():
            return None
        import yaml

        return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    def _guard_s6_before_concurrency_limit(self) -> None:
        """S6 must pass before the lane's aam-backup limit is created: wrap launch._ensure_concurrency_limit."""
        import launch

        os.environ.pop("PREFECT_API_URL", None)       # launch.py sets the production :4200 at import (H1)
        real, hooks = launch._ensure_concurrency_limit, self

        def checked(*a, **k):
            from prefect.settings import PREFECT_API_URL

            safety.check_prefect_api_url(PREFECT_API_URL.value(), hooks.rig.prefect_api_url)
            return real(*a, **k)

        launch._ensure_concurrency_limit = checked
        self._stack.callback(setattr, launch, "_ensure_concurrency_limit", real)

    def close(self) -> None:
        try:
            self._evidence_at_end()
            self._write_summary()
        finally:
            self._stack.close()
        self._ev_safe("dataset_manifest", self._write_manifest)

    def _write_manifest(self) -> None:
        from tools.livebox import evidence

        evidence.write_dataset_manifest(self.ev_dir)

    # ------------------------------------------------------------------ evidence dataset
    def _ev_safe(self, label: str, fn: Callable[[], Any]) -> Any:
        """Run one piece of evidence collection; a failure is logged to events.jsonl and never aborts the lane."""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            row = {"evidence_error": label, "error": f"{type(e).__name__}: {str(e)[:300]}"}
            self.evidence_errors.append(row)
            if self._loop_started:
                with contextlib.suppress(OSError):
                    _jsonl(self.ev_dir / "events.jsonl", row)
            return None

    def _flush_evidence_errors(self) -> None:
        for row in self.evidence_errors:
            with contextlib.suppress(OSError):
                _jsonl(self.ev_dir / "events.jsonl", row)

    def record_input(self, kind: str, name: str, args=None, **extra) -> dict:
        """One row in inputs.jsonl (the same file and counter as the rig's knob log)."""
        if self.inputs is None:
            from tools.livebox import evidence

            self.inputs = evidence.InputsLog(
                self.ev_dir / "inputs.jsonl",
                lambda: (getattr(getattr(self.rig, "clock", None), "now", None)))
        return self.inputs.record(kind, name, args, **extra)

    def _evidence_at_start(self) -> None:
        from tools.livebox import evidence

        def identity() -> None:
            ident = evidence.harness_identity(self.code_root, Path(__file__).resolve().parent)
            (self.ev_dir / "identity.json").write_text(json.dumps(ident, indent=1, default=str), encoding="utf-8")

        self._ev_safe("identity", identity)
        self._ev_safe("expected", lambda: evidence.write_expected(self.ev_dir, self.days))
        self._ev_safe("env_versions", lambda: evidence.write_env_versions(
            self.ev_dir, self.days[-1].date if self.days else None))
        if self.cli_args is not None:
            self._ev_safe("cli_input", lambda: self.record_input("cli", "run_lane", dict(self.cli_args)))
        for o in self.outages:
            self._ev_safe("outage_input", lambda o=o: self.record_input(
                "outage", "server_off", {"start": str(o.start), "end": str(o.end), "catchup": o.catchup}))

    def _evidence_after_day(self, day: cal.Day) -> None:
        from tools.livebox import evidence

        iso = day.date.isoformat()
        db = self.rig.runtime_dir / "manifest.db"
        self._ev_safe("db_dump", lambda: evidence.dump_db(db, self.ev_dir / "db_dump", iso, day_index=day.index))

        def cfg_hash() -> None:
            _jsonl(self.ev_dir / "config_hashes.jsonl",
                   {"day": iso, "sha256": evidence.config_snapshot(self.rig.config)[1]})

        self._ev_safe("config_hash", cfg_hash)
        last = bool(self.days) and day.date == self.days[-1].date
        if (self._special_day(day) and not day.is_sunday) or last:
            for kind, root in (("source", self.rig.lane_dir / "src"),
                               ("cloud", self.rig.bucket_root / self.rig.bucket_name),
                               ("nas", self.rig.lane_dir / "nas")):
                self._ev_safe(f"tree_{kind}", lambda k=kind, r=root: evidence.write_tree_listing(
                    self.ev_dir, iso, k, r, ignore=[self.MARKER]))
        if day.index and day.index % 30 == 0 and self._last_logs_day != day.index:
            self._last_logs_day = day.index
            self._ev_safe("logs", self._collect_logs)

    LOG_CAP_BYTES = 5 * 1024 * 1024

    def _collect_logs(self) -> None:
        from tools.livebox import evidence

        evidence.collect_logs(self.rig.runtime_dir, self.ev_dir, self.LOG_CAP_BYTES)

    def _evidence_at_end(self) -> None:
        if self.rig is None:
            return
        from tools.livebox import evidence

        self._ev_safe("logs", self._collect_logs)
        self._ev_safe("manifest_db", lambda: evidence.copy_manifest_db(self.rig.runtime_dir / "manifest.db",
                                                                      self.ev_dir / "manifest.db"))

    # ------------------------------------------------------------------ safety
    def _config(self):
        return self.rig.load_config()

    def _sandbox_paths(self, cfg) -> dict[str, str]:
        rig = self.rig
        return {"source": str(cfg.paths.source_drive), "nas": str(cfg.paths.lan_destination),
                "bucket": str(rig.bucket_root), "runtime": str(cfg.paths.runtime_dir),
                "database": str(cfg.paths.database_path), "log_dir": str(cfg.paths.log_directory),
                "run_logs": str(cfg.run_logs_dir), "lock": str(cfg.paths.backup_lock_path),
                "prefect_home": os.environ.get("PREFECT_HOME", ""), "tmp": os.environ.get("TEMP", ""),
                "rclone_conf": str(rig.seams.rclone_conf) if rig.seams else str(rig.lane_dir / "rclone_local.conf")}

    def _preflight_paths(self, rig) -> None:
        """S2/S3 on the derived config before anything is created."""
        from models.config import AppConfig  # noqa: F401  (import check)
        from tools.livebox.derive_config import derive_config
        from tools.livebox.rig import dev_prod_raw

        raw = derive_config(self.prod_raw or dev_prod_raw(), rig.lane_dir, smtp_port=1,
                            has_backup_privilege=self.has_backup_privilege, retention_days=self.retention_days,
                            test_fy=self.test_fy)
        paths = {k: v for k, v in raw["paths"].items() if k in ("source_drive", "lan_destination", "runtime_dir",
                                                              "gcs_key_path")}
        self._s2(paths)
        safety.check_not_production_paths(paths, self._production_roots())

    def _production_roots(self) -> list[str]:
        roots = list(PRODUCTION_ROOTS)
        for k in ("runtime_dir", "log_directory", "lan_destination"):
            if self.prod.get(k):
                roots.append(self.prod[k])
        return roots

    def _s2(self, paths: dict[str, str]) -> None:
        if self.allow_dev_path:
            for name, p in paths.items():
                if not p or not safety.is_under(p, self.lane_root):
                    raise SafetyRefusal("S2", f"{name} = {p!r} is not under the lane root {self.lane_root}")
        else:
            safety.check_sandbox_paths(paths, str(self.lane_root))

    def _count_lane_processes(self) -> int:
        try:
            import psutil

            n = 0
            for p in psutil.process_iter(["name", "cmdline"]):
                try:
                    name = (p.info.get("name") or "").lower()
                    cmd = " ".join(p.info.get("cmdline") or [])
                    if name.startswith("python") and "run_lane" in cmd and "--cleanup" not in cmd:
                        n += 1
                except Exception:  # noqa: BLE001
                    continue
            return max(n, 1)
        except Exception:  # noqa: BLE001
            return 1

    def _git(self, *args: str) -> str | None:
        import subprocess

        try:
            r = subprocess.run(["git", "-C", str(self.code_root), *args], capture_output=True, text=True, timeout=60)
            return r.stdout if r.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None

    def _lane_size(self) -> int:
        total = 0
        for dp, _d, fns in os.walk(self.lane_root):
            for f in fns:
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(dp, f))
        return total

    def _safety_preflight(self, phase: str = "all") -> None:
        """S1-S9, S12-S15 before the first event; any refusal aborts. S10/S11 are checked (and paused) per event.
        phase "pre" (before rig.install): S1-S5, S7; "post" (inside the lane harness): S6, S8-S14; "all": both."""
        import core.cloud_preflight as cpre
        import core.cloud_sync as csync
        import core.shutdown as sd
        import core.wol as wol
        import flow
        import models.config as mc

        cfg = self._config()
        res: list[safety.Result] = list(self.safety_results) if phase == "post" else []
        self.safety_results = res

        def run(rule: str, fn: Callable[[], None]) -> None:
            try:
                fn()
                res.append(safety.Result(rule, "ok"))
            except SafetyPause as e:
                res.append(safety.Result(rule, "pause", e.message))
            except SafetyRefusal as e:
                res.append(safety.Result(rule, "refuse", e.message))
                self._write_safety()
                raise
            except Exception as e:  # noqa: BLE001 - fail closed
                res.append(safety.Result(rule, "refuse", f"check crashed: {type(e).__name__}: {e}"))
                self._write_safety()
                raise SafetyRefusal(rule, f"check crashed: {type(e).__name__}: {e}") from e

        if phase in ("all", "pre"):
            head = (self._git("rev-parse", "HEAD") or "").strip() or None
            porcelain = self._git("status", "--porcelain", "--untracked-files=no")
            expected = self.expected_commit or (head if self.allow_dev_path else None)
            run("S1", lambda: safety.check_box_head(head, expected, porcelain))
            paths = self._sandbox_paths(cfg)
            run("S2", lambda: self._s2(paths))
            run("S3", lambda: safety.check_not_production_paths(paths, self._production_roots()))
            leaves = {Path(str(cfg.paths.source_drive)).name, Path(str(cfg.paths.lan_destination)).name}
            run("S4", lambda: safety.check_fy_leaves(leaves, [Path(x).name for x in (self.prod.get("lan_destination"),)
                                                              if x], century=False,
                                                              sandbox_leaves=safety.sandbox_leaves_from(self.test_fy)))
            run("S5", lambda: safety.check_config_binding(mc.CONFIG_PATH, str(self.rig.config_path), flow.__file__,
                                                           str(self.code_root)))
            run("S7", lambda: safety.check_notifications(
                cfg.notifications.smtp_host, cfg.notifications.smtp_auth,
                list(cfg.notifications.recipients) + list(cfg.notifications.report_recipients)))
        if phase in ("all", "post"):
            from prefect.settings import PREFECT_API_URL

            url = PREFECT_API_URL.value()

            def s6() -> None:
                safety.check_prefect_api_url(url, self.rig.prefect_api_url)
                if env_api_url_is_prod():
                    raise SafetyRefusal("S6", "environment PREFECT_API_URL names the production :4200 server")

            run("S6", s6)
            rtext = Path(self.rig.seams.rclone_conf).read_text(encoding="utf-8")
            conf = parse_rclone_conf(rtext)
            rtype, rend, ralias = conf["type"], conf["endpoint"], conf["remote"]

            def s8() -> None:
                if getattr(self.rig.seams, "_installed", True):
                    safety.check_rclone_seams(flow.temp_rclone_config, csync.temp_rclone_config,
                                              cpre.temp_rclone_config)
                safety.check_rclone_config(str(self.rig.seams.rclone_conf), str(self.rig.seams.rclone_conf),
                                           rtype, rend, alias_remote=ralias, lane_root=str(self.lane_root))

            run("S8", s8)
            run("S9", lambda: safety.check_shutdown_raisers(sd.shutdown_server, wol._send_magic_packet,
                                                             flow.lan_shutdown_task))
            if self.prod.get("lock"):
                run("S10", lambda: safety.check_production_lock(self.prod["lock"]))
            elif not self.allow_dev_path:
                run("S10", lambda: safety.check_production_lock(None))
            if self.prod.get("crons"):
                run("S11", lambda: safety.check_production_schedule(self.real.now(), self.prod["crons"]))
            elif not self.allow_dev_path:
                run("S11", lambda: safety.check_production_schedule(self.real.now(), None))
            run("S12", self._resources)
            run("S13", lambda: safety.check_lane_process_count(self._count_lane_processes()))
            if self.prod_api:
                run("S14", lambda: _s14_slot(self.prod_api))
            elif not self.allow_dev_path:
                run("S14", lambda: _s14_slot(None))
        self._write_safety()

    def _write_safety(self) -> None:
        rows = [{"rule": r.rule, "status": r.status, "message": r.message} for r in self.safety_results]
        (self.ev_dir / "safety.json").write_text(json.dumps(
            {"allow_dev_path": self.allow_dev_path, "prod_facts": bool(self.prod), "results": rows}, indent=1),
            encoding="utf-8")

    _LAN_RCLONE_STEPS = frozenset({"rclone-lan-plan", "rclone-lan-sync", "rclone-lan-check", "rclone-lan-hash"})

    def _install_run_tool_guard(self) -> None:
        """S15 + S9: every rclone argv stays inside the lane; gcloud / shutdown are refused."""
        import core.tool_runner as tr

        real = tr.run_tool
        lane_root, viol, hooks = str(self.lane_root), self.argv_violations, self
        lan_steps = self._LAN_RCLONE_STEPS

        def guarded(name, argv, **kw):
            exe = os.path.basename(str(argv[0])).lower()
            if exe.startswith(("gcloud", "shutdown", "wakeonlan")):
                viol.append(f"S9: {exe} spawned")
                raise SafetyRefusal("S9", f"{exe} spawned: {argv}")
            if exe.startswith(("robocopy", "rclone")):
                try:
                    safety.check_argv([str(x) for x in argv], lane_root)
                except SafetyRefusal as e:
                    viol.append(f"{e.rule}: {e.message}")
                    raise
            fname = ("lan_rclone_runs.jsonl" if name in lan_steps else
                     "rclone_runs.jsonl" if exe.startswith("rclone") else
                     "robocopy_runs.jsonl" if exe.startswith("robocopy") else None)
            if fname:
                start = hooks.real.now()
                row: dict = {"step": name, "exit_code": None}
                try:
                    result = real(name, argv, **kw)
                    code = getattr(result, "exit_code", None)
                    row["exit_code"] = code if isinstance(code, int) else None
                    return result
                except BaseException as e:
                    row["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                    raise
                finally:
                    with contextlib.suppress(Exception):
                        _jsonl(hooks.ev_dir / fname,
                               {"start": start.isoformat(), "end": hooks.real.now().isoformat(),
                                "argv": [str(x) for x in argv], **row})
            return real(name, argv, **kw)

        guarded.livebox_guard = True
        patched = []
        for name, mod in list(sys.modules.items()):
            if mod is not None and (name == "flow" or name.startswith("core.")) and getattr(mod, "run_tool", None) is real:
                patched.append((mod, real))
                mod.run_tool = guarded

        def undo():
            for mod, orig in patched:
                mod.run_tool = orig

        self._stack.callback(undo)

    def safety_check(self, ev: cal.Event) -> None:
        """Before every event: production-busy pauses (S10 lock, S11 schedule: real clock), aborts (argv, spies)."""
        if self.argv_violations:
            raise SafetyRefusal("S15", "; ".join(self.argv_violations[:3]))
        if env_api_url_is_prod():
            raise SafetyRefusal("S6", "environment PREFECT_API_URL names the production :4200 server")
        import core.shutdown as sd
        import core.wol as wol
        import flow

        safety.check_shutdown_raisers(sd.shutdown_server, wol._send_magic_packet, flow.lan_shutdown_task)
        if self.prod.get("lock"):
            safety.check_production_lock(self.prod["lock"])
        if self.prod.get("crons"):
            safety.check_production_schedule(self.real.now(), self.prod["crons"])
        self._events_since_resources += 1
        if self._events_since_resources >= 20:
            self._events_since_resources = 0
            self._resources()

    def _resources(self) -> None:
        """S12. Dev PC (allow_dev_path): the 50 GB floor is a box value; the dev floor is DEV_MIN_FREE_GB."""
        free, size = shutil.disk_usage(self.lane_root).free, self._lane_size()
        if self.allow_dev_path:
            if free < DEV_MIN_FREE_GB * safety.GB:
                raise SafetyRefusal("S12", f"free {free / safety.GB:.1f} GB is below the dev floor {DEV_MIN_FREE_GB} GB")
            free = max(free, safety.MIN_FREE_BYTES)
        safety.check_resources(free, size)

    # ------------------------------------------------------------------ seed / churn
    def _seed(self) -> None:
        from tools.livebox import churn

        first = self.days[0].date if self.days else DEFAULT_START
        src = self.rig.source_dir
        if not any(src.rglob("*")) if src.exists() else True:
            fy = cal.fy_of_date(first)
            if src.name != fy:
                pass
            churn.seed_tree(src, self.seed, self.scale, now=first - dt.timedelta(days=1))

    def src_dir(self, fy: str) -> Path:
        return self.rig.lane_dir / "src" / fy

    def nas_dir(self, fy: str) -> Path:
        return self.rig.lane_dir / "nas" / "backup" / fy

    def bucket_dir(self, fy: str) -> Path:
        return self.rig.bucket_prefix(fy)

    def churn(self, day: cal.Day) -> None:
        """Called once per day before the first event. The day's churn is applied FIRST so that the scenario's
        guard injections (wipe, mass delete, shrink) in before_day are never undone by the same day's churn."""
        if not self._loop_started:
            self._loop_started = True
            self._flush_evidence_errors()
        self.pending_churn = day
        self._maybe_churn(None)
        if self.scenario.before_day:
            self.scenario.before_day(self, day)

    def _apply_churn(self, day: cal.Day) -> list:
        from tools.livebox import churn as ch

        ops = []
        cfg_fy, nxt = day.configured_fy_end, day.next_fy_end
        plan: list[tuple[str, str]] = [(cfg_fy, ch.ROLE_CONFIGURED)]
        if nxt:
            plan.append((nxt, ch.ROLE_NEXT))
        plan += [(fy, ch.ROLE_CLOSED) for fy in self.closed_fys]
        orig_counts = ch._counts
        for fy, role in plan:
            root = self.src_dir(fy)
            if not root.is_dir():
                continue
            small = len(ch._regular_files(root)) < SMALL_TREE_FILES
            if (role == ch.ROLE_NEXT or small) and not self.n_deletes:
                # a tiny folder + one delete trips G3 (1 of 7 files = 14 % > 10 %, AAM-DEL-01): finding
                # F-NEW-G3-SMALL-N. The healthy lane does not delete in N or in any tree of fewer than
                # SMALL_TREE_FILES files; scenario packs (X22, n_deletes) turn it back on.
                ch._counts = lambda r, _o=orig_counts: (lambda a, m, d: (a, m, (0, 0)))(*_o(r))
            try:
                ops += [(fy, role, o) for o in ch.apply_day(root, day.date, f"{self.seed}:{fy}", role=role,
                                                            large_file_mb=self.large_file_mb)]
            finally:
                ch._counts = orig_counts
        return ops

    def _maybe_churn(self, ev_at: dt.datetime | None) -> None:
        day = self.pending_churn
        if day is None:
            return
        if ev_at is None or ev_at.hour >= 12:
            self.pending_churn = None
            ops = self._apply_churn(day)
            counts = {k: sum(1 for o in ops if o[2].kind == k) for k in {o[2].kind for o in ops}}
            _jsonl(self.ev_dir / "churn.jsonl", {"day": day.date.isoformat(), "ops": counts})
            self._ev_safe("churn_input", lambda: self.record_input("churn", "apply_day", {
                "day": day.date.isoformat(), "ops": counts}))

    # ------------------------------------------------------------------ events
    def _apply_injections(self, at: dt.datetime) -> list[str]:
        """Run, once each and in time order, every scenario injection whose date + time (IST) is <= `at`."""
        due = []
        for inj in getattr(self.scenario, "injections", ()) or ():
            key = (inj.date, inj.time)
            if key in self._applied_injections:
                continue
            h, m = (int(x) for x in inj.time.split(":"))
            when = dt.datetime.combine(dt.date.fromisoformat(inj.date), dt.time(h, m), tzinfo=IST)
            if when <= at:
                due.append((when, inj))
        applied = []
        for when, inj in sorted(due, key=lambda t: t[0]):
            self._applied_injections.add((inj.date, inj.time))
            self._ev_safe("injection_input", lambda inj=inj: self.record_input(
                "injection", getattr(inj.fn, "__name__", "injection"), {"date": inj.date, "time": inj.time}))
            inj.fn(self.rig)
            applied.append(f"{inj.date} {inj.time}")
        return applied

    def _scheduled_at(self, ev: cal.Event) -> dt.datetime:
        late = (getattr(self.scenario, "lateness", None) or {}).get(f"{ev.at.date().isoformat()} {ev.kind}")
        if late is not None:
            return ev.at - late
        if not ev.is_catchup:
            return ev.at
        hm = cal.CLOUD_AT if ev.base_kind == cal.CLOUD else cal.LAN_AT
        for back in range(0, 60):
            cand = cal.at_ist(ev.at.date() - dt.timedelta(days=back), hm)
            if cand < ev.at:
                return cand
        return ev.at

    def _rows_now(self) -> list[dict]:
        db = self.rig.runtime_dir / "manifest.db"
        if not db.exists():
            return []
        conn = oracles.open_ro(db)
        try:
            return oracles.run_rows(conn)
        finally:
            conn.close()

    def run_event(self, day: cal.Day, ev: cal.Event) -> EventOutcome:
        t0 = time.perf_counter()
        group = (ev.at, ev.kind)
        base = ev.base_kind
        if base in (cal.CLOUD, cal.LAN) and self.last_group == group and self.last_outcome is not None:
            return EventOutcome(ev, flow=self.last_outcome.flow, shared=True, value=self.last_outcome.value)
        self.last_group, self.last_outcome = group, None
        self._maybe_churn(ev.at)
        out = EventOutcome(ev)
        timer = None
        try:
            from tools.livebox import evidence

            timer = evidence.ResourceTimer()
            timer.__enter__()
        except Exception:  # noqa: BLE001
            timer = None
        try:
            self._apply_injections(ev.at)
            if self.scenario.inject:
                self.scenario.inject(self, day, ev)
            self._run_event_inner(day, ev, out)
            if self.scenario.expect:
                got = self.scenario.expect(self, day, ev, out)
                out.checks.extend(list(got or []))
        except (KeyboardInterrupt, SystemExit, SafetyRefusal):
            raise
        except Exception as exc:  # noqa: BLE001 - a harness bug is a FAIL check, not a dead lane
            out.harness_error = traceback.format_exc()
            out.error = {"type": type(exc).__name__, "message": str(exc), "traceback": out.harness_error}
            out.ok = False
            out.checks.append(_check("HARNESS", "FAIL", "event handled", out.harness_error[-1500:]))
        with contextlib.suppress(Exception):
            out.scheduled_at = self._scheduled_at(ev)
        out.new_run_ids = [str(r.get("run_id")) for r in (out.new_rows or []) if isinstance(r, dict)
                           and r.get("run_id") is not None]
        out.real_seconds = time.perf_counter() - t0
        if timer is not None:
            try:
                timer.__exit__(None, None, None)
                out.resources = timer.as_dict()
            except Exception:  # noqa: BLE001
                pass
        self.event_seconds.append({"day": day.date.isoformat(), "kind": ev.kind, "real_seconds": round(out.real_seconds, 2)})
        self.day_seconds[day.date.isoformat()] = self.day_seconds.get(day.date.isoformat(), 0.0) + out.real_seconds
        self.last_outcome = out
        return out

    def _run_event_inner(self, day: cal.Day, ev: cal.Event, out: EventOutcome) -> None:
        rig, base = self.rig, ev.base_kind
        sched = self._scheduled_at(ev)
        if base in (cal.STATUS_AM, cal.STATUS_PM):
            rig.clock.move_to(ev.at)
            self._status_event(day, ev, out)
            return
        spy = (len(rig.seams.wake_calls), len(rig.seams.shutdown_calls), len(rig.seams.archive_calls))
        before = self._rows_now()
        if base == cal.CLOUD:
            for e in day.events:
                if e.at == ev.at and e.kind == ev.kind and e.fy:
                    rig.ensure_prefix(e.fy)
            out.flow = rig.run_flow("backup", at=ev.at, scheduled_at=sched, mode="cloud",
                                    **self._owner_approval(day, base))
        elif base == cal.LAN:
            out.flow = rig.run_flow("backup", at=ev.at, scheduled_at=sched, mode="lan",
                                    **self._owner_approval(day, base))
            self.lan_flows += 1
        elif base == cal.ROLLOVER_CHECK:
            out.flow = rig.run_flow("rollover", at=ev.at, scheduled_at=sched)
        elif base == cal.AUDIT:
            out.flow = rig.run_flow("audit", at=ev.at, scheduled_at=sched, mode="all")
        elif base == cal.WEEKLY:
            out.flow = rig.run_flow("weekly", at=ev.at, scheduled_at=sched)
        elif base == cal.MONTHLY:
            out.flow = rig.run_flow("monthly", at=ev.at, scheduled_at=sched)
        else:
            raise ValueError(f"unknown event kind {ev.kind}")
        out.value = out.flow.value
        rig.drain_mail(ev.at)
        before_ids = {r.get("run_id") for r in before}
        out.new_rows = [r for r in self._rows_now() if r.get("run_id") not in before_ids]
        wake = len(rig.seams.wake_calls) - spy[0]
        shut = len(rig.seams.shutdown_calls) - spy[1]
        arch = len(rig.seams.archive_calls) - spy[2]
        self._event_checks(day, ev, out, wake, shut, arch)

    def _owner_approval(self, day: cal.Day, leg: str) -> dict:
        """Play the owner: approve the oldest pending AAM-DEL-01 (G3_MASS_DELETE) request of `leg` once it is
        `approve_del_after_days` simulated days old, and hand its id to the flow like the dashboard's approved
        re-run does. Off unless asked for; every approval is logged to events.jsonl. Other guards are never
        approved here."""
        n = self.approve_del_after_days
        if n is None:
            return {}
        from core import approvals
        from core.manifest import ManifestDB
        from core.time_utils import now_iso

        cfg = self.rig.load_config()
        db = ManifestDB.from_config(cfg)
        try:
            for req in db.open_approvals():
                guards = [g.get("guard") for g in req["guards"]]
                if req["leg"] != leg or "G3_MASS_DELETE" not in guards:
                    continue
                if set(guards) - {"G3_MASS_DELETE"}:
                    continue                                   # a mixed block needs a human
                if req["status"] == approvals.PENDING:
                    age = (day.date - dt.datetime.fromisoformat(str(req["created_at"])).date()).days
                    if age < n:
                        continue
                    at = now_iso()
                    if not db.approve_request(req["approval_id"], approved_by="livebox-owner", approved_at=at,
                                              expires_at=approvals.expires_at(at, cfg.approvals.expiry_hours)):
                        continue
                    _jsonl(self.ev_dir / "events.jsonl", {
                        "event": "owner_approval", "day": day.date.isoformat(), "leg": leg, "fy": req.get("fy"),
                        "approval_id": req["approval_id"], "guard": "G3_MASS_DELETE", "age_days": age})
                return {"approval_id": req["approval_id"]}
        finally:
            db.close()
        return {}

    # ------------------------------------------------------------------ per-event checks
    def _event_checks(self, day, ev, out: EventOutcome, wake: int, shut: int, arch: int) -> None:
        base = ev.base_kind
        tag = f"{day.date} {ev.kind}"
        ok_sets = {cal.CLOUD: cal.CLOUD_OK, cal.LAN: cal.LAN_OK}
        flow_out = out.flow
        if base in ok_sets:
            statuses = [r["status"] for r in out.new_rows if r["mode"] == base]
            all_ok = bool(statuses) and all(s in ok_sets[base] for s in statuses)
            raised = flow_out.exc is not None
            out.checks.append(_check(
                "FLOW_STATE", "PASS" if raised != all_ok else "FAIL",
                "flow raises iff a pass is not a success status (Prefect never shows COMPLETED for a failure)",
                f"{tag}: statuses {statuses}, exception {type(flow_out.exc).__name__ if raised else None}"))
            if raised and all_ok:
                out.checks[-1]["detail"] = str(flow_out.exc)[:500]
        elif flow_out.exc is not None and base in (cal.ROLLOVER_CHECK, cal.AUDIT) and                 {cal.ROLLOVER_CHECK: "rollover", cal.AUDIT: "audit"}[base] in (getattr(day, "expect_raise", ()) or ()):
            out.checks.append(_check(
                "FLOW_STATE", "PASS", "flow raises (the calendar expects a blocked rollover / incomplete audit today)",
                f"{tag}: {type(flow_out.exc).__name__}: {str(flow_out.exc)[:300]}"))
        elif flow_out.exc is not None:
            out.checks.append(_check("FLOW_STATE", "FAIL", "flow completes", f"{tag}: {type(flow_out.exc).__name__}: "
                                                                          f"{str(flow_out.exc)[:600]}"))
        if base == cal.LAN:
            statuses = [r["status"] for r in out.new_rows if r["mode"] == cal.LAN]
            want_shut = 1 if statuses and all(s in cal.LAN_OK for s in statuses) else 0
            if want_shut:
                self.lan_flows_complete += 1
            passes = max(1, len(statuses))
            self.lan_passes += passes
            if wake == passes and passes > 1 and shut == want_shut:
                # D6 gate: plan 1.6 expects one wake per LAN flow; stays FINDING/KNOWN until WS-E E4 merges (then FAIL)
                first = not self._wake_per_pass_seen
                self._wake_per_pass_seen = True
                out.checks.append(_check(
                    "SPY_WAKE_PER_PASS", "FINDING" if first else "KNOWN",
                    "NAS wake spy = 1 per LAN flow (plan 1.6)",
                    f"{tag}: wake {wake}x for {passes} passes (flow.py:1129 wakes once per pass; the second call "
                    f"finds the NAS up), shutdown {shut}x", finding="F-NEW-WAKE-PER-PASS"))
            else:
                out.checks += _viol_checks("SPY", [] if (wake == 1 and shut == want_shut) else
                                           [oracles.Violation("SPY", f"{tag}: wake {wake}x (want 1), shutdown {shut}x "
                                                                     f"(want {want_shut})")],
                                           "NAS wake spy = 1 and shutdown spy = 1 only when every LAN pass is complete")
        elif base == cal.AUDIT:
            lan_on = any(r.leg == "audit_lan" for r in day.rows)
            self.audit_wakes += wake
            self.audit_shutdowns += shut
            want = audit_spy_expectation(lan_on, self.rig.config)
            out.checks.append(_check("SPY_AUDIT", "PASS" if (wake, shut) == want else "FAIL",
                                     "the audit's LAN leg wakes the NAS once and shuts it down after the last pass "
                                     "only when lan.shutdown_after_backup and wol.enabled are set "
                                     "(flow.py integrity_audit_flow _shut_nas_down)",
                                     f"{tag}: wake {wake}, shutdown {shut}, want {want}"))
        elif base != cal.ROLLOVER_CHECK and (wake or shut):
            out.checks.append(_check("SPY_UNEXPECTED", "FAIL", f"a {base} flow never wakes or shuts down the NAS",
                                     f"{tag}: wake {wake}, shutdown {shut}"))
        if base == cal.ROLLOVER_CHECK:
            self.rollover_wakes += wake
            self.rollover_shutdowns += shut
            if day.is_switch_day:
                # OD6: wake allowed at 1 Apr (new FY folder creation); shutdown follows lan.shutdown_after_backup or 0
                shut_after = bool(self.rig.config.get("lan", {}).get("shutdown_after_backup", True))
                shut_ok = (shut == 0) or (shut == 1 and shut_after)
                verdict = "PASS" if shut_ok else "FAIL"
            else:
                verdict = "FINDING" if (wake or shut) else "PASS"
            out.checks.append(_check(
                "SPY_ROLLOVER_NAS", verdict,
                "OD6: wake allowed at 1 Apr switch; non-switch rollover never touches the NAS",
                f"{tag}: wake {wake}, shutdown {shut}",
                finding="F-OWN-1" if verdict == "FINDING" else None))
            exp_arch = 1 if day.is_switch_day else 0
            out.checks.append(_check("ARCHIVE", "PASS" if arch == exp_arch else "FAIL",
                                     f"archive transition calls: {exp_arch}", f"{tag}: {arch}"))
            text = json.dumps(out.value, default=str) if out.value is not None else ""
            if day.rollover_result:
                out.checks.append(_check("ROLLOVER_RESULT", "PASS" if day.rollover_result in text else "FAIL",
                                         day.rollover_result, f"{tag}: {text[:300]}"))
        lock = Path(self.rig.config.get("paths", {}).get("runtime_dir", self.rig.runtime_dir)) / "backup.lock"
        out.checks.append(_check("LOCK", "PASS" if not lock.exists() else "FAIL", "backup.lock absent after the event",
                                 f"{tag}: {'present' if lock.exists() else 'absent'}"))
        if self.argv_violations:
            out.checks.append(_check("ARGV", "FAIL", "all argv paths inside the lane", "; ".join(self.argv_violations)))
        if base in (cal.CLOUD, cal.LAN):
            out.checks += self._tree_checks(day, ev, out)

    def _special_day(self, day: cal.Day) -> bool:
        return (day.is_sunday or day.is_first_run or day.is_activation_day or day.is_switch_day
                or day.is_first_of_month or day.is_leap_day or day.index == 0)

    def _tree_checks(self, day, ev, out: EventOutcome) -> list[dict]:
        base = ev.base_kind
        rows = {r["run_id"]: r for r in out.new_rows}
        fys = [e.fy for e in day.events if e.at == ev.at and e.kind == ev.kind and e.fy]
        ok_set = cal.CLOUD_OK if base == cal.CLOUD else cal.LAN_OK
        res: list[dict] = []
        full = self._special_day(day)
        for i, fy in enumerate(fys):
            sts = [r["status"] for r in out.new_rows if r["mode"] == base]
            if len(sts) != len(fys) or sts[i] not in ok_set:
                continue                                    # the row check reports the failed pass
            src = self.src_dir(fy)
            dst = self.bucket_dir(fy) if base == cal.CLOUD else self.nas_dir(fy)
            label = f"{day.date} {base} {fy}: {src.name} -> {dst}"
            if full:
                v = oracles.check_tree(src, dst, label, ignore=[self.MARKER])
                res += _viol_checks("TREE_FULL", v, "sha256 identical (source == destination)")
            else:
                a, b = tree_summary(src, [self.MARKER]), tree_summary(dst, [self.MARKER])
                res.append(_check("TREE_CHEAP", "PASS" if _summary_equal(a, b) else "FAIL",
                                  "count, bytes, newest mtime equal", f"{label}: src {a} dst {b}"))
        return res

    # ------------------------------------------------------------------ status / watchdog
    def _status_event(self, day, ev, out: EventOutcome) -> None:
        cfg = self.rig.load_config()
        view = None
        try:
            view = self._fetch_status(cfg, ev)
        except Exception:  # noqa: BLE001
            out.checks.append(_check("STATUS_FETCH", "FAIL", "/status answers", traceback.format_exc()[-800:]))
        out.status = view
        self.status_views[(day.date, ev.base_kind)] = view
        # the watchdog's overdue check, real function, called by the driver (never watchdog.main)
        try:
            import watchdog

            stale = watchdog._check_stale_legs(cfg)
            self.rig.drain_mail(ev.at)
            bad = unexpected_stale_legs(stale, day)
            out.checks.append(_check("WATCHDOG", "PASS" if not bad else "FAIL",
                                     "no overdue leg on a healthy lane", f"{day.date} {ev.kind}: stale {stale}"))
        except Exception:  # noqa: BLE001
            out.checks.append(_check("WATCHDOG", "FAIL", "watchdog check callable", traceback.format_exc()[-800:]))

    def _fetch_status(self, cfg, ev) -> "oracles.StatusView":
        if "ui" not in sys.modules:                       # ui mounts the relative folder "static" at import
            prev = os.getcwd()
            os.chdir(self.code_root)
            try:
                import ui  # noqa: F401
            finally:
                os.chdir(prev)
        import ui
        from fastapi.testclient import TestClient

        from core import admin_auth

        tok = "livebox-" + "s" * 56
        with ui._CFG_LOCK:
            ui._config = None
        ui._sessions[tok] = {"created_at": time.time(),
                             "fp": admin_auth.fingerprint(cfg.dashboard.login_password_hash)}
        client = TestClient(ui.app, cookies={"session": tok})
        r = client.get("/status")
        if r.status_code != 200:
            raise RuntimeError(f"/status HTTP {r.status_code}: {r.text[:300]}")
        d = self.ev_dir / "status"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{ev.at:%Y-%m-%d_%H%M}.json").write_text(json.dumps(r.json(), indent=1), encoding="utf-8")
        return oracles.read_status(r.json())

    # ------------------------------------------------------------------ per-day oracles
    def _mails(self) -> list[dict]:
        idx = self.ev_dir / "mail_index.csv"
        if not idx.exists():
            return []
        mails = oracles.load_mail_index(idx)
        db = self.rig.runtime_dir / "manifest.db"
        notes = []
        if db.exists():
            conn = oracles.open_ro(db)
            try:
                notes = oracles.notification_rows(conn)
            finally:
                conn.close()
        mails = oracles.join_notifications(mails, notes)
        import re

        for m in mails:
            m["severity"] = (m.get("kind") or "").upper()
            key = m.get("key") or ""
            m["fy"] = (re.search(r"FY\d{2}-\d{2}", key) or re.search(r"FY\d{2}-\d{2}", m.get("subject", "")) or
                       [None])[0] or ""
            m["cls"] = ("overdue" if key.startswith("stale:") else
                        "failure" if m["severity"] in ("FAILURE", "ACTION_REQUIRED") else "")
        return mails

    def check_day(self, day: cal.Day, results: list) -> list[dict]:
        checks: list[dict] = []
        for _ev, obj in results:
            if isinstance(obj, EventOutcome) and not obj.shared:
                checks += obj.checks
        # one end-of-day mtime normalisation pass is done per event by the rig; the day's own oracles follow
        fams: list[tuple[str, Callable[[], list[dict]]]] = [
            ("ROWS", lambda: self._day_rows(day)), ("MAIL", lambda: self._day_mail(day)),
            ("META", lambda: self._day_meta(day)), ("CONFIG_FY", lambda: self._day_config(day)),
            ("STATUS", lambda: self._day_status(day)), ("FROZEN", lambda: self._day_frozen(day)),
            ("RETENTION", lambda: self._day_retention(day)),
        ]
        if self.scenario.end_of_day:
            fams.append(("SCENARIO_END_OF_DAY", lambda: list(self.scenario.end_of_day(self, day, results) or [])))
        for name, fn in fams:
            try:
                checks += fn()
            except Exception:  # noqa: BLE001
                checks.append(_check(f"{name}", "FAIL", "oracle runs", "harness error: " + traceback.format_exc()[-900:]))
        apply_known_findings(day, checks)
        if day.is_activation_day and day.is_sunday:      # plan 2, edge E1: a red audit here is a finding, not a harness FAIL
            for c in checks:
                obs = str(c.get("observed", ""))
                if c["verdict"] == "FAIL" and (c["check_id"].startswith(("ROWS:AUDIT", "MAIL")) or "AAM-AUD-01" in obs
                                               or (c["check_id"] == "FLOW_STATE" and " audit:" in obs)):
                    c["verdict"], c["finding"] = "FINDING", "F-E1-ACTIVATION-SUNDAY-AUDIT"
        self._after_day(day)
        self._ev_safe("after_day", lambda: self._evidence_after_day(day))
        total = round(self.day_seconds.get(day.date.isoformat(), 0.0), 2)
        _jsonl(self.ev_dir / "days.jsonl", {"day": day.date.isoformat(), "real_seconds": total,
                                            "events": len(day.events),
                                            "fails": sum(1 for c in checks if c["verdict"] == "FAIL"),
                                            "checks": len(checks)})
        return checks

    def _day_rows(self, day) -> list[dict]:
        conn = oracles.open_ro(self.rig.runtime_dir / "manifest.db") if (self.rig.runtime_dir / "manifest.db").exists() else None
        if conn is None:
            return [_check("ROWS", "FAIL" if day.rows else "PASS", f"{len(day.rows)} rows expected", "no manifest.db")]
        try:
            v = oracles.check_run_rows(conn, day)
            mv = oracles.check_rclone_lan_metrics(conn, day.date)
        finally:
            conn.close()
        return (_viol_checks("ROWS", v, f"{len(day.rows)} expected rows (run_history + audits) with the allowed statuses")
                + _viol_checks("METRICS", mv, "rclone LAN metrics (counts_source, arrival, hash, deletes_held)"))

    def _day_mail(self, day) -> list[dict]:
        self.rig.drain_mail(cal.at_ist(day.date, (23, 59)))
        mails = self._mails()
        v = oracles.check_expected_mails(mails, day)
        cfgd = self.rig.config
        n = cfgd.get("alerts", {}) or {}
        allv = oracles.check_mail_rules(
            mails, recipients=cfgd["notifications"]["recipients"],
            report_recipients=cfgd["notifications"]["report_recipients"],
            repeat_cooldown_hours=n.get("repeat_cooldown_hours", 12),
            stale_repeat_hours=n.get("stale_repeat_hours", 24),
            injection_dates=self._injection_dates,
            outages=[(cal._as_dt(o.start, False), cal._as_dt(o.end, True)) for o in self.outages])
        new = [x for x in allv if str(x) not in self._reported_mail_violations]
        self._reported_mail_violations.update(str(x) for x in allv)
        today = [m for m in mails if m["sim_time"].astimezone(IST).date() == day.date]
        ev_ref = [f"mail_index.csv ({len(today)} mails today)"]
        weekly = [m for m in today if "weekly backup report" in m.get("subject", "").lower()]
        monthly = [m for m in today if "monthly backup report" in m.get("subject", "").lower()]
        extra = []
        if len(weekly) != day.mail_count(cal.MAIL_WEEKLY):
            extra.append(oracles.Violation("MAIL", f"{day.date}: {len(weekly)} weekly mails, expected "
                                                   f"{day.mail_count(cal.MAIL_WEEKLY)}"))
        if len(monthly) != day.mail_count(cal.MONTHLY_MAIL):
            extra.append(oracles.Violation("MAIL", f"{day.date}: {len(monthly)} monthly mails, expected "
                                                   f"{day.mail_count(cal.MONTHLY_MAIL)}"))
        return (_viol_checks("MAIL", v + extra, "keyed mails exactly as expected, 0 problem mails", ev_ref)
                + _viol_checks("MAIL_RULES_M1_M5", new, "storm rules M1-M5", ev_ref))

    def _day_meta(self, day) -> list[dict]:
        db = self.rig.runtime_dir / "manifest.db"
        if not db.exists():
            return []
        conn = oracles.open_ro(db)
        v: list = []
        try:
            nxt = day.next_fy_end
            if nxt:
                self.ever_activated.add(nxt)
            for fy in sorted(self.ever_activated):
                active = fy == nxt
                v += oracles.check_overlap_meta(conn, fy, active=True, ended=not active)
            probe = cal.next_fy(day.configured_fy_end)
            if probe not in self.ever_activated:
                v += oracles.check_overlap_meta(conn, probe, active=False)
        finally:
            conn.close()
        return _viol_checks("META", v, "db_meta overlap:<fy> present exactly while/after the overlap, ended_at after the switch")

    def _day_config(self, day) -> list[dict]:
        import yaml

        raw = yaml.safe_load(self.rig.config_path.read_text(encoding="utf-8"))
        s = Path(raw["paths"]["source_drive"]).name
        n = Path(raw["paths"]["lan_destination"].replace("\\", "/")).name
        want = day.configured_fy_end
        ok = s == want and n == want
        return [_check("CONFIG_FY", "PASS" if ok else "FAIL", f"config paths on {want} (switch only on 1 April)",
                       f"source leaf {s}, NAS leaf {n}")]

    def _day_status(self, day) -> list[dict]:
        res: list[dict] = []
        for kind, active, name in ((cal.STATUS_AM, day.overlap_active_at_0900, "0900"),
                                   (cal.STATUS_PM, day.overlap_active_end, "2330")):
            if not any(e.base_kind == kind for e in day.events):
                continue
            view = self.status_views.get((day.date, kind))
            if view is None:
                res.append(_check(f"STATUS_{name}", "FAIL", "/status answered", "no /status view"))
                continue
            cards = [c for leg in ("cloud", "lan") for c in ((leg, leg + "_next") if active else (leg,))]
            kw = dict(cards=cards, overlap_active=active, configured_fy=day.configured_fy_end,
                      next_fy=day.next_fy_end if active else None)
            if not active:
                kw["activates_on"] = cal.activation_date(day.configured_fy_end, self.activate_on).isoformat()
            res += _viol_checks(f"STATUS_{name}", oracles.check_status(view, **kw),
                                f"cards {cards}, overlap.active {active}, fy {day.configured_fy_end}",
                                [f"status/{day.date}_{name}.json"])
        return res

    def _special_dirs(self) -> dict[str, Path]:
        d = {}
        for fy in self.closed_fys:
            d[f"nas:{fy}"] = self.nas_dir(fy)
            d[f"bucket:{fy}"] = self.bucket_dir(fy)
        return d

    def _day_frozen(self, day) -> list[dict]:
        res: list[dict] = []
        full = self._special_day(day)
        for key, path in self._special_dirs().items():
            snap = self.frozen.get(key)
            if snap is None:
                continue
            if full:
                v = oracles.check_frozen(snap["hashes"], path, key, ignore=[self.MARKER])
                res += _viol_checks("FROZEN_FULL", v, f"{key} unchanged since the switch (sha256)")
            else:
                now = tree_summary(path, [self.MARKER])
                res.append(_check("FROZEN_CHEAP", "PASS" if _summary_equal(tuple(snap["summary"]), now) else "FAIL",
                                  f"{key} unchanged since the switch", f"{key}: was {snap['summary']} now {now}"))
        return res

    def _day_retention(self, day) -> list[dict]:
        ret = (self.rig.config.get("run_logs", {}) or {}).get("retention_days", 90)
        run_logs = self.rig.runtime_dir / "run_logs"
        if not run_logs.is_dir() or day.index <= ret + 1:
            return []
        oldest = None
        for d in run_logs.iterdir():
            try:
                dd = dt.date.fromisoformat(d.name)
            except ValueError:
                continue
            oldest = dd if oldest is None else min(oldest, dd)
        if oldest is None:
            return []
        age = (day.date - oldest).days
        return [_check("RETENTION", "PASS" if age <= ret + 1 else "FAIL",
                       f"run_logs day folders at most {ret} days old", f"oldest {oldest} = {age} days old")]

    def _after_day(self, day: cal.Day) -> None:
        """End of day bookkeeping: close a switched FY (snapshot its destinations), flush a never-applied churn."""
        if self.pending_churn is not None:
            self._maybe_churn(None)
        if day.is_switch_day:
            old = cal.fy_of_date(day.date - dt.timedelta(days=1)) if False else None
            old = self._closing_fy(day)
            if old and old not in self.closed_fys:
                self.closed_fys.append(old)
                for kind, path in (("nas", self.nas_dir(old)), ("bucket", self.bucket_dir(old))):
                    self.frozen[f"{kind}:{old}"] = {"hashes": oracles.tree_hash(path, [self.MARKER]),
                                                    "summary": list(tree_summary(path, [self.MARKER]))}
                (self.ev_dir / "frozen").mkdir(exist_ok=True)
                (self.ev_dir / "frozen" / f"{old}.json").write_text(json.dumps(
                    {k: v["summary"] for k, v in self.frozen.items()}, indent=1), encoding="utf-8")

    def _closing_fy(self, day: cal.Day) -> str | None:
        """The FY the switch of `day` closed = the configured FY at the start of that day."""
        return day.configured_fy

    # ------------------------------------------------------------------ state / summary
    def _spy_totals(self) -> dict:
        s = self.rig.seams if self.rig else None
        cur = {"wake": len(s.wake_calls), "shutdown": len(s.shutdown_calls), "archive": len(s.archive_calls)} if s else {}
        return {k: self.prev_spies.get(k, 0) + cur.get(k, 0) for k in ("wake", "shutdown", "archive")}

    def get_state(self) -> dict:
        return {"closed_fys": self.closed_fys, "ever_activated": sorted(self.ever_activated),
                "lan_flows": self.lan_flows, "lan_flows_complete": self.lan_flows_complete,
                "spies": self._spy_totals(), "lan_passes": self.lan_passes, "audit_wakes": self.audit_wakes, "audit_shutdowns": self.audit_shutdowns,
                "rollover_wakes": self.rollover_wakes, "rollover_shutdowns": self.rollover_shutdowns,
                "frozen": {k: {"summary": v["summary"], "hashes": v["hashes"]} for k, v in self.frozen.items()}}

    def load_state(self, state: dict) -> None:
        h = state.get("hooks") or {}
        self.closed_fys = list(h.get("closed_fys", []))
        self.ever_activated = set(h.get("ever_activated", []))
        self.lan_flows = h.get("lan_flows", 0)
        self.lan_flows_complete = h.get("lan_flows_complete", 0)
        self.prev_spies = dict(h.get("spies") or self.prev_spies)
        self.lan_passes = h.get("lan_passes", 0)
        self.audit_wakes = h.get("audit_wakes", 0)
        self.audit_shutdowns = h.get("audit_shutdowns", 0)
        self.rollover_wakes = h.get("rollover_wakes", 0)
        self.rollover_shutdowns = h.get("rollover_shutdowns", 0)
        self.frozen = h.get("frozen", {})

    def final_checks(self) -> list[dict]:
        out: list[dict] = []
        rig = self.rig
        t = cal.totals(self.days)
        tot = self._spy_totals()
        out.append(_check("FINAL_ARCHIVE", "PASS" if tot["archive"] == t["switches"] else "FAIL",
                          f"archive transition recorded {t['switches']}x (once per switch, never the open FYs)",
                          f"{tot['archive']}: {rig.seams.archive_calls}"))
        v = oracles.check_spies(wake_calls=tot["wake"] - self.audit_wakes - self.rollover_wakes,
                                shutdown_calls=tot["shutdown"] - self.audit_shutdowns - self.rollover_shutdowns,
                                lan_flows=self.lan_passes or self.lan_flows, lan_flows_all_complete=self.lan_flows_complete)
        out += _viol_checks("FINAL_SPY", v, "wake = LAN flows of this process, shutdown = fully complete LAN flows")
        out.append(_check("FINAL_NO_REAL_POWER", "PASS" if not self.argv_violations else "FAIL",
                          "no shutdown / wake / gcloud ever executed", f"violations {self.argv_violations}"))
        return out

    def _orphan_rclone(self) -> list[int]:
        """pids of rclone processes of THIS lane (command line holds the lane root) still alive."""
        pids: list[int] = []
        try:
            import psutil

            root = os.path.normcase(str(self.lane_root))
            for p in psutil.process_iter(["name", "cmdline"]):
                try:
                    if not (p.info.get("name") or "").lower().startswith("rclone"):
                        continue
                    if root in os.path.normcase(" ".join(p.info.get("cmdline") or [])):
                        pids.append(p.pid)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        return pids

    def _write_summary(self) -> None:
        if self.rig is None:
            return
        rig = self.rig
        summary = {"lane": self.lane, "stamp": self.stamp, "lane_root": str(self.lane_root),
                   "evidence": str(self.ev_dir),
                   "wake_calls": len(rig.seams.wake_calls) if rig.seams else None,
                   "shutdown_calls": len(rig.seams.shutdown_calls) if rig.seams else None,
                   "archive_calls": rig.seams.archive_calls if rig.seams else None,
                   "argv_violations": self.argv_violations, "seconds_per_day": self.day_seconds,
                   "orphan_rclone": self._orphan_rclone(), "created_paths": list(self.created_paths),
                   "junction_target_entries": list(self.junction_target_entries),
                   "switch_before_manual_lan": bool(self.switch_before_manual_lan),
                   "spies": self._spy_totals(), "evidence_errors": self.evidence_errors,
                   "total_real_seconds": round(sum(self.day_seconds.values()), 1),
                   "safety": [{"rule": r.rule, "status": r.status} for r in self.safety_results]}
        (self.ev_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
