#!/usr/bin/env python
"""Bulk upload one local folder to Google Cloud Storage.

    python upload_fy.py --config C:\\AAMBackup\\config.yaml --list-local
    python upload_fy.py --config C:\\AAMBackup\\config.yaml ^
        --source E:\\Archive --fy FY24-25 --tier ARCHIVE
    python upload_fy.py --config C:\\AAMBackup\\config.yaml ^
        --source E:\\Archive --fy archive --tier COLDLINE --dry-run

The destination is always `gs://<bucket>/<folder>`, where the folder is an FY
(`FY24-25`) or any other folder name (`archive`). Only `rclone copy` is used:
nothing is deleted, and an interrupted run resumes by re-running the command.
There is no time limit unless `--timeout` is supplied.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from loguru import logger

from core.upload import list_local_fy_folders, resolve_local_folder, upload_fy


def _candidate_configs() -> list[Path]:
    candidates = [Path("config.yaml")]
    try:
        from models.config import CONFIG_PATH

        candidates.append(Path(CONFIG_PATH))
    except Exception:
        pass
    candidates.extend([
        Path(r"C:\BackupAgent\config\config.yaml"),
        Path(r"C:\AAMBackup\config.yaml"),
    ])
    return candidates


def _load_config(path: str | None):
    if path:
        candidate = Path(path)
        if not candidate.is_file():
            print(f"ERROR: config file not found: {candidate}", file=sys.stderr)
            return None
        candidates = [candidate]
    else:
        candidates = _candidate_configs()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            from models.config import load_config

            return load_config(str(candidate))
        except Exception as exc:  # noqa: BLE001
            print(f"warning: config {candidate} could not be loaded: {exc}",
                  file=sys.stderr)
    return None


def _local_size(path: Path) -> tuple[int, int]:
    count = 0
    total = 0
    for dirpath, _dirnames, filenames in path.walk():
        for name in filenames:
            if name == ".AAM_TARGET_MOUNTED":
                continue
            try:
                total += (dirpath / name).stat().st_size
                count += 1
            except OSError:
                continue
    return count, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload a whole folder from a local disk to Google Cloud Storage.",
    )
    parser.add_argument("--list-local", action="store_true",
                        help="list folders under --source")
    parser.add_argument("--source", help="parent holding the folder, or the folder itself")
    parser.add_argument("--fy", help="folder to upload, e.g. FY24-25 or archive")
    parser.add_argument("--tier", choices=("STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"),
                        help="storage class for new objects")
    parser.add_argument("--bucket", help="GCS bucket (overrides config)")
    parser.add_argument("--key", help="service-account JSON key (overrides config)")
    parser.add_argument("--project", help="GCP project number")
    parser.add_argument("--location", default=None)
    parser.add_argument("--config", help="config.yaml (defaults: cwd, install config)")
    parser.add_argument("--rclone-config", help="existing rclone config (advanced/testing)")
    parser.add_argument("--transfers", type=int, default=4)
    parser.add_argument("--checkers", type=int, default=8)
    parser.add_argument("--bwlimit", default=None,
                        help="optional rclone bandwidth limit (e.g. 20M); default unlimited")
    parser.add_argument("--timeout", type=float, default=None,
                        help="optional time budget in seconds (default: no limit)")
    parser.add_argument("--log-dir", default="upload_logs")
    parser.add_argument("--json", help="write the result JSON to this file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="WARNING")

    config = _load_config(args.config)
    bucket = args.bucket or (config.cloud.bucket if config else "")
    key = args.key or (config.paths.gcs_key_path if config else "")
    project = args.project or (config.cloud.project_number if config else "")
    location = args.location or (config.cloud.location if config else "asia-south1")
    protected: list[str] = []          # every ACTIVE year: the configured one and, with overlap, the early next one
    if config:
        from core.fy_overlap import protected_fy_names
        try:
            protected = [n.casefold() for n in protected_fy_names(config)]
        except AttributeError:         # a partial config object: fall back to the configured folder name below
            protected = []
        leaf = Path(config.paths.source_drive).name.upper()
        if leaf.startswith("FY") and leaf.casefold() not in protected:
            protected.append(leaf.casefold())

    source = args.source
    if not source and config:
        source = str(Path(config.paths.source_drive).parent)

    if args.list_local:
        if not source:
            print("ERROR: pass --source (or a config with paths.source_drive).", file=sys.stderr)
            return 2
        listing = list_local_fy_folders(source)
        if listing.get("error"):
            print(f"ERROR: {listing['error']}", file=sys.stderr)
            return 1
        print(f"Local folders under {listing['source']}:")
        for folder in listing.get("folders") or []:
            tag = "[FY]" if folder.get("is_fy") else "[other]"
            print(f"  {tag:<7} {folder['name']}   {folder['path']}")
        return 0

    if not bucket:
        print("ERROR: no bucket. Pass --bucket or a config.yaml with cloud.bucket.", file=sys.stderr)
        return 2
    if not key and not args.rclone_config:
        print("ERROR: no GCS key. Pass --key (or --rclone-config for testing).", file=sys.stderr)
        return 2
    if not args.fy:
        print("ERROR: choose a folder with --fy (use --list-local to see them).", file=sys.stderr)
        return 2
    if not args.tier:
        print("ERROR: choose the storage tier with --tier "
              "(STANDARD, NEARLINE, COLDLINE, ARCHIVE).", file=sys.stderr)
        return 2
    if not source:
        print("ERROR: pass --source (the parent holding the folder).", file=sys.stderr)
        return 2

    requested = args.fy.strip()
    try:
        fy_dir = resolve_local_folder(source, requested)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    folder = fy_dir.name
    if folder.casefold() in protected:
        print(
            f"ERROR: {folder} is a live FY (the configured year or, with the overlap on, the early next year); "
            "this tool uploads closed/old folders only. The nightly cloud backup owns that prefix.",
            file=sys.stderr,
        )
        return 2
    count, total = _local_size(fy_dir)
    remote = f"gs://{bucket}/{folder}"

    print(f"Folder:       {folder}")
    print(f"Source:       {fy_dir}")
    print(f"Destination:  {remote}")
    print(f"Tier:         {args.tier}")
    print(f"Files:        {count} ({total / 2**20:.1f} MiB)")
    print("Time limit:   " + ("none" if args.timeout is None else f"{args.timeout:.0f}s"))
    if args.dry_run:
        print("Mode:         dry run")
    elif args.verify_only:
        print("Mode:         verify only")

    if not args.yes and not args.dry_run and not args.verify_only:
        if not sys.stdin.isatty():
            print("ERROR: non-interactive; pass --yes to confirm.", file=sys.stderr)
            return 2
        expected = f"UPLOAD {folder} TO {remote} AS {args.tier}"
        print(f"Type exactly: {expected}")
        if input("> ").strip() != expected:
            print("Aborted. Nothing was changed.")
            return 2

    from core.run_artefacts import RunArtefacts
    from core.time_utils import now_iso

    stamp = now_iso().replace(":", "").replace("+", "_")[:18]
    artefacts = RunArtefacts(
        Path(args.log_dir).resolve(),
        f"upload_{folder}_{stamp}_{uuid.uuid4().hex[:6]}",
    )
    try:
        result = upload_fy(
            fy_dir, bucket=bucket, fy=folder, storage_class=args.tier,
            gcs_key_path=key, project_number=project, location=location,
            rclone_config_path=args.rclone_config,
            transfers=args.transfers, checkers=args.checkers, bwlimit=args.bwlimit,
            timeout_s=args.timeout, dry_run=args.dry_run,
            verify_only=args.verify_only, artefacts=artefacts,
        )
    except KeyboardInterrupt:
        print("\nCancelled. Uploaded objects are kept; re-run the same command to resume.",
              file=sys.stderr)
        return 130

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, default=str),
                                   encoding="utf-8")

    print()
    print(f"Status:       {result.get('status')}")
    print(f"Verified:     {result.get('verified')}")
    print(f"Matched:      {result.get('matched')}")
    print(f"Missing:      {result.get('missing_at_remote')}  "
          f"Different: {result.get('modified_at_remote')}")
    print(f"Report:       {result.get('report_path')}")
    if result.get("error"):
        print(f"Error:        {result['error']}", file=sys.stderr)
    for warning in result.get("warnings") or []:
        print(f"Warning:      {warning}", file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
