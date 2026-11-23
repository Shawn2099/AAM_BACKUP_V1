#!/usr/bin/env python
"""Bulk restore CLI - list folders in the bucket, restore one whole folder.

Disaster-recovery use:
    python restore.py --config C:\\AAMBackup\\config.yaml --list
    python restore.py --config C:\\AAMBackup\\config.yaml --fy FY26-27 --target D:\\Restore
    python restore.py --config C:\\AAMBackup\\config.yaml --fy archive --target D:\\Restore

It restores the **latest live state** of the selected folder into
`<target>\\<folder>` and verifies every file by MD5. Single-file restores are
left to the Google Cloud Console.

On a clean machine without config.yaml, pass --bucket/--key/--project:
    python restore.py --bucket aam-backup-demo-innovizta ^
        --key C:\\keys\\restore-ro.json --fy FY26-27 --target D:\\Restore
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from loguru import logger

from core.restore import FY_RE, fy_info, list_fy_folders, restore_fy, valid_folder_name
from core.run_artefacts import RunArtefacts
from core.time_utils import now_iso


def _candidate_configs() -> list[Path]:
    candidates = [Path("config.yaml")]
    try:
        from models.config import CONFIG_PATH

        candidates.append(Path(CONFIG_PATH))
    except Exception:
        pass
    candidates.extend(
        [Path(r"C:\BackupAgent\config\config.yaml"),
         Path(r"C:\AAMBackup\config.yaml")]
    )
    return candidates


def _load_config(path: str | None):
    if path:
        candidate = Path(path)
        if not candidate.is_file():
            print(f"ERROR: config file not found: {candidate}", file=sys.stderr)
            return None, None
        candidates = [candidate]
    else:
        candidates = _candidate_configs()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            from models.config import load_config

            return load_config(str(candidate)), candidate
        except Exception as exc:  # noqa: BLE001
            print(f"warning: config {candidate} could not be loaded: {exc}", file=sys.stderr)
            continue
    return None, None


def _forbidden_paths(config, config_path: Path | None) -> list[str]:
    paths: list[str] = [str(Path(__file__).resolve().parent)]
    if config is not None:
        paths.extend([
            config.paths.source_drive,
            config.paths.lan_destination,
            config.paths.runtime_dir,
        ])
        from core.fy_overlap import protected_folders
        paths.extend(protected_folders(config))          # the early next year's folders too (R-20)
    if config_path is not None:
        paths.append(str(config_path.parent))
    for system in (r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)",
                   r"C:\ProgramData"):
        if Path(system).exists():
            paths.append(system)
    return paths


def _print_folder_table(result: dict, sizes: bool, key: str, project: str,
                        location: str, storage: str, rclone_config: str | None) -> None:
    if result.get("error"):
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return
    folders = result.get("folders") or []
    print(f"Top-level folders in {result.get('remote')}:")
    if not folders:
        print("  (none found)")
        return
    for folder in folders:
        tag = "[FY]" if folder.get("is_fy") else "[other]"
        line = f"  {tag:<7} {folder['name']}"
        if sizes:
            info = fy_info(
                folder["name"], bucket=result["bucket"], gcs_key_path=key,
                project_number=project, location=location,
                storage_class=storage, rclone_config_path=rclone_config,
            )
            if info.get("error"):
                line += f"   (size unavailable: {info['error']})"
            else:
                line += f"   {info['count']} files, {info['bytes'] / 2**20:.1f} MiB"
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore a whole folder from Google Cloud Storage.",
    )
    parser.add_argument("--list", action="store_true",
                        help="list the top-level folders in the bucket")
    parser.add_argument("--sizes", action="store_true", help="with --list, also show count/bytes")
    parser.add_argument("--fy", help="folder to restore, e.g. FY26-27 or archive")
    parser.add_argument("--target", help="destination parent folder; the folder is created inside it")
    parser.add_argument("--into", action="store_true",
                        help="restore directly into --target instead of --target\\<folder>")
    parser.add_argument("--config", help="config.yaml (defaults: cwd, install config)")
    parser.add_argument("--bucket", help="GCS bucket (overrides config)")
    parser.add_argument("--key", help="service-account JSON key (overrides config)")
    parser.add_argument("--project", help="GCP project number (optional for read-only)")
    parser.add_argument("--location", default=None, help="GCS location (default asia-south1)")
    parser.add_argument("--storage-class", default=None, help="GCS storage class (default STANDARD)")
    parser.add_argument("--rclone-config", help="use an existing rclone config (advanced/testing)")
    parser.add_argument("--log-dir", default="restore_logs", help="where the restore report/logs go")
    parser.add_argument("--json", help="write the result JSON to this file")
    parser.add_argument("--transfers", type=int, default=8)
    parser.add_argument("--checkers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=None,
                        help="optional time budget in seconds (default: no limit)")
    parser.add_argument("--dry-run", action="store_true", help="measure only; write nothing")
    parser.add_argument("--verify-only", action="store_true", help="verify an existing restore")
    parser.add_argument("--force", action="store_true",
                        help="allow a non-empty target (merge; nothing is deleted)")
    parser.add_argument("--skip-space-check", action="store_true")
    parser.add_argument("--include-control", action="store_true",
                        help="include .AAM_TARGET_MOUNTED control files")
    parser.add_argument("--protect", action="append", default=[],
                        help="extra path that must never be a restore target")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    parser.add_argument("--verbose", action="store_true", help="show engine progress logs")
    args = parser.parse_args(argv)

    if not args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="WARNING")

    config, config_path = _load_config(args.config)
    bucket = args.bucket or (config.cloud.bucket if config else "")
    key = args.key or (config.paths.gcs_key_path if config else "")
    project = args.project or (config.cloud.project_number if config else "")
    location = args.location or (config.cloud.location if config else "asia-south1")
    storage = args.storage_class or (config.cloud.storage_class if config else "STANDARD")
    forbidden = _forbidden_paths(config, config_path) + list(args.protect)

    if not bucket:
        print("ERROR: no bucket. Pass --bucket or a config.yaml with cloud.bucket.", file=sys.stderr)
        return 2
    if not key and not args.rclone_config:
        print("ERROR: no GCS key. Pass --key (or --rclone-config for testing).", file=sys.stderr)
        return 2

    if args.list:
        result = list_fy_folders(
            bucket=bucket, gcs_key_path=key, project_number=project,
            location=location, storage_class=storage,
            rclone_config_path=args.rclone_config,
        )
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        _print_folder_table(result, args.sizes, key, project, location, storage,
                            args.rclone_config)
        return 0 if not result.get("error") else 1

    if not args.fy:
        print("ERROR: choose a folder with --fy (use --list to see them).", file=sys.stderr)
        return 2
    folder = args.fy.strip()
    if not valid_folder_name(folder):
        print(f"ERROR: {args.fy!r} is not a top-level folder name.", file=sys.stderr)
        return 2
    if FY_RE.fullmatch(folder):
        folder = folder.upper()
    if not args.target:
        print("ERROR: choose a destination folder with --target.", file=sys.stderr)
        return 2

    parent = Path(args.target).expanduser()
    if not parent.exists() or not parent.is_dir():
        print(f"ERROR: destination folder does not exist: {parent}", file=sys.stderr)
        return 2
    final_target = parent if args.into else parent / folder

    info = fy_info(
        folder, bucket=bucket, gcs_key_path=key, project_number=project,
        location=location, storage_class=storage, rclone_config_path=args.rclone_config,
    )
    if info.get("error"):
        print(f"ERROR: cannot read {folder}: {info['error']}", file=sys.stderr)
        return 1
    print(f"Folder:       {folder}")
    print(f"Source:       gs://{bucket}/{folder}")
    print(f"Files:        {info['count']} ({info['bytes'] / 2**20:.1f} MiB)")
    print(f"Destination:  {final_target}")
    print("Mode:         latest state, MD5-verified" +
          (" (verify only)" if args.verify_only else
           " (dry run)" if args.dry_run else
           " (merge)" if args.force else ""))
    print("Time limit:   " + ("none" if args.timeout is None else f"{args.timeout:.0f}s"))
    if args.force:
        print("WARNING: the destination is not empty; files may be overwritten, "
              "but nothing is deleted.")

    if not args.yes and not args.dry_run and not args.verify_only:
        if not sys.stdin.isatty():
            print("ERROR: non-interactive; pass --yes to confirm.", file=sys.stderr)
            return 2
        expected = f"RESTORE {folder} TO {final_target}"
        print(f"Type exactly: {expected}")
        if input("> ").strip() != expected:
            print("Aborted. Nothing was changed.")
            return 2

    stamp = now_iso().replace(":", "").replace("+", "_")[:18]
    artefacts = RunArtefacts(
        Path(args.log_dir).resolve(),
        f"restore_{folder}_{stamp}_{uuid.uuid4().hex[:6]}",
    )
    try:
        result = restore_fy(
            parent, fy=folder, bucket=bucket, gcs_key_path=key, project_number=project,
            location=location, storage_class=storage, rclone_config_path=args.rclone_config,
            transfers=args.transfers, checkers=args.checkers, timeout_s=args.timeout,
            into=args.into, allow_non_empty=args.force, dry_run=args.dry_run,
            verify_only=args.verify_only, skip_space_check=args.skip_space_check,
            forbidden_paths=forbidden, exclude_control=not args.include_control,
            artefacts=artefacts,
        )
    except KeyboardInterrupt:
        print("\nCancelled. Completed files are kept; re-run the same command "
              "with --force to resume.", file=sys.stderr)
        return 130
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    print()
    print(f"Status:       {result.get('status')}")
    print(f"Verified:     {result.get('verified')}")
    print(f"Matched:      {result.get('matched')}")
    print(f"Missing:      {result.get('missing_at_target')}  "
          f"Different: {result.get('modified_at_target')}  "
          f"Extra: {result.get('extra_at_target')}")
    print(f"Report:       {result.get('report_path')}")
    if result.get("error"):
        print(f"Error:        {result['error']}", file=sys.stderr)
    for warning in result.get("warnings") or []:
        print(f"Warning:      {warning}", file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
