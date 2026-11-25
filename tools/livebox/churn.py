"""Deterministic daily source-folder churn for the live-box lanes (plan section 3).

Standard library only. Every random choice comes from ``random.Random`` seeded
with ``"<seed>:<date>"`` so the same (seed, day) on the same starting tree
always yields the same operations and bytes. Every written file gets its mtime
set to 00:30 of the simulated day (before the first scheduled job). Nothing is
read or written outside the ``root`` that is passed in (``safe_path`` enforces it).

API:
    seed_tree(root, seed, scale=300, now=None) -> list[str]
    apply_day(root, day, seed, role="C", adverse=None, large_file_mb=50) -> list[Op]
    apply_guard_event(root, day, seed, kind) -> list[Op]
    safe_path(root, rel) -> Path        (raises ChurnPathError)
"""
from __future__ import annotations

import datetime as dt
import os
import random
from dataclasses import dataclass
from pathlib import Path

ROLE_CONFIGURED = "C"
ROLE_NEXT = "N"
ROLE_CLOSED = "closed"

GUARD_KINDS = ("mass_delete", "shrink", "wipe")
_GUARD_FRACTION = {"mass_delete": 0.15, "shrink": 0.25}

TYPES = ("Invoices", "Returns", "Statements", "Correspondence", "Workpapers")
_EXT_WEIGHTS = (("pdf", 55), ("txt", 15), ("xlsx", 20), ("bin", 10))
_UNICODE_TAGS = ("résumé", "Übersicht", "ക്ലയിന്റ്", "報告")
RENAME_MAX_SHARE = 0.10  # a folder rename may touch at most this share of the tree's files
SEED_NOW = dt.date(2000, 1, 1)  # default mtime day of seeded files (deterministic)

SHARED_REL = "Clients/shared.txt"
LARGE_DIR = "Large"
LARGE_PREFIX = "large_"
LATE_DIR = "Late"
_SPECIAL_TOP = {"Clients", LARGE_DIR, LATE_DIR}


class ChurnPathError(ValueError):
    """A relative path would leave the churn root."""


@dataclass(frozen=True)
class Op:
    kind: str
    path: str = ""


# --------------------------------------------------------------------------- helpers

def sim_timestamp(day: dt.date, at: dt.time = dt.time(0, 30, 0)) -> float:
    """Epoch seconds of ``at`` (local time, default 00:30 before 02:01 cloud) on the simulated ``day``."""
    return dt.datetime(day.year, day.month, day.day, at.hour, at.minute, at.second).timestamp()


def safe_path(root, rel) -> Path:
    """Join ``rel`` under ``root``; raise ChurnPathError if it escapes."""
    base = os.path.abspath(str(root))
    rel_s = str(rel)
    if os.path.isabs(rel_s) or os.path.splitdrive(rel_s)[0] or rel_s.startswith(("/", "\\")):
        raise ChurnPathError(f"absolute path not allowed: {rel_s!r}")
    full = os.path.abspath(os.path.join(base, rel_s))
    try:
        common = os.path.commonpath([base, full])
    except ValueError:
        raise ChurnPathError(f"path {rel_s!r} escapes {base}") from None
    if os.path.normcase(common) != os.path.normcase(base):
        raise ChurnPathError(f"path {rel_s!r} escapes {base}")
    if os.path.normcase(full) == os.path.normcase(base) and rel_s.strip() in ("..",):
        raise ChurnPathError("path escapes root")
    return Path(full)


def _rel(root: Path, p: Path) -> str:
    return Path(os.path.relpath(p, root)).as_posix()


def _rng(seed, day: dt.date) -> random.Random:
    return random.Random(f"{seed}:{day.isoformat()}")


def _is_link(p: Path) -> bool:
    try:
        return p.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(p))
    except OSError:
        return False


def _list_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not _is_link(Path(dirpath) / d))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if not _is_link(p):
                out.append(p)
    return out


def _regular_files(root: Path) -> list[Path]:
    """Files eligible for random modify/delete (not the special event files)."""
    out = []
    for p in _list_files(root):
        top = Path(os.path.relpath(p, root)).parts[0]
        if top in _SPECIAL_TOP:
            continue
        out.append(p)
    return out


def _type_dirs(root: Path) -> list[Path]:
    """Depth-2 folders Client_*/<Type> in sorted order."""
    out = []
    for c in sorted(root.iterdir()):
        if c.is_dir() and not _is_link(c) and c.name not in _SPECIAL_TOP:
            for t in sorted(c.iterdir()):
                if t.is_dir() and not _is_link(t):
                    out.append(t)
    return out


def _pick_ext(rng: random.Random) -> str:
    n = rng.uniform(0, sum(w for _, w in _EXT_WEIGHTS))
    for ext, w in _EXT_WEIGHTS:
        n -= w
        if n <= 0:
            return ext
    return "pdf"


def _size(rng: random.Random, ext: str) -> int:
    if ext == "txt":
        return rng.randint(200, 6_000)
    if ext == "xlsx":
        return rng.randint(8_000, 90_000)
    if ext == "bin":
        return rng.randint(1_000, 40_000)
    return rng.randint(15_000, 220_000)


def _write(root: Path, rel: str | Path, data: bytes, ts: float) -> Path:
    p = safe_path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (ts, ts))
    return p


# --------------------------------------------------------------------------- seed tree

def seed_tree(root, seed, scale: int = 300, now=None) -> list[str]:
    """Build ``scale`` files as Client_###/<Type>/file_####.<ext> with random bytes.

    A few names carry unicode or are very long (still ``file_`` prefixed). mtime
    is ``now`` (date, datetime or epoch float; default a fixed past day so the
    tree is byte- and mtime-deterministic). Returns sorted relative POSIX paths.
    """
    root_p = Path(os.path.abspath(str(root)))
    root_p.mkdir(parents=True, exist_ok=True)
    if now is None:
        ts = sim_timestamp(SEED_NOW)
    elif isinstance(now, dt.datetime):
        ts = now.timestamp()
    elif isinstance(now, dt.date):
        ts = sim_timestamp(now)
    else:
        ts = float(now)
    rng = random.Random(f"{seed}:seed")
    n_clients = max(3, scale // 12)
    rels: list[str] = []
    for i in range(scale):
        client = f"Client_{(i * 7 + rng.randrange(n_clients)) % n_clients:03d}"
        typ = TYPES[rng.randrange(len(TYPES))]
        ext = _pick_ext(rng)
        stem = f"file_{i:04d}"
        if i % 97 == 5:
            stem += "_" + _UNICODE_TAGS[(i // 97) % len(_UNICODE_TAGS)]
        elif i % 131 == 7:
            stem += "_" + "long_name_" * 14
        rel = f"{client}/{typ}/{stem}.{ext}"
        _write(root_p, rel, rng.randbytes(_size(rng, ext)), ts)
        rels.append(rel)
    return sorted(rels)


# --------------------------------------------------------------------------- daily churn

def _is_idle(day: dt.date) -> bool:
    return day.weekday() == 6 or (day.month == 8 and 1 <= day.day <= 7)


def _first_wednesday(day: dt.date) -> bool:
    return day.weekday() == 2 and day.day <= 7


def _counts(role: str):
    if role == ROLE_NEXT:
        return (2, 6), (1, 3), (0, 1)
    return (3, 8), (2, 5), (0, 2)


def _large_event(root: Path, day: dt.date, ts: float, mb: int, rng: random.Random) -> list[Op]:
    """From the 15th on, once per month: drop last month's large file, add a new one."""
    ops: list[Op] = []
    folder = safe_path(root, LARGE_DIR)
    name = f"{LARGE_PREFIX}{day.strftime('%Y%m')}.bin"
    if day.day < 15 or (folder / name).exists():
        return ops
    if folder.is_dir():
        for old in sorted(folder.iterdir()):
            if old.is_file() and old.name.startswith(LARGE_PREFIX):
                old.unlink()
                ops.append(Op("large_delete", _rel(root, old)))
    folder.mkdir(parents=True, exist_ok=True)
    p = safe_path(root, f"{LARGE_DIR}/{name}")
    chunk = rng.randbytes(1024 * 1024)
    with open(p, "wb") as fh:
        for _ in range(mb):
            fh.write(chunk)
    os.utime(p, (ts, ts))
    ops.append(Op("large_add", _rel(root, p)))
    return ops


def apply_day(root, day: dt.date, seed, role: str = ROLE_CONFIGURED, adverse: str | None = None,
              large_file_mb: int = 50) -> list[Op]:
    """Apply one simulated day of churn under ``root`` and return the ops done.

    role: "C" configured FY, "N" next FY, "closed" (closed FY: only the Apr 2
    late edit). ``adverse`` (one of GUARD_KINDS) runs that guard event instead
    of normal churn. Normal days never emit guard events.
    """
    root_p = Path(os.path.abspath(str(root)))
    if adverse is not None:
        return apply_guard_event(root_p, day, seed, adverse)
    ts = sim_timestamp(day)
    rng = _rng(seed, day)
    ops: list[Op] = []

    if role == ROLE_CLOSED:
        if (day.month, day.day) == (4, 2):
            p = _write(root_p, f"{LATE_DIR}/late_edit_{day.year}.txt", rng.randbytes(2048), ts)
            ops.append(Op("late_edit", _rel(root_p, p)))
        return ops

    # dated shared-path events (run even on idle days)
    if (day.month, day.day) == (1, 3):
        p = _write(root_p, SHARED_REL, f"shared {role} {day.isoformat()}\n".encode(), ts)
        ops.append(Op("shared_add", _rel(root_p, p)))
    elif (day.month, day.day) == (2, 15) and role == ROLE_CONFIGURED:
        p = safe_path(root_p, SHARED_REL)
        if p.exists():
            p.unlink()
            ops.append(Op("shared_delete", SHARED_REL))

    if _is_idle(day):
        return ops or [Op("idle", "")]

    add_r, mod_r, del_r = _counts(role)

    # deletes first so they cannot hit today's adds
    files = _regular_files(root_p)
    n_del = min(rng.randint(*del_r), len(files))
    for p in rng.sample(files, n_del):
        ops.append(Op("delete", _rel(root_p, p)))
        safe_path(root_p, _rel(root_p, p)).unlink()

    files = _regular_files(root_p)
    n_mod = min(rng.randint(*mod_r), len(files))
    for p in rng.sample(files, n_mod):
        old_size = p.stat().st_size
        size = old_size if rng.random() < 0.5 else _size(rng, p.suffix.lstrip(".").lower())
        _write(root_p, _rel(root_p, p), rng.randbytes(max(size, 1)), ts)
        ops.append(Op("modify", _rel(root_p, p)))

    dirs = _type_dirs(root_p) or [safe_path(root_p, f"Client_000/{TYPES[0]}")]
    tag = day.strftime("%m%d")
    for _ in range(rng.randint(*add_r)):
        ext = _pick_ext(rng)
        for _try in range(100):
            d = dirs[rng.randrange(len(dirs))]
            p = d / f"file_{tag}{rng.randint(0, 9999):04d}.{ext}"
            if not p.exists():
                break
        else:
            raise RuntimeError("no free file name")
        _write(root_p, _rel(root_p, p), rng.randbytes(_size(rng, ext)), ts)
        ops.append(Op("add", _rel(root_p, p)))

    if role == ROLE_CONFIGURED and _first_wednesday(day):
        clients = [c for c in sorted(root_p.iterdir())
                   if c.is_dir() and not _is_link(c) and c.name not in _SPECIAL_TOP]
        # A real office renames one small client folder, never most of the tree. In a tiny or
        # single-client tree (a new FY folder grown from scratch) there is no such folder, so the
        # rename is skipped (it renamed 316 of 319 files and tripped AAM-DEL-01 for a whole year).
        limit = RENAME_MAX_SHARE * len(_regular_files(root_p))
        clients = [c for c in clients if len(_regular_files(c)) <= limit]
        if clients:
            src = clients[rng.randrange(len(clients))]
            dst = safe_path(root_p, f"{src.name}_r{day.strftime('%y%m')}")
            if not dst.exists():
                src.rename(dst)
                os.utime(dst, (ts, ts))
                ops.append(Op("rename", _rel(root_p, dst)))

    ops.extend(_large_event(root_p, day, ts, large_file_mb, rng))
    return ops


# --------------------------------------------------------------------------- guard events

def apply_guard_event(root, day: dt.date, seed, kind: str) -> list[Op]:
    """Guard scenario event: mass_delete (15 %), shrink (25 %), wipe (all files).

    Only ever called explicitly by adverse lanes. Folders stay in place.
    """
    if kind not in GUARD_KINDS:
        raise ValueError(f"unknown guard event {kind!r}")
    root_p = Path(os.path.abspath(str(root)))
    files = _list_files(root_p)
    if kind == "wipe":
        victims = files
    else:
        n = min(len(files), round(len(files) * _GUARD_FRACTION[kind]))
        victims = _rng(f"{seed}:{kind}", day).sample(files, n)
    ops = []
    for p in sorted(victims):
        rel = _rel(root_p, p)
        safe_path(root_p, rel).unlink()
        ops.append(Op(kind, rel))
    return ops
