"""Compare manifest.file_entries with the live source tree.

Runs on the backup server, read-only. Sizes only: content hashes are verified
separately by hash_tree.py + compare_trees.py.

Prints one JSON object with counts and verdicts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

OK_APP = {"LAN_COMPLETE", "CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"}


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: entries_check.py <manifest.db> <source_dir> [canary]", file=sys.stderr)
        return 2

    db_path, source_dir = sys.argv[1], sys.argv[2]
    canary = sys.argv[3] if len(sys.argv) > 3 else ".AAM_TARGET_MOUNTED"

    src = {}
    for dirpath, _dirnames, filenames in os.walk(source_dir):
        for fn in filenames:
            if fn == canary:
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, source_dir).replace("\\", "/")
            try:
                src[rel] = os.path.getsize(path)
            except OSError:
                continue

    con = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    rows = {
        r["relative_path"]: dict(r)
        for r in con.execute(
            "SELECT relative_path, file_size, lan_status, cloud_status, "
            "lan_size, cloud_size, updated_at FROM file_entries"
        )
    }
    con.close()

    db_content = {p: r for p, r in rows.items() if os.path.basename(p) != canary}
    control_rows = {p: r for p, r in rows.items() if os.path.basename(p) == canary}

    missing_from_db = sorted(set(src) - set(db_content))
    stale_rows = sorted(set(db_content) - set(src))
    size_diff = sorted(
        p for p in set(src) & set(db_content) if src[p] != db_content[p]["file_size"]
    )
    not_synced = sorted(
        p
        for p, r in db_content.items()
        if p in src and (r["lan_status"] != "synced" or r["cloud_status"] != "synced")
    )

    # Tombstones are bookkeeping only. A handful is normal after deletions;
    # treat a growing pile as the signal worth a warning.
    stale_limit = max(5, len(src) // 100)
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    if missing_from_db:
        add("source files in manifest", "FAIL", f"{len(missing_from_db)} source file(s) missing from DB")
    else:
        add("source files in manifest", "PASS", f"all {len(src)} source files present")

    if size_diff:
        add("manifest sizes", "FAIL", f"{len(size_diff)} recorded size(s) differ from source")
    else:
        add("manifest sizes", "PASS", f"all {len(src)} sizes match")

    if len(stale_rows) > stale_limit:
        add("manifest stale rows", "WARN",
            f"{len(stale_rows)} tombstone row(s) not in source: " + ", ".join(stale_rows[:3]))
    elif stale_rows:
        add("manifest stale rows", "PASS",
            f"{len(stale_rows)} tombstone row(s) from deleted files (harmless)")
    else:
        add("manifest stale rows", "PASS", "no stale rows")

    if not_synced:
        add("manifest sync status", "WARN",
            f"{len(not_synced)} content row(s) not synced on both legs")
    else:
        add("manifest sync status", "PASS", f"all {len(src)} live content rows synced LAN+cloud")

    print(
        json.dumps(
            {
                "source_files": len(src),
                "db_rows": len(rows),
                "content_rows": len(db_content),
                "control_rows": len(control_rows),
                "missing_from_db": missing_from_db[:20],
                "missing_from_db_count": len(missing_from_db),
                "stale_rows": stale_rows[:20],
                "stale_row_count": len(stale_rows),
                "size_mismatch": size_diff[:20],
                "size_mismatch_count": len(size_diff),
                "not_synced": not_synced[:20],
                "not_synced_count": len(not_synced),
                "checks": checks,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
