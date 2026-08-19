"""Cloud verification — rclone check --one-way post-sync integrity.

Runs after cloud_sync to confirm source matches GCS. Uses size-only
comparison (not MD5 hash) to avoid 2-hour HDD re-hashing of 500GB.

Exit codes (rclone check):
    0 = verified — source and GCS file counts and sizes agree
    1 = mismatch — something didn't sync or sizes diverged
    2+ = error — connection failure, invalid config, etc.
"""

import re
import subprocess

from loguru import logger

from core.lan_sync import MIRROR_EXCLUDED_DIRS
from core.process import resolve_binary

# rclone check exit codes
_EXIT_VERIFIED = 0
_EXIT_MISMATCH = 1


def verify_cloud_integrity(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
    timeout: int = 14400,
) -> dict:
    """Run rclone check --one-way --size-only to verify source matches GCS.

    Args:
        source: Source drive path.
        bucket: GCS bucket name.
        fy_prefix: Fiscal year folder prefix.
        config_path: Path to rclone config file.
        timeout: Max seconds for the check (default 14400 — 4 hours for large HDD datasets).

    Returns:
        {"verified": bool, "exit_code": int, "error": str | None,
         "error_class": str | None}
        `error_class` is the NV-02 classification of a failed check
        (None on success and on local exceptions — timeout/missing binary —
        which are not GCS outcomes).
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"

    rclone_exe = resolve_binary("rclone") or "rclone"
    cmd = [
        rclone_exe, "check",
        source, dest,
        "--one-way",               # Only check source→GCS, not reverse
        "--fast-list",             # Fewer GCS API calls (uses more memory but faster)
        "--size-only",             # Compare sizes only — avoids expensive MD5 re-hashing on HDD
        "--modify-window", "2s",   # NTFS mtime has 2s granularity; default 1ns causes false positives
        # R4/E2E-verified: the check must compare EXACTLY the file set the
        # sync transferred (see MIRROR_EXCLUDED_DIRS + the NA-01 seed
        # marker). Without the marker exclusion, any source carrying the
        # FY-rollover seed marker (i.e. EVERY run after an April-1
        # rollover — the marker is never removed) makes rclone check report
        # it "missing from GCS" and the pipeline fails with a false
        # CLOUD_VERIFY_FAILED. Live-verified in the 2026-08-19 E2E (P6/P6c).
        # Directory exclusions use the "<dir>/*" any-depth form (a bare name
        # matches the full path exactly and would filter nothing — see
        # cloud_sync.build_rclone_sync_command for the verified semantics).
        "--exclude", ".AAM_SOURCE_SEEDED",
        *[opt for d in MIRROR_EXCLUDED_DIRS for opt in ("--exclude", d, "--exclude", f"{d}/*")],
        # NOTE: --check-first and --transfers are intentionally omitted here.
        # rclone check does no file transfers, so both flags are no-ops on this command.
        "--checkers", "4",         # Concurrent metadata checkers — safe for GCS API rate limits
        "--config", config_path,
        "--gcs-no-check-bucket",   # Bucket already verified by preflight; skip redundant check
    ]

    logger.info(f"Cloud verify: checking {source} <-> {bucket}/{fy_prefix}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        verified = result.returncode == _EXIT_VERIFIED
        error_class = _classify_check_failure(result.returncode, result.stderr or "")

        if verified:
            logger.info("Cloud integrity verified — source matches GCS")
        else:
            # NV-02: distinguish a true integrity mismatch from a GCS access
            # failure — rclone check exits 1 for BOTH, so the exit code alone
            # mislabels an auth/network outage as "data mismatch" (live
            # verified: bad credentials → exit 1, "N errors while checking").
            label = error_class
            # Log full stderr — truncating hides the actual error in production
            stderr_output = result.stderr.strip() if result.stderr else "no stderr"
            logger.warning(f"Cloud verify {label} (exit {result.returncode}): {stderr_output}")

        return {
            "verified": verified,
            "exit_code": result.returncode,
            "error": _build_error_message(result.returncode, error_class),
            "error_class": error_class,
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Cloud verify timed out after {timeout}s")
        return {"verified": False, "exit_code": -1,
                "error": f"Timeout after {timeout}s", "error_class": None}
    except FileNotFoundError:
        logger.error("rclone not found")
        return {"verified": False, "exit_code": -1,
                "error": "rclone not found", "error_class": None}
    except OSError as e:
        logger.error(f"Cloud verify error: {e}")
        return {"verified": False, "exit_code": -1, "error": str(e),
                "error_class": None}


# E2E-verified on real GCS (2026-08-19): a genuinely MISSING file makes
# rclone check report it in ALL THREE counters — "N files missing",
# "N differences found" AND "N errors while checking" (the per-file
# "ERROR : <path>: file not in ... bucket" line is counted as a check
# error). The original NV-02 heuristic (errors >= diffs => access_error)
# therefore mislabelled EVERY true mismatch as an access error. Reliable
# classification must be based on the CONTENT of the ERROR lines: access
# failures carry credential/network/API signatures; missing-file lines
# carry "file not in <backend>". The signatures below are taken from
# live rclone v1.74.3 output:
#   - bad/truncated SA key:  "error reading destination root directory:
#     Get ...: private key should be a PEM or plain PKCS1 or PKCS8; parse
#     error: asn1: syntax error: data truncated"
#   - rejected/expired key:  "googleapi: Error 403: ... access denied or
#     insufficient permissions" / "401: ... Unauthenticated"
#   - network:               "no such host" / "connection refused" /
#     "i/o timeout" / "deadline exceeded"
_ACCESS_SIGNATURE_RE = re.compile(
    r"error reading destination root directory"
    r"|private key should be a pem"
    r"|could not read private key"
    r"|asn1:"
    r"|invalid[_ ]grant"
    r"|unauthenticated"
    r"|\b40[134]\b"
    r"|forbidden"
    r"|access[_ ]denied"
    r"|permission denied"
    r"|no such host"
    r"|connection (refused|reset)"
    r"|i/o timeout"
    r"|deadline exceeded"
    r"|oauth2:"
    r"|googleapi: error",
    re.IGNORECASE,
)


def _classify_check_failure(exit_code: int, stderr_text: str) -> str | None:
    """Classify a failed `rclone check` (NV-02, E2E-refined 2026-08-19).

    rclone check exits 1 for BOTH a true integrity mismatch AND a
    destination access failure (bad credentials, network drop, missing
    bucket) — and, live-verified, a missing file is counted in the
    "files missing", "differences found" AND "errors while checking"
    counters simultaneously. The counters therefore cannot distinguish
    the two cases; the ERROR-line content can (see
    _ACCESS_SIGNATURE_RE for the verified signatures).

    Returns:
        None for exit 0;
        "access_error" — credential/network/API read failures (NOT a data
                         mismatch; do not conclude about the data);
        "mismatch"     — true divergence (files missing/changed in GCS);
        "mixed"        — access failures AND differences in one run
                         (treat as access_error until GCS is reachable);
        "error"        — exit 2+ (config/usage-level failure).
    """
    if exit_code == _EXIT_VERIFIED:
        return None
    if exit_code != _EXIT_MISMATCH:
        return "error"

    error_lines = re.findall(r"ERROR\s*:\s*(.+)", stderr_text)
    has_access = any(_ACCESS_SIGNATURE_RE.search(line) for line in error_lines)
    has_missing = any("file not in" in line for line in error_lines) or bool(
        re.search(r"\d+ files? missing", stderr_text)
    )
    has_diffs = bool(re.search(r"\d+ differences? found", stderr_text))

    # If the destination ROOT could not be read at all, the destination
    # listing is empty, so every source file appears "missing" — those
    # missing/diff counts are byproducts of the access failure, not real
    # differences. Classify as access_error, not mixed. (Live-verified:
    # the 2026-08-19 P4a bad-key run.)
    if any("error reading destination root directory" in line for line in error_lines):
        return "access_error"

    if has_access and (has_missing or has_diffs):
        return "mixed"
    if has_access:
        return "access_error"
    if has_missing or has_diffs:
        return "mismatch"
    # Defensive fallback: exit 1 with no parseable counters or recognizable
    # error lines. "N errors while checking" with no other signal means the
    # destination could not be read — do not claim a data mismatch.
    if re.search(r"\d+ errors? while checking", stderr_text):
        return "access_error"
    return "mismatch"


def _build_error_message(exit_code: int, error_class: str | None = None) -> str | None:
    """Build a human-readable error message from rclone exit code + class.

    Exit 0 = no error.
    Exit 1 = mismatch (source and GCS diverged) — or a GCS access failure,
        which NV-02's classifier re-labels so an auth/network outage is not
        alerted as "data may be missing".
    Exit 2+ = rclone error (connection, auth, invalid config, etc.).
    """
    if exit_code == _EXIT_VERIFIED:
        return None
    if error_class == "access_error":
        return (
            "Cloud verify could not read GCS (access/auth/network error) — "
            "this is NOT a confirmed integrity mismatch; GCS reachability "
            "must be checked before drawing conclusions about the data"
        )
    if error_class == "mixed":
        return (
            "Cloud verify found differences AND destination read errors — "
            "treat as unverified until GCS is reachable; the mismatch count "
            "is unreliable while reads fail"
        )
    if exit_code == _EXIT_MISMATCH:
        return "Integrity mismatch — source and GCS file counts or sizes differ"
    return f"Rclone check failed with exit code {exit_code} — check rclone logs for details"
