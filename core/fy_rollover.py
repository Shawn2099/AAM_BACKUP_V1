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
from pathlib import Path

from loguru import logger

from core.config_file import (  # noqa: F401 - backup_config_file is part of this module's API
    backup_config_file,
    config_transaction,
    load_round_trip,
    write_atomic,
)
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
    if path_str.startswith("\\\\"):
        p = PureWindowsPath(path_str)
        return str(p.parent).rstrip("\\")
    elif "\\" in path_str:
        p = PureWindowsPath(path_str)
        return str(p.parent).rstrip("\\")
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


def manual_run_state(configured: str | None, current: str) -> dict:
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
    """FY the backups are configured for (source leaf first), or None."""
    return _fy_name(source_drive) or _fy_name(lan_destination)


def detect_rollover(source_drive: str, lan_destination: str) -> bool:
    """Return True if the configured FY suffix doesn't match the computed FY prefix."""
    current_fy = _fy_name(source_drive) or _fy_name(lan_destination)
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


def update_config_yaml(config_path: str, source_root: str, lan_root: str,
                       new_fy: str) -> Path:
    """Atomically update source_drive and lan_destination in config.yaml; returns the backup.

    Uses the shared config writer (core.config_file): ruamel round-trip, so
    comments and formatting survive; atomic replace.
    """
    new_source = _child_path(source_root, new_fy)
    new_lan = _child_path(lan_root, new_fy)
    with config_transaction(config_path, tmp_prefix=".config_rollover_") as txn:
        old_source = txn.doc["paths"]["source_drive"]
        old_lan = txn.doc["paths"]["lan_destination"]
        txn.doc["paths"]["source_drive"] = new_source
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
) -> bool:
    """Transition all GCS objects under old_fy/ to ARCHIVE storage class.

    ``gcloud storage objects update gs://<bucket>/<old_fy>/ --recursive
    --storage-class=ARCHIVE`` - metadata only, nothing is downloaded. Runs
    through run_tool (full output kept, process tree killed on timeout).

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
    artefacts = artefacts or RunArtefacts.ephemeral("fy-archive")
    # Stateless auth: the key path is injected per call; no persistent login.
    env = os.environ.copy()
    env["GOOGLE_APPLICATION_CREDENTIALS"] = str(gcs_key_path)

    steps = []
    if Path(gcs_key_path).is_file():
        steps.append(("gcloud-auth", [gcloud_exe, "auth", "activate-service-account",
                                      f"--key-file={gcs_key_path}"], auth_timeout))
    else:
        logger.info("FY rollover: no GCS key file found, using ambient gcloud authentication")
    # --recursive on the bare prefix (no ** glob) - no shell wildcard expansion.
    steps.append(("gcloud-archive", [gcloud_exe, "storage", "objects", "update",
                                     f"gs://{bucket}/{old_fy}/", "--storage-class=ARCHIVE",
                                     "--recursive"], archive_timeout))
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
