"""The one place that rewrites config.yaml (FY rollover + dashboard settings).

Every writer (core.fy_rollover WP11, core.config_edit DEC-9 settings and password
change) goes through `config_transaction`, so all get the same guarantees:
  * one writer at a time across processes (plan K: a lock file beside config.yaml);
  * a timestamped byte-identical backup next to the file first;
  * ruamel.yaml round-trip, so the operator's comments and layout survive;
  * an atomic replace (temp file in the same folder + os.replace) - a crash
    or power cut leaves either the old or the new file, never half of one;
    on Windows the replace is retried while another process has the file open;
  * the written file is loaded exactly like service start; if it does not
    load, the backup is restored.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from loguru import logger

from core.time_utils import now_iso


def backup_config_file(config_path: str) -> Path:
    """Timestamped byte-identical copy of config.yaml next to it."""
    src = Path(config_path)
    stamp = now_iso().replace(":", "").replace("+", "_")[:24]
    dst = src.with_name(f"{src.name}.bak-{stamp}")
    shutil.copy2(src, dst)
    logger.info(f"config.yaml backed up to {dst}")
    return dst


def prune_backups(config_path: str, keep: int) -> int:
    """Delete all but the newest `keep` config.yaml.bak-* copies; returns how many."""
    src = Path(config_path)
    backups = sorted(src.parent.glob(f"{src.name}.bak-*"), key=lambda p: p.stat().st_mtime,
                     reverse=True)
    removed = 0
    for old in backups[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError as e:
            logger.warning(f"Could not delete old config backup {old}: {e}")
    return removed


def load_round_trip(config_path: str):
    """(yaml handler, document) - the document keeps comments and quoting."""
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096   # never wrap a long value (hash, UNC path) onto the next line
    with open(config_path, encoding="utf-8") as f:
        return yaml, yaml.load(f)


def set_dotted(doc, dotted: str, value) -> None:
    """Set `section.key` (or a top-level `key`) in a round-trip document."""
    *parents, key = dotted.split(".")
    node = doc
    for part in parents:
        if part not in node or node[part] is None:
            node[part] = {}
        node = node[part]
    node[key] = value


def write_atomic(config_path: str, yaml, doc, *, tmp_prefix: str,
                 replace_retries: int = 0, replace_delay_ms: int = 50) -> None:
    """Dump `doc` to a temp file beside config.yaml, then os.replace it in.

    os.replace fails with PermissionError on Windows while another process has
    config.yaml open (Python opens files without FILE_SHARE_DELETE): it is
    retried `replace_retries` extra times, `replace_delay_ms` apart.
    """
    path = Path(config_path)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".yaml", prefix=tmp_prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(doc, f)
        attempt = 0
        while True:
            try:
                os.replace(tmp_path, str(path))
                return
            except PermissionError:
                if attempt >= replace_retries:
                    raise
                attempt += 1
                time.sleep(replace_delay_ms / 1000)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


class ConfigWriteError(RuntimeError):
    """config.yaml could not be changed; it is left as it was."""


@dataclass
class ConfigTransaction:
    """What a `config_transaction` body gets; `backup` / `saved` are set on success."""
    yaml: Any
    doc: Any
    backup: Path | None = None
    saved: Any = field(default=None)


def _maintenance_options(config_path: str) -> dict:
    """Lock / replace settings from the file's own `maintenance:` section (model
    defaults when absent). Read before the lock; the file may not load yet."""
    from models.config import MaintenanceConfig, model_default

    try:
        _, doc = load_round_trip(config_path)
        section = (doc or {}).get("maintenance") or {}
    except Exception:  # noqa: BLE001 - an unreadable file is reported by the transaction itself
        section = {}
    return {name: section.get(name, model_default(MaintenanceConfig, name))
            for name in ("config_lock_timeout_seconds", "config_replace_retries",
                         "config_replace_delay_ms")}


@contextlib.contextmanager
def config_transaction(config_path: str, *, tmp_prefix: str):
    """The only way to change config.yaml.

        with config_transaction(path, tmp_prefix=".x_") as txn:
            set_dotted(txn.doc, "section.key", value)   # or raise to abort: nothing is written
        txn.backup, txn.saved                           # backup file, the re-loaded config

    Compare-and-set callers check the current value inside the body (they hold
    the lock). Raises ConfigWriteError when the lock is not obtained in time, the
    file cannot be replaced, or the result does not load (previous file restored).
    """
    from models.config import load_config

    opts = _maintenance_options(config_path)
    lock = FileLock(f"{config_path}.lock", timeout=opts["config_lock_timeout_seconds"])
    try:
        lock.acquire()
    except Timeout as e:
        raise ConfigWriteError(
            f"config.yaml is in use by another change (waited "
            f"{opts['config_lock_timeout_seconds']} s). Nothing was written; try again.") from e
    try:
        yaml, doc = load_round_trip(config_path)
        txn = ConfigTransaction(yaml=yaml, doc=doc)
        yield txn
        txn.backup = backup_config_file(config_path)
        try:
            write_atomic(config_path, yaml, doc, tmp_prefix=tmp_prefix,
                         replace_retries=opts["config_replace_retries"],
                         replace_delay_ms=opts["config_replace_delay_ms"])
        except OSError as e:
            raise ConfigWriteError(
                f"The new config.yaml could not be replaced ({e}); the previous file is "
                f"unchanged (backup {txn.backup.name}).") from e
        try:
            txn.saved = load_config(config_path)
        except Exception as e:  # noqa: BLE001 - must never leave an unloadable file
            shutil.copy2(txn.backup, config_path)
            raise ConfigWriteError(
                f"The written file did not load ({e}); the previous config.yaml was "
                f"restored from {txn.backup.name}.") from e
    finally:
        lock.release()
