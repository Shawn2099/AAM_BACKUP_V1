"""Read-only oracles for a live-box lane (plan LIVEBOX_TEST_PLAN_2026-11-24 sections 1.6 and 2, row L7a).

Every function here only reads: the lane's manifest.db is opened with ``mode=ro``, trees are hashed, mail
indexes are parsed, /status JSON is fetched with GET. Each check returns a list of ``Violation`` (empty = pass)
so a driver can collect everything of one day before deciding.

Standard library only; types come from ``tools.livebox.calendar``.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import sqlite3
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:  # package import (normal) or flat import (tests run from the folder)
    from tools.livebox.calendar import Day, ExpectedRow
except ImportError:  # pragma: no cover
    from calendar import Day, ExpectedRow  # type: ignore

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

PROBLEM_KINDS = frozenset({"FAILURE", "ACTION_REQUIRED", "WARNING", "RECOVERED"})
_NEXT_SUFFIX = re.compile(r"-(?:FY\d{2}-\d{2}|rollover-final-(?:cloud|lan))$")


@dataclass(frozen=True)
class Violation:
    rule: str          # "ROWS", "AUDIT", "M1".."M5", "TREE", "STATUS", "SPY", ...
    detail: str

    @property
    def message(self) -> str:
        return self.detail

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


# --------------------------------------------------------------------------------------------- helpers

def parse_ts(value: str | dt.datetime | None) -> dt.datetime | None:
    """ISO text (or datetime) -> aware datetime; a naive value is read as IST."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        d = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            d = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    return d.replace(tzinfo=IST) if d.tzinfo is None else d


def ist_date(value: str | dt.datetime | None) -> dt.date | None:
    d = parse_ts(value)
    return d.astimezone(IST).date() if d else None


# --------------------------------------------------------------------------------------------- DB access

def open_ro(db_path: str | Path) -> sqlite3.Connection:
    """Open the lane's manifest.db read-only (a missing file raises, it is never created)."""
    p = Path(db_path).resolve()
    conn = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _all(conn: sqlite3.Connection, sql: str, args: Sequence[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def run_rows(conn: sqlite3.Connection, date: dt.date | None = None) -> list[dict]:
    rows = _all(conn, "SELECT * FROM run_history ORDER BY started_at, id")
    return [r for r in rows if date is None or ist_date(r["started_at"]) == date]


def notification_rows(conn: sqlite3.Connection, date: dt.date | None = None) -> list[dict]:
    rows = _all(conn, "SELECT * FROM notifications ORDER BY created_at, id")
    return [r for r in rows if date is None or ist_date(r["created_at"]) == date]


def audit_rows(conn: sqlite3.Connection, date: dt.date | None = None) -> list[dict]:
    rows = _all(conn, "SELECT * FROM integrity_audits ORDER BY started_at, id")
    return [r for r in rows if date is None or ist_date(r["started_at"]) == date]


def db_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in _all(conn, "SELECT key, value FROM db_meta")}


def _matches_suffix(run_id: str, suffix: str) -> bool:
    if suffix:
        return run_id.endswith(suffix)
    return not _NEXT_SUFFIX.search(run_id)


def check_run_rows(conn: sqlite3.Connection, day: Day, *, allow_extra: bool = False) -> list[Violation]:
    """Compare the day's ``run_history`` / ``integrity_audits`` rows with ``day.rows``.

    Each expected row consumes exactly one DB row (leg + run-id suffix + status in the allowed set);
    leftover rows, missing rows, a RUNNING row, or a wrong status are violations.
    """
    out: list[Violation] = []
    runs = run_rows(conn, day.date)
    audits = audit_rows(conn, day.date)
    used_r: set[int] = set()
    used_a: set[int] = set()
    for exp in day.rows:
        if exp.leg in ("audit_cloud", "audit_lan"):
            mode = exp.leg.removeprefix("audit_")
            cand = [i for i, a in enumerate(audits) if i not in used_a and a["mode"] == mode]
            if not cand:
                out.append(Violation("AUDIT", f"{day.date}: no integrity_audits row for {mode} {exp.fy}"))
                continue
            i = cand[0]
            used_a.add(i)
            if audits[i]["status"] not in exp.statuses:
                out.append(Violation("AUDIT", f"{day.date}: {mode} audit status {audits[i]['status']} "
                                              f"not in {list(exp.statuses)}"))
            continue
        cand = [i for i, r in enumerate(runs) if i not in used_r and r["mode"] == exp.leg
                and _matches_suffix(r["run_id"], exp.run_suffix)]
        if not cand:
            out.append(Violation("ROWS", f"{day.date}: no run_history row for {exp.leg} "
                                         f"{exp.fy} (suffix '{exp.run_suffix}')"))
            continue
        i = cand[0]
        used_r.add(i)
        if runs[i]["status"] not in exp.statuses:
            out.append(Violation("ROWS", f"{day.date}: {exp.leg} {exp.fy} status {runs[i]['status']} "
                                         f"not in {list(exp.statuses)}"))
    if not allow_extra:
        for i, r in enumerate(runs):
            if i not in used_r:
                out.append(Violation("ROWS", f"{day.date}: unexpected row {r['run_id']} "
                                             f"mode={r['mode']} status={r['status']}"))
        for i, a in enumerate(audits):
            if i not in used_a:
                out.append(Violation("AUDIT", f"{day.date}: unexpected audit row {a['audit_id']} {a['mode']}"))
    out.extend(check_no_running(conn))
    return out


def check_no_running(conn: sqlite3.Connection) -> list[Violation]:
    return [Violation("ROWS", f"row {r['run_id']} is still RUNNING")
            for r in _all(conn, "SELECT run_id FROM run_history WHERE status = 'RUNNING'")]


def check_overlap_meta(conn: sqlite3.Connection, fy: str, *, active: bool, ended: bool = False) -> list[Violation]:
    """``db_meta['overlap:<fy>']`` presence (and ended_at) matches the expected overlap state."""
    raw = db_meta(conn).get(f"overlap:{fy}")
    if not active:
        return [] if raw is None else [Violation("META", f"overlap:{fy} exists but overlap should not")]
    if raw is None:
        return [Violation("META", f"overlap:{fy} missing")]
    try:
        rec = json.loads(raw)
    except ValueError:
        return [Violation("META", f"overlap:{fy} is not JSON")]
    has_end = bool(rec.get("ended_at"))
    if has_end != ended:
        return [Violation("META", f"overlap:{fy} ended_at {'set' if has_end else 'absent'}, expected "
                                  f"{'set' if ended else 'absent'}")]
    return []


# --------------------------------------------------------------------------------------------- run_history (plain)

def run_history_rows(db_path: str | Path, day: dt.date) -> list[dict]:
    """run_history rows whose started_at falls on sim ``day`` (IST), by started_at.

    Each dict: run_id, leg (column ``mode``), fy (``extended_metrics`` JSON key "fy", else None), status.
    """
    conn = open_ro(db_path)
    try:
        rows = run_rows(conn, day)
    finally:
        conn.close()
    out = []
    for r in rows:
        fy = None
        try:
            fy = (json.loads(r.get("extended_metrics") or "{}") or {}).get("fy")
        except (ValueError, AttributeError):
            pass
        out.append({"run_id": r["run_id"], "leg": r["mode"], "fy": fy, "status": r["status"],
                    "started_at": r["started_at"]})
    return out


def check_run_history(db_path: str | Path, day: dt.date, expected: Sequence[Mapping]) -> list[str]:
    """Compare the day's rows with ``expected`` = [{"leg", "fy", "status": str | set}] in order.

    Lists are compared (count, order, leg, fy, status), never totals only. A RUNNING row is always reported.
    """
    rows = run_history_rows(db_path, day)
    problems: list[str] = []
    for r in rows:
        if r["status"] == "RUNNING":
            problems.append(f"{day}: row {r['run_id']} ({r['leg']} {r['fy']}) is still RUNNING")
    if len(rows) != len(expected):
        problems.append(f"{day}: {len(rows)} rows, expected {len(expected)}: "
                        f"{[(r['leg'], r['fy'], r['status']) for r in rows]}")
    for i, (r, e) in enumerate(zip(rows, expected)):
        if r["leg"] != e["leg"]:
            problems.append(f"{day} row {i}: leg {r['leg']}, expected {e['leg']}")
        if e.get("fy") is not None and r["fy"] != e["fy"]:
            problems.append(f"{day} row {i}: fy {r['fy']}, expected {e['fy']}")
        want = e["status"]
        allowed = {want} if isinstance(want, str) else set(want)
        if r["status"] not in allowed:
            problems.append(f"{day} row {i} ({r['leg']} {r['fy']}): status {r['status']} not in {sorted(allowed)}")
    return problems


# --------------------------------------------------------------------------------------------- mail

PROBLEM_SEVERITIES = frozenset({"FAILURE", "ACTION_REQUIRED", "WARNING"})
HEALTHY_FORBIDDEN = PROBLEM_SEVERITIES | {"RECOVERED"}


def _split(text: str | None) -> list[str]:
    return [x.strip() for x in re.split(r"[;,]", text or "") if x.strip()]


def load_mail_index(csv_path: str | Path) -> list[dict]:
    """Read ``mail_index.csv`` (sim_time, real_time, rcpts, subject, key, kind, leg, fy, cls, severity,
    audience). ``rcpts`` becomes a list, ``sim_time`` an aware datetime."""
    out: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=1):
            sim = parse_ts(row.get("sim_time"))
            if sim is None:
                raise ValueError(f"mail_index row {n}: bad sim_time {row.get('sim_time')!r}")
            m = {k: (v or "") for k, v in row.items() if k is not None}
            m["sim_time"] = sim
            m["rcpts"] = _split(row.get("rcpts"))
            out.append(m)
    return out


def join_notifications(mails: Sequence[dict], notes: Sequence[Mapping]) -> list[dict]:
    """Fill key/kind/severity-less fields of index rows from the SENT ``notifications`` row with the same
    subject (in order, each used once): key, kind, mode -> leg, fault_code, audience."""
    pool = [n for n in notes if (n.get("status") or "").upper() == "SENT"]
    used: set[int] = set()
    for m in mails:
        for i, n in enumerate(pool):
            if i in used or (n.get("subject") or "") != m.get("subject"):
                continue
            used.add(i)
            m.setdefault("key", n.get("event_key"))
            m.setdefault("kind", n.get("kind"))
            m.setdefault("leg", n.get("mode"))
            m.setdefault("fault_code", n.get("fault_code"))
            m.setdefault("audience", n.get("audience"))
            break
    return list(mails)


def _sev(m: Mapping) -> str:
    return (m.get("severity") or "").upper()


def _rcpts(m: Mapping) -> list[str]:
    r = m.get("rcpts") or []
    return _split(r) if isinstance(r, str) else list(r)


def _day(m: Mapping) -> dt.date:
    return m["sim_time"].astimezone(IST).date()


def _hours(a: dt.datetime, b: dt.datetime) -> float:
    return (b - a).total_seconds() / 3600.0


def check_m1(mails: Sequence[Mapping], repeat_cooldown_hours: float = 12, stale_repeat_hours: float = 24,
             activation_hours: float = 24) -> list[Violation]:
    """M1: the same notification key is mailed at most once per cooldown."""
    by_key: dict[str, list[Mapping]] = defaultdict(list)
    for m in mails:
        if m.get("key"):
            by_key[m["key"]].append(m)
    out: list[Violation] = []
    for key, items in by_key.items():
        if key.startswith("stale:"):
            limit = stale_repeat_hours
        elif key.startswith("rollover:activat"):
            limit = activation_hours
        else:
            limit = repeat_cooldown_hours
        items.sort(key=lambda m: m["sim_time"])
        for a, b in zip(items, items[1:]):
            if _hours(a["sim_time"], b["sim_time"]) < limit:
                out.append(Violation("M1", f"key {key} mailed at {a['sim_time']:%Y-%m-%d %H:%M} and "
                                           f"{b['sim_time']:%Y-%m-%d %H:%M} (< {limit} h)"))
    return out


def check_m2(mails: Sequence[Mapping], injection_dates: Iterable[dt.date]) -> list[Violation]:
    """M2: a day is healthy when neither it nor the day before is an injection day; no FAILURE /
    ACTION_REQUIRED / WARNING / RECOVERED mail on a healthy day."""
    inj = set(injection_dates)
    out: list[Violation] = []
    for m in mails:
        d = _day(m)
        if d in inj or (d - dt.timedelta(days=1)) in inj:
            continue
        if _sev(m) in HEALTHY_FORBIDDEN:
            out.append(Violation("M2", f"{d}: {_sev(m)} mail on a healthy day: {m.get('subject')}"))
    return out


def check_m3(mails: Sequence[Mapping], expected_per_day: Mapping[dt.date, int] | None = None,
             limit: int = 4) -> list[Violation]:
    """M3: at most ``limit`` problem mails per sim day unless the scenario expects more."""
    per: dict[dt.date, int] = defaultdict(int)
    for m in mails:
        if _sev(m) in PROBLEM_SEVERITIES:
            per[_day(m)] += 1
    exp = expected_per_day or {}
    return [Violation("M3", f"{d}: {n} problem mails (limit {exp.get(d, limit)})")
            for d, n in sorted(per.items()) if n > exp.get(d, limit)]


def check_m4(mails: Sequence[Mapping], outages: Iterable[tuple[dt.datetime, dt.datetime]],
             window_hours: float = 24) -> list[Violation]:
    """M4: after an outage ends, at most 1 mail per (leg, FY) per class (missed/overdue/failure) in the
    catch-up window (``window_hours`` after the outage end)."""
    out: list[Violation] = []
    for start, end in outages:
        cnt: dict[tuple[str, str, str], int] = defaultdict(int)
        for m in mails:
            cls = m.get("cls") or ""
            if cls and end <= m["sim_time"] <= end + dt.timedelta(hours=window_hours):
                cnt[(m.get("leg") or "", m.get("fy") or "", cls)] += 1
        for (leg, fy, cls), n in sorted(cnt.items()):
            if n > 1:
                out.append(Violation("M4", f"after outage ending {end:%Y-%m-%d %H:%M}: {n} '{cls}' mails "
                                           f"for leg={leg or '?'} fy={fy or '?'}"))
    return out


def check_m5(mails: Sequence[Mapping], recipients: Sequence[str],
             report_recipients: Sequence[str]) -> list[Violation]:
    """M5: report mails go to report_recipients only, problem mails to recipients only."""
    rec, rep = {a.lower() for a in recipients}, {a.lower() for a in report_recipients}
    out: list[Violation] = []
    for m in mails:
        aud = (m.get("audience") or "").lower()
        if not aud:
            aud = "reports" if _sev(m) == "REPORT" else "alerts" if _sev(m) in HEALTHY_FORBIDDEN else ""
        if not aud:
            continue
        allowed = rep if aud == "reports" else rec if aud == "alerts" else rep | rec
        bad = [r for r in _rcpts(m) if r.lower() not in allowed]
        if bad:
            out.append(Violation("M5", f"{m.get('subject')!r} ({aud}) sent to {bad}"))
    return out


def check_mail_rules(mails: Sequence[Mapping], *, recipients: Sequence[str], report_recipients: Sequence[str],
                     repeat_cooldown_hours: float = 12, stale_repeat_hours: float = 24,
                     injection_dates: Iterable[dt.date] = (),
                     outages: Iterable[tuple[dt.datetime, dt.datetime]] = (),
                     expected_per_day: Mapping[dt.date, int] | None = None) -> list[Violation]:
    """Storm rules M1-M5 over a list of mail dicts (see ``load_mail_index``)."""
    out = check_m1(mails, repeat_cooldown_hours, stale_repeat_hours)
    out += check_m2(mails, injection_dates)
    out += check_m3(mails, expected_per_day)
    out += check_m4(mails, outages)
    out += check_m5(mails, recipients, report_recipients)
    return out


def check_expected_mails(mails: Sequence[Mapping], day: Day) -> list[Violation]:
    """Day's mails against ``day.mails``: keyed expectations by key count; problem mails not above the
    expected ``problem`` count (0 on a healthy day)."""
    out: list[Violation] = []
    todays = [m for m in mails if _day(m) == day.date]
    for exp in day.mails:
        if exp.key:
            got = sum(1 for m in todays if m.get("key") == exp.key)
            if got != exp.count:
                out.append(Violation("MAIL", f"{day.date}: key {exp.key} sent {got}, expected {exp.count}"))
    want = sum(e.count for e in day.mails if e.mail_class in ("problem", "activation_waiting", "rollover_blocked"))
    got = sum(1 for m in todays if _sev(m) in PROBLEM_SEVERITIES)
    if got > want:
        out.append(Violation("MAIL", f"{day.date}: {got} problem mails, expected {want}"))
    return out


# --------------------------------------------------------------------------------------------- trees

def tree_hash(root: str | Path, ignore: Iterable[str] = ()) -> dict[str, str]:
    """{posix relative path: sha256 of the bytes} of every file under root ({} when root is absent)."""
    base = Path(root)
    skip = set(ignore)
    out: dict[str, str] = {}
    if not base.exists():
        return out
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name in skip:
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        out[p.relative_to(base).as_posix()] = h.hexdigest()
    return out


hash_tree = tree_hash


@dataclass(frozen=True)
class TreeDiff:
    missing: list[str]     # in a, not in b
    extra: list[str]       # in b, not in a
    differing: list[str]   # in both, different bytes

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.differing)

    def __str__(self) -> str:
        return f"missing={self.missing} extra={self.extra} differing={self.differing}"


def _as_hashes(x: Mapping[str, str] | str | Path, ignore: Iterable[str]) -> Mapping[str, str]:
    return x if isinstance(x, Mapping) else tree_hash(x, ignore)


def compare_trees(a: Mapping[str, str] | str | Path, b: Mapping[str, str] | str | Path,
                  ignore: Iterable[str] = ()) -> TreeDiff:
    """Byte-for-byte comparison; ``a`` is the reference (source), ``b`` the copy. Paths or tree_hash dicts."""
    ha, hb = _as_hashes(a, ignore), _as_hashes(b, ignore)
    return TreeDiff(
        missing=sorted(set(ha) - set(hb)),
        extra=sorted(set(hb) - set(ha)),
        differing=sorted(k for k in set(ha) & set(hb) if ha[k] != hb[k]),
    )


def _empty_dirs(root: str | Path, ignore: Iterable[str] = ()) -> set[str]:
    """Relative POSIX paths of empty directories under ``root`` (rclone --create-empty-src-dirs)."""
    base = Path(root)
    skip = set(ignore)
    out: set[str] = set()
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if not p.is_dir() or p.name in skip:
            continue
        entries = [c for c in p.iterdir() if c.name not in skip]
        if not entries:
            out.add(p.relative_to(base).as_posix())
    return out


def check_tree(src, dst, label: str = "", ignore: Iterable[str] = ()) -> list[Violation]:
    d = compare_trees(src, dst, ignore)
    violations: list[Violation] = []
    if not d.ok:
        violations.append(Violation("TREE", f"{label or dst}: {d}"))
    if isinstance(src, (str, Path)) and isinstance(dst, (str, Path)):
        sa, sb = _empty_dirs(src, ignore), _empty_dirs(dst, ignore)
        miss_d, extra_d = sorted(sa - sb), sorted(sb - sa)
        if miss_d or extra_d:
            violations.append(Violation("TREE", f"{label or dst}: empty_dirs missing={miss_d} extra={extra_d}"))
    return violations


_PARTIAL_SUFFIX_RE = re.compile(r"\.[0-9a-f]{8}\.partial$")


def check_rclone_lan_metrics(conn: sqlite3.Connection, day: dt.date) -> list[Violation]:
    """D3/D5: validate rclone LAN extended_metrics (counts_source, arrival, hash, deletes_held, killed_after_cutoff)."""
    out: list[Violation] = []
    for r in run_rows(conn, day):
        if r.get("mode") != "lan":
            continue
        status = r.get("status")
        raw = r.get("extended_metrics")
        try:
            m = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
        except ValueError:
            m = {}
        if status in ("LAN_COMPLETE", "LAN_PARTIAL", "LAN_SUSPECT"):
            if m.get("counts_source") != "rclone":
                out.append(Violation("METRICS", f"{day} {r['run_id']}: counts_source={m.get('counts_source')!r}, want 'rclone'"))
        if status == "LAN_COMPLETE":
            arr = m.get("arrival") or {}
            if arr.get("verified") is not True:
                out.append(Violation("METRICS", f"{day} {r['run_id']}: LAN_COMPLETE arrival.verified={arr.get('verified')!r}"))
            copied = int(r.get("files_copied") or 0)
            if copied > 0:
                h = arr.get("hash") or {}
                if h.get("ran") is True:
                    if h.get("verified") is not True:
                        out.append(Violation("METRICS", f"{day} {r['run_id']}: LAN_COMPLETE arrival.hash.ran=True but verified={h.get('verified')!r}"))
                    if int(h.get("files") or 0) <= 0:
                        out.append(Violation("METRICS", f"{day} {r['run_id']}: arrival.hash.ran=True with files={h.get('files')!r}"))
                    if int(h.get("missing") or 0) != 0 or int(h.get("differ") or 0) != 0:
                        out.append(Violation("METRICS", f"{day} {r['run_id']}: arrival.hash.ran=True on LAN_COMPLETE with missing={h.get('missing')} differ={h.get('differ')}"))
                elif not h.get("reason"):
                    out.append(Violation("METRICS", f"{day} {r['run_id']}: LAN_COMPLETE with {copied} copies missing arrival.hash ran/reason"))
        elif status == "LAN_PARTIAL":
            if m.get("deletes_held") is True and int(r.get("files_deleted") or 0) != 0:
                out.append(Violation("METRICS", f"{day} {r['run_id']}: deletes_held=True but files_deleted={r.get('files_deleted')}"))
            if m.get("killed_after_cutoff") is True:
                if r.get("fault_code") != "AAM-LAN-04":
                    out.append(Violation("METRICS", f"{day} {r['run_id']}: killed_after_cutoff=True but fault_code={r.get('fault_code')!r}, want 'AAM-LAN-04'"))
                if m.get("budget_stopped"):
                    out.append(Violation("METRICS", f"{day} {r['run_id']}: killed_after_cutoff=True must not set budget_stopped=True"))
            for fp in m.get("failed_paths") or ():
                if isinstance(fp, str) and _PARTIAL_SUFFIX_RE.search(fp):
                    out.append(Violation("METRICS", f"{day} {r['run_id']}: unstripped partial suffix in failed_paths: {fp!r}"))
        elif status == "LAN_SUSPECT":
            arr = m.get("arrival") or {}
            h = arr.get("hash") or {}
            if arr.get("verified") is True and not (h.get("ran") is True and h.get("verified") is False):
                out.append(Violation("METRICS", f"{day} {r['run_id']}: LAN_SUSPECT but arrival.verified=True"))
            if h.get("ran") is True and h.get("verified") is False and r.get("fault_code") != "AAM-LAN-04":
                out.append(Violation("METRICS", f"{day} {r['run_id']}: hash unverified but fault_code={r.get('fault_code')!r}, want 'AAM-LAN-04'"))
        elif status == "LAN_BLOCKED" and (r.get("fault_code") == "AAM-DEL-03" or m.get("max_delete_reached") is True):
            if r.get("fault_code") != "AAM-DEL-03":
                out.append(Violation("METRICS", f"{day} {r['run_id']}: max_delete_reached=True but fault_code={r.get('fault_code')!r}, want 'AAM-DEL-03'"))
            if m.get("max_delete_reached") is not True:
                out.append(Violation("METRICS", f"{day} {r['run_id']}: AAM-DEL-03 with max_delete_reached={m.get('max_delete_reached')!r}, want True"))
            if m.get("counts_source") != "rclone":
                out.append(Violation("METRICS", f"{day} {r['run_id']}: LAN_BLOCKED exit-7 counts_source={m.get('counts_source')!r}, want 'rclone'"))
        elif r.get("fault_code") == "AAM-DEL-03" and status != "LAN_BLOCKED":
            out.append(Violation("METRICS", f"{day} {r['run_id']}: AAM-DEL-03 with status={status!r}, want 'LAN_BLOCKED'"))
    return out


def check_frozen(snapshot: Mapping[str, str], root: str | Path, label: str = "",
                 ignore: Iterable[str] = ()) -> list[Violation]:
    """A tree must still equal an earlier hash snapshot (e.g. FY90-91 after the switch)."""
    d = compare_trees(snapshot, root, ignore)
    return [] if d.ok else [Violation("TREE", f"{label or root} changed since snapshot: {d}")]


# --------------------------------------------------------------------------------------------- /status

CARD_NAMES = ("cloud", "cloud_next", "lan", "lan_next")


@dataclass(frozen=True)
class StatusView:
    raw: Mapping[str, Any]

    @property
    def cards(self) -> list[str]:
        return sorted(k for k in CARD_NAMES if self.raw.get(k))

    def card(self, name: str) -> Mapping:
        if name not in CARD_NAMES or not self.raw.get(name):
            raise KeyError(name)
        return self.raw[name]

    @property
    def overlap(self) -> Mapping:
        return self.raw.get("overlap") or {}

    @property
    def overlap_active(self) -> bool:
        return self.overlap.get("active") is True

    @property
    def activates_on(self) -> str | None:
        return self.overlap.get("activates_on")

    @property
    def next_fy(self) -> str | None:
        return self.overlap.get("next_fy")

    @property
    def configured_fy(self) -> str | None:
        return self.raw.get("fy_prefix")

    @property
    def recent_runs(self) -> list[Mapping]:
        return list(self.raw.get("recent_runs") or [])


def read_status(payload: str | bytes | Mapping) -> StatusView:
    data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    return StatusView(data)


parse_status = read_status


def fetch_status(base_url: str, timeout: float = 10.0) -> StatusView:
    """GET <base_url>/status (read-only, loopback lane)."""
    root = base_url.rstrip("/")
    url = root if root.endswith("/status") else root + "/status"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return read_status(resp.read())


def check_status(view: StatusView, *, cards: Sequence[str] | None = None, overlap_active: bool | None = None,
                 configured_fy: str | None = None, activates_on: str | None = None,
                 next_fy: str | None = None) -> list[Violation]:
    out: list[Violation] = []
    if cards is not None and view.cards != sorted(cards):
        out.append(Violation("STATUS", f"cards {view.cards}, expected {sorted(cards)}"))
    if overlap_active is not None and view.overlap_active is not overlap_active:
        out.append(Violation("STATUS", f"overlap.active {view.overlap_active}, expected {overlap_active}"))
    for name, got, want in (("fy_prefix", view.configured_fy, configured_fy),
                            ("activates_on", view.activates_on, activates_on),
                            ("next_fy", view.next_fy, next_fy)):
        if want is not None and got != want:
            out.append(Violation("STATUS", f"{name} {got}, expected {want}"))
    return out


# --------------------------------------------------------------------------------------------- spies

def check_spies(*, wake_calls: int, shutdown_calls: int, lan_flows: int,
                lan_flows_all_complete: int) -> list[Violation]:
    """NAS wake spy = 1 per LAN flow; shutdown spy = 1 per LAN flow whose passes are all LAN_COMPLETE,
    else 0 (so total shutdowns == lan_flows_all_complete)."""
    out: list[Violation] = []
    if wake_calls != lan_flows:
        out.append(Violation("SPY", f"wake called {wake_calls}x for {lan_flows} LAN flows"))
    if shutdown_calls != lan_flows_all_complete:
        out.append(Violation("SPY", f"shutdown called {shutdown_calls}x, expected {lan_flows_all_complete} "
                                    f"(LAN flows with all passes LAN_COMPLETE)"))
    return out


def check_no_nas_activity(*, wake_calls: int, shutdown_calls: int, what: str = "rollover check") -> list[Violation]:
    """The NAS must not be woken or shut down at all (e.g. the 06:00 rollover check, F-OWN-1)."""
    return [Violation("SPY", f"{what}: wake={wake_calls} shutdown={shutdown_calls}, expected 0/0")] \
        if (wake_calls or shutdown_calls) else []
