"""WS-F F1: Shadow-trial runner using exact product `build_lan_rclone_command` argv."""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

import psutil

from core.lan_sync import build_lan_rclone_command, decide_lan_rclone_result
from models.config import load_config


def _is_inside_mirror(dest: str, mirror_root: str) -> bool:
    """True when `dest` equals or sits inside `mirror_root` (case-insensitive Windows paths)."""
    d = str(Path(dest)).rstrip("\\/").casefold()
    m = str(Path(mirror_root)).rstrip("\\/").casefold()
    if not m:
        return False
    return d == m or d.startswith(m + "\\") or d.startswith(m + "/")


def run_shadow_step(
    *,
    step: str,
    config_path: str,
    source: str,
    dest: str,
    out_dir: str,
    dry_run: bool = False,
    max_delete: int = 0,
    backup_dir: str | None = None,
    poll_seconds: float = 5.0,
) -> dict:
    """Build exact product rclone argv, enforce mirror-write safety, execute, and sample RSS/CPU."""
    cfg = load_config(config_path)
    if not dry_run and _is_inside_mirror(dest, cfg.paths.lan_destination):
        raise ValueError(
            f"Refusing non-dry-run shadow write inside real mirror {cfg.paths.lan_destination!r}: {dest!r}"
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle = Path(out_dir) / f"shadow_{step}_{stamp}"
    bundle.mkdir(parents=True, exist_ok=True)
    log_path = bundle / "rclone.log"
    combined_path = (bundle / "combined.txt") if dry_run else None

    cmd = build_lan_rclone_command(
        cfg.lan,
        source,
        dest,
        str(log_path),
        max_delete=None if dry_run else max_delete,
        dry_run=dry_run,
        combined_path=str(combined_path) if combined_path else None,
        backup_dir=backup_dir,
    )
    (bundle / "argv.json").write_text(json.dumps(cmd, indent=2), encoding="utf-8")

    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ps_proc = psutil.Process(proc.pid)
    peak_rss_mb = 0.0
    mem_rows: list[tuple[float, float, float]] = []
    while proc.poll() is None:
        try:
            rss_mb = ps_proc.memory_info().rss / (1024 * 1024)
            cpu_pct = ps_proc.cpu_percent(interval=None)
            peak_rss_mb = max(peak_rss_mb, rss_mb)
            mem_rows.append((round(time.monotonic() - t0, 2), round(rss_mb, 2), round(cpu_pct, 1)))
        except psutil.Error:
            pass
        time.sleep(poll_seconds)
    stdout_text, _ = proc.communicate()
    wall_s = round(time.monotonic() - t0, 2)

    with (bundle / "memory.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["elapsed_s", "rss_mb", "cpu_pct"])
        w.writerows(mem_rows)

    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else stdout_text
    decision = decide_lan_rclone_result(proc.returncode, log_text, error_tail_lines=cfg.lan.error_tail_lines)
    (bundle / "stats.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")
    timings = {"step": step, "wall_s": wall_s, "peak_rss_mb": round(peak_rss_mb, 2), "exit_code": proc.returncode}
    (bundle / "timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
    return {"bundle": str(bundle), "cmd": cmd, "decision": decision, "timings": timings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WS-F shadow trial step runner")
    ap.add_argument("--step", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-delete", type=int, default=0)
    ap.add_argument("--backup-dir", default=None)
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ns = ap.parse_args(argv)
    res = run_shadow_step(
        step=ns.step, config_path=ns.config, source=ns.source, dest=ns.dest,
        out_dir=ns.out_dir, dry_run=ns.dry_run, max_delete=ns.max_delete,
        backup_dir=ns.backup_dir, poll_seconds=ns.poll_seconds,
    )
    print(json.dumps(res["timings"]))
    return 0 if res["timings"]["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
