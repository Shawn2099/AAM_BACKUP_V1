# Cloud restore (whole FY)

Disaster-recovery restore from Google Cloud Storage. This is the bulk path:
a server or disk is gone, and the client needs the latest state of a whole
financial year back on a folder they choose. **Single-file restores stay in
the Google Cloud Console.**

## What it does

- Lists the FY folders in the bucket (`FY25-26`, `FY26-27`, ...).
- Restores the selected FY's **latest live state** to `<destination>\<FY>`.
- Verifies every restored file against a pinned MD5 manifest.
- Never deletes anything: only `rclone copy` is used, never `sync`.
- Works with old FYs in the same bucket.
- Runs without Prefect, the manifest DB, or the dashboard, so it works on a
  clean machine during a disaster.
- **No time limit by default.** The optional `--timeout` adds a soft budget;
  without it rclone runs until it finishes or the operator cancels.
- Keeps interrupted work resumable: completed files stay, in-flight files use
  a `.aamrestore` temporary name, and re-running with `--force` continues.

## What it does not do

- **No point-in-time restore.** rclone cannot select GCS object generations.
  The selected FY is restored as it exists in the bucket now. A future
  version/timestamp restore needs `gcloud storage` (GCS Object Versioning).
- **No single-file restore.** Use the Google Cloud Console for that.
- **No Windows metadata fidelity.** File content and the original modification
  time are restored; Windows ACLs, attributes, ADS, EFS, empty folders and
  junctions are not stored in GCS.
- **No deletes or repairs of the bucket.** The service account should be
  read-only (`roles/storage.objectViewer`).

## CLI

```bat
:: list the available FY folders
python restore.py --config C:\AAMBackup\config.yaml --list

:: list with object counts/bytes (one size query per FY)
python restore.py --config C:\AAMBackup\config.yaml --list --sizes

:: restore a whole FY into D:\Restore\FY26-27
python restore.py --config C:\AAMBackup\config.yaml --fy FY26-27 --target D:\Restore

:: restore directly into the selected folder (must be empty)
python restore.py --config C:\AAMBackup\config.yaml --fy FY26-27 --target D:\Restore\FY26-27 --into

:: restore an older year
python restore.py --config C:\AAMBackup\config.yaml --fy FY25-26 --target D:\Restore

:: plan only (no files written)
python restore.py --config C:\AAMBackup\config.yaml --fy FY26-27 --target D:\Restore --dry-run

:: verify an existing restore against the bucket
python restore.py --config C:\AAMBackup\config.yaml --fy FY26-27 --target D:\Restore --verify-only

:: repair an existing restore (merge; nothing deleted)
python restore.py --config C:\AAMBackup\config.yaml --fy FY26-27 --target D:\Restore --force
```

On a clean machine with no `config.yaml`:

```bat
python restore.py --bucket aam-backup-demo-innovizta ^
  --key C:\keys\aam-restore-ro.json ^
  --fy FY26-27 --target D:\Restore
```

`--project` is optional for a read-only restore. `--yes` skips the
confirmation phrase (use only in automation).

## Bulk upload of old FY data

Use the companion tool; it is copy-only, resumable, verifies every file by MD5,
and has no time limit by default.

```bat
:: show the local FY folders under a parent
python upload_fy.py --config C:\AAMBackup\config.yaml --source E:\Archive --list-local

:: plan only
python upload_fy.py --config C:\AAMBackup\config.yaml ^
  --source E:\Archive --fy FY24-25 --tier ARCHIVE --dry-run

:: upload a whole old FY
python upload_fy.py --config C:\AAMBackup\config.yaml ^
  --source E:\Archive --fy FY24-25 --tier ARCHIVE

:: prove an existing upload
python upload_fy.py --config C:\AAMBackup\config.yaml ^
  --source E:\Archive --fy FY24-25 --tier ARCHIVE --verify-only
```

- `--tier` is required: `STANDARD`, `NEARLINE`, `COLDLINE`, `ARCHIVE`.
- The destination is always `gs://<bucket>/<FY>`; it refuses a different
  prefix. It never deletes.
- `--gcs-storage-class` applies to **new** objects. Re-running skips identical
  objects; objects already in the bucket keep their current class.
- `--bwlimit 20M` is optional; the default is unlimited.
- `--timeout` is optional; without it there is no time limit.
- Each run writes `upload_logs\<date>\upload_<FY>_...\upload-report.json`.
- Upload statuses: `UPLOADED_VERIFIED`, `UPLOADED_UNVERIFIED`, `VERIFIED`
  (`--verify-only`), `DRY_RUN`, `INCOMPLETE`, `FAILED`.

See `docs/GCS_LIFECYCLE.md` for the bucket versioning, soft-delete, and
end-of-year ARCHIVE settings.

## Status codes

| Status | Meaning |
| --- | --- |
| `RESTORED_VERIFIED` | Copied, and every file matched the snapshot MD5 |
| `RESTORED_UNVERIFIED` | Copied, but verification did not pass — do not sign off |
| `VERIFIED` | `--verify-only`: the target matches the bucket |
| `DRY_RUN` | Nothing written; source count/bytes reported |
| `INCOMPLETE` | Time budget reached (rclone exit 10); re-run the same command |
| `FAILED` | Refused or the copy/verify could not run |

## Safety gates

- Target must be empty unless `--force` is used.
- Target cannot be the source, the LAN copy, the runtime folder, the config
  folder or the code folder (both directions are checked).
- Drive roots, junctions and symlinks are refused.
- Hash coverage is proven before the copy: if any object has no MD5, the
  restore fails instead of silently degrading to a size-only check.
- Free space is checked (`bytes * 1.10 + 1 GiB + 4 KiB/file`).
- Case-insensitive filename collisions are refused (Windows would lose one).
- Verification uses a **pinned manifest** (`rclone check --checkfile md5`), not
  the live remote, so files deleted or changed during the restore are caught.
- `hashes missing`, `hashes could not be checked`, `!` errors and any `+`/`*`
  difference fail the restore. Extra local files are allowed only with
  `--force`, and are reported.

## Reports

Every run writes:

```
restore_logs\<date>\restore_<FY>_<time>_<id>\
  snapshot.md5              pinned MD5 manifest
  copy.ndjson               rclone copy audit log
  verify.ndjson             rclone check log
  verify-diff.txt           per-file = / + / - / * / ! result
  restore-report.json       machine-readable result
```

`restore-report.json` is the record to keep for the client; it includes the
snapshot SHA-256, counts, bytes, verification counts and the target path.

## UI integration

The dashboard can import the engine directly; no Prefect/DB dependency:

```python
from core.restore import list_fy_folders, fy_info, restore_fy

folders = list_fy_folders(bucket=..., gcs_key_path=...)
info = fy_info("FY26-27", bucket=..., gcs_key_path=...)
result = restore_fy(
    destination_parent,
    fy="FY26-27",
    bucket=...,
    gcs_key_path=...,
    forbidden_paths=[...],
    artefacts=run_artefacts,
)
```

Suggested endpoints (admin-only):

- `GET  /api/restore/folders` -> `list_fy_folders()`
- `GET  /api/restore/info?fy=FY26-27` -> `fy_info()`
- `POST /api/restore/start` -> background thread calling `restore_fy()`
- `GET  /api/restore/status/{job_id}` -> progress/status
- `GET  /api/restore/report/{job_id}` -> `restore-report.json`

The UI page needs: FY dropdown, destination folder picker, dry-run button,
confirmation phrase, progress/log view, and a result summary. The engine
already writes the report and copy log for the progress view.

## Operational checklist

1. Use a dedicated read-only service account (`roles/storage.objectViewer`)
   and a separate key from the backup writer.
2. Restore to a local fixed disk with enough free space; avoid the live
   source, the LAN share, OneDrive and git working trees.
3. Quiesce the backup service while restoring the current FY (a restore reads
   the live prefix; a running sync can change objects mid-copy).
4. Run `--dry-run` first, confirm the FY and destination.
5. Sign off only on `RESTORED_VERIFIED`; keep `restore-report.json`.
6. Do a real restore drill on a clean Server 2016 VM before client rollout.

## Validation performed

- `FY25-26`: 4 files, `RESTORED_VERIFIED`.
- `FY26-27`: 690 files / 92,543,999 bytes, `RESTORED_VERIFIED`; the restored
  tree was independently hashed and compared with the live source:
  **690/690 files, 0 missing, 0 extra, 0 size differences, 0 MD5 differences.**
- Non-empty target without `--force`: refused.
- `--dry-run`: no target folder created.
- `--verify-only` after deleting one file: `FAILED`, `missing=1`.
- `--force` after the deletion: restored the file, kept an extra local file,
  `RESTORED_VERIFIED`, `extra=1`.
- Old FY (`FY25-26`) restore and the FY list both work against the real bucket.
- Bulk upload of a real `FY24-25` to the demo bucket with `ARCHIVE`:
  `UPLOADED_VERIFIED`; `gcloud storage objects describe` confirmed
  `storageClass=ARCHIVE`.
- Unit tests: `tests/test_restore_engine.py` (8 tests) and
  `tests/test_upload_engine.py` (6 tests), both against a local rclone backend.
- Defaults are unlimited: `restore_from_cloud(timeout_s=None)` and
  `upload_fy(timeout_s=None)` add no `--max-duration` and pass
  `run_tool(timeout_s=None)`. `--timeout` remains available when an operator
  wants a soft budget.
