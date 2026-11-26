"""F3: ManifestDB rolls back on failed write (e.g. database is locked) so reads never go stale."""
from __future__ import annotations

import sqlite3
import threading
import time

from core.manifest import ManifestDB


def test_f3_insert_run_rolls_back_on_lock_so_reads_stay_fresh(tmp_path):
    db_path = tmp_path / "m.db"
    db = ManifestDB(db_path, busy_timeout_ms=50)
    db._get_conn()
    other = sqlite3.connect(str(db_path), timeout=0.05)
    try:
        # Hold an IMMEDIATE write lock from another connection so insert_run fails with database is locked.
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "INSERT INTO db_meta (key, value) VALUES ('outside', 'v1')"
        )
        # Do not commit yet; let insert_run fail on lock.
        try:
            db.insert_run({
                "run_id": "r1",
                "mode": "cloud",
                "started_at": "2026-11-26T10:00:00+05:30",
                "status": "CLOUD_COMPLETE",
            })
        except sqlite3.OperationalError:
            pass

        # Now commit the external connection's write.
        other.commit()

        # db's connection must NOT be stuck in an open transaction with a stale snapshot!
        assert not db._get_conn().in_transaction
        assert db._meta("outside") == "v1"
    finally:
        other.close()
        db.close()
