"""The one place that rewrites config.yaml (FY rollover + dashboard settings).

Shared by core.fy_rollover (WP11) and core.config_edit (DEC-9) so both get
the same guarantees:
  * a timestamped byte-identical backup next to the file first;
  * ruamel.yaml round-trip, so the operator's comments and layout survive;
  * an atomic replace (temp file in the same folder + os.replace) - a crash
    or power cut leaves either the old or the new file, never half of one.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

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


def write_atomic(config_path: str, yaml, doc, *, tmp_prefix: str) -> None:
    """Dump `doc` to a temp file beside config.yaml, then os.replace it in."""
    path = Path(config_path)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".yaml", prefix=tmp_prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(doc, f)
        os.replace(tmp_path, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def write_config_values(config_path: str, values: dict, *, tmp_prefix: str = ".config_edit_") -> None:
    """Atomically set several dotted keys, keeping every comment."""
    yaml, doc = load_round_trip(config_path)
    for dotted, value in values.items():
        set_dotted(doc, dotted, value)
    write_atomic(config_path, yaml, doc, tmp_prefix=tmp_prefix)
