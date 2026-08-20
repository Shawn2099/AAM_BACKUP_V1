"""S3 pre-rt bucket snapshot (prod key via the app's own temp-config writer)."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.rclone_config import temp_rclone_config  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RC = r"C:\AAMBackup\deploy\bin\rclone.exe"
KEY = r"C:\AAMBackup\deploy\keys\aam-gcs-key.json"
bucket = "aam-backup-demo-innovizta"
result = {}


def run(*args, cfg, timeout=180):
    r = subprocess.run([RC, "--config", cfg, *args],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


with temp_rclone_config(KEY, "asia-south1", "920173882190", "STANDARD") as rcfile:
    for prefix in ("", "FY26-27/", "E2E_TEST_FY/", "FY23-24/", "NONEXISTENT_BUCKET/", "docs/"):
        code, out, err = run("lsf", f"aam_gcs:{bucket}/{prefix}", cfg=rcfile)
        lines = [l for l in out.splitlines() if l.strip()] if code == 0 else []
        result[prefix or "(root)"] = {
            "exists": code == 0,
            "count": len(lines),
            "entries": lines[:40],
            "stderr_head": err.strip()[:200] if code != 0 else "",
        }
        print(f"{prefix or '(root)'}: {'EXISTS' if code == 0 else 'absent'} ({len(lines)} entries)")

    code, out, err = run("lsl", f"aam_gcs:{bucket}/FY26-27/", cfg=rcfile)
    print("--- lsl FY26-27/ (first 3) ---")
    print("\n".join((out or err).strip().splitlines()[:3]))
    code, out, err = run("size", f"aam_gcs:{bucket}/FY26-27/", cfg=rcfile)
    print("--- size FY26-27/ ---")
    print((out or err).strip())

Path(r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\_s3_pre_state\bucket_state.json").write_text(
    json.dumps(result, indent=1), encoding="utf-8"
)
print("BUCKET SNAPSHOT DONE")
