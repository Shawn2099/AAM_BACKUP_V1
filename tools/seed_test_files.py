r"""Fill a folder with random test files so a backup run can be checked end to end.

  python tools/seed_test_files.py --dest D:\FY26-27                 # 40 new files in D:\FY26-27\_seed_test\
  python tools/seed_test_files.py --from-config --count 100         # folder = paths.source_drive of config.yaml
  python tools/seed_test_files.py --dest D:\FY26-27 --modify 5      # change 5 earlier seeded files
  python tools/seed_test_files.py --dest D:\FY26-27 --delete 2      # remove 2 earlier seeded files

Every file is under 2 MB, in a mix of formats (txt, csv, json, xml, html, md, log, png, bmp,
pdf, zip, docx, xlsx, bin) and sizes (1 KB up to --max-kb). Files go into a `_seed_test` sub-folder
(never mixed with real client files) and are listed in `_seed_test/seed_manifest.csv`.

Use it before a manual run to see "files added", after `--modify` to see "files changed", and
after `--delete` to see deletions reach the copies. `--delete` only ever removes files this
script created. Random content is seeded (`--seed`), so a run can be repeated exactly.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import struct
import sys
import zipfile
import zlib
from pathlib import Path

SEED_DIR = "_seed_test"
MANIFEST = "seed_manifest.csv"
HARD_MAX_KB = 2047            # "under 2 MB" is a promise, not a default
WORDS = ("invoice ledger audit balance receipt voucher client payment tax return asset "
         "liability journal entry gst tds payroll quarter statement reconciliation").split()


def _text(rng: random.Random, size: int) -> bytes:
    out, total = [], 0
    while total < size:
        line = " ".join(rng.choice(WORDS) for _ in range(rng.randint(5, 14))) + "\n"
        out.append(line)
        total += len(line)
    return "".join(out).encode()[:size]


def _csv(rng, size):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "party", "ref", "amount"])
    while buf.tell() < size:
        w.writerow([f"2026-{rng.randint(4, 12):02d}-{rng.randint(1, 28):02d}", rng.choice(WORDS),
                    f"INV-{rng.randint(1000, 99999)}", f"{rng.uniform(10, 99999):.2f}"])
    return buf.getvalue().encode()[:size]


def _json(rng, size):
    rows, total = [], 2
    while total < size:
        row = {"id": rng.randint(1, 10**6), "note": rng.choice(WORDS), "amount": round(rng.uniform(1, 9999), 2)}
        rows.append(row)
        total += len(json.dumps(row)) + 2
    return json.dumps(rows).encode()


def _xml(rng, size):
    body = "".join(f"<entry id=\"{i}\">{rng.choice(WORDS)}</entry>\n" for i in range(max(1, size // 30)))
    return f"<?xml version=\"1.0\"?>\n<ledger>\n{body}</ledger>\n".encode()


def _html(rng, size):
    body = "".join(f"<p>{_text(rng, 80).decode()}</p>\n" for _ in range(max(1, size // 90)))
    return f"<!doctype html><html><body>\n{body}</body></html>\n".encode()


def _random(rng, size):
    return rng.randbytes(size)


def _png(rng, size):
    """A valid PNG of random noise, about `size` bytes (noise barely compresses)."""
    width = 64
    height = max(1, size // (width * 3 + 1))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + rng.randbytes(width * 3) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b"")


def _bmp(rng, size):
    width = 100
    row = width * 3 + (-width * 3) % 4
    height = max(1, (size - 54) // row)
    data = rng.randbytes(row * height)
    header = b"BM" + struct.pack("<IHHI", 54 + len(data), 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(data), 2835, 2835, 0, 0)
    return header + dib + data


def _pdf(rng, size):
    text = _text(rng, max(200, size - 400)).decode().replace("(", "[").replace(")", "]").replace("\\", "/")
    stream = ("BT /F1 10 Tf 40 780 Td 12 TL\n" +
              "".join(f"({line[:90]}) '\n" for line in text.splitlines()[:60]) + "ET").encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out + b"%" + rng.randbytes(max(0, size - len(out) - 1)).replace(b"\n", b"x")  # pad after EOF


def _zip(rng, size):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("data.bin", rng.randbytes(max(1, size - 200)))
    return buf.getvalue()


def _office(kind):
    def make(rng, size):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "<?xml version=\"1.0\"?><Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"/>")
            z.writestr(f"{kind}/document.xml", _xml(rng, max(100, size // 2)))
            z.writestr("docProps/blob.bin", rng.randbytes(max(1, size // 2)))
        return buf.getvalue()
    return make


FORMATS = {".txt": _text, ".md": _text, ".log": _text, ".csv": _csv, ".json": _json, ".xml": _xml,
           ".html": _html, ".png": _png, ".bmp": _bmp, ".pdf": _pdf, ".zip": _zip,
           ".docx": _office("word"), ".xlsx": _office("xl"), ".bin": _random}
PREFIXES = ("invoice", "ledger", "statement", "scan", "return", "voucher", "notes", "backup_test")


def _size(rng: random.Random, max_bytes: int) -> int:
    """Many small files, some large: log-uniform between 1 KB and max_bytes."""
    return int(2 ** rng.uniform(10, max(10.1, min(21, max_bytes.bit_length()))) ) if max_bytes > 1024 else 1024


def make_file(path: Path, rng: random.Random, max_bytes: int) -> int:
    # leave room for headers: structured formats (png, zip, pdf) overshoot their target a little
    size = max(1024, min(_size(rng, max_bytes), max_bytes - 8192))
    data = FORMATS[path.suffix](rng, size)
    path.write_bytes(data[:max_bytes])
    return path.stat().st_size


def _read_manifest(folder: Path) -> list[str]:
    m = folder / MANIFEST
    if not m.exists():
        return []
    with m.open(newline="", encoding="utf-8") as f:
        return [row["file"] for row in csv.DictReader(f)]


def _write_manifest(folder: Path, files: list[str]) -> None:
    with (folder / MANIFEST).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file", "bytes"])
        for rel in sorted(files):
            p = folder / rel
            w.writerow([rel, p.stat().st_size if p.exists() else 0])


def seed(dest: Path, *, count: int, max_kb: int, seed_value: int | None, modify: int = 0,
         delete: int = 0, subfolders: int = 4) -> dict:
    max_bytes = min(max_kb, HARD_MAX_KB) * 1024
    rng = random.Random(seed_value)
    folder = dest / SEED_DIR
    folder.mkdir(parents=True, exist_ok=True)
    known = [f for f in _read_manifest(folder) if (folder / f).exists()]
    result = {"added": 0, "modified": 0, "deleted": 0, "bytes": 0}

    for rel in rng.sample(known, min(delete, len(known))):
        (folder / rel).unlink()
        known.remove(rel)
        result["deleted"] += 1
    for rel in rng.sample(known, min(modify, len(known))):
        result["bytes"] += make_file(folder / rel, rng, max_bytes)
        result["modified"] += 1
    stamp = rng.randint(0, 10**6)
    for i in range(count):
        sub = f"client_{rng.randint(1, subfolders):02d}"
        (folder / sub).mkdir(exist_ok=True)
        ext = rng.choice(list(FORMATS))
        name = f"{sub}/{rng.choice(PREFIXES)}_{stamp:06d}_{i:04d}{ext}"
        result["bytes"] += make_file(folder / name, rng, max_bytes)
        known.append(name)
        result["added"] += 1
    _write_manifest(folder, known)
    result["folder"] = str(folder)
    result["total_files"] = len(known)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--dest", type=Path, help="folder to seed (a _seed_test sub-folder is created in it)")
    where.add_argument("--from-config", action="store_true", help="use paths.source_drive of the active config.yaml")
    ap.add_argument("--count", type=int, default=40, help="new files to create (default 40)")
    ap.add_argument("--max-kb", type=int, default=1900, help=f"largest file in KB (default 1900, at most {HARD_MAX_KB})")
    ap.add_argument("--modify", type=int, default=0, help="rewrite this many files created earlier")
    ap.add_argument("--delete", type=int, default=0, help="delete this many files created earlier")
    ap.add_argument("--seed", type=int, default=None, help="repeat an exact run")
    args = ap.parse_args(argv)
    if min(args.count, args.modify, args.delete) < 0 or args.max_kb < 1:
        ap.error("numbers must be positive")

    if args.from_config:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from models.config import CONFIG_PATH, load_config
        dest = Path(load_config(CONFIG_PATH).paths.source_drive)
    else:
        dest = args.dest
    if not dest.is_dir():
        ap.error(f"{dest} is not an existing folder")
    r = seed(dest, count=args.count, max_kb=args.max_kb, seed_value=args.seed,
             modify=args.modify, delete=args.delete)
    print(f"{r['folder']}: {r['added']} added, {r['modified']} modified, {r['deleted']} deleted "
          f"({r['bytes'] / 1024 / 1024:.1f} MB written, {r['total_files']} seeded files now)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
