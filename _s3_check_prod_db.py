"""S3 pre-rt check: read the PROD run history (read-only) for recent runs."""
import sqlite3

db = sqlite3.connect(r"C:\BackupAgent\manifest.db")
db.row_factory = sqlite3.Row
rows = db.execute(
    "SELECT run_id, mode, started_at, ended_at, status, exit_code, error_message "
    "FROM run_history WHERE started_at >= '2026-08-19' ORDER BY started_at DESC LIMIT 8"
).fetchall()
for r in rows:
    d = dict(r)
    err = (d.get("error_message") or "")[:200]
    print(f"{d['started_at']}  {d['mode']:6s}  {d['status']:20s}  exit={d['exit_code']}  err={err!r}")
db.close()
