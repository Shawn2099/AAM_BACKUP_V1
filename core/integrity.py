"""Weekly independent integrity audit — read-only deep verification.

Two-concept architecture:

    BACKUP RESULT (COMPLETE/PARTIAL/SUSPECT/FAILED — transfer evidence)
    INTEGRITY STATUS (NOT_VERIFIED/VERIFIED/VERIFICATION_FAILED — this module)

`rclone check` is the verification engine for BOTH legs (measured on
rclone v1.74.2, local backend — see capability notes below). It NEVER
modifies data: no sync/copy/delete flags are used anywhere here.
Results land in the `integrity_audits` manifest table and never rewrite
historical backup rows. Absence of a row means NOT_VERIFIED.

Measured rclone capabilities (this environment, rclone v1.74.2):
  * default `check` (NO --size-only) compares size + MD5 content hash;
    same-size/different-content IS detected ("md5 differ", exit 1).
  * local-backend hashes are computed by reading file contents (no stored
    hashes on NTFS/SMB); no --download flag is required for this.
  * --size-only is BLIND to same-size corruption (exit 0) and must NEVER
    produce VERIFIED — size-only outcomes are recorded as
    VERIFICATION_FAILED with capability=SIZE_ONLY.
  * --one-way: `+` = missing from dest, `-` extras ignored for the gate
    (mirror policy: next transfer converges them); extras still reported.
  * timestamp-only differences compare EQUAL (`=`, exit 0) — content
    equality is the verified property, matching /MIR semantics.
  * exits: 0 = match, 1 = differences, >=2 = process/transport error.
  * GCS side: backend-advertised MD5/CRC32C (not live-probed from this
    rig — recorded as capability=HASH_GCS_ADVERTISED in cloud rows).

Resource model (dual-core Xeon, HDD, DDR3, ~400 GB): single checker
(--checkers 1), one audit at a time, streaming reads, progress logs.
Concurrency is NEVER raised to shorten the audit.
"""

import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path, PureWindowsPath

from loguru import logger

from core.process import resolve_binary

# Alert/detail payload bound: full mismatch COUNTS are always recorded;
# the path list is truncated so a large divergence cannot blow up the
# manifest row or the alert email.
_MAX_DETAIL_PATHS = 100

# Verification capability labels. VERIFIED is recorded ONLY for
# hash-capable comparisons; anything else fails closed.
CAPABILITY_HASH_LOCAL = "HASH_MD5_RCLONE_LOCAL"      # measured §probe
CAPABILITY_HASH_GCS = "HASH_GCS_ADVERTISED"          # backend-advertised
CAPABILITY_SIZE_ONLY = "SIZE_ONLY_INSUFFICIENT"      # never VERIFIED


def _norm_rel(rel: str) -> str:
    return PureWindowsPath(rel).as_posix().lstrip("/")


def _parse_combined_file(diff_file: str | None) -> tuple[list, list, list, list, bool]:
    """Parse `rclone check --combined` output. Returns (added, removed,
    modified, unchanged, file_present). added=`+` missing-from-dest,
    removed=`-` extra-in-dest, modified=`*` differ, unchanged=`=`."""
    added: list[str] = []
    removed: list[str] = []
    modified: list[str] = []
    unchanged: list[str] = []
    if not diff_file or not os.path.exists(diff_file):
        return added, removed, modified, unchanged, False
    try:
        with open(diff_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if len(line) >= 3 and line[1] == " ":
                    p = _norm_rel(line[2:].strip())
                    if not p or p == ".":
                        continue
                    if line[0] == "+":
                        added.append(p)
                    elif line[0] == "-":
                        removed.append(p)
                    elif line[0] == "*":
                        modified.append(p)
                    elif line[0] == "=":
                        unchanged.append(p)
    except OSError:
        return added, removed, modified, unchanged, False
    return added, removed, modified, unchanged, True


def _run_rclone_check(
    source: str,
    dest: str,
    scope: str,
    timeout: int,
    extra_args: list[str] | None = None,
    size_only: bool = False,
) -> dict:
    """Run one read-only `rclone check --combined` and classify the result.

    NEVER passes sync/copy/delete/move flags. Returns a result dict with
    an explicit capability label; VERIFIED requires hash capability AND a
    clean diff AND a completed process (exit 0/1 with a present diff file).
    """
    started = time.monotonic()
    rclone_exe = resolve_binary("rclone") or "rclone"
    diff_file = None
    try:
        fd, diff_file = tempfile.mkstemp(suffix=".txt", prefix="integrity_audit_")
        os.close(fd)
        # NOTE: deliberately NO --one-way here (unlike the daily check).
        # The weekly audit is the deep comparison: extra destination objects
        # (`-`) are a reportable divergence (§16 unexpected files), even
        # though the mirror transfers converge them on the next daily run.
        cmd = [
            rclone_exe, "check",
            source, dest,
            "--combined", diff_file,
            "--fast-list",
            "--modify-window", "2s",
            "--checkers", "1",
            "--retries", "3",
            "--retries-sleep", "10s",
        ]
        if size_only:
            cmd.append("--size-only")
        if extra_args:
            cmd.extend(extra_args)
        logger.info(f"Integrity audit ({scope}): rclone check {source} <-> {dest}")
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        added, removed, modified, _unchanged, present = _parse_combined_file(diff_file)
        elapsed = time.monotonic() - started
        capability = CAPABILITY_SIZE_ONLY if size_only else CAPABILITY_HASH_LOCAL
        if result.returncode >= 2 or not present:
            status = "VERIFICATION_FAILED"
            detail = (
                f"capability={capability} audit process error "
                f"(exit {result.returncode}): {(result.stderr or '').strip()[:2000]}"
            )
            mismatches = -1
        elif size_only:
            # §11/acceptance-15: a size-only comparison is NOT deep
            # verification and must never be recorded VERIFIED.
            status = "VERIFICATION_FAILED"
            mismatches = len(added) + len(modified)
            detail = (
                f"capability={capability} size-only comparison is insufficient "
                f"for VERIFIED (missing={len(added)} changed={len(modified)})"
            )
        else:
            mismatches = len(added) + len(modified) + len(removed)
            status = "VERIFIED" if mismatches == 0 else "VERIFICATION_FAILED"
            detail_paths = (added + modified + removed)[:_MAX_DETAIL_PATHS]
            detail = (
                f"capability={capability} missing-from-dest={len(added)} "
                f"extra-in-dest={len(removed)} content-changed={len(modified)} "
                f"elapsed_s={elapsed:.0f} paths={detail_paths}"
            )
        logger.info(f"Integrity audit ({scope}) {status}: {detail}")
        return {
            "status": status, "scope": scope, "capability": capability,
            "files_checked": -1, "bytes_checked": -1,
            "mismatches": mismatches, "missing": added, "extra": removed,
            "detail": detail, "elapsed_s": round(elapsed, 1),
        }
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.error(f"Integrity audit ({scope}) error: {e}")
        return {
            "status": "VERIFICATION_FAILED", "scope": scope,
            "capability": CAPABILITY_HASH_LOCAL,
            "files_checked": -1, "bytes_checked": -1, "mismatches": -1,
            "missing": [], "extra": [],
            "detail": f"audit execution failed: {e}", "elapsed_s": 0.0,
        }
    finally:
        if diff_file:
            try:
                Path(diff_file).unlink()
            except OSError:
                pass


def audit_lan(
    source: str,
    dest: str,
    scope_prefixes: list[str] | None = None,
    timeout: int = 14400,
) -> dict:
    """Read-only LAN audit via `rclone check` (local backend, hash-aware).

    The LAN backup was created by Robocopy; Robocopy does not participate
    here — rclone independently compares source path ↔ backup path by
    size + MD5 content (measured). No --size-only: size-only can never
    yield VERIFIED. UNC destinations (\\\\server\\share) work through the
    local backend like any other path.
    """
    scope = "full" if not scope_prefixes else f"shard:{','.join(sorted(scope_prefixes))}"
    extra = None
    if scope_prefixes:
        extra = []
        for p in scope_prefixes:
            extra.extend(["--include", f"{p.strip('/')}/**"])
    # NOTE: include-filter form is validated by test_scope_sharding; if a
    # future rclone changes filter semantics the test fails closed first.
    return _run_rclone_check(source, dest, f"lan/{scope}", timeout, extra_args=extra)


def audit_cloud(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
    timeout: int = 14400,
) -> dict:
    """Read-only cloud audit: hash-aware `rclone check` (NOT --size-only).

    Uses the existing GCS/rclone configuration. GCS exposes stored content
    hashes (backend-advertised MD5/CRC32C), so no source re-read beyond what
    rclone performs and no --download flag. Single checker.
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    result = _run_rclone_check(
        source, dest, "cloud/full", timeout,
        extra_args=["--config", config_path, "--gcs-no-check-bucket"],
    )
    if result["capability"] == CAPABILITY_HASH_LOCAL:
        result["capability"] = CAPABILITY_HASH_GCS
        result["detail"] = result["detail"].replace(CAPABILITY_HASH_LOCAL, CAPABILITY_HASH_GCS)
    return result


def new_audit_id(mode: str, scope: str) -> str:
    """Traceable audit execution id: links the manifest row to the run."""
    return f"{uuid.uuid4()}-{mode}-{scope}"
