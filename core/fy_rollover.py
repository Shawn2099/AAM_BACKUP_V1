"""Fiscal year rollover building blocks (plan WP11, owner DEC-4).

The orchestration lives in flow.rollover_check_flow (daily schedule): the
final backup of the closing FY runs through the NORMAL pipelines (backup
slot, guards, permission checks, run rows, alerts); then, inside the backup
slot, the new FY folders are created, config.yaml is backed up and updated
atomically, and the closing FY is moved to the ARCHIVE storage class.

This module holds the pieces: FY parsing, the forward-only classifier,
folder creation (+ placeholder), config backup/update, archive transition.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from loguru import logger

from core.config_file import (  # noqa: F401 - backup_config_file is part of this module's API
    backup_config_file,
    config_transaction,
    load_round_trip,
    write_atomic,
)
from core.guards import MOUNT_MARKER
from core.run_artefacts import RunArtefacts
from core.time_utils import get_fy_prefix
from core.tool_runner import run_tool
from models.config import HealthConfig, model_default

FY_PATTERN = re.compile(r"^FY\d{2}-\d{2}$", re.IGNORECASE)

# classify_rollover() verdicts
NONE = "NONE"                  # configured FY is today's FY
FORWARD = "FORWARD"            # exactly the next FY -> automatic (DEC-4)
JUMP = "JUMP"                  # more than one FY ahead -> human approval
CLOCK_BEHIND = "CLOCK_BEHIND"  # today's FY is EARLIER -> never roll back (probe P4)

_PLACEHOLDER_TEXT = (
    "This folder holds the {fy} accounting files that are backed up every night.\n"
    "This note was created automatically at the start of {fy} so the backup can tell\n"
    "a new, empty financial year from a missing or unmounted drive. It is safe to keep.\n"
)

# Project root (two levels up from this file: core/ → project root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_gcloud() -> str | None:
    """Resolve the gcloud executable using multi-path fallback.

    Resolution order (most specific → most generic):
    1. deploy/bin/gcloud.cmd or gcloud.exe  — bundled/pinned version
    2. Known Windows all-users SDK install   — GoogleCloudSDKInstaller /allusers
    3. Known Windows per-user SDK install    — GoogleCloudSDKInstaller default
    4. System PATH                           — shutil.which fallback

    Returns the first path that exists, or None if gcloud is not found anywhere.
    """
    candidates: list[Path] = [
        # 1. deploy/bin (checked first — bundled copy takes priority)
        _PROJECT_ROOT / "deploy" / "bin" / "google-cloud-sdk" / "bin" / "gcloud.cmd",
        _PROJECT_ROOT / "deploy" / "bin" / "gcloud.cmd",
        _PROJECT_ROOT / "deploy" / "bin" / "gcloud.exe",

        # 2. All-users install (GoogleCloudSDKInstaller.exe /S /allusers)
        Path(r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"),
        Path(r"C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"),

        # 3. Per-user install (default GoogleCloudSDKInstaller.exe /S)
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd",
        Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd",
    ]

    for path in candidates:
        if path.exists() and path.is_file():
            return str(path)

    # 4. Final fallback — system PATH (handles Linux/WSL and custom installs)
    return shutil.which("gcloud")


class RolloverError(Exception):
    """FY rollover could not complete — config unchanged, retry next run."""


def _fy_name(path_str: str) -> str | None:
    """Extract FY suffix from path. E.g. 'E:\\SOURCE\\FY26-27' → 'FY26-27'.

    Works cross-platform with both forward and backslash separators."""
    parts = path_str.replace("\\", "/").rstrip("/").split("/")
    name = parts[-1]
    return name.upper() if FY_PATTERN.match(name) else None


def _parent_path(path_str: str) -> str:
    """Get parent directory preserving separator style."""
    from pathlib import PurePosixPath, PureWindowsPath
    if path_str.startswith("\\\\") or "\\" in path_str:
        parent = str(PureWindowsPath(path_str).parent).rstrip("\\")
        # a drive root keeps its backslash ("D:\"), otherwise _child_path would join with "/" ("D:/FY26-27")
        return parent + "\\" if re.fullmatch(r"[A-Za-z]:", parent) else parent
    else:
        p = PurePosixPath(path_str)
        return str(p.parent).rstrip("/")


def _child_path(root: str, fy: str) -> str:
    """Append FY folder to root using same separator style."""
    sep = "\\" if "\\" in root else "/"
    return f"{root.rstrip(sep)}{sep}{fy}"


def _fy_start(fy: str) -> int:
    return int(fy[2:4])


def classify_rollover(configured_fy: str, computed_fy: str) -> str:
    """Forward-only rollover decision (plan WP11, G6).

    2-digit FY start years are compared modulo 100, so FY99-00 -> FY00-01 is
    FORWARD. A difference in the "upper half" means the clock is behind the
    configured FY - that is never a rollover (the old `!=` test rolled
    backwards, probe P4).
    """
    diff = (_fy_start(computed_fy) - _fy_start(configured_fy)) % 100
    if diff == 0:
        return NONE
    if diff == 1:
        return FORWARD
    return JUMP if diff < 50 else CLOCK_BEHIND


def reentered_message(from_fy: str, to_fy: str) -> str:
    """Why a rollover whose journal is already finished is not run again (plan FY-2)."""
    return (f"The switch from {from_fy} to {to_fy} was already finished, but the settings now point at "
            f"{from_fy} again (they were put back by hand or from an old copy). Nothing was backed up, "
            f"switched or archived. Put source_drive and lan_destination back to {to_fy}. Starting that "
            "switch again is a support action - ask your support contact.")


def manual_run_state(configured: str | None, current: str, finished_journal: bool = False) -> dict:
    """What the dashboard's "Run FY rollover now" button may do (owner 2026-10-15).

    The button never bypasses a rule: it starts the same rollover check the
    06:00 schedule runs. It is offered only when a rollover is due; a blocked
    rollover is forced only through its approval (typed phrase); a clock that
    is behind is never forceable - that would roll the backups backwards.
    """
    if configured is None:
        return {"kind": None, "can_run": False,
                "message": "The source / NAS folders do not end in an FY folder (e.g. FY26-27), "
                           "so there is no automatic FY rollover."}
    kind = classify_rollover(configured, current)
    if finished_journal and kind in (FORWARD, JUMP):
        return {"kind": kind, "can_run": False, "message": reentered_message(configured, current)}
    messages = {
        NONE: f"Not due: the backups already use {configured}, which is the current FY.",
        FORWARD: f"Due: the backups still use {configured}, but the current FY is {current}. "
                 "Running it does the final backup of the old FY, then switches to the new one.",
        JUMP: f"Due, but unusual: {configured} to {current} skips at least one FY. Running it "
              "will stop and ask for your approval on the dashboard.",
        CLOCK_BEHIND: f"Refused: the server clock says {current}, which is EARLIER than {configured}. "
                      "Correct the server date/time - a rollover never goes backwards.",
    }
    return {"kind": kind, "can_run": kind in (FORWARD, JUMP), "message": messages[kind]}


def effective_fy_prefix(source_drive: str) -> str:
    """The FY the backups use: the source folder's FY leaf when it is one,
    else the calendar FY. Shared by the cloud prefix and the dashboard so the
    page never shows a different FY than the backups use (plan WP9)."""
    return _fy_name(source_drive) or get_fy_prefix()


def configured_fy(source_drive: str, lan_destination: str) -> str | None:
    """FY the backups are configured for: the SOURCE folder's FY leaf, or None (plan FY-9).

    The NAS path never decides: with the NAS leg on, the config validator forces it to match the source;
    with the NAS off (cloud-only) it is an unused placeholder, and reading its leaf would make the rollover
    rewrite a source folder that has no FY folder. `lan_destination` is kept for the callers' signature."""
    return _fy_name(source_drive)


def detect_rollover(source_drive: str, lan_destination: str) -> bool:
    """Return True if the configured FY suffix doesn't match the computed FY prefix."""
    current_fy = configured_fy(source_drive, lan_destination)
    if current_fy is None:
        # G2: explicit opt-out signal. No FY suffix in either path means the
        # operator is not using FY folders, so rollover is intentionally a
        # permanent no-op for this config. (The 4-digit FY trap that used to
        # end up here is now refused at config load — see models/config.py.)
        logger.warning(
            "FY rollover: no FYxx-xx folder name in source_drive or "
            "lan_destination - rollover is disabled by configuration for this "
            "deployment (not using FY folders is a valid choice; data simply "
            f"accumulates in the same folders). source_drive={source_drive!r}, "
            f"lan_destination={lan_destination!r}"
        )
        return False
    computed = get_fy_prefix()
    return current_fy != computed


def new_fy_file_count(source_drive: str, to_fy: str, placeholder_name: str | None) -> int | None:
    """Real files in the new FY folder next to the configured source, or None when it does not exist.

    The placeholder and the entries G1 never counts (desktop.ini, Thumbs.db, ...) are not files of the
    office. Never follows links (robocopy /XJ would not either). Plan FY-6."""
    from core.guards import SOURCE_IGNORE_NAMES

    root = _child_path(_parent_path(source_drive), to_fy)
    if not os.path.isdir(root):
        return None
    ignore = {n.lower() for n in SOURCE_IGNORE_NAMES}
    if placeholder_name:
        ignore.add(placeholder_name.lower())
    count = 0
    for _, _, files in os.walk(root, followlinks=False):
        count += sum(1 for f in files if f.lower() not in ignore)
    return count


def unprotected_new_fy(config, db) -> dict | None:
    """{from_fy, to_fy, files} when files were saved in the NEXT FY's folder before the switch, else None.

    Read-only and clock-light: it needs no proof of the clock because it changes nothing. Only the normal
    case (exactly the next FY) counts; a clock that is behind, a jump of more than one FY and a folder that
    only holds the placeholder do not. Ends by itself when the config points at the new FY. Plan FY-6."""
    source, lan = config.paths.source_drive, config.paths.lan_destination
    configured = configured_fy(source, lan)
    if configured is None:
        return None
    journal = db.rollover_incomplete()
    if journal is not None and journal["from_fy"] == configured:
        to_fy = journal["to_fy"]
    else:
        current = get_fy_prefix()
        if classify_rollover(configured, current) != FORWARD:
            return None
        to_fy = current
    files = new_fy_file_count(source, to_fy, config.rollover.source_placeholder_name)
    if not files:
        return None
    return {"from_fy": configured, "to_fy": to_fy, "files": files}


def unprotected_message(found: dict) -> str:
    return (f"{found['files']} file(s) were saved in the {found['to_fy']} folder, but the backups still "
            f"use {found['from_fy']} until the yearly switch is done, so those files are NOT backed up. "
            f"Do not move files out of {found['from_fy']} meanwhile: the next backup would remove them "
            "from the backup copies too.")


def create_new_fy_folders(source_root: str, lan_root: str, new_fy: str,
                          placeholder_name: str | None = None) -> dict[str, Path]:
    """Create new FY folders on source and LAN. Returns dict of created Paths.

    placeholder_name (rollover.source_placeholder_name): a short note written
    into the NEW source folder - never overwritten - so the first nights of
    the new FY back up a real file instead of being blocked by guard G1.

    Source folder creation is mandatory — raises on failure (local disk problem).
    LAN folder creation is best-effort — logs a clear error if NAS is offline
    at rollover time, but does NOT block the rollover. The operator must create
    the LAN folder manually before the next LAN backup runs.
    """
    created = {}
    new_source = Path(_child_path(source_root, new_fy))
    new_lan = Path(_child_path(lan_root, new_fy))

    # Source folder: local disk — must succeed
    new_source.mkdir(parents=True, exist_ok=True)
    created["source"] = new_source
    logger.info(f"FY rollover: created source folder {new_source}")
    if placeholder_name:
        placeholder = new_source / placeholder_name
        try:
            with open(placeholder, "x", encoding="utf-8") as f:
                f.write(_PLACEHOLDER_TEXT.format(fy=new_fy))
            logger.info(f"FY rollover: wrote placeholder {placeholder}")
        except FileExistsError:
            logger.info(f"FY rollover: placeholder {placeholder} already exists - kept")
        created["placeholder"] = placeholder

    # LAN folder: network path — may fail if NAS is offline at rollover time
    try:
        new_lan.mkdir(parents=True, exist_ok=True)
        (new_lan / ".AAM_TARGET_MOUNTED").touch(exist_ok=True)
        created["lan"] = new_lan
        logger.info(f"FY rollover: created LAN folder {new_lan}")
    except OSError as e:
        logger.error(
            f"FY rollover: FAILED to create LAN folder {new_lan}: {e}. "
            f"The NAS may have been offline at rollover time. "
            f"ACTION REQUIRED: Manually create '{new_lan}' on the NAS "
            f"before the next LAN backup or it will fail with 'destination not found'."
        )
        # Non-blocking — config.yaml still gets updated to new FY.
        # The LAN backup will fail gracefully at the preflight stage until the
        # folder is created manually.

    return created


def create_source_folder(source_root: str, new_fy: str,
                         placeholder_name: str | None = None) -> Path:
    """Step `source_folder`: the new source FY folder (+ placeholder). Idempotent: an
    existing folder is kept and a placeholder file is never overwritten.

    placeholder_name (rollover.source_placeholder_name): a short note written into the NEW
    source folder so the first nights of the new FY back up a real file instead of being
    blocked by guard G1. Raises on failure (local disk problem).
    """
    new_source = Path(_child_path(source_root, new_fy))
    new_source.mkdir(parents=True, exist_ok=True)
    logger.info(f"FY rollover: source folder {new_source} ready")
    if placeholder_name:
        placeholder = new_source / placeholder_name
        try:
            with open(placeholder, "x", encoding="utf-8") as f:
                f.write(_PLACEHOLDER_TEXT.format(fy=new_fy))
            logger.info(f"FY rollover: wrote placeholder {placeholder}")
        except FileExistsError:
            logger.info(f"FY rollover: placeholder {placeholder} already exists - kept")
    return new_source


def create_nas_folder(lan_root: str, new_fy: str) -> Path:
    """Step `nas_folder`: the new NAS FY folder + the mount marker. Idempotent.

    Raises OSError when the share root is not reachable: the folder is only ever created
    inside an existing share (never invented on a wrong or offline path, F-9), and the
    caller records the failure instead of swallowing it.
    """
    root = Path(lan_root)
    if not root.is_dir():
        raise OSError(f"NAS share {lan_root} is not reachable - the new folder was not created")
    new_lan = Path(_child_path(lan_root, new_fy))
    new_lan.mkdir(exist_ok=True)
    (new_lan / MOUNT_MARKER).touch(exist_ok=True)
    logger.info(f"FY rollover: NAS folder {new_lan} ready")
    return new_lan


def update_config_yaml(config_path: str, source_root: str, lan_root: str,
                       new_fy: str, *, expect_fy: str | None = None) -> Path | None:
    """Step `config_switch`: point source_drive and lan_destination at `new_fy`.

    Returns the config backup. With `expect_fy` this is compare-and-set, decided inside the
    config lock: config at `expect_fy` -> switch; already at `new_fy` -> nothing is written
    and None is returned (a crash after the write, R3); anything else -> RolloverError,
    nothing written.

    Uses the shared config writer (core.config_file): ruamel round-trip, so
    comments and formatting survive; atomic replace.
    """
    new_source = _child_path(source_root, new_fy)
    new_lan = _child_path(lan_root, new_fy)
    with config_transaction(config_path, tmp_prefix=".config_rollover_") as txn:
        old_source = txn.doc["paths"]["source_drive"]
        old_lan = txn.doc["paths"]["lan_destination"]
        lan_has_fy = _fy_name(str(old_lan)) is not None      # decided inside the lock, from the file itself
        if expect_fy is not None:
            current = configured_fy(str(old_source), str(old_lan))
            if current == new_fy:
                logger.info(f"FY rollover: config.yaml already points at {new_fy} - kept")
                txn.unchanged = True
                return None
            if current != expect_fy:
                raise RolloverError(
                    f"config.yaml points at {current or 'no FY folder'}, not {expect_fy} - "
                    f"the switch to {new_fy} was not made. Nothing was written.")
        txn.doc["paths"]["source_drive"] = new_source
        if lan_has_fy:      # an unused NAS placeholder without an FY folder (cloud-only) stays as it is
            txn.doc["paths"]["lan_destination"] = new_lan
    logger.info(
        f"FY rollover: config.yaml updated\n"
        f"  source:  {old_source} → {new_source}\n"
        f"  LAN:     {old_lan} → {new_lan}"
    )
    return txn.backup


def run_archive_transition(
    bucket: str,
    old_fy: str,
    gcs_key_path: str,
    auth_timeout: int = model_default(HealthConfig, "rollover_auth_timeout_seconds"),       # config.health.rollover_auth_timeout_seconds
    archive_timeout: int = model_default(HealthConfig, "rollover_archive_timeout_seconds"),   # config.health.rollover_archive_timeout_seconds
    artefacts: RunArtefacts | None = None,
    config_dir: str | None = None,
) -> bool:
    """Transition all GCS objects under old_fy/ to ARCHIVE storage class.

    ``gcloud storage objects update gs://<bucket>/<old_fy>/ --recursive
    --storage-class=ARCHIVE`` - the objects are rewritten inside Google, nothing is downloaded.
    Runs through run_tool (full output kept, process tree killed on timeout).

    The key is handed to gcloud as a file (``CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE``), never
    activated: ``activate-service-account`` would store a second copy of the key in gcloud's
    ``credentials.db``. ``config_dir`` (a folder in the runtime directory) keeps gcloud's own
    settings out of a user profile the service account may not have.

    Non-blocking by design: any failure is logged as a WARNING and returns
    False; the rollover itself is already complete and the transition can be
    re-run (it is idempotent).
    """
    logger.info(
        f"FY rollover: transitioning GCS objects under gs://{bucket}/{old_fy}/ "
        "to ARCHIVE storage class"
    )
    gcloud_exe = _resolve_gcloud()
    if not gcloud_exe:
        logger.warning(
            "FY rollover: 'gcloud' CLI not found - archive transition skipped. "
            "Searched: deploy/bin/, Program Files, LOCALAPPDATA, and system PATH. "
            "Install the Google Cloud SDK (setup_system.bat), then re-run the transition."
        )
        return False
    from core import gcs_key
    if gcs_key.is_protected(gcs_key_path):
        logger.error("FY rollover: the Google key is stored DPAPI-protected, but the archive step "
                     "still needs the key as a file for gcloud (plan S4 is not done) - archive "
                     "transition NOT run")
        return False
    artefacts = artefacts or RunArtefacts.ephemeral("fy-archive")
    # Stateless: everything gcloud needs is injected per call; nothing is installed or stored.
    env = os.environ.copy()
    env["CLOUDSDK_PYTHON"] = env.get("CLOUDSDK_PYTHON") or sys.executable   # the SDK ships without one
    env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    if config_dir:
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        env["CLOUDSDK_CONFIG"] = str(config_dir)
    if Path(gcs_key_path).is_file():
        env["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] = str(gcs_key_path)
    else:
        logger.info("FY rollover: no GCS key file found, using ambient gcloud authentication")

    # First prove gcloud starts at all (a missing Python made every transition a silent no-op).
    # --recursive on the bare prefix (no ** glob) - no shell wildcard expansion.
    steps = [("gcloud-check", [gcloud_exe, "--version"], auth_timeout),
             ("gcloud-archive", [gcloud_exe, "storage", "objects", "update",
                                 f"gs://{bucket}/{old_fy}/", "--storage-class=ARCHIVE",
                                 "--recursive"], archive_timeout)]
    for name, argv, timeout in steps:
        result = run_tool(name, argv, timeout_s=timeout, artefacts=artefacts, env=env)
        if not result.completed or result.exit_code != 0:
            detail = result.describe_failure() or (
                f"exit {result.exit_code}: {result.stderr_text().strip() or 'no output'}")
            logger.warning(f"FY rollover: {name} failed for gs://{bucket}/{old_fy}/ ({detail}); "
                           f"full output in {artefacts.dir}")
            return False
    logger.info(f"FY rollover: archive transition succeeded for gs://{bucket}/{old_fy}/")
    return True
