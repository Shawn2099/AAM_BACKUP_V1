"""Phase 0.1d - rebuild production source baseline + supervised cloud re-alignment.

Steps (all explicit, reversible from docs/phase0_gcs_before.json):
  1. Wipe C:\\BackupData\\FY26-27 (operator-authorized: demo placeholder).
  2. Recreate the agreed sample set.
  3. rclone deletefile the stray FY25-26/closing_ledger.xlsx.
  4. ONE supervised rclone sync source -> bucket/FY26-27.
  5. Post-snapshot -> docs/phase0_gcs_after.json

Usage: .venv\\Scripts\\python.exe scripts\\phase0_restore.py
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.process import resolve_binary
from core.rclone_config import temp_rclone_config
from models.config import load_config

BASELINE_FILES = {
    "README_BASELINE.txt": (
        "AAM FY26-27 baseline sample set.\n"
        f"Rebuilt by phase0_restore.py on {datetime.now().isoformat()} after the\n"
        "2026-08-23 e2e-sandbox contamination incident (see IMPLEMENTATION_FIX_PLAN.md\n"
        "P0-DATA). Original contents were demo placeholders; loss authorized by operator.\n"
    ),
    "Clients/Sample_Client_A/Ledger_2026-04.txt": (
        "Sample ledger entry - baseline placeholder content.\n"
    ),
    "Clients/Sample_Client_B/GST_Summary_2026-04.txt": (
        "Sample GST summary - baseline placeholder content.\n"
    ),
    "Registers/Cash_Register_2026-04.csv": (
        "date,particulars,receipt,payment\n"
        "2026-04-01,opening,0,0\n"
    ),
    "Reports/Monthly/Report_2026-04.txt": (
        "Monthly report placeholder - rebuilt during P0-DATA restoration.\n"
    ),
}


def rc(rcfg: str, *args: str, timeout: int = 600):
    exe = resolve_binary("rclone") or "rclone"
    proc = subprocess.run(
        [exe, *args, "--config", rcfg],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8",
    )
    return proc


def main() -> int:
    cfg = load_config()
    src = Path(cfg.paths.source_drive)
    bucket = cfg.cloud.bucket

    # -- 1. wipe prod tree ---------------------------------------------------
    import shutil
    shutil.rmtree(src, ignore_errors=True)
    src.mkdir(parents=True, exist_ok=True)

    # -- 2. agreed sample set -------------------------------------------------
    for rel, content in BASELINE_FILES.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
    print(f"LOCAL_BASELINE_OK files={len(BASELINE_FILES)} at {src}")

    with temp_rclone_config(
        cfg.paths.gcs_key_path,
        cfg.cloud.location,
        cfg.cloud.project_number,
        cfg.cloud.storage_class,
    ) as rcfg:
        # -- 3. stray object removal (explicit) --------------------------------
        stray = rc(rcfg, "deletefile", f"aam_gcs:{bucket}/FY25-26/closing_ledger.xlsx")
        print(f"STRAY_DELETE rc={stray.returncode}")

        # -- 4. one supervised sync --------------------------------------------
        sync = rc(
            rcfg, "sync", str(src), f"aam_gcs:{bucket}/FY26-27",
            "--error-on-no-transfer", "--use-json-log", "--log-level", "INFO",
            timeout=900,
        )
        print(f"SUPERCISED_SYNC rc={sync.returncode}")
        if sync.returncode not in (0, 9):
            print(f"SYNC_STDERR {sync.stderr[:800]}")
            return 2

        # -- 5. post snapshot ----------------------------------------------------
        lst = rc(rcfg, "lsjson", "-R", f"aam_gcs:{bucket}", timeout=300)
        if lst.returncode != 0:
            print(f"POST_SNAPSHOT_FAILED rc={lst.returncode}")
            return 3

    entries = json.loads(lst.stdout or "[]")
    Path("docs/phase0_gcs_after.json").write_text(
        json.dumps({
            "captured_at": datetime.now().isoformat(),
            "bucket": bucket,
            "object_count": len(entries),
            "entries": entries,
        }, indent=1),
        encoding="utf-8",
    )
    fy = sorted(e["Path"] for e in entries if e["Path"].startswith("FY26-27/") and e.get("Size", -1) >= 0)
    print(f"AFTER_SNAPSHOT_OK total_objects={len(entries)}")
    print("FY26-27 objects now:")
    for p in fy:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
