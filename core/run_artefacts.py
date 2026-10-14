"""Per-run artefact store — full tool logs kept for debugging (plan §5.2, WP2).

Layout::

    <run_logs_dir>/<YYYY-MM-DD>/<run_id>/
        manifest.json            every artefact + tool, argv, exit, timing
        01_rclone-sync.log       full rclone JSON log (--log-file)
        02_robocopy.log          full robocopy /UNILOG (UTF-16)
        ...

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
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MANIFEST_NAME = "manifest.json"


def _safe(name: str) -> str:
    cleaned = _DOT_RUNS.sub("_", _SAFE_NAME.sub("_", str(name))).strip("._")
    return cleaned[:120] or "unnamed"


class RunArtefacts:
    """Folder holding every artefact of one run (or one ad-hoc tool call)."""

    def __init__(self, root: str | os.PathLike, run_id: str, *, day: str | None = None):
        import pendulum

        day = day or pendulum.now(IST).format("YYYY-MM-DD")
        self.root = Path(root)
        self.run_id = run_id
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
    def for_config(cls, config, run_id: str) -> RunArtefacts:
        artefacts = cls(config.run_logs_dir, run_id)
        artefacts.check_free_space(config.run_logs.min_free_gb)
        return artefacts

    @classmethod
    def ephemeral(cls, label: str = "adhoc") -> RunArtefacts:
        """Throwaway store for tool calls outside a run (caller deletes)."""
        return cls(tempfile.mkdtemp(prefix="aam_tool_"), label, day="adhoc")

    # ── paths ─────────────────────────────────────────────────────────
    def new_path(self, step: str, ext: str = "log") -> Path:
        """Reserve a unique, ordered artefact path, e.g. 03_rclone-sync.log."""
        with self._lock:
            self._seq += 1
            seq = self._seq
        return self.dir / f"{seq:02d}_{_safe(step)}.{ext.lstrip('.')}"

    # ── manifest ──────────────────────────────────────────────────────
    def record(self, entry: dict) -> None:
        with self._lock:
            self._manifest["artefacts"].append(entry)
            self._write_manifest()

    def note(self, text: str, *, degraded: bool = False) -> None:
        with self._lock:
            self._manifest["notes"].append({"at": now_iso(), "text": text})
            if degraded:
                self._manifest["degraded"] = True
            self._write_manifest()

    @property
    def degraded(self) -> bool:
        return bool(self._manifest["degraded"])

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
) -> int:
    """Delete entries under `root` matching `glob` older than `days`.

    One retention function for every kind of retained log (plan §5.2):
    run-artefact day folders, NSSM-rotated service logs, app logs.
    Age is the entry's mtime. Date-named folders (YYYY-MM-DD) are removed
    recursively. Never raises; returns the number of entries removed.
    """
    base = Path(root)
    if not base.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - int(days) * 86400
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
                entry.unlink()
            removed += 1
        except OSError as e:
            logger.warning(f"Retention purge could not remove {entry}: {e}")
    if removed:
        logger.info(f"Retention purge removed {removed} entr(y/ies) older than {days}d from {base}")
    return removed
