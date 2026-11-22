"""Whole-folder cloud restore engine - GCS prefix to a local folder, read-only.

The disaster-recovery use case:
  * the server/disk is gone;
  * the operator picks a destination folder;
  * the tool lists the top-level folders in the bucket (FY25-26, FY26-27,
    archive, ...);
  * the operator picks one folder and the tool restores its **latest live
    state** into `<destination>\\<folder>` and verifies every file by MD5.

Deliberate limits:
  * latest state only - rclone cannot select GCS object generations. Point-in-
    time restore needs `gcloud storage` and is a separate feature.
  * whole top-level folder only - single-file restores are done in the Google
    Console.
  * no deletes, ever: only `rclone copy`; `sync`/`delete`/`move` are never used.
  * the target must be empty unless the caller explicitly allows a merge.
  * a restore without a complete MD5 verification is never reported as success.

This module has no Prefect or manifest-DB dependency so it can run on a clean
DR machine. The dashboard can import `list_fy_folders()` and `restore_fy()`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

from core.cloud_sync import final_stats
from core.cloud_verify import decide_cloud_verify_result
from core.process import resolve_binary
from core.rclone_config import temp_rclone_config
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool, tail_lines

# Control-plane file that is never part of the client's data.
CONTROL_FILES: tuple[str, ...] = (".AAM_TARGET_MOUNTED",)
PARTIAL_SUFFIX = ".aamrestore"
FY_RE = re.compile(r"^FY\d{2}-\d{2}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
_MD5_LINE_RE = re.compile(r"^([0-9a-f]{32})  (.+)$")

_EXIT_OK = 0
_EXIT_DURATION = 10


def _fail(status: str, error: str, **extra) -> dict:
    result = {
        "status": status,
        "ok": False,
        "error": error,
        "verified": False,
        "warnings": [],
    }
    result.update(extra)
    return result


# Characters Windows refuses in a file name (a ':' also makes 'D:evil' drive-relative or an NTFS stream).
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def valid_folder_name(value: str | None) -> bool:
    """A single top-level prefix name (FY, `archive`, etc.), not a path."""
    text = (value or "").strip()
    return (bool(text) and "/" not in text and "\\" not in text and text not in (".", "..")
            and not _ILLEGAL_NAME_CHARS.search(text) and not text.endswith("."))


def _norm(path: str | os.PathLike) -> str:
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    # A local loopback share is the same directory as the drive it exposes.
    for alias, drive in (("\\\\127.0.0.1\\C$", "C:"), ("\\\\localhost\\C$", "C:")):
        if text.lower().startswith(alias.lower()):
            text = drive + text[len(alias):]
            break
    return os.path.normcase(os.path.abspath(text))


def _is_within(child: str | os.PathLike, parent: str | os.PathLike) -> bool:
    try:
        return os.path.commonpath([_norm(child), _norm(parent)]) == _norm(parent)
    except ValueError:
        return False


def _paths_overlap(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    """Equal, one inside the other, or the same directory spelled differently."""
    return _is_within(a, b) or _is_within(b, a)


def _existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _is_reparse_point(path: Path) -> bool:
    try:
        if os.path.islink(path):
            return True
        if os.name == "nt":
            import stat

            return bool(os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return False
    return False


def _find_reparse_points(root: Path, limit: int = 20) -> list[str]:
    found: list[str] = []
    if not root.exists():
        return found
    if _is_reparse_point(root):
        return [str(root)]
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            candidate = Path(dirpath) / name
            if _is_reparse_point(candidate):
                found.append(str(candidate))
                if len(found) >= limit:
                    return found
    return found


def _last_json(text: str) -> dict | None:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _parse_combined(path: Path) -> dict:
    counts = {"=": 0, "+": 0, "-": 0, "*": 0, "!": 0}
    if not path.exists():
        return counts
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line[:1] in counts:
                    counts[line[0]] += 1
    except OSError as exc:
        logger.warning(f"Could not read restore diff {path}: {exc}")
    return counts


def _counts_from_log(text: str) -> dict:
    """rclone check's own summary lines. Missing values stay None (fail closed)."""
    patterns = {
        "differences": r"(\d+)\s+differences found",
        "hashes_missing": r"(\d+)\s+hashes missing",
        "hashes_unchecked": r"(\d+)\s+hashes could not be checked",
        "check_errors": r"(\d+)\s+errors? while checking",
    }
    out: dict = {}
    for key, pattern in patterns.items():
        matches = re.findall(pattern, text or "", flags=re.IGNORECASE)
        out[key] = int(matches[-1]) if matches else None
    return out


@contextmanager
def _rclone_config(
    gcs_key_path: str,
    location: str,
    project_number: str,
    storage_class: str,
    rclone_config_path: str | None,
):
    if rclone_config_path:
        cfg = Path(rclone_config_path)
        if not cfg.is_file():
            raise FileNotFoundError(f"rclone config not found: {cfg}")
        yield str(cfg)
        return
    with temp_rclone_config(gcs_key_path, location, project_number, storage_class) as cfg:
        yield cfg


def _run(
    rclone: str,
    argv: list[str],
    *,
    artefacts: RunArtefacts,
    name: str,
    timeout_s: float | None,
    log_path: Path | None = None,
):
    return run_tool(name, argv, timeout_s=timeout_s, artefacts=artefacts, log_path=log_path)


def _size_of(
    rclone: str,
    config_path: str,
    remote_or_path: str,
    *,
    excludes: Iterable[str],
    artefacts: RunArtefacts,
    label: str,
    timeout_s: float | None = None,
) -> dict | None:
    argv = [rclone, "size", remote_or_path, "--json", "--config", config_path]
    for name in excludes:
        argv.extend(["--exclude", name])
    result = _run(rclone, argv, artefacts=artefacts, name=label, timeout_s=timeout_s)
    if not result.completed or result.exit_code != _EXIT_OK:
        logger.warning(f"{label} failed: {result.describe_failure() or result.stderr_text()[:300]}")
        return None
    data = _last_json(result.stdout_text())
    if data is None:
        return None
    try:
        return {"count": int(data.get("count", 0)), "bytes": int(data.get("bytes", 0))}
    except (TypeError, ValueError):
        return None


def _write_snapshot(
    rclone: str,
    config_path: str,
    remote: str,
    manifest_path: Path,
    *,
    excludes: Iterable[str],
    artefacts: RunArtefacts,
    timeout_s: float | None = None,
) -> tuple[dict | None, str | None]:
    """Create and validate a md5sum manifest. This is the hash-coverage gate."""
    argv = [
        rclone, "md5sum", remote,
        "--output-file", str(manifest_path),
        "--config", config_path,
        "--fast-list",
    ]
    for name in excludes:
        argv.extend(["--exclude", name])
    result = _run(rclone, argv, artefacts=artefacts, name="restore-manifest",
                  timeout_s=timeout_s)
    if not result.completed:
        return None, f"manifest did not finish: {result.describe_failure()}"
    if result.exit_code != _EXIT_OK:
        return None, (
            "could not read MD5 hashes for every object (composite/unhashed objects "
            "cannot be verified): " + tail_lines(result.stderr_text(), 20)
        )
    if not manifest_path.exists():
        return None, "rclone produced no MD5 manifest"

    count = 0
    bad: list[str] = []
    with open(manifest_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line:
                continue
            if not _MD5_LINE_RE.match(line):
                bad.append(line[:200])
                continue
            count += 1
    if bad:
        return None, f"{len(bad)} manifest line(s) have no usable MD5: " + "; ".join(bad[:5])

    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return {"count": count, "sha256": digest, "path": str(manifest_path)}, None


def _case_collisions(manifest_path: Path) -> list[str]:
    seen: dict[str, str] = {}
    collisions: list[str] = []
    with open(manifest_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            match = _MD5_LINE_RE.match(line.rstrip("\r\n"))
            if not match:
                continue
            path = match.group(2)
            key = path.casefold()
            if key in seen and seen[key] != path:
                collisions.append(f"{seen[key]} <-> {path}")
            else:
                seen[key] = path
    return collisions


def validate_target(
    target_dir: str | os.PathLike,
    *,
    allow_non_empty: bool = False,
    forbidden_paths: Iterable[str | os.PathLike] = (),
) -> tuple[Path | None, str | None]:
    """Return (resolved target, error). Does not create anything."""
    try:
        raw = Path(target_dir).expanduser()
        # the junction/symlink check must look at the path as given: resolve() follows it away
        if raw.exists() and _is_reparse_point(raw):
            return None, f"target is a junction/symlink; choose its real folder: {raw}"
        path = raw.resolve(strict=False)
    except OSError as exc:
        return None, f"target path is not usable: {exc}"

    if path.anchor == str(path):
        return None, f"refusing to restore into a drive root: {path}"
    if path.exists() and not path.is_dir():
        return None, f"target exists and is not a folder: {path}"
    if path.exists() and _is_reparse_point(path):
        return None, f"target is a junction/symlink; choose its real folder: {path}"

    for forbidden in forbidden_paths:
        if forbidden and _paths_overlap(path, forbidden):
            return None, (
                f"refusing to restore into a protected path: {path} "
                f"(overlaps {forbidden})"
            )

    try:
        non_empty = path.exists() and any(path.iterdir())
    except OSError as exc:
        return None, f"cannot read target folder: {exc}"
    if non_empty and not allow_non_empty:
        return None, (
            f"target is not empty: {path}. Choose an empty folder, or pass --force "
            "to merge (existing files are never deleted)."
        )

    if path.exists():
        reparse = _find_reparse_points(path)
        if reparse:
            return None, (
                "target contains a junction/symlink; refusing to write through it: "
                + ", ".join(reparse[:5])
            )
    return path, None


def list_fy_folders(
    *,
    bucket: str,
    gcs_key_path: str = "",
    project_number: str = "",
    location: str = "asia-south1",
    storage_class: str = "STANDARD",
    rclone_exe: str | None = None,
    rclone_config_path: str | None = None,
    artefacts: RunArtefacts | None = None,
) -> dict:
    """List the top-level folders in the bucket (read-only).

    Returns every prefix, with `is_fy` as a display hint only. The UI and CLI
    can select any of them (FY, `archive`, or another name); no FY rule is
    enforced on restore.
    """
    if not _BUCKET_RE.match(bucket or ""):
        return {"error": f"invalid bucket name: {bucket!r}", "folders": [], "other_folders": []}
    if not rclone_config_path and not gcs_key_path:
        return {"error": "no GCS key", "folders": [], "other_folders": []}

    own = artefacts is None
    artefacts = artefacts or RunArtefacts.ephemeral("restore-list")
    rclone = str(rclone_exe or resolve_binary("rclone") or "rclone")
    remote = f"aam_gcs:{bucket}"
    try:
        with _rclone_config(gcs_key_path, location, project_number, storage_class,
                            rclone_config_path) as config_path:
            result = _run(
                rclone,
                [rclone, "lsf", remote, "--dirs-only", "--max-depth", "1",
                 "--config", config_path],
                artefacts=artefacts,
                name="restore-list-folders",
                timeout_s=300,
            )
            if not result.completed or result.exit_code != _EXIT_OK:
                return {
                    "error": result.describe_failure() or tail_lines(result.stderr_text(), 10),
                    "folders": [],
                    "other_folders": [],
                }
            names = [
                line.strip().rstrip("/")
                for line in result.stdout_text().splitlines()
                if line.strip()
            ]
            folders = [
                {
                    "name": name,
                    "prefix": name,
                    "remote": f"gs://{bucket}/{name}",
                    "is_fy": bool(FY_RE.fullmatch(name)),
                }
                for name in names
            ]
            folders.sort(key=lambda item: item["name"], reverse=True)
            return {
                "bucket": bucket,
                "remote": f"gs://{bucket}",
                "folders": folders,
                # Deprecated: kept for callers that still read it.
                "other_folders": [],
                "error": None,
            }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not list FY folders")
        return {"error": f"{type(exc).__name__}: {exc}", "folders": [], "other_folders": []}
    finally:
        if own:
            shutil.rmtree(artefacts.dir, ignore_errors=True)


def fy_info(
    fy: str,
    *,
    bucket: str,
    gcs_key_path: str = "",
    project_number: str = "",
    location: str = "asia-south1",
    storage_class: str = "STANDARD",
    rclone_exe: str | None = None,
    rclone_config_path: str | None = None,
    artefacts: RunArtefacts | None = None,
) -> dict:
    """Count and bytes for one top-level folder (FY, archive, etc.)."""
    folder = (fy or "").strip()
    if not valid_folder_name(folder):
        return {"error": f"invalid folder name: {fy!r}"}
    if not rclone_config_path and not gcs_key_path:
        return {"error": "no GCS key"}
    own = artefacts is None
    artefacts = artefacts or RunArtefacts.ephemeral("restore-info")
    rclone = str(rclone_exe or resolve_binary("rclone") or "rclone")
    try:
        with _rclone_config(gcs_key_path, location, project_number, storage_class,
                            rclone_config_path) as config_path:
            size = _size_of(
                rclone, config_path, f"aam_gcs:{bucket}/{folder}",
                excludes=CONTROL_FILES, artefacts=artefacts, label="restore-info",
            )
        if size is None:
            return {"error": "could not read the folder", "folder": folder}
        return {"folder": folder, "fy": folder, **size, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "folder": folder}
    finally:
        if own:
            shutil.rmtree(artefacts.dir, ignore_errors=True)


def _target_summary(
    path: Path, rclone: str, config_path: str, excludes: Iterable[str],
    artefacts: RunArtefacts, label: str,
    timeout_s: float | None = None,
) -> dict | None:
    if not path.exists():
        return {"count": 0, "bytes": 0}
    return _size_of(
        rclone, config_path, str(path),
        excludes=[*excludes, f"*{PARTIAL_SUFFIX}"],
        artefacts=artefacts, label=label, timeout_s=timeout_s,
    )


def restore_from_cloud(
    target_dir: str | os.PathLike,
    *,
    bucket: str,
    prefix: str,
    fy: str | None = None,
    gcs_key_path: str = "",
    project_number: str = "",
    location: str = "asia-south1",
    storage_class: str = "STANDARD",
    rclone_exe: str | None = None,
    rclone_config_path: str | None = None,
    transfers: int = 8,
    checkers: int = 8,
    timeout_s: float | None = None,
    allow_non_empty: bool = False,
    dry_run: bool = False,
    verify_only: bool = False,
    skip_space_check: bool = False,
    forbidden_paths: Iterable[str | os.PathLike] = (),
    exclude_control: bool = True,
    artefacts: RunArtefacts | None = None,
) -> dict:
    """Restore one top-level folder prefix to a local folder and verify it by MD5.

    `status`: RESTORED_VERIFIED, RESTORED_UNVERIFIED, VERIFIED, DRY_RUN,
    INCOMPLETE (time-boxed, resumable) or FAILED.
    """
    started = time.monotonic()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    artefacts = artefacts or RunArtefacts.ephemeral("restore")
    report_path = Path(artefacts.dir) / "restore-report.json"

    def finish(result: dict) -> dict:
        result.setdefault("fy", fy or prefix)
        result.setdefault("bucket", bucket)
        result.setdefault("prefix", prefix)
        result.setdefault("remote", f"gs://{bucket}/{prefix}")
        result.setdefault("target", str(target_dir))
        result.setdefault("dry_run", dry_run)
        result.setdefault("verify_only", verify_only)
        result.setdefault("started_at", started_at)
        result.setdefault("ended_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        result.setdefault("duration_s", round(time.monotonic() - started, 1))
        result.setdefault("artefacts_dir", str(artefacts.dir))
        result.setdefault("report_path", str(report_path))
        try:
            report_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            result.setdefault("warnings", []).append(f"could not write report: {exc}")
        return result

    prefix = (prefix or "").strip().strip("/\\")
    if not _BUCKET_RE.match(bucket or ""):
        return finish(_fail("FAILED", f"invalid GCS bucket name: {bucket!r}"))
    if not valid_folder_name(prefix):
        return finish(_fail("FAILED", f"invalid folder prefix: {prefix!r}"))
    if not rclone_config_path and not gcs_key_path:
        return finish(_fail("FAILED", "no GCS key: pass --key or configure paths.gcs_key_path"))

    target, target_error = validate_target(
        target_dir,
        allow_non_empty=allow_non_empty or verify_only,
        forbidden_paths=forbidden_paths,
    )
    if target_error:
        return finish(_fail("FAILED", target_error))
    assert target is not None  # noqa: S101 - explicit check above

    rclone = str(rclone_exe or resolve_binary("rclone") or "rclone")
    excludes = list(CONTROL_FILES) if exclude_control else []
    remote = f"aam_gcs:{bucket}/{prefix}"
    warnings: list[str] = []

    try:
        with _rclone_config(gcs_key_path, location, project_number, storage_class,
                            rclone_config_path) as config_path:
            # 1. Size + snapshot manifest; hash coverage is mandatory.
            remote_size = _size_of(
                rclone, config_path, remote, excludes=excludes,
                artefacts=artefacts, label="restore-size", timeout_s=timeout_s,
            )
            if remote_size is None:
                return finish(_fail("FAILED", "could not read the cloud prefix"))
            if remote_size["count"] == 0:
                return finish(_fail(
                    "FAILED", f"the cloud prefix is empty or wrong: {remote}",
                    remote_count=0, remote_bytes=0,
                ))

            manifest_path = Path(artefacts.new_path("snapshot", "md5"))
            snapshot, manifest_error = _write_snapshot(
                rclone, config_path, remote, manifest_path,
                excludes=excludes, artefacts=artefacts, timeout_s=timeout_s,
            )
            if snapshot is None:
                return finish(_fail("FAILED", manifest_error or "snapshot manifest failed"))
            if snapshot["count"] != remote_size["count"]:
                return finish(_fail(
                    "FAILED",
                    f"manifest count {snapshot['count']} != object count "
                    f"{remote_size['count']} (unhashed objects cannot be verified)",
                    remote_count=remote_size["count"], remote_bytes=remote_size["bytes"],
                ))
            collisions = _case_collisions(manifest_path)
            if collisions:
                return finish(_fail(
                    "FAILED",
                    "case-insensitive filename collision(s) would overwrite files on "
                    "Windows: " + "; ".join(collisions[:5]),
                ))

            base = {
                "fy": fy or prefix,
                "remote_count": remote_size["count"],
                "remote_bytes": remote_size["bytes"],
                "snapshot_manifest": str(manifest_path),
                "snapshot_sha256": snapshot["sha256"],
            }

            # 2. Free space: 10% + 1 GiB + 4 KiB per file.
            if not skip_space_check and not verify_only:
                need = int(remote_size["bytes"] * 1.10) + 1024 ** 3 + 4096 * remote_size["count"]
                try:
                    free = shutil.disk_usage(_existing_parent(target)).free
                    base["free_bytes"] = free
                    base["needed_bytes"] = need
                    if free < need:
                        message = (
                            f"not enough free space at {target}: need {need / 2**30:.1f} GiB, "
                            f"have {free / 2**30:.1f} GiB"
                        )
                        if not dry_run:
                            return finish(_fail("FAILED", message, **base))
                        warnings.append(message)
                except OSError as exc:
                    warnings.append(f"free-space check unavailable: {exc}")

            # 3. Verify-only: compare the target to the fresh snapshot.
            if verify_only:
                if not target.exists():
                    return finish(_fail(
                        "FAILED", f"target does not exist: {target}", **base,
                    ))
                counts, verdict, verify_exit = _verify(
                    rclone, config_path, remote, manifest_path, target,
                    excludes=excludes, checkers=checkers, artefacts=artefacts,
                    timeout_s=timeout_s,
                )
                ok = _verify_gate(counts, verdict, allow_extra=False)
                return finish({
                    **base,
                    "status": "VERIFIED" if ok else "FAILED",
                    "ok": ok,
                    "verified": ok,
                    "differences": counts.get("differences"),
                    "hashes_missing": counts.get("hashes_missing"),
                    "hashes_unchecked": counts.get("hashes_unchecked"),
                    "check_errors": counts.get("check_errors"),
                    "matched": counts.get("=", 0),
                    "missing_at_target": counts.get("+", 0),
                    "modified_at_target": counts.get("*", 0),
                    "extra_at_target": counts.get("-", 0),
                    "verify_exit": verify_exit,
                    "warnings": warnings,
                    "error": None if ok else _verify_reason(counts),
                })

            # 4. Dry-run: no writes, no target creation.
            if dry_run:
                return finish({
                    **base,
                    "status": "DRY_RUN",
                    "ok": True,
                    "verified": False,
                    "differences": None,
                    "target_before": _target_summary(
                        target, rclone, config_path, excludes, artefacts,
                        "restore-size-before", timeout_s=timeout_s,
                    ),
                    "warnings": warnings + ["dry-run: no files were written"],
                })

            # 5. Copy. Never sync, never delete.
            target.mkdir(parents=True, exist_ok=True)
            target_before = _target_summary(
                target, rclone, config_path, excludes, artefacts,
                "restore-size-before", timeout_s=timeout_s,
            )
            base["target_before"] = target_before
            if target_before and target_before.get("count"):
                warnings.append("target was not empty; files were merged and nothing was deleted")

            copy_log = Path(artefacts.new_path("copy", "ndjson"))
            copy_argv = [
                rclone, "copy", remote, str(target),
                "--config", config_path,
                "--checksum",
                "--fast-list",
                "--transfers", str(transfers),
                "--checkers", str(checkers),
                "--partial-suffix", PARTIAL_SUFFIX,
                "--retries", "5",
                "--retries-sleep", "30s",
                "--contimeout", "30s",
                "--use-json-log",
                "--log-level", "INFO",
                "--log-file", str(copy_log),
                "--stats", "30s",
            ]
            for name in excludes:
                copy_argv.extend(["--exclude", name])
            if timeout_s is not None:
                max_duration = max(int(timeout_s) - 120, 60)
                copy_argv.extend(["--max-duration", f"{max_duration}s",
                                  "--cutoff-mode", "SOFT"])
            copy_timeout = None if timeout_s is None else timeout_s + 120

            copy_res = _run(
                rclone, copy_argv, artefacts=artefacts, name="restore-copy",
                timeout_s=copy_timeout, log_path=copy_log,
            )
            copy_stats = final_stats(copy_res.log_text()) or final_stats(copy_res.stderr_text())
            base["copy_exit"] = copy_res.exit_code
            base["copy_log"] = str(copy_log)
            base["copy_stats"] = copy_stats
            if isinstance(copy_stats, dict):
                base["transferred_files"] = copy_stats.get("transfers")
                base["transferred_bytes"] = copy_stats.get("bytes")
            if not copy_res.completed:
                return finish(_fail(
                    "FAILED", f"copy did not finish: {copy_res.describe_failure()}", **base,
                ))
            if copy_res.exit_code == _EXIT_DURATION:
                return finish({
                    **base,
                    "status": "INCOMPLETE",
                    "ok": False,
                    "verified": False,
                    "warnings": warnings + [
                        "copy reached the time budget; re-run the same command "
                        "with --force to resume"
                    ],
                    "error": "time budget reached (rclone exit 10)",
                })
            if copy_res.exit_code != _EXIT_OK:
                return finish(_fail(
                    "FAILED",
                    f"rclone copy exited {copy_res.exit_code}: "
                    + tail_lines(copy_res.stderr_text(), 20),
                    **base,
                ))
            if isinstance(copy_stats, dict) and copy_stats.get("errors"):
                return finish(_fail(
                    "FAILED", f"rclone reported {copy_stats.get('errors')} transfer error(s)",
                    **base,
                ))

            # 6. Verify against the pinned snapshot.
            counts, verdict, verify_exit = _verify(
                rclone, config_path, remote, manifest_path, target,
                excludes=excludes, checkers=checkers, artefacts=artefacts,
                timeout_s=timeout_s,
            )
            allow_extra = bool(allow_non_empty)
            ok = _verify_gate(counts, verdict, allow_extra=allow_extra)
            if counts.get("-"):
                warnings.append(
                    f"{counts['-']} extra local file(s) were left untouched"
                )
            target_after = _target_summary(
                target, rclone, config_path, excludes, artefacts,
                "restore-size-after", timeout_s=timeout_s,
            )
            base["target_after"] = target_after

            return finish({
                **base,
                "status": "RESTORED_VERIFIED" if ok else "RESTORED_UNVERIFIED",
                "ok": ok,
                "verified": ok,
                "differences": counts.get("differences"),
                "hashes_missing": counts.get("hashes_missing"),
                "hashes_unchecked": counts.get("hashes_unchecked"),
                "check_errors": counts.get("check_errors"),
                "matched": counts.get("=", 0),
                "missing_at_target": counts.get("+", 0),
                "modified_at_target": counts.get("*", 0),
                "extra_at_target": counts.get("-", 0),
                "verify_exit": verify_exit,
                "target_after": target_after,
                "warnings": warnings,
                "error": None if ok else _verify_reason(counts),
            })
    except Exception as exc:  # noqa: BLE001 - a restore must return a report
        logger.exception("Restore failed")
        return finish(_fail("FAILED", f"{type(exc).__name__}: {exc}"))


def _verify(
    rclone: str,
    config_path: str,
    remote: str,
    manifest_path: Path,
    target: Path,
    *,
    excludes: Iterable[str],
    checkers: int,
    artefacts: RunArtefacts,
    timeout_s: float | None = None,
) -> tuple[dict, dict, int | None]:
    diff_path = Path(artefacts.new_path("verify-diff", "txt"))
    verify_log = Path(artefacts.new_path("verify", "ndjson"))
    argv = [
        rclone, "check",
        "--checkfile", "md5", str(manifest_path), str(target),
        "--config", config_path,
        "--combined", str(diff_path),
        "--checkers", str(checkers),
        "--retries", "1",
        "--use-json-log",
        "--log-level", "INFO",
        "--log-file", str(verify_log),
    ]
    for name in [*excludes, f"*{PARTIAL_SUFFIX}"]:
        argv.extend(["--exclude", name])
    result = _run(rclone, argv, artefacts=artefacts, name="restore-verify",
                  timeout_s=timeout_s, log_path=verify_log)
    log_text = f"{result.stderr_text()}\n{result.stdout_text()}\n{result.log_text()}"
    counts = _parse_combined(diff_path)
    counts.update(_counts_from_log(log_text))
    for key in ("hashes_missing", "hashes_unchecked", "check_errors"):
        if counts.get(key) is None:
            counts[key] = 0
    verdict = decide_cloud_verify_result(result.exit_code, log_text, "")
    return counts, verdict, result.exit_code


def _verify_gate(counts: dict, verdict: dict, *, allow_extra: bool) -> bool:
    if not verdict.get("completion"):
        return False
    differences = counts.get("differences")
    if differences is None:
        return False
    extra = counts.get("-", 0)
    # With --checkfile an extra local file has no manifest hash, so rclone
    # reports it as "hash missing" and "error while checking". That is not a
    # missing backup file; a real missing file is a '+' line.
    allowed_noise = extra if allow_extra else 0
    if counts.get("hashes_missing") not in (None, 0, allowed_noise):
        return False
    if counts.get("hashes_unchecked") not in (None, 0):
        return False
    if counts.get("check_errors") not in (None, 0, allowed_noise):
        return False
    if counts.get("+", 0) or counts.get("*", 0) or counts.get("!", 0):
        return False
    if extra and not allow_extra:
        return False
    expected = counts.get("+", 0) + counts.get("*", 0) + counts.get("!", 0) + allowed_noise
    if differences != expected:
        return False
    return allow_extra or bool(verdict.get("verified"))


def _verify_reason(counts: dict) -> str:
    if counts.get("hashes_unchecked"):
        return (
            f"{counts['hashes_unchecked']} file(s) could not be hash-checked "
            "(objects without MD5 cannot be trusted)"
        )
    if counts.get("hashes_missing"):
        return f"{counts['hashes_missing']} file(s) missing from the target"
    if counts.get("+") or counts.get("*") or counts.get("!"):
        return (
            f"verification failed: missing={counts.get('+', 0)}, "
            f"different={counts.get('*', 0)}, errors={counts.get('!', 0)}"
        )
    if counts.get("-") :
        return f"{counts['-']} extra local file(s) found"
    return "rclone check did not prove the restore"


def restore_fy(
    target_parent: str | os.PathLike,
    *,
    fy: str,
    bucket: str,
    gcs_key_path: str = "",
    project_number: str = "",
    location: str = "asia-south1",
    storage_class: str = "STANDARD",
    rclone_exe: str | None = None,
    rclone_config_path: str | None = None,
    transfers: int = 8,
    checkers: int = 8,
    timeout_s: float | None = None,
    into: bool = False,
    allow_non_empty: bool = False,
    dry_run: bool = False,
    verify_only: bool = False,
    skip_space_check: bool = False,
    forbidden_paths: Iterable[str | os.PathLike] = (),
    exclude_control: bool = True,
    artefacts: RunArtefacts | None = None,
) -> dict:
    """Restore a whole top-level folder into `<target_parent>\\<folder>`.

    The folder can be an FY (`FY26-27`) or any other bucket prefix
    (`archive`, etc.). `list_fy_folders()` gives the names to offer.
    """
    folder = (fy or "").strip()
    if not valid_folder_name(folder):
        return _fail(
            "FAILED",
            f"choose a folder name such as FY26-27 or archive (got {fy!r}); "
            "use --list to see them",
        )
    if FY_RE.fullmatch(folder.upper()):
        folder = folder.upper()
    parent = Path(target_parent).expanduser()
    if not parent.exists() or not parent.is_dir():
        return _fail(
            "FAILED",
            f"destination folder does not exist: {parent}. Create/select it first.",
        )
    target = parent if into else parent / folder
    result = restore_from_cloud(
        target,
        bucket=bucket,
        prefix=folder,
        fy=folder,
        gcs_key_path=gcs_key_path,
        project_number=project_number,
        location=location,
        storage_class=storage_class,
        rclone_exe=rclone_exe,
        rclone_config_path=rclone_config_path,
        transfers=transfers,
        checkers=checkers,
        timeout_s=timeout_s,
        allow_non_empty=allow_non_empty,
        dry_run=dry_run,
        verify_only=verify_only,
        skip_space_check=skip_space_check,
        forbidden_paths=forbidden_paths,
        exclude_control=exclude_control,
        artefacts=artefacts,
    )
    result["folder"] = folder
    result["fy"] = folder
    result["target_parent"] = str(parent)
    result["into"] = into
    return result
