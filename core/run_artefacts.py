"""Per-run artefact store — full tool logs kept for debugging (plan §5.2, WP2).

Layout::

    <run_logs_dir>/<YYYY-MM-DD>/<YYYY-MM-DD>_<HHmm>_<leg>[_<FYnn-nn>]_<short id>/
        manifest.json                          every artefact + tool, argv, exit, timing
        <YYYY-MM-DD>_<HHmm>_<leg>_rclone-sync.log     full rclone JSON log (--log-file)
        <YYYY-MM-DD>_<HHmm>_<leg>_robocopy.log        full robocopy /UNILOG (UTF-16)
        ...

(Plan F: a log file keeps its meaning when it is mailed or copied out of its folder. The DB run
id is unchanged; retention still removes whole day folders. A tool call outside a run keeps the
plain run-id folder and numbered files.)

Nothing here decides backup outcomes; it only preserves evidence. Writing
artefacts must never break a backup: failures are logged and the run is
marked `degraded` in the manifest instead of raising.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from loguru import logger

from core.time_utils import IST, now_iso

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_DOT_RUNS = re.compile(r"\.{2,}")
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)
_RUN_DIR = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{4}_[a-z-]+(?:_FY\d{2}-\d{2})?)_[A-Za-z0-9]+$")
_LEGS = ("cloud", "lan", "audit", "rollover")
# New ids carry the FY (`<uuid>-cloud-FY27-28`, audit `<uuid>-cloud-FY27-28-full`); old ones do not.
_FY_PART = r"(?:-(FY\d{2}-\d{2}))?"
_RUN_ID_TAIL = re.compile(rf"-(cloud|lan|rollover|audit){_FY_PART}$", re.IGNORECASE)
_AUDIT_ID_TAIL = re.compile(rf"-(cloud|lan){_FY_PART}-(full|quick)$", re.IGNORECASE)
MANIFEST_NAME = "manifest.json"


def _safe(name: str) -> str:
    cleaned = _DOT_RUNS.sub("_", _SAFE_NAME.sub("_", str(name))).strip("._")
    return cleaned[:120] or "unnamed"


def _leg_of(run_id: str) -> str | None:
    """cloud | lan | rollover from a run id `<uuid>-<mode>[-<fy>]`; audit-cloud | audit-lan from an
    audit id `<uuid>-<mode>[-<fy>]-<scope>`; None for anything else (plain folder)."""
    run_id = str(run_id)
    m = _AUDIT_ID_TAIL.search(run_id)
    if m:
        return f"audit-{m.group(1).lower()}"
    m = _RUN_ID_TAIL.search(run_id)
    return m.group(1).lower() if m else None


def _fy_of(run_id: str) -> str | None:
    """The canonical FYnn-nn carried by a new-form run or audit id, upper-cased; None when absent."""
    run_id = str(run_id)
    m = _AUDIT_ID_TAIL.search(run_id) or _RUN_ID_TAIL.search(run_id)
    if not m:
        return None
    fy = m.group(2)
    return fy.upper() if fy else None


class RunArtefacts:
    """Folder holding every artefact of one run (or one ad-hoc tool call)."""

    def __init__(self, root: str | os.PathLike, run_id: str, *, day: str | None = None,
                 started=None):
        import pendulum

        started = started or pendulum.now(IST)
        day = day or started.format("YYYY-MM-DD")
        leg = _leg_of(str(run_id))
        self.root = Path(root)
        self.run_id = run_id
        if leg and day != "adhoc":
            fy = _fy_of(str(run_id))
            self._prefix = (f"{started.format('YYYY-MM-DD_HHmm')}_{leg}"
                            + (f"_{fy}" if fy else ""))
            self.dir = self.root / day / f"{self._prefix}_{_safe(str(run_id)[:8])}"
        else:
            self._prefix = None
            self.dir = self.root / day / _safe(run_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._manifest: dict = {
            "run_id": run_id,
            "created_at": now_iso(),
            "degraded": False,
            "notes": [],
            "artefacts": [],
        }
        self._write_manifest()

    @classmethod
    def open(cls, directory: str | os.PathLike) -> RunArtefacts:
        """Re-open an existing run folder (e.g. inside a Prefect task that
        received only the folder path). Sequence numbering continues after
        the highest existing artefact."""
        d = Path(directory)
        obj = cls.__new__(cls)
        obj.root = d.parent.parent
        obj.dir = d
        obj.run_id = d.name
        obj._lock = threading.Lock()
        d.mkdir(parents=True, exist_ok=True)
        try:
            obj._manifest = json.loads((d / MANIFEST_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            obj._manifest = {"run_id": d.name, "created_at": now_iso(),
                             "degraded": False, "notes": [], "artefacts": []}
        match = _RUN_DIR.match(d.name)
        obj._prefix = match.group(1) if match else None
        seqs = [int(p.name[:2]) for p in d.iterdir() if p.name[:2].isdigit()]
        obj._seq = max(seqs, default=0)
        return obj

    @classmethod
    def for_config(cls, config, run_id: str, *, started=None) -> RunArtefacts:
        artefacts = cls(config.run_logs_dir, run_id, started=started)
        artefacts.check_free_space(config.run_logs.min_free_gb)
        return artefacts

    @classmethod
    def ephemeral(cls, label: str = "adhoc") -> RunArtefacts:
        """Throwaway store for tool calls outside a run (caller deletes)."""
        return cls(tempfile.mkdtemp(prefix="aam_tool_"), label, day="adhoc")

    # ── paths ─────────────────────────────────────────────────────────
    def new_path(self, step: str, ext: str = "log") -> Path:
        """Reserve a unique artefact path, e.g. 2026-10-18_2100_lan_rclone-sync.log (a second
        call for the same step gets -2, -3 ...; outside a run: 03_rclone-sync.log)."""
        ext = ext.lstrip(".")
        with self._lock:
            if self._prefix is None:
                self._seq += 1
                return self.dir / f"{self._seq:02d}_{_safe(step)}.{ext}"
            taken = {p.name for p in self.dir.iterdir()} | getattr(self, "_reserved", set())
            n = 1
            while True:
                tail = _safe(step) if n == 1 else f"{_safe(step)}-{n}"
                name = f"{self._prefix}_{tail}.{ext}"
                if name not in taken:
                    break
                n += 1
            self._reserved = getattr(self, "_reserved", set()) | {name}
            return self.dir / name

    # ── manifest ──────────────────────────────────────────────────────
    def record(self, entry: dict) -> None:
        self._update(lambda m: m["artefacts"].append(entry))

    def note(self, text: str, *, degraded: bool = False) -> None:
        def _apply(m: dict) -> None:
            m["notes"].append({"at": now_iso(), "text": text})
            if degraded:
                m["degraded"] = True
        self._update(_apply)

    @property
    def degraded(self) -> bool:
        return bool(self._load_manifest().get("degraded"))

    def _load_manifest(self) -> dict:
        """The on-disk manifest is the source of truth: several objects (the
        pipeline's and each task's re-opened one) write to the same folder."""
        try:
            return json.loads((self.dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._manifest

    def _update(self, apply) -> None:
        with self._lock:
            manifest = self._load_manifest()
            for key, default in (("artefacts", []), ("notes", []), ("degraded", False)):
                manifest.setdefault(key, default)
            apply(manifest)
            self._manifest = manifest
            self._write_manifest()

    def _write_manifest(self) -> None:
        target = self.dir / MANIFEST_NAME
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".manifest.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, indent=2, default=str)
            os.replace(tmp, target)
        except OSError as e:
            logger.warning(f"Run artefact manifest write failed ({target}): {e}")

    # ── health / finalisation ─────────────────────────────────────────
    def check_free_space(self, min_free_gb: float) -> bool:
        try:
            free_gb = shutil.disk_usage(self.dir).free / 1024**3
        except OSError as e:
            self.note(f"free-space check failed: {e}", degraded=True)
            return False
        if free_gb < min_free_gb:
            msg = (f"run-log volume low on space: {free_gb:.1f} GB free "
                   f"(minimum {min_free_gb} GB) - full logs may be incomplete")
            logger.warning(msg)
            self.note(msg, degraded=True)
            return False
        return True

    def finalize(self, compress_after_kb: int) -> None:
        """gzip artefacts above the threshold; never raises."""
        threshold = max(int(compress_after_kb), 0) * 1024
        for path in sorted(self.dir.iterdir()):
            if path.name == MANIFEST_NAME or path.suffix == ".gz" or path.name.startswith("."):
                continue
            try:
                if path.is_file() and path.stat().st_size > threshold:
                    gz = path.with_name(path.name + ".gz")
                    with open(path, "rb") as src, gzip.open(gz, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    path.unlink()
            except OSError as e:
                self.note(f"compress failed for {path.name}: {e}", degraded=True)
        self.note("finalized")


def read_artefact_text(path: str | os.PathLike, encoding: str = "utf-8") -> str:
    """Read an artefact (plain or .gz) as text, replacing undecodable bytes."""
    p = Path(path)
    if not p.exists() and Path(str(p) + ".gz").exists():
        p = Path(str(p) + ".gz")
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rb") as f:
        data = f.read()
    return data.decode(encoding, errors="replace")


def purge_older_than(
    root: str | os.PathLike,
    days: int,
    *,
    glob: str = "*",
    now: float | None = None,
    files: bool = False,
) -> int:
    """Delete entries under `root` matching `glob` older than `days`.

    One retention function for every kind of retained log (plan §5.2):
    run-artefact day folders, NSSM-rotated service logs, app logs.
    Age is the entry's mtime. Date-named folders (YYYY-MM-DD) are removed
    recursively. Plain files are removed only when `files=True` (service
    logs); by default only day folders are, so a misset directory cannot lose
    unrelated files. Retention below 1 day (or not a number) is refused and
    keeps everything. Never raises; returns the number of entries removed.
    """
    try:
        days_i = int(days)
    except (TypeError, ValueError, OverflowError):
        logger.warning(f"Retention purge refused: invalid retention {days!r}")
        return 0
    if days_i < 1:
        logger.warning(f"Retention purge refused: retention {days_i}d is below 1 day")
        return 0
    base = Path(root)
    if not base.is_dir():
        return 0
    try:
        cutoff = (now if now is not None else time.time()) - days_i * 86400
    except OverflowError:
        logger.warning(f"Retention purge refused: retention {days_i}d is absurd")
        return 0
    removed = 0
    for entry in base.glob(glob):
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                if not _DATE_DIR.match(entry.name):
                    continue  # only ever remove our own day folders
                shutil.rmtree(entry)
            else:
                if not files:
                    continue  # plain files only when the caller opts in
                entry.unlink()
            removed += 1
        except OSError as e:
            logger.warning(f"Retention purge could not remove {entry}: {e}")
    if removed:
        logger.info(f"Retention purge removed {removed} entr(y/ies) older than {days}d from {base}")
    return removed
