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
        data = json.loads(result.stdout.strip())
        logger.info(f"Cloud size: {data['count']} files, {data['bytes']} bytes")
        return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Cloud size query failed: {e}")
        return {"count": 0, "bytes": 0, "sizeless": "0", "_error": str(e)}


def get_cloud_manifest(bucket: str, fy_prefix: str, config_path: str, timeout: int = 300) -> list[dict]:
    """rclone lsjson -R → [{Path, Size, ModTime, MimeType, IsDir}, ...].

    Files only — directory entries filtered out. No file content read,
    just metadata from GCS listing API.
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    rclone_exe = resolve_binary("rclone") or "rclone"
    cmd = [rclone_exe, "lsjson", dest, "-R", *_base_args(config_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        data = json.loads(result.stdout)
        files = [f for f in data if not f.get("IsDir")]
        logger.info(f"Cloud manifest: {len(files)} files")
        return files
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Cloud manifest query failed: {e}")
        return []


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
            "--check-first",           # Metadata comparison before any I/O — separates random from sequential
            "--transfers", "2",        # Throttled for mechanical HDD
            "--checkers", "4",         # Throttled for mechanical HDD
            "--retries", "3",          # Retry transient network errors
            "--retries-sleep", "10s",  # Back off between retries
            *_base_args(config_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        # rclone check exits 0 on match, 1 on mismatch, 2+ on error.
        # Even on mismatch (exit 1), the --combined file is valid and useful.
        # On error (exit 2+), the file might be empty or incomplete.
        if result.returncode >= 2:
            stderr_snippet = result.stderr[:500] if result.stderr else "no stderr"
            logger.warning(f"Cloud diff rclone failed (exit {result.returncode}): {stderr_snippet}")

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
            logger.warning("Cloud diff file not found after rclone check — rclone may have failed")

        logger.info(
            f"Cloud diff: +{len(diff['added'])} -{len(diff['removed'])} "
            f"*{len(diff['modified'])} ={len(diff['unchanged'])}"
        )
        return diff

    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"Cloud diff query failed: {e}")
        return {"added": [], "removed": [], "modified": [], "unchanged": []}
    finally:
        if diff_file:
            try:
                Path(diff_file).unlink()
            except OSError:
                pass
