"""Contract assertions over OBSERVED application state.

Each check reads what the real code persisted/reported (DB rows, Prefect
states, log excerpts, hashes) and compares against contract literals.
Nothing here recomputes verdicts — e.g. we never re-derive LAN_COMPLETE
from an exit code; we assert the application's own recorded status.
"""

import sqlite3

from chaos_harness import safety

CHAOS_DB = r"C:\ChaosRuntime\manifest.db"


def _ro_conn(db_path: str = CHAOS_DB) -> sqlite3.Connection:
    safety.assert_chaos_path(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def latest_run(mode: str, db_path: str = CHAOS_DB) -> dict | None:
    conn = _ro_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM run_history WHERE mode = ? ORDER BY started_at DESC LIMIT 1",
            (mode,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def count_table(table: str, where: str = "", db_path: str = CHAOS_DB) -> int:
    conn = _ro_conn(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]
    finally:
        conn.close()


def latest_audit(mode: str, db_path: str = CHAOS_DB) -> dict | None:
    conn = _ro_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM integrity_audits WHERE mode = ? "
            "ORDER BY started_at DESC LIMIT 1", (mode,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def check(condition: bool, label: str, detail: str = "") -> dict:
    return {"label": label, "pass": bool(condition), "detail": detail}


def assert_status(row: dict | None, expected: str, label: str) -> dict:
    actual = (row or {}).get("status")
    return check(actual == expected, label,
                 f"expected={expected} actual={actual}")


def assert_unchanged(before: int, after: int, label: str) -> dict:
    return check(before == after, label, f"before={before} after={after}")


def assert_contains(haystack: str | None, needle: str, label: str) -> dict:
    return check(needle in (haystack or ""), label, f"needle={needle!r}")


def assert_not_contains(haystack: str | None, needle: str, label: str) -> dict:
    return check(needle not in (haystack or ""), label, f"needle={needle!r}")


def summarize(results: list[dict]) -> dict:
    failed = [r for r in results if not r["pass"]]
    return {"total": len(results), "passed": len(results) - len(failed),
            "failed": len(failed), "failures": failed,
            "verdict": "PASS" if not failed else "FAIL"}
