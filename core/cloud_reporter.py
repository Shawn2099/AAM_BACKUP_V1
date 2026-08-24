"""Cloud reporter — rclone native commands for GCS state reporting.

Every function calls one rclone subcommand. Zero custom logic.
Rclone IS the source of truth for GCS state.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from core.process import resolve_binary


class CloudReporterError(RuntimeError):
    """M6: a GCS state query failed. Raised by get_cloud_manifest so callers
    can distinguish 'bucket is empty' from 'we could not ask' — a failed
    listing must never masquerade as an empty bucket."""


def _base_args(config_path: str) -> list[str]:
    """Shared rclone flags for all reporter functions.

    --config: rclone config with GCS credentials.
    --gcs-no-check-bucket: Skip bucket existence check (already verified by preflight).
    --fast-list: Use recursive listing — fewer GCS API calls, faster for large buckets.
    """
    return ["--config", config_path, "--gcs-no-check-bucket", "--fast-list"]


def get_cloud_size(bucket: str, fy_prefix: str, config_path: str, timeout: int = 30) -> dict:
    """rclone size --json → {"count": int, "bytes": int, "sizeless": str}.

    Instant — GCS returns pre-computed object counts. No file traversal.
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    rclone_exe = resolve_binary("rclone") or "rclone"
    cmd = [rclone_exe, "size", dest, "--json", *_base_args(config_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        logger.warning(f"Cloud size query failed: {e}")
        return {"count": 0, "bytes": 0, "sizeless": "0", "_error": str(e)}
    except FileNotFoundError as e:
        # M6: missing binary is data, not a crash — callers see _error
        logger.warning(f"Cloud size query failed: rclone not found: {e}")
        return {"count": 0, "bytes": 0, "sizeless": "0", "_error": f"rclone not found: {e}"}
    if result.returncode != 0:
        stderr_output = result.stderr.strip() if result.stderr else "no stderr"
        logger.warning(f"Cloud size rclone exited {result.returncode}: {stderr_output}")
        data = None
    else:
        try:
            data = json.loads(result.stdout.strip())
        except json.JSONDecodeError as e:
            logger.warning(f"Cloud size query returned unparsable output: {e}")
            return {"count": 0, "bytes": 0, "sizeless": "0", "_error": str(e)}
    if data is None:
        # Non-zero exit: do NOT parse stdout — report the failure honestly
        return {
            "count": 0,
            "bytes": 0,
            "sizeless": "0",
            "_error": f"rclone size exited {result.returncode}: "
                      f"{(result.stderr or '').strip() or 'no stderr'}",
        }
    # Use .get() so a malformed-but-valid JSON response (e.g. {}) doesn't escape as KeyError
    count = data.get("count", 0)
    size_bytes = data.get("bytes", 0)
    logger.info(f"Cloud size: {count} files, {size_bytes} bytes")
    return data


def get_cloud_manifest(bucket: str, fy_prefix: str, config_path: str, timeout: int = 300) -> list[dict]:
    """rclone lsjson -R -> [{Path, Size, ModTime, MimeType, IsDir}, ...].

    Files only — directory entries filtered out. No file content read,
    just metadata from GCS listing API.

    M6: raises CloudReporterError on ANY failure (nonzero exit, timeout,
    missing binary, unparsable output). The old behavior returned [] on
    failure — indistinguishable from a genuinely empty bucket, which let
    failed queries silently corrupt downstream metrics.
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    rclone_exe = resolve_binary("rclone") or "rclone"
    cmd = [rclone_exe, "lsjson", dest, "-R", *_base_args(config_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise CloudReporterError(
            f"cloud manifest listing timed out after {timeout}s for {dest}"
        ) from e
    except FileNotFoundError as e:
        raise CloudReporterError(f"rclone not found while listing manifest: {e}") from e
    except OSError as e:
        raise CloudReporterError(f"cloud manifest listing failed for {dest}: {e}") from e

    if result.returncode != 0:
        stderr_output = (result.stderr or "").strip() or "no stderr"
        raise CloudReporterError(
            f"rclone lsjson exited {result.returncode} for {dest}: {stderr_output}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise CloudReporterError(
            f"unparsable lsjson output for {dest}: {e}"
        ) from e

    files = [f for f in data if not f.get("IsDir")]
    logger.info(f"Cloud manifest: {len(files)} files")
    return files


def get_cloud_diff(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
    timeout: int = 600,  # override via config.cloud.diff_timeout_seconds
) -> dict:
    """rclone check --combined --size-only → {added, removed, modified, unchanged}.

    Compares source and GCS by file SIZE only (not MD5 hash) to avoid
    2+ hour re-hashing of 500GB on mechanical HDD. Size comparison is
    sufficient for accounting documents — content changes almost always
    change file size.

    Writes diff to temp file, parses +/-/*/= prefixes, cleans up in finally.

    Returns:
        {"added": [...], "removed": [...], "modified": [...], "unchanged": [...]}
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    diff_file = None

    try:
        fd, diff_file = tempfile.mkstemp(suffix=".txt", prefix="cloud_diff_")
        os.close(fd)  # Release handle so rclone can write to it

        rclone_exe = resolve_binary("rclone") or "rclone"
        cmd = [
            rclone_exe, "check",
            source, dest,
            "--combined", diff_file,   # Write unified diff to file (not stderr)
            "--size-only",             # Compare sizes only — avoids expensive MD5 re-hashing on HDD
            "--modify-window", "2s",   # NTFS mtime has 2s granularity; default 1ns causes false positives
            # NOTE: --check-first and --transfers are intentionally omitted here.
            # rclone check does no file transfers, so both flags are no-ops.
            "--checkers", "4",         # Concurrent metadata checkers — safe for GCS API rate limits
            "--retries", "3",          # Retry transient network errors
            "--retries-sleep", "10s",  # Back off between retries
            *_base_args(config_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        # rclone check exits 0 on match, 1 on mismatch, 2+ on error.
        # Even on mismatch (exit 1), the --combined file is valid and useful.
        # On error (exit 2+), the file might be empty or incomplete.
        partial = False
        error_msg = None
        if result.returncode >= 2:
            partial = True
            stderr_output = result.stderr.strip() if result.stderr else "no stderr"
            error_msg = f"rclone check exited {result.returncode}: {stderr_output}"
            logger.warning(f"Cloud diff rclone failed (exit {result.returncode}): {stderr_output}")

        diff = {"added": [], "removed": [], "modified": [], "unchanged": []}

        try:
            with open(diff_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    # rclone check --combined format: <prefix> <filename>
                    # Prefix: + (added to dest), - (removed from dest), * (modified), = (unchanged)
                    if line[0] == "+":
                        diff["added"].append(line[2:])
                    elif line[0] == "-":
                        diff["removed"].append(line[2:])
                    elif line[0] == "*":
                        diff["modified"].append(line[2:])
                    elif line[0] == "=":
                        diff["unchanged"].append(line[2:])
        except FileNotFoundError:
            # Diff file missing — rclone failed to create it
            logger.warning("Cloud diff file not found after rclone check - rclone may have failed")

        if partial:
            diff["_partial"] = True
            if error_msg:
                diff["_error"] = error_msg

        logger.info(
            f"Cloud diff: +{len(diff['added'])} -{len(diff['removed'])} "
            f"*{len(diff['modified'])} ={len(diff['unchanged'])}"
            + (" [PARTIAL — rclone exited with error]" if partial else "")
        )
        return diff

    except (subprocess.TimeoutExpired, OSError) as e:
        # M6: a timed-out/failed scan must be distinguishable from "no changes"
        logger.warning(f"Cloud diff query failed: {e}")
        return {
            "added": [], "removed": [], "modified": [], "unchanged": [],
            "_partial": True, "_error": str(e),
        }
    finally:
        if diff_file:
            try:
                Path(diff_file).unlink()
            except OSError:
                pass
