"""Cloud reporter — rclone native commands for GCS state reporting.

Every function calls one rclone subcommand. Zero custom logic.
Rclone IS the source of truth for GCS state.
"""

import json
import os
import tempfile
from pathlib import Path, PureWindowsPath

from loguru import logger

from core.process import resolve_binary
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool, tail_lines


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


def count_objects(target: str, config_path: str, timeout: int, artefacts: RunArtefacts) -> int:
    """File count of an rclone path (local folder or `aam_gcs:bucket/prefix`).

    `rclone size --json` sees exactly what `rclone sync` sees (same
    backend, no filters), so the count matches the sync's view — guard
    input G2/G3 (M2 2.2). Raises CloudReporterError on ANY failure: a
    guard must never mistake "could not count" for zero.
    """
    rclone_exe = resolve_binary("rclone") or "rclone"
    cmd = [rclone_exe, "size", target, "--json", *_base_args(config_path)]
    result = run_tool("rclone-size", cmd, timeout_s=timeout, artefacts=artefacts)
    if not result.completed:
        raise CloudReporterError(f"rclone size {target}: {result.describe_failure()}")
    if result.exit_code != 0:
        raise CloudReporterError(
            f"rclone size {target} exited {result.exit_code}: "
            f"{tail_lines(result.stderr_text(), 20) or 'no stderr'}")
    try:
        count = json.loads(result.stdout_text())["count"]
    except (ValueError, KeyError, TypeError) as e:
        raise CloudReporterError(f"rclone size {target}: unreadable output ({e})") from e
    if type(count) is not int or count < 0:
        raise CloudReporterError(f"rclone size {target}: invalid count {count!r}")
    logger.info(f"rclone size {target}: {count} file(s)")
    return count


def get_cloud_size(bucket: str, fy_prefix: str, config_path: str, *, timeout: int,
                   artefacts: RunArtefacts | None = None) -> dict:
    """rclone size --json → {"count": int, "bytes": int, "sizeless": str}.

    Instant — GCS returns pre-computed object counts. No file traversal.
    Any failure is returned as data under "_error" (reporting only).
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    rclone_exe = resolve_binary("rclone") or "rclone"
    cmd = [rclone_exe, "size", dest, "--json", *_base_args(config_path)]

    result = run_tool("rclone-size-report", cmd, timeout_s=timeout,
                      artefacts=artefacts or RunArtefacts.ephemeral("cloud-size"))
    if not result.completed:
        # M6: a missing binary / timeout is data, not a crash — callers see _error
        reason = result.describe_failure()
        logger.warning(f"Cloud size query failed: {reason}")
        return {"count": 0, "bytes": 0, "sizeless": "0", "_error": reason}
    if result.exit_code != 0:
        # Non-zero exit: do NOT parse stdout — report the failure honestly
        stderr_output = result.stderr_text().strip() or "no stderr"
        logger.warning(f"Cloud size rclone exited {result.exit_code}: {stderr_output}")
        return {
            "count": 0,
            "bytes": 0,
            "sizeless": "0",
            "_error": f"rclone size exited {result.exit_code}: {stderr_output}",
        }
    try:
        data = json.loads(result.stdout_text().strip())
    except json.JSONDecodeError as e:
        logger.warning(f"Cloud size query returned unparsable output: {e}")
        return {"count": 0, "bytes": 0, "sizeless": "0", "_error": str(e)}
    # Use .get() so a malformed-but-valid JSON response (e.g. {}) doesn't escape as KeyError
    count = data.get("count", 0)
    size_bytes = data.get("bytes", 0)
    logger.info(f"Cloud size: {count} files, {size_bytes} bytes")
    return data


def get_cloud_manifest(bucket: str, fy_prefix: str, config_path: str, *, timeout: int,
                       artefacts: RunArtefacts | None = None) -> list[dict]:
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

    result = run_tool("rclone-manifest", cmd, timeout_s=timeout,
                      artefacts=artefacts or RunArtefacts.ephemeral("cloud-manifest"))
    if result.timed_out:
        raise CloudReporterError(
            f"cloud manifest listing timed out after {timeout}s for {dest}")
    if result.spawn_error:
        if "FileNotFoundError" in result.spawn_error:
            raise CloudReporterError(
                f"rclone not found while listing manifest: {result.spawn_error}")
        raise CloudReporterError(
            f"cloud manifest listing failed for {dest}: {result.spawn_error}")

    if result.exit_code != 0:
        stderr_output = result.stderr_text().strip() or "no stderr"
        raise CloudReporterError(
            f"rclone lsjson exited {result.exit_code} for {dest}: {stderr_output}"
        )

    try:
        data = json.loads(result.stdout_text())
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
    *,
    timeout: int,
    checkers: int,
    modify_window_seconds: int,
    retries: int,
    retries_sleep_seconds: int,
    artefacts: RunArtefacts | None = None,
) -> dict:
    """rclone check --combined --size-only → {added, removed, modified, unchanged}.

    Compares source and GCS by file SIZE only (not MD5 hash) to avoid
    2+ hour re-hashing of 500GB on mechanical HDD. Size comparison is
    sufficient for accounting documents — content changes almost always
    change file size.

    Writes diff to temp file, parses +/-/*/= prefixes, cleans up in finally
    (the combined listing names every unchanged file too; it is not kept).
    Tunables come from config: cloud.diff_timeout_seconds, cloud.checkers,
    cloud.modify_window_seconds, cloud.retry_count, cloud.retries_sleep_seconds.

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
            "--modify-window", f"{int(modify_window_seconds)}s",  # NTFS mtime granularity
            "--checkers", str(int(checkers)),        # Concurrent metadata checkers (GCS rate limits)
            "--retries", str(int(retries)),          # Retry transient network errors
            "--retries-sleep", f"{int(retries_sleep_seconds)}s",  # Back off between retries
            *_base_args(config_path),
        ]

        result = run_tool("rclone-diff", cmd, timeout_s=timeout,
                          artefacts=artefacts or RunArtefacts.ephemeral("cloud-diff"))
        if not result.completed:
            # M6: a timed-out/failed scan must be distinguishable from "no changes"
            reason = result.describe_failure()
            logger.warning(f"Cloud diff query failed: {reason}")
            return {
                "added": [], "removed": [], "modified": [], "unchanged": [],
                "_partial": True, "_error": reason,
            }

        partial = False
        error_msg = None
        if result.exit_code >= 2:
            partial = True
            stderr_output = result.stderr_text().strip() or "no stderr"
            error_msg = f"rclone check exited {result.exit_code}: {stderr_output}"
            logger.warning(f"Cloud diff rclone failed (exit {result.exit_code}): {stderr_output}")

        diff = {"added": [], "removed": [], "modified": [], "unchanged": []}

        try:
            with open(diff_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    # rclone check --combined format: <prefix> <filename>
                    # Prefix: + (added to dest), - (removed from dest), * (modified), = (unchanged)
                    if len(line) >= 3 and line[1] == " ":
                        symbol = line[0]
                        raw_p = line[2:].strip()
                        p = PureWindowsPath(raw_p).as_posix().lstrip("/")
                        if not p or p == ".":
                            continue
                        if symbol == "+":
                            diff["added"].append(p)
                        elif symbol == "-":
                            diff["removed"].append(p)
                        elif symbol == "*":
                            diff["modified"].append(p)
                        elif symbol == "=":
                            diff["unchanged"].append(p)
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

    except OSError as e:
        # M6: a failed scan (e.g. temp diff file) must be distinguishable from "no changes"
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
