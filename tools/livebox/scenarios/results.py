"""Build a Results object from run_lane evidence, and adapt catalog Scenarios to the run_lane driver.

Every evidence file is optional; corrupt or unknown files are ignored; nothing here raises.
"""
from __future__ import annotations

import csv
import email
import email.policy
import gzip
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Results:
    rows: list = field(default_factory=list)
    mails: list = field(default_factory=list)
    approvals: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    config_fy_by_day: dict = field(default_factory=dict)
    activated_on: list = field(default_factory=list)
    switched_on: list = field(default_factory=list)
    clock_proven_by_day: dict = field(default_factory=dict)
    created_paths: list = field(default_factory=list)
    junction_target_entries: list = field(default_factory=list)
    archive_calls: list = field(default_factory=list)
    orphan_robocopy: list = field(default_factory=list)
    robocopy_runs: list = field(default_factory=list)
    lan_runs_0600: int = 0
    switch_before_manual_lan: bool = False


def empty_results() -> Results:
    return Results()


def _json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _jsonl(path: Path) -> list:
    out: list = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _day(text: Any) -> str:
    return str(text or "")[:10]


def _leg(mode: Any) -> str:
    m = str(mode).lower()
    return "cloud" if m == "cloud" else "rollover" if m == "rollover" else "lan"


def _clock_ok(run: dict) -> bool:
    em = run.get("extended_metrics")
    if isinstance(em, str):
        try:
            em = json.loads(em)
        except Exception:
            em = None
    clock = em.get("clock") if isinstance(em, dict) else None
    return isinstance(clock, dict) and clock.get("state") == "OK"


def _parse_x_headers(text: str) -> dict:
    """Accept the JSON object run_lane writes ({"X-AAM-Reference": ..., "X-AAM-Fault-Code": ...})
    and the k=v;k=v form. Returns lower-cased keys."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            d = json.loads(text)
            if isinstance(d, dict):
                return {str(k).lower(): str(v) for k, v in d.items()}
        except Exception:
            return {}
        return {}
    out = {}
    for part in text.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip().lower()] = v.strip()
    return out


_SEVERITY_WORDS = {"PROBLEM": "PROBLEM", "WARNING": "WARNING", "RESOLVED": "RESOLVED", "INFO": "INFO",
                   "ACTION NEEDED": "ACTION_REQUIRED"}


def _subject_severity(subject: str) -> str:
    """Severity word after the '[<firm> Backup] ' prefix; '' for report mails."""
    s = re.sub(r"^\s*\[[^\]]*\]\s*", "", subject or "")
    word = s.split(":", 1)[0].strip().upper() if ":" in s else ""
    return _SEVERITY_WORDS.get(word, "")


def _norm_subject(text: Any) -> str:
    return " ".join(str(text or "").split())


def _eml_fault_codes(mail_dir: Path) -> dict:
    """subject -> X-AAM-Fault-Code of the captured .eml files (first one with a code wins)."""
    out: dict = {}
    if not mail_dir.is_dir():
        return out
    for p in sorted(mail_dir.glob("*.eml")):
        try:
            msg = email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
            code = str(msg.get("X-AAM-Fault-Code") or "").strip()
            if code:
                out.setdefault(_norm_subject(msg.get("Subject")), code)
        except Exception:
            continue
    return out


def _db_rows_from_dump(dump_dir: Path, table: str) -> list:
    by_id: dict = {}
    extra: list = []
    for p in sorted(dump_dir.glob("*.jsonl*")):
        try:
            if p.name.endswith(".gz"):
                with gzip.open(p, "rt", encoding="utf-8") as fh:
                    text = fh.read()
            else:
                text = p.read_text(encoding="utf-8-sig")
        except Exception:
            continue
        for line in text.splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            if isinstance(o, dict) and o.get("table") == table and isinstance(o.get("row"), dict):
                row = o["row"]
                if row.get("id") is None:
                    extra.append(row)
                else:
                    by_id[row["id"]] = row
    return [by_id[k] for k in sorted(by_id, key=lambda x: (str(type(x)), x))] + extra


def _notifications(ev: Path) -> list:
    db = ev / "manifest.db"
    if db.is_file():
        try:
            con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            try:
                con.row_factory = sqlite3.Row
                return [dict(r) for r in con.execute("select * from notifications order by id")]
            finally:
                con.close()
        except Exception:
            pass
    dump = ev / "db_dump"
    return _db_rows_from_dump(dump, "notifications") if dump.is_dir() else []


def _mails(path: Path, notes: list | None = None, eml_codes: dict | None = None) -> list:
    out: list = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                h = _parse_x_headers(r.get("x_headers") or "")
                subject = r.get("subject") or ""
                code = h.get("x-aam-fault-code") or h.get("code") or ""
                if not code and eml_codes:
                    code = eml_codes.get(_norm_subject(subject), "")
                m = {
                    "day": _day(r.get("sim_time")),
                    "sim_time": r.get("sim_time") or "",
                    "subject": subject,
                    "rcpts": r.get("rcpts") or "",
                    "severity": (_subject_severity(subject) or h.get("severity", "")).upper(),
                    "code": code,
                    "fault": h.get("fault") or h.get("x-aam-fault-code") or None,
                }
                if h.get("key"):
                    m["key"] = h["key"]
                if h.get("kind"):
                    m["kind"] = h["kind"]
                out.append(m)
    except Exception:
        return []
    try:
        if notes:
            from tools.livebox import oracles
            oracles.join_notifications(out, notes)
    except Exception:
        pass
    for m in out:
        m["key"] = m.get("key") or ""
        m["kind"] = m.get("kind") or ""
    return out


def build_results(evidence_dir) -> Results:
    res = Results()
    try:
        _fill(res, Path(evidence_dir))
    except Exception:
        pass
    return res


def _fill(res: Results, ev: Path) -> None:
    if not ev.is_dir():
        return
    seen: dict = {}
    last_fy_by_day: dict = {}
    activated: set = set()
    clock: dict = {}
    sdir = ev / "status"
    files = sorted(sdir.glob("*.json")) if sdir.is_dir() else []
    for f in files:
        doc = _json_file(f)
        if not isinstance(doc, dict):
            continue
        fy_cfg = doc.get("fy_prefix")
        if fy_cfg:
            last_fy_by_day[f.name[:10]] = fy_cfg
        ov = doc.get("overlap")
        if isinstance(ov, dict) and ov.get("activated_at"):
            activated.add(_day(ov["activated_at"]))
        runs = doc.get("recent_runs")
        if not isinstance(runs, list):
            continue
        for run in runs:
            if not isinstance(run, dict):
                continue
            leg, started = _leg(run.get("mode")), run.get("started_at")
            if not started or (leg, started) in seen:
                continue
            seen[(leg, started)] = {
                "day": _day(started), "leg": leg, "fy": run.get("fy") or fy_cfg,
                "status": run.get("status"), "fault": run.get("fault_code") or None,
                "started_at": started, "files": run.get("files"),
            }
            if leg == "cloud":
                clock[_day(started)] = _clock_ok(run)
    res.rows = sorted(seen.values(), key=lambda r: (str(r["started_at"]), r["leg"]))
    res.config_fy_by_day = dict(sorted(last_fy_by_day.items()))
    res.activated_on = sorted(activated)
    prev = None
    for d, fy in res.config_fy_by_day.items():
        if prev is not None and fy != prev:
            res.switched_on.append(d)
        prev = fy
    res.clock_proven_by_day = dict(sorted(clock.items()))

    if (ev / "mail_index.csv").is_file():
        res.mails = _mails(ev / "mail_index.csv", _notifications(ev), _eml_fault_codes(ev / "mail"))

    for c in _jsonl(ev / "checks.jsonl"):
        if c.get("verdict") != "PASS" and c.get("check_id") is not None:
            res.notes.append(f"{c.get('check_id')}: {c.get('observed', '')}")

    state = _json_file(ev / "state.json")
    if isinstance(state, dict) and isinstance(state.get("hooks"), dict):
        res.facts.update(state["hooks"])
    summ = _json_file(ev / "summary.json")
    if isinstance(summ, dict):
        res.facts.update(summ)
        for k in ("created_paths", "junction_target_entries", "archive_calls", "orphan_robocopy"):
            if isinstance(summ.get(k), list):
                setattr(res, k, list(summ[k]))
        res.switch_before_manual_lan = bool(summ.get("switch_before_manual_lan", False))

    res.approvals = _jsonl(ev / "approvals.jsonl")
    res.robocopy_runs = _jsonl(ev / "robocopy_runs.jsonl")

    res.lan_runs_0600 = sum(
        1 for e in _jsonl(ev / "events.jsonl")
        if e.get("kind") == "lan" and str(e.get("at", ""))[11:13] == "06")


def to_driver_scenario(catalog):
    """Wrap a catalog Scenario as a run_lane.Scenario: setup once on the first day, then on_day each day."""
    from tools.livebox import run_lane

    state = {"done": False}

    def before_day(ctx, day):
        rig = getattr(ctx, "rig", None)
        if not state["done"]:
            state["done"] = True
            if catalog.setup:
                catalog.setup(rig)
        if catalog.on_day:
            catalog.on_day(rig, day)
        return None

    return run_lane.Scenario(name=catalog.id, before_day=before_day, outages=tuple(catalog.outages),
                             lateness=dict(catalog.lateness), injections=tuple(catalog.injections),
                             overrides=tuple(getattr(catalog, "overrides", ()) or ()),
                             n_deletes=bool(getattr(catalog, "n_deletes", False)))


def judge(catalog, evidence_dir) -> list:
    out = catalog.expect(build_results(evidence_dir))
    return [str(x) for x in out] if out else []


def _knob_names(ev: Path) -> set:
    return {str(r.get("name")) for r in _jsonl(ev / "inputs.jsonl") if r.get("kind") == "knob"}


def _unmet_requires(catalog, ev: Path, res: Results) -> list:
    knobs = None
    out = []
    for req in getattr(catalog, "requires", ()) or ():
        kind, _, name = str(req).partition(":")
        if kind == "knob":
            knobs = _knob_names(ev) if knobs is None else knobs
            ok = name in knobs
        elif kind == "fact":
            ok = bool(res.facts.get(name))
        elif kind == "file":
            try:
                ok = (ev / name).is_file() and (ev / name).stat().st_size > 0
            except OSError:
                ok = False
        else:
            ok = False
        if not ok:
            out.append(str(req))
    return out


def verdict_for(catalog, evidence_dir) -> dict:
    """PASS / FAIL / FINDING / NOT_RUN for one catalog scenario. Never raises."""
    try:
        if not getattr(catalog, "injection_wired", True):
            return {"verdict": "NOT_RUN", "failures": [], "reason": "injection not wired in the driver"}
        ev = Path(evidence_dir)
        res = build_results(ev)
        unmet = _unmet_requires(catalog, ev, res)
        if unmet:
            return {"verdict": "NOT_RUN", "failures": [], "reason": "unmet requires: " + ", ".join(unmet)}
        try:
            out = catalog.expect(res)
        except Exception as e:  # noqa: BLE001
            return {"verdict": "FAIL", "failures": [f"expect raised {type(e).__name__}: {e}"],
                    "reason": "expect raised"}
        if isinstance(out, str):
            return {"verdict": "NOT_RUN", "failures": [], "reason": out}
        fails = [str(x) for x in out] if out else []
        if fails and fails[0].startswith("NOT_RUN:"):
            return {"verdict": "NOT_RUN", "failures": [], "reason": fails[0]}
        if not fails:
            return {"verdict": "PASS", "failures": [], "reason": ""}
        red = getattr(catalog, "expected_red", None)
        if red:
            unmasked = [f for f in fails if str(red) not in f]
            if unmasked:
                return {"verdict": "FAIL", "failures": unmasked, "reason": f"unmasked failures besides {red}"}
            return {"verdict": "FINDING", "failures": fails, "reason": f"expected red {red}"}
        return {"verdict": "FAIL", "failures": fails, "reason": ""}
    except Exception as e:  # noqa: BLE001
        return {"verdict": "FAIL", "failures": [f"verdict_for error {type(e).__name__}: {e}"], "reason": "error"}
