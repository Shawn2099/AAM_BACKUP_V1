"""Read-only health report for the AAM manifest DB and the Prefect DB.

Runs on the backup server (stdin-piped by server_health.ps1). Prints one JSON
object. Never writes to either database.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys

OK_APP = {"LAN_COMPLETE", "CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"}
FAIL_APP_HINTS = ("FAIL", "TIMEOUT", "ABANDON", "ERROR", "PARTIAL")


def open_ro(path: str) -> sqlite3.Connection:
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def parse_dt(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def hours_since(value, now: dt.datetime):
    ts = parse_dt(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def table_counts(con: sqlite3.Connection, names) -> dict:
    out = {}
    for name in names:
        try:
            out[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        except sqlite3.Error as exc:
            out[name] = f"ERR: {exc}"
    return out


def db_integrity(con: sqlite3.Connection) -> dict:
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    info = {}
    for pragma in ("journal_mode", "page_size", "page_count", "freelist_count"):
        try:
            info[pragma] = con.execute(f"PRAGMA {pragma}").fetchone()[0]
        except sqlite3.Error:
            info[pragma] = None
    return {"quick_check": quick, "foreign_key_violations": len(fk), "info": info}


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: db_health.py <manifest.db> <prefect.db> [warn_h] [fail_h]", file=sys.stderr)
        return 2

    manifest_path, prefect_path = sys.argv[1], sys.argv[2]
    warn_h = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    fail_h = float(sys.argv[4]) if len(sys.argv) > 4 else 48.0
    watchdog_warn_min = float(sys.argv[5]) if len(sys.argv) > 5 else 15.0

    now = dt.datetime.now(dt.timezone.utc)
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    report = {"generated_at": now.isoformat(), "manifest": {}, "prefect": {}, "checks": checks}

    # ---------------- manifest DB ----------------
    m = open_ro(manifest_path)
    m_int = db_integrity(m)
    m_size = os.path.getsize(manifest_path)
    m_wal = os.path.getsize(manifest_path + "-wal") if os.path.exists(manifest_path + "-wal") else 0
    m_counts = table_counts(
        m, ("run_history", "file_entries", "notifications", "integrity_audits", "approvals")
    )
    report["manifest"] = {
        "path": manifest_path,
        "size_bytes": m_size,
        "wal_bytes": m_wal,
        "tables": m_counts,
        **m_int,
    }

    add("manifest DB integrity", "PASS" if m_int["quick_check"] == "ok" else "FAIL",
        f"quick_check={m_int['quick_check']}, fk_violations={m_int['foreign_key_violations']}")
    if m_int["foreign_key_violations"]:
        add("manifest DB foreign keys", "FAIL",
            f"{m_int['foreign_key_violations']} violation(s)")

    # Latest run per leg + status distribution
    runs = {}
    for mode in ("lan", "cloud"):
        row = m.execute(
            "SELECT id, run_id, mode, started_at, ended_at, status, exit_code, "
            "files_copied, files_failed, error_message FROM run_history "
            "WHERE mode=? ORDER BY id DESC LIMIT 1",
            (mode,),
        ).fetchone()
        runs[mode] = dict(row) if row else None
    report["manifest"]["last_runs"] = runs

    run_status_counts = {
        r["status"]: r["n"]
        for r in m.execute("SELECT status, COUNT(*) n FROM run_history GROUP BY status")
    }
    report["manifest"]["run_status_counts"] = run_status_counts

    for mode in ("lan", "cloud"):
        run = runs[mode]
        if not run:
            add(f"{mode.upper()} run recorded", "FAIL", "no run history")
            continue
        age = hours_since(run["started_at"], now)
        status = run["status"]
        detail = f"last={run['started_at']}, status={status}, age={age:.1f}h"
        if status not in OK_APP or any(h in status for h in FAIL_APP_HINTS):
            add(f"{mode.upper()} last run", "FAIL", detail)
        elif age is not None and age > fail_h:
            add(f"{mode.upper()} last run", "FAIL", detail)
        elif age is not None and age > warn_h:
            add(f"{mode.upper()} last run", "WARN", detail)
        else:
            add(f"{mode.upper()} last run", "PASS", detail)

    # Watchdog heartbeat
    meta = {r["key"]: r["value"] for r in m.execute("SELECT key, value FROM db_meta")}
    report["manifest"]["db_meta"] = {
        k: meta.get(k) for k in ("schema_version", "schedule_in_effect", "watchdog_healthy_at")
    }
    wd_age_min = None
    if meta.get("watchdog_healthy_at"):
        wd_age_min = hours_since(meta["watchdog_healthy_at"], now)
        wd_age_min = wd_age_min * 60 if wd_age_min is not None else None
    if wd_age_min is None:
        add("watchdog heartbeat", "WARN", "watchdog_healthy_at missing")
    elif wd_age_min > watchdog_warn_min * 4:
        add("watchdog heartbeat", "FAIL", f"stale {wd_age_min:.0f} min")
    elif wd_age_min > watchdog_warn_min:
        add("watchdog heartbeat", "WARN", f"stale {wd_age_min:.0f} min")
    else:
        add("watchdog heartbeat", "PASS", f"{wd_age_min:.0f} min ago")

    # Notifications that never left the queue
    notif = {
        r["status"]: r["n"]
        for r in m.execute("SELECT status, COUNT(*) n FROM notifications GROUP BY status")
    }
    recent_cut = now - dt.timedelta(days=7)
    stuck = 0
    for row in m.execute("SELECT status, created_at FROM notifications"):
        if row["status"] in ("SENT", "SUPPRESSED"):
            continue
        created = parse_dt(row["created_at"])
        if created is not None and created >= recent_cut:
            stuck += 1
    report["manifest"]["notifications"] = notif
    if stuck:
        add("notification queue", "WARN", f"{stuck} unsent notification(s) in the last 7 days")
    else:
        add("notification queue", "PASS", f"statuses={notif}")

    # Latest integrity audit per leg
    audits = {}
    for mode in ("lan", "cloud"):
        row = m.execute(
            "SELECT id, mode, scope, started_at, status, files_checked, mismatches, detail "
            "FROM integrity_audits WHERE mode=? ORDER BY id DESC LIMIT 1",
            (mode,),
        ).fetchone()
        audits[mode] = dict(row) if row else None
        if not row:
            add(f"integrity audit {mode.upper()}", "WARN", "no audit recorded")
            continue
        status = row["status"]
        detail = f"{row['scope']} {status} at {row['started_at']}, checked={row['files_checked']}, mismatches={row['mismatches']}"
        if status == "VERIFICATION_FAILED":
            add(f"integrity audit {mode.upper()}", "FAIL", detail)
        elif status in ("AUDIT_INCOMPLETE", "NOT_VERIFIED"):
            add(f"integrity audit {mode.upper()}", "WARN", detail)
        else:
            # VERIFIED and PENDING_BACKUP (newer source files waiting) are normal
            add(f"integrity audit {mode.upper()}", "PASS", detail)
    report["manifest"]["integrity_audits"] = audits
    m.close()

    # ---------------- Prefect DB ----------------
    p = open_ro(prefect_path)
    p_int = db_integrity(p)
    p_size = os.path.getsize(prefect_path)
    p_wal = os.path.getsize(prefect_path + "-wal") if os.path.exists(prefect_path + "-wal") else 0
    p_counts = table_counts(p, ("flow_run", "task_run", "flow_run_state", "log", "deployment"))
    alembic = p.execute("SELECT version_num FROM alembic_version").fetchone()
    state_counts = {
        r["state_type"]: r["n"]
        for r in p.execute("SELECT state_type, COUNT(*) n FROM flow_run GROUP BY state_type")
    }
    failed_48h = p.execute(
        "SELECT COUNT(*) FROM flow_run WHERE state_type IN ('FAILED','CRASHED') "
        "AND created >= datetime('now','-2 day')"
    ).fetchone()[0]
    failed_7d = p.execute(
        "SELECT COUNT(*) FROM flow_run WHERE state_type IN ('FAILED','CRASHED') "
        "AND created >= datetime('now','-7 day')"
    ).fetchone()[0]
    last_completed = p.execute(
        "SELECT MAX(end_time) FROM flow_run WHERE state_type='COMPLETED'"
    ).fetchone()[0]
    last_activity = p.execute("SELECT MAX(created) FROM flow_run").fetchone()[0]
    next_scheduled = p.execute(
        "SELECT MIN(expected_start_time) FROM flow_run WHERE state_type='SCHEDULED'"
    ).fetchone()[0]
    overdue = p.execute(
        "SELECT id, name, expected_start_time FROM flow_run "
        "WHERE state_type='SCHEDULED' AND expected_start_time < datetime('now','-30 minute') "
        "ORDER BY expected_start_time LIMIT 5"
    ).fetchall()
    recent_errors = p.execute(
        "SELECT COUNT(*) FROM log WHERE level >= 40 AND timestamp >= datetime('now','-1 day')"
    ).fetchone()[0]
    deployments = [
        dict(r)
        for r in p.execute(
            "SELECT d.name, d.paused, d.status, s.schedule, s.active "
            "FROM deployment d LEFT JOIN deployment_schedule s ON s.deployment_id = d.id "
            "ORDER BY d.name"
        )
    ]

    report["prefect"] = {
        "path": prefect_path,
        "size_bytes": p_size,
        "wal_bytes": p_wal,
        "tables": p_counts,
        "alembic_version": alembic[0] if alembic else None,
        "flow_run_states": state_counts,
        "failed_last_48h": failed_48h,
        "failed_last_7d": failed_7d,
        "last_completed_end": last_completed,
        "last_flow_run_created": last_activity,
        "next_scheduled": next_scheduled,
        "overdue_scheduled": [dict(r) for r in overdue],
        "error_logs_last_24h": recent_errors,
        "deployments": deployments,
        **p_int,
    }

    add("prefect DB integrity", "PASS" if p_int["quick_check"] == "ok" else "FAIL",
        f"quick_check={p_int['quick_check']}, fk_violations={p_int['foreign_key_violations']}")
    if p_int["foreign_key_violations"]:
        add("prefect DB foreign keys", "FAIL",
            f"{p_int['foreign_key_violations']} violation(s)")

    if failed_48h:
        add("prefect failed runs", "WARN", f"{failed_48h} failed/crashed in the last 48h")
    else:
        add("prefect failed runs", "PASS", f"0 in 48h ({failed_7d} in 7d)")

    if overdue:
        add("prefect overdue schedules", "WARN",
            f"{len(overdue)} scheduled run(s) overdue, oldest {overdue[0]['expected_start_time']}")
    else:
        add("prefect overdue schedules", "PASS", f"next={next_scheduled} (UTC)")

    activity_age = hours_since(last_activity, now)
    if activity_age is None:
        add("prefect activity", "WARN", "no flow runs recorded")
    elif activity_age > fail_h:
        add("prefect activity", "WARN", f"last flow run created {activity_age:.1f}h ago")
    else:
        add("prefect activity", "PASS", f"last flow run created {activity_age:.1f}h ago")

    if recent_errors:
        add("prefect error logs", "WARN", f"{recent_errors} ERROR+ line(s) in the last 24h")
    else:
        add("prefect error logs", "PASS", "0 ERROR+ lines in the last 24h")

    p.close()

    print(json.dumps(report, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
