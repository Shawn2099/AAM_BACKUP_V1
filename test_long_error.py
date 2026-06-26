import json
import logging
from core.manifest import ManifestDB
from core.report import generate_report_html, _generate_csv_data

db = ManifestDB("test_report.db")

long_error = "A" * 5000

db.insert_run({
    "run_id": "test_1",
    "mode": "cloud",
    "status": "FAILED",
    "started_at": "2026-10-10T12:00:00Z",
    "ended_at": "2026-10-10T12:05:00Z",
    "files_copied": 10,
    "files_failed": 5,
    "bytes_copied": 1024,
    "duration_seconds": 12.0,
    "exit_code": 1,
    "error_message": long_error
})

runs = db.get_runs_since(7)
print("DB error length:", len(runs[0]["error_message"]))

html = generate_report_html(db, "Test Firm", 7, "Weekly", True)
print("HTML has truncated error:", html.find("A"*97+"...") > 0)
print("HTML doesn't have full error:", html.find(long_error) == -1)

csv_data = _generate_csv_data(runs)
print("CSV has full error:", csv_data.decode("utf-8").find(long_error) > 0)

import os
os.remove("test_report.db")
