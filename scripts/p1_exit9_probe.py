"""P1-EXIT9 live verification - typo bucket must classify as CLOUD_FAILED.

Usage: .venv\\Scripts\\python.exe scripts\\p1_exit9_probe.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.cloud_sync import run_cloud_sync
from models.config import load_config


def main() -> int:
    cfg = load_config()
    result = run_cloud_sync(
        source=cfg.paths.source_drive,
        bucket="typo-bucket-does-not-exist-x9q",
        fy_prefix="P1_PROBE",
        gcs_key_path=cfg.paths.gcs_key_path,
        project_number=cfg.cloud.project_number,
        storage_class=cfg.cloud.storage_class,
        location=cfg.cloud.location,
    )
    print(f"status={result['status']}")
    print(f"exit_code={result['exit_code']}")
    tail = (result.get("error") or "")[:300].replace("\n", " | ")
    print(f"error_tail={tail}")
    ok = result["status"] == "CLOUD_FAILED"
    print("VERDICT: PASS - exit 9 correctly reclassified" if ok else "VERDICT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
