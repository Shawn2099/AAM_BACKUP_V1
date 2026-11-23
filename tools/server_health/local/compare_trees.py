"""Compare two hash_tree.py TSV manifests (source vs destination).

Exit 0 when every source file exists at the destination with the same size and
MD5 and there are no unexpected extras. Exit 1 on any difference. Writes a
human-readable summary and, optionally, JSON.
"""

from __future__ import annotations

import argparse
import io
import json
import sys


def detect_encoding(path: str) -> str:
    with open(path, "rb") as fh:
        head = fh.read(4)
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "utf-16"
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "utf-8"


def load(path: str) -> dict:
    rows = {}
    with open(path, encoding=detect_encoding(path)) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            rel, size, md5, _mtime = parts
            rows[rel.replace("\\", "/")] = (int(size), md5.lower())
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--source-label", default="source")
    parser.add_argument("--dest-label", default="destination")
    parser.add_argument("--json", default="")
    parser.add_argument("--max-list", type=int, default=20)
    args = parser.parse_args()

    src = load(args.source)
    dest = load(args.dest)

    missing = sorted(set(src) - set(dest))
    extra = sorted(set(dest) - set(src))
    size_diff = sorted(p for p in set(src) & set(dest) if src[p][0] != dest[p][0])
    hash_diff = sorted(p for p in set(src) & set(dest) if src[p][1] != dest[p][1])

    src_bytes = sum(v[0] for v in src.values())
    dest_bytes = sum(v[0] for v in dest.values())
    ok = not (missing or extra or size_diff or hash_diff)

    result = {
        "status": "PASS" if ok else "FAIL",
        "source": args.source_label,
        "destination": args.dest_label,
        "source_files": len(src),
        "source_bytes": src_bytes,
        "destination_files": len(dest),
        "destination_bytes": dest_bytes,
        "missing_at_destination": len(missing),
        "extra_at_destination": len(extra),
        "size_mismatch": len(size_diff),
        "hash_mismatch": len(hash_diff),
        "missing_sample": missing[: args.max_list],
        "extra_sample": extra[: args.max_list],
        "size_mismatch_sample": size_diff[: args.max_list],
        "hash_mismatch_sample": hash_diff[: args.max_list],
    }

    print(
        f"{args.source_label} -> {args.dest_label}: "
        f"files {len(src)} vs {len(dest)}, bytes {src_bytes} vs {dest_bytes}, "
        f"missing={len(missing)} extra={len(extra)} "
        f"size_diff={len(size_diff)} hash_diff={len(hash_diff)} "
        f"=> {'PASS' if ok else 'FAIL'}"
    )
    for label, rows in (
        ("MISSING at destination", missing),
        ("EXTRA at destination", extra),
        ("SIZE mismatch", size_diff),
        ("HASH mismatch", hash_diff),
    ):
        if rows:
            print(f"  {label}:")
            for rel in rows[: args.max_list]:
                print(f"    {rel} src={src.get(rel)} dest={dest.get(rel)}")
            if len(rows) > args.max_list:
                print(f"    ... {len(rows) - args.max_list} more")

    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
