"""L1 spike: prove the time_machine simulated clock (test plan section 1.4).

Standalone, offline, no services, no production config, no network.
Usage:
  python spike_clock.py --overlay <dir with time_machine> --lane-dir <dir> --out <json>
                        [--allow-dev-path]
Exit code 0 only if every probe passed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

LIVEBOX_ROOT = Path(r"C:\AAMLivebox")
IST_OFFSET = timedelta(hours=5, minutes=30)
START = "2090-12-31T06:00:00+05:30"
N_EVENTS = 10


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _set_creation_time(path: Path, ts: float) -> bool:
    """Set the Windows creation time (best effort, ctypes). False when unsupported."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(str(path), 0x100, 7, None, 3, 0x02000000, None)  # FILE_WRITE_ATTRIBUTES, backup semantics
    if h in (None, wintypes.HANDLE(-1).value):
        return False
    try:
        ft = ctypes.c_ulonglong(int((ts + 11644473600) * 10_000_000))
        return bool(k32.SetFileTime(h, ctypes.byref(ft), None, None))
    finally:
        k32.CloseHandle(h)


class MtimeNormaliser:
    """After each event, give every NEW file/folder under root the simulated time."""

    def __init__(self, root: Path):
        self.root = root
        self.seen: set[str] = set()

    def normalise(self, sim_ts: float) -> int:
        n = 0
        for dp, dns, fns in os.walk(self.root):
            for name in dns + fns:
                p = os.path.join(dp, name)
                if p in self.seen:
                    continue
                self.seen.add(p)
                os.utime(p, (sim_ts, sim_ts))
                _set_creation_time(Path(p), sim_ts)
                n += 1
        return n


def run(overlay: Path, lane: Path) -> dict:
    sys.path.insert(0, str(overlay))
    import time_machine  # noqa: PLC0415 - must come from the overlay

    import pendulum  # noqa: PLC0415

    res: dict = {"time_machine_file": time_machine.__file__, "python": sys.version,
                 "probes": {}}
    probes = res["probes"]

    def probe(name, fn):
        try:
            ok, detail = fn()
        except Exception:  # noqa: BLE001
            ok, detail = False, traceback.format_exc()
        probes[name] = {"ok": bool(ok), "detail": detail}

    real_before = time.time()
    start = pendulum.parse(START)
    traveller = time_machine.travel(start, tick=True)
    coords = traveller.start()
    try:
        def clocks():
            t = time.time()
            d = datetime.now(tz=start.timezone)
            p = pendulum.now("Asia/Kolkata")
            want = start.timestamp()
            ok = all(abs(x - want) < 60 for x in (t, d.timestamp(), p.timestamp()))
            ok = ok and p.year == 2090 and p.month == 12 and p.day == 31 and p.hour == 6
            return ok, {"time.time": t, "datetime": d.isoformat(), "pendulum": p.isoformat()}
        probe("clocks_moved", clocks)

        def sub():
            r = subprocess.run(["cmd", "/c", "echo", "alive"], capture_output=True, text=True, timeout=30)
            return r.returncode == 0 and "alive" in r.stdout, {"rc": r.returncode, "out": r.stdout.strip()}
        probe("real_subprocess", sub)

        def ticking():
            a = time.time()
            time.sleep(0.3)
            b = time.time()
            return 0.2 < b - a < 5, {"delta_s": b - a}
        probe("ticks_inside_event", ticking)

        # 10 events, 1-day jumps; each event writes a file, then the normaliser stamps it
        lane.mkdir(parents=True, exist_ok=True)
        norm = MtimeNormaliser(lane)
        norm.normalise(0.0)  # baseline: ignore whatever exists before the spike
        norm.seen = {os.path.join(dp, n) for dp, dn, fn in os.walk(lane) for n in dn + fn}
        per_event, jumps, mt = [], [], []
        for i in range(N_EVENTS):
            target = start.add(days=i + 1)
            t0 = time.perf_counter()
            coords.move_to(target, tick=True)
            seen = pendulum.now("Asia/Kolkata")
            f = lane / f"event_{i:02d}.txt"
            f.write_text(f"event {i}")
            norm.normalise(target.timestamp())
            per_event.append(time.perf_counter() - t0)
            jumps.append(abs(seen.timestamp() - target.timestamp()) < 60)
            mt.append(abs(f.stat().st_mtime - target.timestamp()) < 1)
        probe("jump_10_events", lambda: (all(jumps) and len(jumps) == N_EVENTS,
              {"final": pendulum.now("Asia/Kolkata").isoformat(), "per_jump_ok": jumps}))
        probe("mtime_normaliser", lambda: (all(mt), {"mtime_ok": mt,
              "example": datetime.fromtimestamp(
                  (lane / "event_09.txt").stat().st_mtime).isoformat()}))
        res["seconds_per_event"] = {"mean": sum(per_event) / len(per_event), "max": max(per_event)}
        probe("subprocess_after_jumps", sub)
    finally:
        traveller.stop()

    after = time.time()
    probes["clock_restored"] = {"ok": abs(after - real_before) < 600,
                                "detail": {"real_before": real_before, "after_stop": after}}
    res["all_ok"] = all(p["ok"] for p in probes.values())
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--overlay", required=True, type=Path)
    ap.add_argument("--lane-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--allow-dev-path", action="store_true",
                    help="dev PC only: allow a lane dir outside C:\\AAMLivebox")
    a = ap.parse_args()
    if not (_under(a.lane_dir, LIVEBOX_ROOT) or a.allow_dev_path):
        print(f"REFUSED: lane dir must be under {LIVEBOX_ROOT} (or pass --allow-dev-path)", file=sys.stderr)
        return 2
    if not (a.overlay / "time_machine").exists():
        print(f"REFUSED: no time_machine package in {a.overlay}", file=sys.stderr)
        return 2
    res = run(a.overlay, a.lane_dir)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"all_ok": res["all_ok"], "probes": {k: v["ok"] for k, v in res["probes"].items()},
                      "seconds_per_event": res.get("seconds_per_event")}, indent=2))
    return 0 if res["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
