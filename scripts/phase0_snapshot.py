"""Phase 0.1d - snapshot the full GCS bucket state before restoration.

Usage: .venv\\Scripts\\python.exe scripts\\phase0_snapshot.py docs\\phase0_gcs_before.json
ASCII-only output. Read-only against the bucket.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.process import resolve_binary
from core.rclone_config import temp_rclone_config
from models.config import load_config


def main() -> int:
    out_path = Path(sys.argv[1])
    cfg = load_config()
    bucket = cfg.cloud.bucket

    with temp_rclone_config(
        cfg.paths.gcs_key_path,
        cfg.cloud.location,
        cfg.cloud.project_number,
        cfg.cloud.storage_class,
    ) as rcfg:
        exe = resolve_binary("rclone") or "rclone"
        proc = subprocess.run(
            [exe, "lsjson", "-R", f"aam_gcs:{bucket}", "--config", rcfg],
            capture_output=True, text=True, timeout=300, encoding="utf-8",
        )
    if proc.returncode != 0:
        print(f"SNAPSHOT_FAILED rc={proc.returncode} err={proc.stderr[:500]}")
        return 1

    entries = json.loads(proc.stdout or "[]")
    payload = {
        "captured_at": __import__("datetime").datetime.now().isoformat(),
        "bucket": bucket,
        "object_count": len(entries),
        "entries": entries,
    }
    out_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    prefixes = {}
    for e in entries:
        p = e.get("Path", "")
        top = p.split("/")[0] if "/" in p else "(root)"
        prefixes[top] = prefixes.get(top, 0) + 1
    print(f"SNAPSHOT_OK objects={len(entries)} -> {out_path}")
    for k in sorted(prefixes):
        print(f"  prefix {k}: {prefixes[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
