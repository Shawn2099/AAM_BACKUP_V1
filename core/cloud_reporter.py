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


def _base_args(config_path: str) -> list[str]:
    return ["--config", config_path, "--gcs-no-check-bucket", "--fast-list"]


def get_cloud_size(bucket: str, fy_prefix: str, config_path: str) -> dict:
    """rclone size → {"count": int, "bytes": int, "sizeless": str}.

    Instant — GCS returns pre-computed object counts.
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    cmd = ["rclone", "size", dest, "--json", *_base_args(config_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout.strip())
        logger.info(f"Cloud size: {data['count']} files, {data['bytes']} bytes")
        return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Cloud size query failed: {e}")
        return {"count": 0, "bytes": 0, "sizeless": "0"}


def get_cloud_manifest(bucket: str, fy_prefix: str, config_path: str) -> list[dict]:
    """rclone lsjson -R → [{Path, Size, ModTime, MimeType, IsDir}, ...].

    Files only — directory entries filtered out.
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    cmd = ["rclone", "lsjson", dest, "-R", *_base_args(config_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        data = json.loads(result.stdout)
        files = [f for f in data if not f.get("IsDir")]
        logger.info(f"Cloud manifest: {len(files)} files")
        return files
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        logger.warning(f"Cloud manifest query failed: {e}")
        return []


def get_cloud_diff(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
) -> dict:
    """rclone check --combined → {added, removed, modified, unchanged}.

    Writes diff to temp file, parses +/-/*/= prefixes, cleans up in finally.

    Returns:
        {"added": [...], "removed": [...], "modified": [...], "unchanged": [...]}
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    diff_file = None

    try:
        fd, diff_file = tempfile.mkstemp(suffix=".txt", prefix="cloud_diff_")
        os.close(fd)  # Release handle so rclone can write to it

        cmd = [
            "rclone", "check",
            source, dest,
            "--combined", diff_file,
            *_base_args(config_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        diff = {"added": [], "removed": [], "modified": [], "unchanged": []}

        try:
            with open(diff_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if line[0] == "+":
                        diff["added"].append(line[2:])
                    elif line[0] == "-":
                        diff["removed"].append(line[2:])
                    elif line[0] == "*":
                        diff["modified"].append(line[2:])
                    elif line[0] == "=":
                        diff["unchanged"].append(line[2:])
        except FileNotFoundError:
            pass

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
