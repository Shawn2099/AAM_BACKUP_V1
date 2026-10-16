"""READ-ONLY status of the trial box, for worker agents (AGENT_TASKS.md).

Run it ON the box by piping it over ssh (there is no scp):

    cat agent_ops/box_status.py | ssh innoviztawinserver2016 "C:\\AAMBackup\\.venv\\Scripts\\python.exe -"

Opens the manifest DB with mode=ro (never writes), prints the code version,
services, lock, recent runs, recent notifications and pending approvals.
"""
import json
import os
import sqlite3
import subprocess
import sys

REPO = r"C:\AAMBackup"
os.environ.setdefault("AAM_CONFIG", os.path.join(REPO, "config.yaml"))
sys.path.insert(0, REPO)

from models.config import load_config  # noqa: E402

cfg = load_config()
db_path = cfg.paths.database_path


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=REPO).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"<{e}>"


print("== code:", sh(["git", "log", "--oneline", "-1"]))
print("== git status:", sh(["git", "status", "--short"]) or "clean")
for svc in ("AamPrefectServer", "AamBackupAgent", "AamWatchdog"):
    out = sh(["sc", "query", svc])
    state = next((ln.split(":", 1)[1].strip() for ln in out.splitlines() if "STATE" in ln), out)
    print(f"== service {svc}: {state}")
lock = os.path.join(cfg.paths.runtime_dir, "backup.lock")
print("== backup.lock:", "PRESENT" if os.path.exists(lock) else "absent")
print("== db:", db_path)

con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row


def table(sql: str, title: str) -> None:
    print(f"\n--- {title}")
    try:
        for r in con.execute(sql):
            d = dict(r)
            if d.get("extended_metrics"):
                try:
                    em = json.loads(d["extended_metrics"])
                    d["extended_metrics"] = {k: em[k] for k in
                                             ("budget", "clock", "nas_shutdown", "sync_status",
                                              "guards", "owner") if k in em}
                except ValueError:
                    pass
            print(d)
    except sqlite3.Error as e:
        print(f"<{e}>")


table("SELECT run_id, mode, status, started_at, ended_at, files_copied, bytes_copied, "
      "files_failed, error_message, extended_metrics FROM run_history ORDER BY rowid DESC LIMIT 12",
      "last 12 runs")
table("SELECT event_key, kind, status, error, attempts, created_at FROM notifications "
      "ORDER BY id DESC LIMIT 12", "last 12 notifications")
table("SELECT approval_id, leg, fy, status, created_at, used_at FROM approvals "
      "ORDER BY id DESC LIMIT 5", "last 5 approvals")
table("SELECT audit_id, mode, status, files_checked, mismatches, detail, started_at FROM "
      "integrity_audits ORDER BY rowid DESC LIMIT 4", "last 4 integrity audits")
