"""Hash a directory tree (MD5) and print TSV: relative_path, size, md5, mtime.

Works on the server and on the machine that holds the LAN copy. Stdout is the
TSV, followed by one line prefixed with ##SUMMARY## carrying a JSON summary.
`.AAM_TARGET_MOUNTED`-style control files are skipped by default (use
--keep-control to include them).

Usage:
    python hash_tree.py <root> [--ignore NAME]... [--keep-control]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    keep_control = "--keep-control" in argv
    ignores = []
    while "--ignore" in argv:
        idx = argv.index("--ignore")
        if idx + 1 >= len(argv):
            print("--ignore needs a value", file=sys.stderr)
            return 2
        ignores.append(argv[idx + 1])
        del argv[idx : idx + 2]
    roots = [a for a in argv if not a.startswith("--")]
    if len(roots) != 1:
        print("usage: hash_tree.py <root> [--ignore NAME] [--keep-control]", file=sys.stderr)
        return 2

    root = roots[0]
    if not os.path.isdir(root):
        print(json.dumps({"error": f"not a directory: {root}"}), file=sys.stderr)
        return 2

    skip_names = set() if keep_control else set(ignores) | {".AAM_TARGET_MOUNTED"}
    rows = []
    total = 0
    errors = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn in skip_names:
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root).replace("\\", "/")
            try:
                digest = hashlib.md5()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        digest.update(chunk)
                stat = os.stat(path)
            except OSError as exc:
                errors.append({"path": rel, "error": str(exc)})
                continue
            rows.append((rel, stat.st_size, digest.hexdigest(), int(stat.st_mtime)))
            total += stat.st_size

    for rel, size, md5, mtime in sorted(rows):
        print(f"{rel}\t{size}\t{md5}\t{mtime}")
    print(
        "##SUMMARY##"
        + json.dumps(
            {
                "root": root,
                "count": len(rows),
                "bytes": total,
                "errors": errors,
            }
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
