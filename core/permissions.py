"""Preflight permission checks (plan WP4, R7; gates CL3, CL13, LA7-LA9).

Every check proves a right the backup is about to use, before anything is
copied or deleted, and fails with a message that names the missing right
and the fix. These are environment faults to repair, so they raise
PermissionCheckError (the run is *_FAILED and retried next night) — they
are never a human approval decision (*_BLOCKED).

Verified on Windows 11 / robocopy 10.0 / rclone v1.74.3 (2026-10-15):
* `whoami /priv /fo csv /nh` prints one CSV row per privilege; column 1 is
  the privilege NAME (locale independent), description and state are
  localized. Git Bash ships its own `whoami`, so the System32 one is called
  by full path.
* `shutil.disk_usage` works on a UNC path.
* `rclone touch` / `rclone deletefile` exit 0 on success (deletefile of a
  missing object exits 4).
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil

from loguru import logger

from core import guards
from core.faults import TRANSPORT_CODES, coded
from core.process import resolve_binary
from core.rclone_errors import find_transport_failure
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool, tail_lines

GB = 1024 ** 3

# Destination write probe. robocopy excludes the pattern (/XF) in both the
# dry-run and the mirror, like the mount marker, so a probe left behind by
# a killed run is never copied, deleted or counted as a planned deletion.
PROBE_PREFIX = ".aam_write_probe_"
PROBE_PATTERN = PROBE_PREFIX + "*"
_PROBE_PAYLOAD = b"AAM backup write probe - safe to delete\n"

# Cloud write probe lives OUTSIDE every FY prefix, so sync, size, verify and
# the integrity audit never see it (DEC-5).
CLOUD_PROBE_PREFIX = "_aam_preflight/probe_"

_PRIVILEGE_NAME = re.compile(r"^Se\w+Privilege$")


class PermissionCheckError(RuntimeError):
    """A right the backup needs is missing (or could not be verified)."""


def _fail(code: str, message: str) -> PermissionCheckError:
    """The error with its core.faults code (audit 2026-10-18)."""
    return coded(PermissionCheckError(message), code)


# ── SeBackupPrivilege (robocopy /ZB) ───────────────────────────────────────

def whoami_path() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe")


def parse_privileges(csv_text: str) -> set[str]:
    """Privilege names held by the token (listed = held, Enabled or Disabled)."""
    names = set()
    for row in csv.reader(io.StringIO(csv_text or "")):
        if row and _PRIVILEGE_NAME.match(row[0].strip()):
            names.add(row[0].strip())
    return names


def check_backup_privilege(artefacts: RunArtefacts, timeout_s: float) -> None:
    """robocopy /ZB needs SeBackupPrivilege, else it stops with exit 16 (C-2).

    A privilege listed by whoami is held (robocopy enables it itself). An
    interactive non-elevated session hides it (UAC); the service context
    shows the real token.
    """
    result = run_tool("whoami-priv", [whoami_path(), "/priv", "/fo", "csv", "/nh"],
                      timeout_s=timeout_s, artefacts=artefacts)
    if not result.completed or result.exit_code != 0:
        detail = result.describe_failure() or (
            f"whoami exit {result.exit_code}: {result.stderr_text().strip() or 'no output'}")
        raise _fail("AAM-LAN-05", f"SeBackupPrivilege could not be checked ({detail}) - "
                                   "refusing to start robocopy /ZB blind.")
    held = parse_privileges(result.stdout_text())
    if not held:
        raise _fail("AAM-LAN-05", "SeBackupPrivilege could not be checked: whoami listed "
                                   "no privileges.")
    if "SeBackupPrivilege" not in held:
        raise _fail("AAM-LAN-05",
            "The account running the backup does not hold SeBackupPrivilege ('Back up files "
            "and directories'), which robocopy /ZB needs - robocopy would stop with exit 16. "
            "Fix: secpol.msc > Local Policies > User Rights Assignment > 'Back up files and "
            "directories' > add the backup service account, then restart the AamBackupAgent "
            "service. Alternative: set lan.use_backup_mode: false (no /ZB; files the account "
            "cannot read will then fail to copy).")
    logger.info("Preflight: SeBackupPrivilege held")


# ── LAN destination: write / read back / delete ───────────────────────────

def _read_back(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Write probe {path} could not be removed: {e}")


def probe_destination_write(dest: str, run_id: str) -> None:
    """Create, write, read back and delete a probe file at the destination root.

    robocopy /L cannot detect write denial (21 nights of 'Access is denied'
    in production); the mirror needs create, write and delete rights.
    """
    path = os.path.join(dest, PROBE_PREFIX + re.sub(r"[^A-Za-z0-9_.-]", "_", run_id))
    fix = ("The account running the backup needs modify rights (create, write, delete) on "
           "the NAS share and folder. On a WORKGROUP server run the AamBackupAgent service "
           "under a dedicated service account that also exists on the NAS with the same "
           "password - LocalSystem cannot write to the NAS (handover checklist, section 10 "
           "item 2).")
    try:
        with open(path, "wb") as f:
            f.write(_PROBE_PAYLOAD)
        data = _read_back(path)
    except OSError as e:
        _remove_quietly(path)
        raise _fail("AAM-NAS-03", f"Cannot write to the LAN destination {dest}: {e}. {fix}") from e
    if data != _PROBE_PAYLOAD:
        _remove_quietly(path)
        raise _fail("AAM-NAS-11",
            f"Wrote a test file to the LAN destination {dest} but could not read back the same "
            "content - the share is not storing data reliably. Check the NAS before backing up.")
    try:
        os.remove(path)
    except OSError as e:
        raise _fail("AAM-NAS-03",
            f"Could create but not delete a test file on the LAN destination {dest} ({e}); the "
            f"mirror must delete files that were removed from the source. {fix}") from e
    logger.info(f"Preflight: LAN destination writable ({dest})")


def check_destination_free_space(dest: str, bytes_needed: int | None, min_free_gb: float) -> None:
    """Free space on the NAS >= bytes the mirror will write + configured margin."""
    if bytes_needed is None:
        raise _fail("AAM-NAS-05",
            "NAS free space could not be measured against the backup size: the robocopy /L "
            "byte count is missing - refusing to start a copy that may fill the NAS.")
    try:
        free = shutil.disk_usage(dest).free
    except OSError as e:
        raise _fail("AAM-NAS-05", f"NAS free space could not be measured on {dest}: {e}") from e
    need = bytes_needed + min_free_gb * GB
    if free < need:
        raise _fail("AAM-NAS-04",
            f"Not enough free space on the NAS {dest}: {free / GB:.1f} GB free, this backup "
            f"needs {need / GB:.1f} GB ({bytes_needed / GB:.1f} GB to copy + "
            f"lan.min_free_dest_gb {min_free_gb} GB margin). Free up space on the NAS or "
            "enlarge the volume.")
    logger.info(f"Preflight: NAS free space {free / GB:.1f} GB >= {need / GB:.1f} GB needed")


# ── Source: the account can read real files ───────────────────────────────

def probe_source_read(source: str) -> str:
    """Open and read the first byte of the first real source file (G1's walk)."""
    try:
        first = guards.first_regular_file(source, guards.SOURCE_IGNORE_NAMES)
    except OSError as e:
        raise _fail("AAM-SRC-02", f"Cannot list the source {source}: {e}") from e
    if first is None:
        raise _fail("AAM-SRC-02", f"No file to test read access on in the source {source}.")
    try:
        with open(first, "rb") as f:
            f.read(1)
    except OSError as e:
        raise _fail("AAM-SRC-02",
            f"Cannot read the source file {first}: {e}. The account running the backup "
            f"needs read rights on {source}.") from e
    logger.info(f"Preflight: source readable ({first})")
    return first


# ── Cloud: create + delete a probe object ─────────────────────────────────

def probe_cloud_write(bucket: str, run_id: str, config_path: str, timeout_s: float,
                      artefacts: RunArtefacts, error_tail_lines: int = 100) -> None:
    """`rclone touch` + `rclone deletefile` of <bucket>/_aam_preflight/probe_<run_id>.

    Same flags as the sync (--gcs-no-check-bucket), so it tests exactly the
    rights the sync uses. In a versioned bucket it leaves one tiny
    noncurrent version, removed by the lifecycle rule (owner DEC-5).
    """
    target = f"aam_gcs:{bucket}/{CLOUD_PROBE_PREFIX}{run_id}"
    exe = resolve_binary("rclone") or "rclone"
    for verb, what in (("touch", "create"), ("deletefile", "delete")):
        result = run_tool(f"rclone-{verb}",
                          [exe, verb, target, "--config", config_path, "--gcs-no-check-bucket"],
                          timeout_s=timeout_s, artefacts=artefacts)
        if result.completed and result.exit_code == 0:
            continue
        text = "\n".join(t for t in (result.stderr_text(), result.stdout_text()) if t)
        cause = find_transport_failure(text)
        cloud_code = TRANSPORT_CODES.get(cause.category, "AAM-CLD-05") if cause else "AAM-CLD-05"
        detail = result.describe_failure() or f"rclone exit {result.exit_code}"
        raise _fail(cloud_code,
            f"Cloud write check failed: could not {what} the test object {target} ({detail}: "
            f"{tail_lines(text, error_tail_lines) or 'no output'})."
            + (f" Cause: {cause.category} - {cause.hint}" if cause else "")
            + " The service account needs create and delete rights on the bucket "
              "(roles/storage.objectAdmin).")
    logger.info(f"Preflight: cloud bucket writable ({bucket})")
