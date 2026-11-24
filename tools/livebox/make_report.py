"""Build REPORT.md (plain language) from live-box test evidence (plan L15).

Usage: python -m tools.livebox.make_report --evidence DIR [DIR ...] --out REPORT.md

Deterministic: no timestamps, everything sorted, independent of the order of the evidence roots.
Never raises on missing or corrupt evidence.
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import os
import sys
from pathlib import Path

from tools.livebox.scenarios import results as _results
from tools.livebox.scenarios.results import verdict_for  # noqa: F401  (module attribute, patchable)

VERDICTS = ("PASS", "FAIL", "FINDING", "NOT_RUN", "HARNESS-FAIL")
MAIL_LIMIT = 4
_MARKERS = ("summary.json", "checks.jsonl", "result.json")


def load_catalog() -> list:
    out: list = []
    for name in ("w1", "w2", "w3", "w4"):
        mod = importlib.import_module(f"tools.livebox.scenarios.{name}")
        out.extend(getattr(mod, "SCENARIOS", []))
    return out


# ---------------------------------------------------------------- reading helpers

def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _is_lane(d: Path) -> bool:
    try:
        return d.is_dir() and any((d / m).is_file() for m in _MARKERS)
    except OSError:
        return False


def discover_lanes(roots) -> list:
    seen: dict = {}
    for r in roots:
        try:
            r = Path(r)
            if not r.is_dir():
                continue
            cands = [r] if _is_lane(r) else sorted(c for c in r.iterdir() if _is_lane(c))
            for c in cands:
                seen.setdefault(str(c.resolve()), c)
        except OSError:
            continue
    return sorted(seen.values(), key=lambda p: (p.name, p.resolve().as_posix()))


def _checks(lane: Path) -> list:
    """[(1-based line number, dict)] of the oracle rows (SCENARIO: rows excluded)."""
    out = []
    try:
        text = (lane / "checks.jsonl").read_text(encoding="utf-8-sig")
    except Exception:
        return out
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if not isinstance(o, dict) or o.get("check_id") is None:
            continue
        if str(o["check_id"]).startswith("SCENARIO:"):
            continue
        out.append((n, o))
    return out


def _norm_verdict(v) -> str:
    v = str(v or "").strip().upper()
    if v == "KNOWN":
        return "FINDING"
    if v in ("NOT_RUN", "SKIP", "SKIPPED"):
        return "NOT_RUN"
    return v if v in VERDICTS else "HARNESS-FAIL"


def _cell(x) -> str:
    return " ".join(str(x if x is not None else "").replace("|", "/").split())


def _rel(path: Path, base: Path) -> str:
    try:
        p = os.path.relpath(path.resolve(), base.resolve())
    except ValueError:
        p = str(path.resolve())
    return p.replace("\\", "/")


def _num(x, default="n/a"):
    return default if x is None else x


# ---------------------------------------------------------------- mail

def _mail_stats(lane: Path) -> dict:
    sev: dict = {}
    code: dict = {}
    per_day: dict = {}
    try:
        with (lane / "mail_index.csv").open(newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                h = _results._parse_x_headers(r.get("x_headers") or "")
                s = (h.get("severity") or _results._subject_severity(r.get("subject") or "")).upper()
                c = h.get("code") or h.get("x-aam-fault-code") or ""
                if c:
                    code[c] = code.get(c, 0) + 1
                if s:
                    sev[s] = sev.get(s, 0) + 1
                    if s != "INFO":
                        d = str(r.get("sim_time") or "")[:10]
                        per_day[d] = per_day.get(d, 0) + 1
    except Exception:
        pass
    return {"sev": sev, "code": code, "max": max(per_day.values()) if per_day else 0}


# ---------------------------------------------------------------- report

def _flow_counts(lane: Path) -> str:
    """Flow outcomes from events.jsonl flow_state (Completed/Failed/Crashed); `ok` only says the harness ran it."""
    counts: dict = {}
    try:
        with (lane / "events.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    st = json.loads(line).get("flow_state")
                except ValueError:
                    continue
                if st:
                    counts[st] = counts.get(st, 0) + 1
    except OSError:
        return "n/a (no events.jsonl)"
    return ", ".join(f"{k} {counts[k]}" for k in sorted(counts)) or "n/a (no flow_state recorded)"


def _lane_state(lane: Path) -> str:
    """"" when the lane finished (result.json status "completed"); otherwise the reason it cannot give a PASS."""
    try:
        doc = json.loads((lane / "result.json").read_text(encoding="utf-8-sig"))
        status = str(doc.get("status", "")) if isinstance(doc, dict) else ""
    except Exception:
        return "lane has no readable result.json (run unfinished)"
    return "" if status == "completed" else f"lane status is {status or 'unknown'!r}, not completed"


def build_report(evidence_roots, out) -> dict:
    out = Path(out)
    base = out.parent
    lanes = discover_lanes(evidence_roots)
    by_name: dict = {}
    for ln in lanes:
        by_name.setdefault(ln.name, ln)
    counts = {k: 0 for k in VERDICTS}

    scen_rows: list = []
    details: list = []
    try:
        catalog = sorted(load_catalog(), key=lambda s: str(s.id))
    except Exception:
        catalog = []
    for sc in catalog:
        lane = by_name.get(sc.lane)
        if lane is None:
            counts["NOT_RUN"] += 1
            scen_rows.append(f"| {_cell(sc.id)} | NOT_RUN | NO EVIDENCE (no folder named {_cell(sc.lane)}) | - |")
            continue
        try:
            v = verdict_for(sc, lane)
        except Exception as e:  # noqa: BLE001
            v = {"verdict": "FAIL", "failures": [f"verdict_for error {type(e).__name__}: {e}"], "reason": ""}
        verdict = _norm_verdict(v.get("verdict"))
        lane_state = _lane_state(lane)
        if verdict == "PASS" and lane_state:           # an unfinished lane never proves a PASS
            verdict = "NOT_RUN"
            v = {"verdict": verdict, "failures": [], "reason": lane_state}
        counts[verdict] += 1
        fails = [str(x) for x in (v.get("failures") or [])]
        if verdict == "FINDING":
            note = _cell(getattr(sc, "expected_red", None) or v.get("reason"))
        elif verdict == "NOT_RUN":
            note = _cell(v.get("reason") or "not run")
        elif verdict == "FAIL":
            note = _cell(fails[0]) if fails else ""
        else:
            note = ""
        scen_rows.append(f"| {_cell(sc.id)} | {verdict} | {note} | {_rel(lane, base)} |")
        if verdict in ("FAIL", "FINDING", "HARNESS-FAIL"):
            blk = [f"### Scenario {_cell(sc.id)} ({verdict}): {_cell(getattr(sc, 'title', ''))}"]
            if verdict == "FINDING":
                blk.append(f"known finding: {note}")
            blk.append(f"expected: scenario {_cell(sc.id)} meets all its checks")
            for f in fails or ["(no detail recorded)"]:
                blk.append(f"observed: {_cell(f)}")
            details.append("\n".join(blk))

    oracle_rows: list = []
    for ln in lanes:
        rel = _rel(ln, base)
        for n, c in _checks(ln):
            verdict = _norm_verdict(c.get("verdict"))
            counts[verdict] += 1
            fid = _cell(c.get("finding")) if verdict == "FINDING" else ""
            vcell = f"{verdict} {fid}".strip()
            oracle_rows.append(f"| {_cell(ln.name)} | {_cell(str(c.get('day') or '')[:10])} | "
                               f"{_cell(c.get('check_id'))} | {vcell} | {rel}/checks.jsonl:{n} |")
            if verdict in ("FAIL", "FINDING", "HARNESS-FAIL"):
                blk = [f"### {_cell(ln.name)} {_cell(c.get('day'))} {_cell(c.get('check_id'))} ({vcell})",
                       f"expected: {_cell(c.get('expected'))}",
                       f"observed: {_cell(c.get('observed'))}"]
                details.append("\n".join(blk))

    L: list = []
    L += ["# Live-box test report", "",
          "## How to read this report", "",
          "Each test either checks one thing the backup system must do (an oracle check) or runs a whole made-up "
          "situation (a scenario). Every item ends up in one of five groups. PASS means the system did what was "
          "expected. FAIL means it did something different and needs a look. FINDING means a known or newly "
          "found problem that is written down under its F- number. NOT_RUN means the test could not be run, "
          "and the reason is shown. HARNESS-FAIL means the test tool itself broke, not the backup system. "
          "The last column of each row names the saved file you can open to see the proof. Days shown are "
          "simulated days, not real ones.", ""]

    L += ["## Headline", "", "| Verdict | Count |", "| --- | --- |"]
    L += [f"| {k} | {counts[k]} |" for k in VERDICTS]
    L.append("")

    L += ["## Build tested", ""]
    commits = set()
    for ln in lanes:
        d = _json(ln / "identity.json")
        if isinstance(d, dict) and d.get("git_commit"):
            commits.add(str(d["git_commit"]))
    L += [f"Build tested: {c}" for c in sorted(commits)] or ["Build tested: unknown (no identity.json found)"]
    L += ["", "This was a test only: no product code was changed.", ""]

    L += ["## Scenario results", "", "| Scenario | Verdict | Note | Evidence folder |", "| --- | --- | --- | --- |"]
    L += scen_rows or ["| - | - | no scenarios in the catalog | - |"]
    L.append("")

    L += ["## Check results", "", "| Lane | Day | Check | Verdict | Evidence file:line |", "| --- | --- | --- | --- | --- |"]
    L += oracle_rows or ["| - | - | - | - | - |"]
    L.append("")

    L += ["## FAIL/FINDING details", ""]
    L += [b + "\n" for b in details] or ["Nothing to report: no failures or findings.", ""]

    L += ["## Mail summary", ""]
    for ln in lanes:
        st = _mail_stats(ln)
        L.append(f"### {ln.name}")
        for s in sorted(st["sev"]):
            L.append(f"- severity {s}: {st['sev'][s]}")
        for c in sorted(st["code"]):
            L.append(f"- code {c}: {st['code'][c]}")
        flag = " EXCEEDS the limit" if st["max"] > MAIL_LIMIT else ""
        L.append(f"- max problem mails/day: {st['max']} (limit {MAIL_LIMIT}){flag}")
        L.append("")

    L += ["## Lane facts", ""]
    for ln in lanes:
        res = _json(ln / "result.json")
        res = res if isinstance(res, dict) else {}
        summ = _json(ln / "summary.json")
        summ = summ if isinstance(summ, dict) else {}
        spies = summ.get("spies") if isinstance(summ.get("spies"), dict) else {}
        orph = summ.get("orphan_robocopy")
        wake = summ.get("wake_calls", spies.get("wake"))
        shut = summ.get("shutdown_calls", spies.get("shutdown"))
        L.append(f"### {ln.name}")
        L += [f"- days simulated: {_num(res.get('days_completed'))}",
              f"- wall time: {_num(summ.get('total_real_seconds'))} s",
              f"- status: {_num(res.get('status'))}",
              f"- flows: {_flow_counts(ln)}",
              f"- orphan_robocopy: {len(orph) if isinstance(orph, list) else 0}",
              f"- wake calls: {_num(wake)}",
              f"- shutdown calls: {_num(shut)}"]
        saf = _json(ln / "safety.json")
        rows = saf.get("results") if isinstance(saf, dict) else None
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict):
                msg = f" ({_cell(r['message'])})" if r.get("message") else ""
                L.append(f"- {_cell(r.get('rule'))}: {_cell(r.get('status'))}{msg}")
        L.append("")

    L += ["## Inputs and outputs saved", ""]
    for ln in lanes:
        man = _json(ln / "dataset_manifest.json")
        files = man.get("files") if isinstance(man, dict) else None
        L.append(f"### {ln.name}")
        got = False
        for f in sorted((x for x in files if isinstance(x, dict)), key=lambda x: str(x.get("path"))) \
                if isinstance(files, list) else []:
            L.append(f"- {_rel(ln, base)}/{_cell(f.get('path'))}: {_cell(f.get('bytes'))} bytes")
            got = True
        if not got:
            L.append("- (no dataset manifest)")
        L.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(("\n".join(L).rstrip("\n") + "\n").encode("utf-8"))
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="make_report")
    ap.add_argument("--evidence", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    counts = build_report([Path(p) for p in a.evidence], Path(a.out))
    print(", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
