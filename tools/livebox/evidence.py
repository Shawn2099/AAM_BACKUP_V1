r"""Long-term evidence dataset helpers for the livebox harness (see tests/test_evidence_dataset.py for the pinned API).

All functions are read-only on product data (the manifest DB is opened with mode=ro or the backup API), write only
below the evidence directory they are given, and never raise on a missing source.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_VERSIONS: dict[str, int] = {
    "inputs.jsonl": 1,
    "expected.jsonl": 1,
    "config_snapshot.json": 1,
    "identity.json": 1,
    "env_versions.json": 1,
    "db_dump/*.jsonl": 1,
    "db_dump/*.jsonl.gz": 1,
    "trees/*.jsonl": 1,
    "logs_index.json": 1,
    "timing.jsonl": 1,
    "dataset_manifest.json": 1,
}

REDACTED = "***REDACTED***"
_SECRET_MARKERS = ("password", "secret", "token", "api_key")
DB_TABLES = ("run_history", "notifications", "db_meta", "approvals")
MANIFEST_NAME = "dataset_manifest.json"
README_NAME = "README_DATASET.md"


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v


# ------------------------------------------------------------------------------------------------ inputs log
class InputsLog:
    """Append-only inputs.jsonl; seq continues after a restart."""

    def __init__(self, path, sim_now: Callable[[], dt.datetime] | None = None):
        self.path = Path(path)
        self.sim_now = sim_now
        self._seq = 0
        try:
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            self._seq = max(self._seq, int(json.loads(line).get("seq", 0)))
                        except (ValueError, TypeError):
                            self._seq += 1
        except OSError:
            pass

    def record(self, kind: str, name: str, args=None, **extra) -> dict:
        self._seq += 1
        sim = None
        if self.sim_now is not None:
            try:
                v = self.sim_now()
                sim = v.isoformat() if v is not None else None
            except Exception:  # noqa: BLE001 - evidence must never break the run
                sim = None
        row = {
            "seq": self._seq,
            "kind": kind,
            "name": name,
            "args": args,
            "sim_time": sim,
            "real_time": dt.datetime.now(dt.timezone.utc).isoformat(),
            **extra,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        return row


# ------------------------------------------------------------------------------------------------ config / identity
def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(m in k.lower() for m in _SECRET_MARKERS):
                out[k] = REDACTED
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


def config_snapshot(raw: dict) -> tuple[dict, str]:
    red = _redact(raw)
    canon = json.dumps(red, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return red, _sha256_bytes(canon)


def _git_commit(code_root: Path) -> str | None:
    try:
        top = subprocess.run(["git", "-C", str(code_root), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=20)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != Path(code_root).resolve():
            return None
        r = subprocess.run(["git", "-C", str(code_root), "rev-parse", "HEAD"], capture_output=True, text=True,
                           timeout=20)
        c = r.stdout.strip()
        return c if r.returncode == 0 and len(c) == 40 else None
    except Exception:  # noqa: BLE001
        return None


def harness_identity(code_root, harness_dir) -> dict:
    code_root, harness_dir = Path(code_root), Path(harness_dir)
    files: dict[str, str] = {}
    try:
        for p in sorted(harness_dir.rglob("*.py")):
            if "__pycache__" in p.parts or not p.is_file():
                continue
            files[p.relative_to(harness_dir).as_posix()] = _sha256_file(p)
    except OSError:
        pass
    try:
        from importlib import metadata
        tm = metadata.version("time_machine")
    except Exception:  # noqa: BLE001
        tm = None
    return {
        "git_commit": _git_commit(code_root),
        "harness_files": files,
        "time_machine_version": tm,
        "python_version": sys.version.split()[0],
    }


# ------------------------------------------------------------------------------------------------ runtime environment
ENV_PACKAGES = ("loguru", "prefect", "time_machine", "pydantic")
BEYOND_2038 = dt.date(2038, 1, 19)     # 32-bit time_t limit; the lane clock must stay before it (recorded, not enforced)


def _package_version(name: str) -> str:
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:  # noqa: BLE001 - a missing or broken package is recorded as n/a, never raised
        return "n/a"


def env_versions(last_sim_date: dt.date | None = None) -> dict:
    """Runtime environment of this harness run: interpreter, platform, key package versions and the 2038 check."""
    beyond = bool(last_sim_date is not None and last_sim_date >= BEYOND_2038)
    return {
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "packages": {p: _package_version(p) for p in ENV_PACKAGES},
        "last_sim_date": _iso(last_sim_date),
        "beyond_2038": beyond,
        "crt_post_2038_ok": not beyond,
    }


def env_summary_line(env: dict) -> str:
    pk = " ".join(f"{k}={v}" for k, v in env["packages"].items())
    return (f"env: python {env['python_version']} ({env['python_executable']}) on {env['platform']}; {pk}; "
            f"last_sim_date={env['last_sim_date']} beyond_2038={env['beyond_2038']}")


def write_env_versions(ev_dir, last_sim_date: dt.date | None = None) -> Path:
    ev_dir = Path(ev_dir)
    ev_dir.mkdir(parents=True, exist_ok=True)
    out = ev_dir / "env_versions.json"
    out.write_text(json.dumps(env_versions(last_sim_date), indent=1, default=str), encoding="utf-8")
    return out


# ------------------------------------------------------------------------------------------------ expected rows
def expected_rows(days) -> list[dict]:
    out = []
    for d in days:
        out.append({
            "day": d.date.isoformat(),
            "events": [{"at": _iso(e.at), "kind": e.kind, "fy": e.fy} for e in d.events],
            "rows": [{"leg": r.leg, "at": _iso(r.at), "fy": r.fy, "pass_kind": r.pass_kind,
                      "run_suffix": r.run_suffix, "statuses": list(r.statuses), "order": r.order}
                     for r in d.rows],
            "mails": [{"mail_class": m.mail_class, "count": m.count, "key": m.key, "at": _iso(m.at),
                       "recipients": m.recipients, "fy": m.fy} for m in d.mails],
            "flags": {
                "is_first_run": d.is_first_run, "is_sunday": d.is_sunday, "is_monday": d.is_monday,
                "is_first_of_month": d.is_first_of_month, "is_leap_day": d.is_leap_day,
                "is_activation_day": d.is_activation_day, "is_switch_day": d.is_switch_day,
                "overlap_active_end": d.overlap_active_end, "overlap_active_at_0900": d.overlap_active_at_0900,
                "retention_boundary": d.retention_boundary, "outage": d.outage, "skipped": list(d.skipped),
                "configured_fy": d.configured_fy, "configured_fy_end": d.configured_fy_end,
                "next_fy_end": d.next_fy_end, "rollover_result": d.rollover_result,
                "activation_result": d.activation_result,
            },
        })
    return out


def write_expected(ev_dir, days) -> Path:
    ev_dir = Path(ev_dir)
    ev_dir.mkdir(parents=True, exist_ok=True)
    p = ev_dir / "expected.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for row in expected_rows(days):
            f.write(json.dumps(row) + "\n")
    return p


# ------------------------------------------------------------------------------------------------ db
def _ro_connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def _jsonable(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(bytes(v)).decode("ascii")}
    return v


def _prior_row_keys(dump_dir: Path, day: str) -> set:
    """Canonical (table, row-json) keys of every row in dumps of earlier days."""
    import gzip
    seen: set = set()
    for p in sorted(dump_dir.glob("*.jsonl.gz")):
        if p.name[:-len(".jsonl.gz")] >= day:
            continue
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    o = json.loads(line)
                    seen.add((o["table"], json.dumps(o["row"], sort_keys=True, default=str)))
        except (OSError, ValueError, KeyError):
            continue
    return seen


def dump_db(db_path, dump_dir, day: str, day_index=None) -> dict:
    """Write <dump_dir>/<day>.jsonl.gz: a full dump when day_index is None or divisible by 7, else a delta
    (rows new or changed by content since the earlier dumps; the file exists even when empty)."""
    import gzip
    db_path = Path(db_path)
    counts: dict[str, int] = {}
    if not db_path.is_file():
        return counts
    rows: list = []
    try:
        con = _ro_connect(db_path)
    except sqlite3.Error:
        return counts
    try:
        existing = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        for t in DB_TABLES:
            if t not in existing:
                continue
            cur = con.execute(f'select * from "{t}"')
            cols = [c[0] for c in cur.description]
            for row in cur.fetchall():
                rows.append((t, {c: _jsonable(v) for c, v in zip(cols, row)}))
            counts[t] = 0
    except sqlite3.Error:
        pass
    finally:
        con.close()
    dump_dir = Path(dump_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)
    full = day_index is None or day_index % 7 == 0
    prior = set() if full else _prior_row_keys(dump_dir, day)
    lines: list[str] = []
    for t, row in rows:
        if not full and (t, json.dumps(row, sort_keys=True, default=str)) in prior:
            continue
        lines.append(json.dumps({"day": day, "table": t, "row": row}, default=str))
        counts[t] = counts.get(t, 0) + 1
    with gzip.open(dump_dir / f"{day}.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write("".join(x + "\n" for x in lines))
    return counts


def copy_manifest_db(db_path, dest) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    db_path = Path(db_path)
    if not db_path.is_file():
        return dest
    try:
        src = _ro_connect(db_path)
    except sqlite3.Error:
        return dest
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error:
        pass
    finally:
        src.close()
    return dest


# ------------------------------------------------------------------------------------------------ logs
def copy_capped(src, dst, cap_bytes: int) -> dict:
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    size = src.stat().st_size
    digest = _sha256_file(src)
    if size <= cap_bytes:
        dst.write_bytes(src.read_bytes())
        return {"src": str(src), "dst": str(dst), "bytes": size, "kept": size, "truncated": False,
                "sha256": digest}
    with open(src, "rb") as f:
        bom = f.read(2)
    if bom in (b"\xff\xfe", b"\xfe\xff"):
        half = (cap_bytes // 2) // 2 * 2
        with open(src, "rb") as f:
            f.seek(2)
            head = f.read(half)
            f.seek(size - half)
            tail = f.read(half)
        data = bom + head + tail
        dst.write_bytes(data)
        omitted = size - len(data)
        dst.with_name(dst.name + ".truncated").write_text(
            f"truncated: {omitted} bytes omitted from the middle of the UTF-16 file {src.name}\n",
            encoding="utf-8")
        return {"src": str(src), "dst": str(dst), "bytes": size, "kept": len(data), "truncated": True,
                "sha256": digest, "encoding": "utf-16"}
    half = cap_bytes // 2
    with open(src, "rb") as f:
        head = f.read(half)
        f.seek(size - half)
        tail = f.read(half)
    omitted = size - 2 * half
    data = head + f"\n...[TRUNCATED {omitted} bytes]...\n".encode("ascii") + tail
    dst.write_bytes(data)
    return {"src": str(src), "dst": str(dst), "bytes": size, "kept": len(data), "truncated": True,
            "sha256": digest}


def collect_logs(runtime_dir, ev_dir, cap_bytes: int) -> list[dict]:
    runtime_dir, ev_dir = Path(runtime_dir), Path(ev_dir)
    records: list[dict] = []
    for sub in ("logs", "run_logs"):
        base = runtime_dir / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            try:
                if not p.is_file():
                    continue
                rel = p.relative_to(runtime_dir)
                rec = copy_capped(p, ev_dir / "product_logs" / rel, cap_bytes)
                rec["src"] = rel.as_posix()
                rec["dst"] = (Path("product_logs") / rel).as_posix()
                records.append(rec)
            except OSError:
                continue
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "logs_index.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
    return records


# ------------------------------------------------------------------------------------------------ trees / timing
def tree_listing(root, ignore: Iterable[str] = ()) -> list[dict]:
    root = Path(root)
    ign = set(ignore)
    rows = []
    if not root.is_dir():
        return rows
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel in ign or p.name in ign:
                continue
            st = p.stat()
            rows.append({"path": rel, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                         "sha256": _sha256_file(p)})
        except OSError:
            continue
    rows.sort(key=lambda r: r["path"])
    return rows


def write_tree_listing(ev_dir, day: str, kind: str, root, ignore: Iterable[str] = ()) -> Path:
    p = Path(ev_dir) / "trees" / f"{day}_{kind}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in tree_listing(root, ignore)), encoding="utf-8")
    return p


class ResourceTimer:
    def __init__(self):
        self.seconds = 0.0
        self.peak_rss_bytes: int | None = None
        self._t0 = 0.0

    @staticmethod
    def _peak() -> int | None:
        try:
            import psutil
            mi = psutil.Process().memory_info()
            v = getattr(mi, "peak_wset", None) or getattr(mi, "peak_pagefile", None) or mi.rss
            return int(v) if v else None
        except Exception:  # noqa: BLE001
            pass
        try:
            import resource
            v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return int(v) * (1 if sys.platform == "darwin" else 1024) or None
        except Exception:  # noqa: BLE001
            return None

    def __enter__(self):
        import time
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        import time
        self.seconds = time.perf_counter() - self._t0
        self.peak_rss_bytes = self._peak()
        return False

    def as_dict(self) -> dict:
        return {"seconds": self.seconds, "peak_rss_bytes": self.peak_rss_bytes}


# ------------------------------------------------------------------------------------------------ packaging
_FORMATS = {".jsonl": "jsonl", ".json": "json", ".csv": "csv", ".eml": "eml", ".db": "sqlite", ".sqlite": "sqlite",
            ".log": "log", ".md": "markdown"}

_DESCRIPTIONS = {
    "inputs.jsonl": "every input the harness applied (knobs, injections, outages, clock moves, flow runs)",
    "expected.jsonl": "the calendar's expected events, run_history rows and mails per day",
    "config_snapshot.json": "redacted lane configuration with its sha256",
    "identity.json": "git commit, harness file hashes, tool versions",
    "env_versions.json": "python, platform and key package versions, last simulated date and the 2038 check",
    "manifest.db": "consistent copy of the lane manifest SQLite database",
    "checks.jsonl": "oracle check results, one line per check",
    "logs_index.json": "index of copied product logs with sizes, hashes and truncation flags",
    "timing.jsonl": "per-step duration and peak memory",
    "mail_index.csv": "index of captured mails",
    "db_dump": "db_dump/<day>.jsonl: per-day dump of run_history, notifications, db_meta, approvals",
    "mail": "mail/*.eml: every captured outgoing mail",
    "trees": "trees/<day>_<kind>.jsonl: file listings of source, cloud and nas trees",
    "product_logs": "product_logs/**: copies of runtime logs and run_logs (capped)",
}


def _fmt(rel: str) -> str:
    if rel.endswith(".jsonl.gz"):
        return "jsonl.gz"
    return _FORMATS.get(Path(rel).suffix.lower(), "text")


def _fmt_version(rel: str) -> int:
    if rel in SCHEMA_VERSIONS:
        return SCHEMA_VERSIONS[rel]
    top = rel.split("/")[0]
    if top == "db_dump" and (rel.endswith(".jsonl") or rel.endswith(".jsonl.gz")):
        return SCHEMA_VERSIONS["db_dump/*.jsonl"]
    if top == "trees" and rel.endswith(".jsonl"):
        return SCHEMA_VERSIONS["trees/*.jsonl"]
    return 1


def _describe(key: str) -> str:
    return _DESCRIPTIONS.get(key, "evidence file")


def _list_files(ev_dir: Path, exclude: Iterable[str] = ()) -> list[str]:
    ex = set(exclude)
    out = []
    for p in ev_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(ev_dir).as_posix()
            if rel not in ex:
                out.append(rel)
    return sorted(out)


def _write_readme(ev_dir: Path) -> None:
    files = _list_files(ev_dir, (MANIFEST_NAME, README_NAME))
    top_files = [f for f in files if "/" not in f]
    folders = sorted({f.split("/")[0] for f in files if "/" in f})
    lines = ["# Livebox evidence dataset", "",
             "Every file of this lane's inputs and outputs. Hashes and sizes are in `dataset_manifest.json`.", "",
             "## Files", ""]
    for f in top_files:
        lines.append(f"- `{f}`: {_describe(f)}")
    lines += ["", "## Folders", ""]
    for d in folders:
        n = sum(1 for f in files if f.startswith(d + "/"))
        lines.append(f"- `{d}/` ({n} files): {_describe(d)}")
    lines += ["", f"- `{MANIFEST_NAME}`: list of every file here with bytes, sha256, format and format version", ""]
    (ev_dir / README_NAME).write_text("\n".join(lines), encoding="utf-8")


def write_dataset_manifest(ev_dir) -> Path:
    ev_dir = Path(ev_dir)
    ev_dir.mkdir(parents=True, exist_ok=True)
    _write_readme(ev_dir)
    files = []
    for rel in _list_files(ev_dir, (MANIFEST_NAME,)):
        p = ev_dir / rel
        files.append({"path": rel, "bytes": p.stat().st_size, "sha256": _sha256_file(p), "format": _fmt(rel),
                      "format_version": _fmt_version(rel)})
    out = ev_dir / MANIFEST_NAME
    out.write_text(json.dumps({"schema_version": 1, "files": files}, indent=1), encoding="utf-8")
    return out


def export_evidence(ev_dir, dest_zip) -> Path:
    ev_dir = Path(ev_dir)
    if not ev_dir.is_dir():
        raise FileNotFoundError(str(ev_dir))
    write_dataset_manifest(ev_dir)
    dest_zip = Path(dest_zip)
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in _list_files(ev_dir):
            zf.write(ev_dir / rel, rel)
    return dest_zip
