r"""One day of churn in the seed folder: add a few files, replace a few, delete a few.

Meant to run unattended once a day (Windows scheduled task, see
tools/install_seed_daily_task.ps1), so every backup run has something new, something
changed and something removed to report.

  python tools/seed_daily.py --dest E:\FY26-27                  # 1-5 added, 0-3 replaced, 0-2 deleted
  python tools/seed_daily.py --dest E:\FY26-27 --dry-run         # print the plan, touch nothing
  python tools/seed_daily.py --dest E:\FY26-27 --max-total 150   # never grow past 150 seeded files

Counts are picked at random up to the given maximum (a day can legitimately be quiet), the
population is capped by --max-total and never falls below --keep-min, so the folder stays a
sensible size forever. Only <dest>\_seed_test is ever touched (see tools/seed_test_files.py,
which does the actual writing): real files are never read, changed or deleted.

Every run appends one line to --log when given, e.g.
  2026-10-27 12:00:01 added 4, replaced 2, deleted 1 (6.1 MB), 108 seeded files in E:\FY26-27\_seed_test
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seed_test_files as sf  # noqa: E402

MIN_KB, MAX_KB = 1, sf.HARD_MAX_KB


def _existing(folder: Path) -> list[str]:
    """Files this tool seeded that are still on disk (the manifest is the only source of truth)."""
    manifest = folder / sf.MANIFEST
    if not manifest.exists():
        return []
    with manifest.open(newline="", encoding="utf-8") as f:
        return [row["file"] for row in csv.DictReader(f) if (folder / row["file"]).exists()]


def plan_day(rng: random.Random, total: int, *, add_max: int, replace_max: int,
             delete_max: int, keep_min: int, cap: int) -> dict[str, int]:
    """How many files to add/replace/delete today, honouring keep_min and cap."""
    delete = min(rng.randint(0, delete_max), max(0, total - keep_min))
    replace = min(rng.randint(0, replace_max), total - delete)
    add = rng.randint(1, add_max) if add_max > 0 else 0
    add = max(0, min(add, cap - (total - delete)))
    return {"add": add, "replace": replace, "delete": delete}


def daily(dest: Path, *, add_max: int = 5, replace_max: int = 3, delete_max: int = 2,
          cap: int = 200, keep_min: int = 10, max_kb: int = 1900,
          seed_value: int | None = None, dry_run: bool = False) -> dict:
    """Run one day of churn under `dest` and return what happened (or would happen)."""
    dest = Path(dest)
    if not dest.is_dir():
        raise NotADirectoryError(f"{dest} is not an existing folder")
    folder = dest / sf.SEED_DIR
    total = len(_existing(folder))
    rng = random.Random(seed_value)
    plan = plan_day(rng, total, add_max=add_max, replace_max=replace_max,
                    delete_max=delete_max, keep_min=keep_min, cap=cap)
    if dry_run:
        return {"added": plan["add"], "replaced": plan["replace"], "deleted": plan["delete"],
                "bytes": 0, "total_files": total, "folder": str(folder), "dry_run": True}
    r = sf.seed(dest, count=plan["add"], max_kb=max_kb, seed_value=seed_value,
                modify=plan["replace"], delete=plan["delete"])
    return {"added": r["added"], "replaced": r["modified"], "deleted": r["deleted"],
            "bytes": r["bytes"], "total_files": r["total_files"], "folder": r["folder"],
            "dry_run": False}


def _line(r: dict, stamp: datetime) -> str:
    head = stamp.strftime("%Y-%m-%d %H:%M:%S")
    if r["dry_run"]:
        return (f"{head} dry-run: would add {r['added']}, replace {r['replaced']}, "
                f"delete {r['deleted']} (now {r['total_files']} seeded files)")
    return (f"{head} added {r['added']}, replaced {r['replaced']}, deleted {r['deleted']} "
            f"({r['bytes'] / 1024 / 1024:.1f} MB), {r['total_files']} seeded files in {r['folder']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, required=True,
                    help="folder holding the _seed_test sub-folder (usually paths.source_drive)")
    ap.add_argument("--add", type=int, default=5, help="at most this many new files a day (default 5)")
    ap.add_argument("--replace", type=int, default=3, help="at most this many rewritten files a day (default 3)")
    ap.add_argument("--delete", type=int, default=2, help="at most this many removed files a day (default 2)")
    ap.add_argument("--max-total", type=int, default=200, help="never keep more than this many seeded files (default 200)")
    ap.add_argument("--keep-min", type=int, default=10, help="never delete below this many seeded files (default 10)")
    ap.add_argument("--max-kb", type=int, default=1900, help=f"largest file in KB (default 1900, at most {MAX_KB})")
    ap.add_argument("--seed", type=int, default=None, help="repeat an exact day (plan and contents)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit, touch nothing")
    ap.add_argument("--log", type=Path, default=None, help="append the result line to this file")
    args = ap.parse_args(argv)

    if min(args.add, args.replace, args.delete) < 0:
        ap.error("counts must not be negative")
    if not MIN_KB <= args.max_kb <= MAX_KB:
        ap.error(f"--max-kb must be 1..{MAX_KB}")
    if args.keep_min < 1 or args.max_total < args.keep_min:
        ap.error("--max-total must be >= --keep-min >= 1")
    if not args.dest.is_dir():
        ap.error(f"{args.dest} is not an existing folder")

    r = daily(args.dest, add_max=args.add, replace_max=args.replace, delete_max=args.delete,
              cap=args.max_total, keep_min=args.keep_min, max_kb=args.max_kb,
              seed_value=args.seed, dry_run=args.dry_run)
    line = _line(r, datetime.now())
    print(line)
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
