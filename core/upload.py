"""Bulk upload of one local FY folder to GCS - copy-only, resumable, no timeout.

Never deletes: only `rclone copy` (no sync/move/delete flags). The destination
prefix is always `<bucket>/<FY>`, validated, so an old FY upload cannot write
into another year. Verification is MD5: every local source file must exist in
GCS with the same size and hash.

No timeout by default. An optional `timeout_s` adds a SOFT `--max-duration`
(rclone exit 10 means INCOMPLETE, re-run to resume). `run_tool(timeout_s=None)`
does not impose a deadline.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

from core.cloud_sync import final_stats
from core.cloud_verify import decide_cloud_verify_result
from core.guards import require_source_files
from core.process import resolve_binary
from core.rclone_config import temp_rclone_config
from core.restore import (
    CONTROL_FILES,
    FY_RE,
    _counts_from_log,
    _parse_combined,
)
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool, tail_lines
from models.config import CloudConfig, model_default

PARTIAL_SUFFIX = ".aamupload"
STORAGE_CLASSES = ("STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
_EXIT_OK = 0
_EXIT_DURATION = 10
_EXIT_DIR_NOT_FOUND = 3


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


@contextmanager
def _rclone_config(gcs_key_path, location, project_number, storage_class,
                   rclone_config_path):
    if rclone_config_path:
        cfg = Path(rclone_config_path)
        if not cfg.is_file():
            raise FileNotFoundError(f"rclone config not found: {cfg}")
        yield str(cfg)
        return
    with temp_rclone_config(gcs_key_path, location, project_number,
                            storage_class) as cfg:
        yield cfg


def _run(rclone, argv, *, artefacts, name, timeout_s, log_path=None):
    return run_tool(name, argv, timeout_s=timeout_s, artefacts=artefacts,
                    log_path=log_path)


def _size(rclone, config_path, target, *, excludes, artefacts, label,
          timeout_s, missing_ok=False):
    argv = [rclone, "size", target, "--json", "--config", config_path]
    for name in excludes:
        argv.extend(["--exclude", name])
    result = _run(rclone, argv, artefacts=artefacts, name=label,
                  timeout_s=timeout_s)
    if result.exit_code == _EXIT_DIR_NOT_FOUND and missing_ok:
        return {"count": 0, "bytes": 0}
    if not result.completed or result.exit_code != _EXIT_OK:
        logger.warning(f"{label} failed: "
                       f"{result.describe_failure() or result.stderr_text()[:300]}")
        return None
    data = _last_json(result.stdout_text())
    if data is None:
        return None
    try:
        return {"count": int(data.get("count", 0)),
                "bytes": int(data.get("bytes", 0))}
    except (TypeError, ValueError):
        return None


def list_local_fy_folders(source_root: str | os.PathLike) -> dict:
    root = Path(source_root).expanduser()
    if not root.is_dir():
        return {"error": f"source folder does not exist: {root}", "folders": []}
    folders = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        try:
            if not entry.is_dir() or entry.is_symlink():
                continue
        except OSError:
            continue
        name = entry.name.upper()
        if FY_RE.fullmatch(name):
            folders.append({"name": name, "path": str(entry)})
    return {"source": str(root), "folders": folders, "error": None}


def resolve_local_fy(source: str | os.PathLike, fy: str) -> Path:
    root = Path(source).expanduser()
    if not root.is_dir():
        raise ValueError(f"source folder does not exist: {root}")
    candidate = root if root.name.upper() == fy else root / fy
    if not candidate.is_dir():
        raise ValueError(f"{fy} not found under {root}")
    if candidate.is_symlink():
        raise ValueError(f"source FY folder is a link/junction: {candidate}")
    if os.name == "nt":
        try:
            import stat
            if os.lstat(candidate).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"source FY folder is a link/junction: {candidate}")
        except AttributeError:
            pass
    if candidate.name.upper() != fy:
        raise ValueError(f"source folder {candidate} is not {fy}")
    return candidate


def count_planned_copies(log_text: str) -> int:
    planned = 0
    for line in (log_text or "").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("skipped") == "copy":
            planned += 1
    return planned


def _copy_argv(rclone, source, remote, config_path, storage_class, *,
               transfers, checkers, bwlimit, retries, retries_sleep,
               stats_interval, excludes, log_path, timeout_s, dry_run):
    argv = [
        rclone, "copy", str(source), remote,
        "--config", config_path,
        "--checksum",
        "--check-first",
        "--fast-list",
        "--gcs-storage-class", storage_class,
        "--transfers", str(transfers),
        "--checkers", str(checkers),
        "--partial-suffix", PARTIAL_SUFFIX,
        "--retries", str(retries),
        "--retries-sleep", f"{int(retries_sleep)}s",
        "--contimeout", "30s",
        "--use-json-log",
        "--log-level", "INFO",
        "--log-file", str(log_path),
        "--stats", f"{int(stats_interval)}s",
    ]
    if bwlimit:
        argv.extend(["--bwlimit", bwlimit])
    for name in excludes:
        argv.extend(["--exclude", name])
    if timeout_s is not None:
        max_duration = max(int(timeout_s) - 120, 60)
        argv.extend(["--max-duration", f"{max_duration}s",
                     "--cutoff-mode", "SOFT"])
    if dry_run:
        argv.append("--dry-run")
    return argv


def _check(rclone, config_path, source, remote, *, excludes, checkers,
           artefacts, timeout_s):
    diff_path = Path(artefacts.new_path("upload-verify-diff", "txt"))
    log_path = Path(artefacts.new_path("upload-verify", "ndjson"))
    argv = [
        rclone, "check", str(source), remote,
        "--one-way",
        "--config", config_path,
        "--combined", str(diff_path),
        "--checkers", str(checkers),
        "--retries", "1",
        "--fast-list",
        "--use-json-log",
        "--log-level", "INFO",
        "--log-file", str(log_path),
    ]
    for name in [*excludes, f"*{PARTIAL_SUFFIX}"]:
        argv.extend(["--exclude", name])
    result = _run(rclone, argv, artefacts=artefacts, name="upload-verify",
                  timeout_s=timeout_s, log_path=log_path)
    log_text = "\n".join(t for t in (
        result.stderr_text(), result.stdout_text(), result.log_text()) if t)
    counts = _parse_combined(diff_path)
    counts.update(_counts_from_log(log_text))
    for key in ("hashes_missing", "hashes_unchecked", "check_errors"):
        if counts.get(key) is None:
            counts[key] = 0
    verdict = decide_cloud_verify_result(result.exit_code, log_text, "")
    return counts, verdict, result


def _gate(counts: dict, verdict: dict, expected: int) -> bool:
    if not verdict.get("completion") or not verdict.get("verified"):
        return False
    if counts.get("+", 0) or counts.get("*", 0) or counts.get("!", 0):
        return False
    if counts.get("hashes_missing") or counts.get("hashes_unchecked"):
        return False
    if counts.get("=", 0) != expected:
        return False
    expected_diff = counts.get("+", 0) + counts.get("*", 0) + counts.get("!", 0)
    return counts.get("differences") == expected_diff


def _reason(counts: dict) -> str:
    if counts.get("hashes_missing"):
        return (f"{counts['hashes_missing']} object(s) have no MD5 in GCS "
                "(composite objects cannot be verified)")
    if counts.get("hashes_unchecked"):
        return f"{counts['hashes_unchecked']} file(s) could not be hash-checked"
    if counts.get("+"):
        return f"{counts['+']} file(s) missing from the bucket"
    if counts.get("*"):
        return f"{counts['*']} file(s) differ from the source"
    if counts.get("!"):
        return f"{counts['!']} error(s) while checking"
    return "rclone check did not prove the upload"


def upload_fy(
    local_fy_dir: str | os.PathLike,
    *,
    bucket: str,
    fy: str,
    storage_class: str = "ARCHIVE",
    gcs_key_path: str = "",
    project_number: str = "",
    location: str = model_default(CloudConfig, "location"),
    rclone_exe: str | None = None,
    rclone_config_path: str | None = None,
    transfers: int = 4,
    checkers: int = 8,
    bwlimit: str | None = None,
    retries: int = 3,
    retries_sleep: int = 30,
    stats_interval: int = 30,
    timeout_s: float | None = None,
    dry_run: bool = False,
    verify_only: bool = False,
    exclude_control: bool = True,
    artefacts: RunArtefacts | None = None,
) -> dict:
    started = time.monotonic()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    artefacts = artefacts or RunArtefacts.ephemeral("upload")
    report_path = Path(artefacts.dir) / "upload-report.json"
    fy = (fy or "").strip().upper()
    storage_class = (storage_class or "").strip().upper()

    def finish(result: dict) -> dict:
        result.setdefault("fy", fy)
        result.setdefault("bucket", bucket)
        result.setdefault("remote", f"gs://{bucket}/{fy}")
        result.setdefault("source", str(local_fy_dir))
        result.setdefault("storage_class", storage_class)
        result.setdefault("dry_run", dry_run)
        result.setdefault("verify_only", verify_only)
        result.setdefault("started_at", started_at)
        result.setdefault("ended_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        result.setdefault("duration_s", round(time.monotonic() - started, 1))
        result.setdefault("artefacts_dir", str(artefacts.dir))
        result.setdefault("report_path", str(report_path))
        try:
            report_path.write_text(json.dumps(result, indent=2, default=str),
                                   encoding="utf-8")
        except OSError as exc:
            result.setdefault("warnings", []).append(f"could not write report: {exc}")
        return result

    if not FY_RE.fullmatch(fy):
        return finish(_fail("FAILED",
                            f"choose one FY folder such as FY24-25 (got {fy!r})"))
    if storage_class not in STORAGE_CLASSES:
        return finish(_fail("FAILED",
                            f"storage class must be one of {', '.join(STORAGE_CLASSES)}"))
    if not _BUCKET_RE.match(bucket or ""):
        return finish(_fail("FAILED", f"invalid GCS bucket name: {bucket!r}"))
    if not rclone_config_path and not gcs_key_path:
        return finish(_fail("FAILED", "no GCS key: pass --key or configure paths.gcs_key_path"))
    try:
        source = resolve_local_fy(local_fy_dir, fy)
        require_source_files(str(source))
    except (OSError, ValueError) as exc:
        return finish(_fail("FAILED", str(exc)))

    rclone = str(rclone_exe or resolve_binary("rclone") or "rclone")
    excludes = list(CONTROL_FILES) if exclude_control else []
    remote = f"aam_gcs:{bucket}/{fy}"
    if not remote.rstrip("/").endswith("/" + fy):
        return finish(_fail("FAILED", f"refusing a remote prefix outside {fy}: {remote}"))
    copy_log: Path | None = None
    warnings: list[str] = []

    try:
        with _rclone_config(gcs_key_path, location, project_number,
                            storage_class, rclone_config_path) as config_path:
            source_size = _size(
                rclone, config_path, str(source), excludes=excludes,
                artefacts=artefacts, label="upload-source-size",
                timeout_s=timeout_s,
            )
            if source_size is None:
                return finish(_fail("FAILED", "could not measure the source folder"))
            if source_size["count"] == 0:
                return finish(_fail("FAILED", f"source folder is empty: {source}"))

            remote_before = _size(
                rclone, config_path, remote, excludes=excludes,
                artefacts=artefacts, label="upload-remote-before",
                timeout_s=timeout_s, missing_ok=True,
            )
            base = {
                "fy": fy,
                "storage_class": storage_class,
                "source_count": source_size["count"],
                "source_bytes": source_size["bytes"],
                "remote_before": remote_before,
            }
            if remote_before and remote_before.get("count"):
                warnings.append(
                    f"destination already has {remote_before['count']} object(s); "
                    "existing objects keep their storage class and are overwritten "
                    "only when content differs"
                )

            # Verify-only: no copy.
            if verify_only:
                counts, verdict, check_res = _check(
                    rclone, config_path, source, remote,
                    excludes=excludes, checkers=checkers, artefacts=artefacts,
                    timeout_s=timeout_s,
                )
                ok = _gate(counts, verdict, source_size["count"])
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
                    "missing_at_remote": counts.get("+", 0),
                    "modified_at_remote": counts.get("*", 0),
                    "verify_exit": check_res.exit_code,
                    "warnings": warnings,
                    "error": None if ok else _reason(counts),
                })

            # Dry-run: no writes.
            if dry_run:
                copy_log = Path(artefacts.new_path("upload-copy", "ndjson"))
                argv = _copy_argv(
                    rclone, source, remote, config_path, storage_class,
                    transfers=transfers, checkers=checkers, bwlimit=bwlimit,
                    retries=retries, retries_sleep=retries_sleep,
                    stats_interval=stats_interval, excludes=excludes,
                    log_path=copy_log, timeout_s=timeout_s, dry_run=True,
                )
                res = _run(rclone, argv, artefacts=artefacts, name="upload-copy",
                           timeout_s=None if timeout_s is None else timeout_s + 120,
                           log_path=copy_log)
                planned = count_planned_copies(res.log_text())
                return finish({
                    **base,
                    "status": "DRY_RUN",
                    "ok": True,
                    "verified": False,
                    "planned_transfers": planned,
                    "warnings": warnings + ["dry-run: no objects were written"],
                    "copy_exit": res.exit_code,
                    "copy_log": str(copy_log),
                })

            # Real copy.
            copy_log = Path(artefacts.new_path("upload-copy", "ndjson"))
            argv = _copy_argv(
                rclone, source, remote, config_path, storage_class,
                transfers=transfers, checkers=checkers, bwlimit=bwlimit,
                retries=retries, retries_sleep=retries_sleep,
                stats_interval=stats_interval, excludes=excludes,
                log_path=copy_log, timeout_s=timeout_s, dry_run=False,
            )
            res = _run(rclone, argv, artefacts=artefacts, name="upload-copy",
                       timeout_s=None if timeout_s is None else timeout_s + 120,
                       log_path=copy_log)
            stats = final_stats(res.log_text()) or final_stats(res.stderr_text())
            base.update({
                "copy_exit": res.exit_code,
                "copy_log": str(copy_log),
                "copy_stats": stats,
            })
            if isinstance(stats, dict):
                base["transferred_files"] = stats.get("transfers")
                base["transferred_bytes"] = stats.get("bytes")
            if not res.completed:
                return finish(_fail(
                    "FAILED", f"upload did not finish: {res.describe_failure()}", **base,
                ))
            if res.exit_code == _EXIT_DURATION:
                return finish({
                    **base,
                    "status": "INCOMPLETE",
                    "ok": False,
                    "verified": False,
                    "warnings": warnings + [
                        "upload reached the time budget; re-run the same command to resume"
                    ],
                    "error": "time budget reached (rclone exit 10)",
                })
            if res.exit_code != _EXIT_OK:
                return finish(_fail(
                    "FAILED",
                    f"rclone copy exited {res.exit_code}: "
                    + tail_lines(res.stderr_text(), 20),
                    **base,
                ))
            if isinstance(stats, dict) and stats.get("errors"):
                return finish(_fail(
                    "FAILED", f"rclone reported {stats.get('errors')} transfer error(s)",
                    **base,
                ))

            # Verify every source file exists in GCS with the same MD5.
            counts, verdict, check_res = _check(
                rclone, config_path, source, remote,
                excludes=excludes, checkers=checkers, artefacts=artefacts,
                timeout_s=timeout_s,
            )
            ok = _gate(counts, verdict, source_size["count"])
            return finish({
                **base,
                "status": "UPLOADED_VERIFIED" if ok else "UPLOADED_UNVERIFIED",
                "ok": ok,
                "verified": ok,
                "differences": counts.get("differences"),
                "hashes_missing": counts.get("hashes_missing"),
                "hashes_unchecked": counts.get("hashes_unchecked"),
                "check_errors": counts.get("check_errors"),
                "matched": counts.get("=", 0),
                "missing_at_remote": counts.get("+", 0),
                "modified_at_remote": counts.get("*", 0),
                "verify_exit": check_res.exit_code,
                "warnings": warnings,
                "error": None if ok else _reason(counts),
            })
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, Exception):
            logger.exception("Upload failed")
            return finish(_fail("FAILED", f"{type(exc).__name__}: {exc}"))
        logger.warning(f"Upload interrupted ({type(exc).__name__}); uploaded objects are kept")
        finish({
            "status": "CANCELLED",
            "ok": False,
            "verified": False,
            "resumable": True,
            "warnings": warnings + ["interrupted; re-run the same command to resume"],
            "error": f"{type(exc).__name__}",
        })
        raise
