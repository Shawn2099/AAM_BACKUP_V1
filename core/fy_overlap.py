"""FY overlap helpers (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, section 2.1).

While two financial years are active (the configured year plus the next one, from the activation day until the
April switch) every backup pass runs the unchanged pipeline once per year. This module holds the pure helpers
the passes and the rollover share: year arithmetic, the per-pass config, ids with an FY suffix, row FY lookup and
the NAS folder rule. Nothing here reads or writes the database.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from core.fy_rollover import _child_path, _fy_name, _parent_path
from core.guards import MOUNT_MARKER

# ensure_nas_folder() outcomes
NAS_CREATED = "created"            # folder missing: made, marker written
NAS_READY = "ready"                # folder and marker already there
NAS_MARKED = "marked"              # folder existed, empty: marker written
NAS_NEEDS_APPROVAL = "needs_approval"   # folder has files but no marker: left alone (guard G5 asks)

# Indian Standard Time has no DST; a fixed offset is exact.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

_FY_RE = re.compile(r"^FY(\d{2})-(\d{2})$", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"-(FY\d{2}-\d{2})$")


def _start_year(fy: str) -> int:
    match = _FY_RE.match(fy or "")
    if not match:
        raise ValueError(f"Not a financial year name: {fy!r} (expected a form like FY26-27)")
    return 2000 + int(match.group(1))


def next_fy(fy: str) -> str:
    """FY26-27 -> FY27-28; FY99-00 -> FY00-01."""
    start = _start_year(fy) + 1
    return f"FY{start % 100:02d}-{(start + 1) % 100:02d}"


def activation_date(configured_fy: str, activate_on: str) -> dt.date:
    """The calendar day the next FY is activated, for the configured FY and an MM-DD day.

    A month from April on falls in the year the configured FY starts (31 Dec 2026 for FY26-27); January to
    March falls in the year after that (15 Jan 2027).
    """
    month, day = (int(part) for part in activate_on.split("-"))
    start = _start_year(configured_fy)
    year = start if month >= 4 else start + 1
    return dt.date(year, month, day)


def closing_moment(from_fy: str) -> dt.datetime:
    """1 April 00:00 IST that closes `from_fy` (2027-04-01 for FY26-27)."""
    return dt.datetime(_start_year(from_fy) + 1, 4, 1, 0, 0, tzinfo=IST)


def fy_of_id(ident: str | None) -> str | None:
    """The FY of a run / audit id that carries a `-FYxx-yy` suffix, else None."""
    if not ident:
        return None
    match = _SUFFIX_RE.search(str(ident))
    return match.group(1) if match else None


def pass_run_id(base: str, fy: str) -> str:
    """Run id of a next-FY pass: `<flow-run>-cloud` -> `<flow-run>-cloud-FY27-28`."""
    return f"{base}-{fy}"


def artifact_key(base: str, fy: str) -> str:
    """Prefect artifact key of a pass (keys are lowercase)."""
    return f"{base}-{fy.lower()}"


def config_for_fy(config, fy: str):
    """A copy of `config` whose source and NAS folders are the `fy` folders (plan D-3).

    `E:\\FY26-27` -> `E:\\FY27-28` and `\\\\nas\\backup\\FY26-27` -> `\\\\nas\\backup\\FY27-28`, the same rule
    `update_config_yaml` uses. The NAS path moves only when its leaf is a year folder (a cloud-only placeholder
    stays). Every other section is shared, unchanged.
    """
    source = config.paths.source_drive
    if _fy_name(source) is None:
        raise ValueError(f"The source folder {source!r} does not end in a financial-year folder; "
                         "an overlap pass cannot be built for it.")
    new_source = _child_path(_parent_path(source), fy)
    new_lan = config.paths.lan_destination
    if _fy_name(new_lan) is not None:
        new_lan = _child_path(_parent_path(new_lan), fy)
    paths = config.paths.model_copy(update={"source_drive": new_source, "lan_destination": new_lan})
    return config.model_copy(update={"paths": paths})


def _metrics(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def row_fy(row: dict) -> str | None:
    """The FY a run_history / audit row belongs to.

    Order: the id suffix, then `extended_metrics.fy`, then `extended_metrics.guards.fy`. Baseline rows have
    none of these and return None (the configured year).
    """
    fy = fy_of_id(row.get("run_id")) or fy_of_id(row.get("audit_id"))
    if fy:
        return fy
    metrics = _metrics(row.get("extended_metrics"))
    if metrics.get("fy"):
        return str(metrics["fy"])
    guards = metrics.get("guards")
    if isinstance(guards, dict) and guards.get("fy"):
        return str(guards["fy"])
    return None


def _is_dir(path: str) -> bool:
    return Path(path).is_dir()


def ensure_nas_folder(lan_root: str, fy: str) -> str:
    """Make `<lan_root>\\<fy>` ready for a backup pass, without ever inventing a share (plan section 3).

    `lan_root` is the share's parent folder (`\\\\nas\\backup`). An unreachable parent raises OSError (the pass
    fails with AAM-LAN-01); the folder is never created outside an existing share. Returns one of NAS_*.
    """
    if not _is_dir(lan_root):
        raise OSError(f"NAS share {lan_root} is not reachable - the folder {fy} was not made")
    folder = Path(lan_root) / fy
    marker = folder / MOUNT_MARKER
    if not folder.exists():
        folder.mkdir()
        marker.touch()
        return NAS_CREATED
    if marker.is_file():
        return NAS_READY
    if not any(folder.iterdir()):
        marker.touch()
        return NAS_MARKED
    return NAS_NEEDS_APPROVAL
