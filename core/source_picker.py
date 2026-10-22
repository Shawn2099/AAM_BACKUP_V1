"""Advanced Settings: pick the folder that holds the financial-year folders (plan FY-8).

The owner chooses a PARENT (a drive or a folder); the FY leaf is always the configured FY. Listing and
checking only - nothing is created and nothing is saved here (the save is the normal Settings flow, which
also refuses any change of the financial year). Local, read-only, never follows links.
"""
from __future__ import annotations

import ntpath
import os
import string

from core import fy_rollover
from core.guards import SOURCE_IGNORE_NAMES, first_regular_file

# folders a person never backs up and a picker should not offer
_HIDDEN = frozenset(n.lower() for n in ("$RECYCLE.BIN", "System Volume Information", "Recovery"))


class PickerError(ValueError):
    """A plain-language reason the folder cannot be listed or used."""


def _drives() -> list[str]:
    return [f"{c}:\\" for c in string.ascii_uppercase if os.path.isdir(f"{c}:\\")]


def _local_absolute(path: str) -> str:
    """The normalised full local path (drive letter + backslash), or PickerError. A relative path, a drive-
    relative one ("D:") and a network path (two backslashes + host) are refused: the source is a local
    folder, and listing a network host would make the service connect to it."""
    path = (path or "").strip()
    if path.startswith(("\\\\", "//")):
        raise PickerError("Only local folders can be chosen here (not network paths).")
    drive, rest = ntpath.splitdrive(path)
    if not (len(drive) == 2 and drive[1] == ":" and rest[:1] in ("\\", "/")):
        raise PickerError("Type the full path including the drive, like D:\\Data.")
    return ntpath.normpath(path)


def _is_root(path: str) -> bool:
    return ntpath.splitdrive(path)[1] in ("\\", "/")


def list_folders(path: str | None) -> dict:
    """{path, parent, folders}: the sub-folders of `path` (links and system folders left out), or the
    drives when `path` is empty. `parent` is None at the top."""
    if not path:
        return {"path": "", "parent": None, "folders": _drives()}
    path = _local_absolute(path)
    if not os.path.exists(path):
        raise PickerError(f"The folder {path} does not exist (or the server cannot see it).")
    if not os.path.isdir(path):
        raise PickerError(f"{path} is not a folder.")
    try:
        with os.scandir(path) as it:
            names = sorted(
                (e.name for e in it
                 if e.is_dir(follow_symlinks=False) and e.name.lower() not in _HIDDEN
                 and not os.path.isjunction(e.path)),
                key=str.lower)
    except OSError as e:
        raise PickerError(f"The server cannot open {path}: {e.strerror or e}") from e
    parent = None if _is_root(path) else ntpath.dirname(path)     # a drive root has no parent: "up" shows the drives
    if parent and _is_root(parent) and not parent.endswith("\\"):
        parent += "\\"
    return {"path": path, "parent": parent, "folders": names}


def _is_link(path: str) -> bool:
    """A link/junction ANYWHERE in the path (not only the last part): the real path differs from the typed one.
    Compared case-insensitively - Windows returns the on-disk case and expands subst drives."""
    return (os.path.islink(path) or os.path.isjunction(path)
            or os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.abspath(path)))


def check_source_parent(config, parent: str) -> dict:
    """Can `<parent>/<configured FY>` become the source folder? {ok, problems, warnings, source, fy,
    cloud_target}. The FY folder must already exist and hold real files; nothing is created."""
    fy = fy_rollover.configured_fy(config.paths.source_drive, config.paths.lan_destination)
    out = {"ok": False, "problems": [], "warnings": [], "source": None, "fy": fy, "cloud_target": None}
    if fy is None:
        out["problems"].append("The backup folders have no financial-year folder (like FY26-27), so the "
                               "folder cannot be picked here. Ask support to change it in config.yaml.")
        return out
    parent = (parent or "").strip()
    if not parent:
        out["problems"].append("Choose a folder first.")
        return out
    try:
        parent = _local_absolute(parent)
    except PickerError as e:
        out["problems"].append(str(e))
        return out
    source = fy_rollover._child_path(parent, fy)
    out["source"] = source
    if os.path.normcase(os.path.normpath(source)) == os.path.normcase(
            os.path.normpath(config.paths.source_drive)):
        out["problems"].append(f"{source} is already the folder that is backed up.")
        return out
    if not os.path.isdir(parent):
        out["problems"].append(f"The folder {parent} does not exist (or the server cannot see it).")
        return out
    if _is_link(parent):
        out["problems"].append(f"{parent} is a link to another place. Choose the real folder.")
        return out
    if not os.path.isdir(source):
        out["problems"].append(
            f"The folder {fy} does not exist inside {parent}. Create it first - this page never creates "
            "folders, and the backup must not start from an empty place by mistake.")
        return out
    if _is_link(source):
        out["problems"].append(f"{source} is a link to another place. Choose the real folder.")
        return out
    try:
        has_files = first_regular_file(source, SOURCE_IGNORE_NAMES) is not None
    except OSError as e:
        out["problems"].append(f"The server cannot read {source}: {e.strerror or e}")
        return out
    if not has_files:
        out["problems"].append(f"{source} holds no files. The backup would stop (guard G1) rather than "
                               "mirror an empty folder.")
        return out
    out["ok"] = True
    out["warnings"].append("If this folder holds fewer files than the one backed up now, the next backup "
                           "stops and asks for your approval (guard G2): a smaller source is treated as "
                           "a possible mistake.")
    if config.cloud.enabled and config.cloud.bucket:
        out["cloud_target"] = f"gs://{config.cloud.bucket}/{fy}"
    return out
