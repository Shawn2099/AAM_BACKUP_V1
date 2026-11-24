r"""Red-first tests for tools/livebox/make_report.py (plan L15). Module not written yet.

PINNED API
  make_report.build_report(evidence_roots: list[Path], out: Path) -> dict
        writes REPORT.md to `out` (parent dirs created) and returns the headline counts:
        {"PASS": n, "FAIL": n, "FINDING": n, "NOT_RUN": n, "HARNESS-FAIL": n}  (all five keys always present;
        extra keys allowed).  Counts = scenario rows + oracle-check rows.
  make_report.main(argv) -> int     --evidence DIR [DIR ...]   --out FILE ; returns 0 and writes the file.
  make_report.load_catalog() -> list[scenarios.base.Scenario]
        every Scenario of scenarios.w1..w4 (tests monkeypatch it).  Scenario verdicts come from
        scenarios.results.verdict_for(catalog_scenario, <lane dir>) (module attribute `verdict_for`, patchable).

EVIDENCE DISCOVERY
  an evidence root is either a lane dir (contains summary.json, checks.jsonl or result.json) or a parent of lane dirs
  (direct children that are lane dirs).  A missing root never crashes.  The lane dir of a catalog scenario is the
  dir whose NAME == scenario.lane; none -> row "NO EVIDENCE", counted NOT_RUN.

REPORT.md CONTENT (tests assert these substrings; markdown tables use '|' separated cells)
  1 headline: one line per verdict "| PASS | n |" ... for PASS, FAIL, FINDING, NOT_RUN, HARNESS-FAIL.
     check verdict KNOWN counts as FINDING.  checks.jsonl rows whose check_id starts "SCENARIO:" are NOT oracle
     rows (the catalog row is the single source for scenarios).
  2 rows: scenario row  "| <id> | <verdict> | ... | <path relative to out's parent dir, '/' separators> |"
          oracle row    "| <lane> | <day> | <check_id> | <verdict> | <rel path>/checks.jsonl:<1-based line> |"
          NOT_RUN row carries the reason text; FINDING row carries the F-* id (scenario: expected_red; check: its
          "finding" field).
  3 section heading containing "FAIL/FINDING details": per non-PASS non-NOT_RUN item its expected and observed text
          as "expected: <text>" and "observed: <text>" lines (scenario: its failures lines).
  4 mail summary per lane: "severity <SEV>: n", "code <code>: n" lines; "max problem mails/day: N (limit 4)" and
          the word "EXCEEDS" when N > 4.  problem mail = x_headers severity present and != INFO; day = sim_time[:10];
          x_headers is "k=v;k=v" (keys lower-case, severity upper-cased).
  5 lane facts lines: "days simulated: N" (result.json days_completed), "wall time: S" (summary total_real_seconds),
          "status: <result.json status>", "orphan_robocopy: N" (len of summary list), "wake calls: N",
          "shutdown calls: N" (summary spies / wake_calls), "S1: ok" ... one line per safety.json results row.
  6 section heading containing "inputs and outputs saved": per lane per dataset_manifest file a line containing the
          path and its byte size.
  8 deterministic bytes (no timestamps, sorted); independent of evidence_roots order.
  9 line "Build tested: <git_commit>" per distinct identity.json commit, plus a sentence containing
          "no product code was changed" (case-insensitive).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

COMMIT = "4730bd7e94b17a39bae19f3c9c734ae678892183"


def mr():
    from tools.livebox import make_report
    return make_report


def jl(path: Path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def check(day, cid, verdict="PASS", expected="exp text", observed="obs text", **kw):
    d = {"lane": "L", "day": day, "check_id": cid, "verdict": verdict, "expected": expected,
         "observed": observed, "evidence": []}
    d.update(kw)
    return d


def make_lane(root: Path, name: str, checks=(), mails=(), files=None, status="completed", days=30,
              wall=226.1, orphans=(), wake=48, shut=34, safety=(("S1", "ok"), ("S12", "ok")), commit=COMMIT):
    d = root / name
    d.mkdir(parents=True)
    jl(d / "checks.jsonl", list(checks))
    (d / "result.json").write_text(json.dumps({"status": status, "days_completed": days, "counts": {}}))
    (d / "summary.json").write_text(json.dumps({
        "lane": name, "wake_calls": wake, "shutdown_calls": shut, "orphan_robocopy": list(orphans),
        "total_real_seconds": wall, "spies": {"wake": wake, "shutdown": shut, "archive": 0}}))
    (d / "safety.json").write_text(json.dumps({"results": [
        {"rule": r, "status": s, "message": ""} for r, s in safety]}))
    (d / "identity.json").write_text(json.dumps({"git_commit": commit, "time_machine_version": "3.6.0"}))
    lines = ["sim_time,real_time,rcpts,subject,from,x_headers"]
    for sim, xh in mails:
        lines.append(f"{sim},2026-11-24T00:00:00+00:00,a@b.invalid,subj,f@b.invalid,{xh}")
    (d / "mail_index.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    files = files if files is not None else {"checks.jsonl": 1234, "mail_index.csv": 77}
    (d / "dataset_manifest.json").write_text(json.dumps({"schema_version": 1, "files": [
        {"path": p, "bytes": b, "sha256": "0" * 64, "format": "x", "format_version": 1} for p, b in files.items()]}))
    return d


def fake_scn(sid, lane, expect=lambda r: [], expected_red=None, injection_wired=True):
    from tools.livebox.scenarios.base import Scenario
    return Scenario(id=sid, wave=1, title=sid, start="2091-02-03", days=2, lane=lane, setup=lambda r: None,
                    on_day=None, expect=expect, expected_red=expected_red, injection_wired=injection_wired)


@pytest.fixture
def run(tmp_path, monkeypatch):
    def _run(roots, catalog=(), out=None):
        m = mr()
        monkeypatch.setattr(m, "load_catalog", lambda: list(catalog))
        out = out or (tmp_path / "REPORT.md")
        res = m.build_report(roots, out)
        return res, out.read_text(encoding="utf-8")
    return _run


def line_with(text, *needles):
    for ln in text.splitlines():
        if all(n in ln for n in needles):
            return ln
    return None


def test_headline_counts_and_return_dict(tmp_path, run):
    ev = tmp_path / "ev"
    make_lane(ev, "A1", checks=[
        check("2090-12-15", "C1"), check("2090-12-15", "C2"),
        check("2090-12-16", "C3", "FAIL"),
        check("2090-12-16", "C4", "FINDING", finding="F-X-1"),
        check("2090-12-17", "C5", "KNOWN", finding="F-NEW-9"),
        check("2090-12-17", "C6", "HARNESS-FAIL"),
        check("2090-12-17", "SCENARIO:S1", "FAIL"),   # not an oracle row
    ])
    res, text = run([ev], catalog=[fake_scn("S1", "lane_s1")])  # no evidence for S1 -> NOT_RUN
    assert res["PASS"] == 2 and res["FAIL"] == 1 and res["FINDING"] == 2
    assert res["NOT_RUN"] == 1 and res["HARNESS-FAIL"] == 1
    for k, n in (("PASS", 2), ("FAIL", 1), ("FINDING", 2), ("NOT_RUN", 1), ("HARNESS-FAIL", 1)):
        assert f"| {k} | {n} |" in text


def test_oracle_rows_have_verdict_and_relative_line_path(tmp_path, run):
    ev = tmp_path / "ev"
    make_lane(ev, "A1", checks=[check("2090-12-15", "C1"), check("2090-12-16", "C2", "FAIL")])
    _, text = run([ev])
    ln = line_with(text, "C2", "FAIL")
    assert ln and "ev/A1/checks.jsonl:2" in ln and "2090-12-16" in ln
    ln1 = line_with(text, "| C1 |")
    assert ln1 and "ev/A1/checks.jsonl:1" in ln1 and "PASS" in ln1
    assert "\\" not in ln


def test_finding_row_lists_f_id_for_check_and_scenario(tmp_path, run):
    ev = tmp_path / "ev"
    make_lane(ev, "A1", checks=[check("2090-12-15", "C9", "KNOWN", finding="F-NEW-WAKE-PER-PASS")])
    make_lane(ev, "lane_red")
    cat = [fake_scn("X09", "lane_red", expect=lambda r: ["NAS was woken (F-OWN-1)"], expected_red="F-OWN-1")]
    res, text = run([ev], catalog=cat)
    assert "F-NEW-WAKE-PER-PASS" in line_with(text, "| C9 |")
    ln = line_with(text, "| X09 |")
    assert "FINDING" in ln and "F-OWN-1" in ln and "ev/lane_red" in ln
    assert res["FINDING"] == 2


def test_scenario_pass_fail_and_not_run_reason(tmp_path, run):
    ev = tmp_path / "ev"
    make_lane(ev, "lp")
    make_lane(ev, "lf")
    make_lane(ev, "ln")
    cat = [fake_scn("P1", "lp"), fake_scn("F1", "lf", expect=lambda r: ["boom: wrong status"]),
           fake_scn("N1", "ln", injection_wired=False)]
    res, text = run([ev], catalog=cat)
    assert "PASS" in line_with(text, "| P1 |")
    assert "FAIL" in line_with(text, "| F1 |")
    ln = line_with(text, "| N1 |")
    assert "NOT_RUN" in ln and "injection not wired in the driver" in ln
    assert res["FAIL"] == 1 and res["NOT_RUN"] == 1


def test_missing_evidence_never_crashes(tmp_path, run):
    ev = tmp_path / "ev"
    ev.mkdir()
    cat = [fake_scn("Z1", "lane_missing")]
    res, text = run([ev, tmp_path / "does_not_exist"], catalog=cat)
    ln = line_with(text, "| Z1 |")
    assert "NO EVIDENCE" in ln and "NOT_RUN" in ln
    assert res["NOT_RUN"] == 1 and res["PASS"] == 0 and res["FAIL"] == 0


def test_corrupt_files_do_not_crash(tmp_path, run):
    ev = tmp_path / "ev"
    d = make_lane(ev, "A1", checks=[check("2090-12-15", "C1")])
    (d / "summary.json").write_text("{not json")
    (d / "dataset_manifest.json").write_text("garbage")
    (d / "identity.json").write_text("[")
    (d / "checks.jsonl").write_text(json.dumps(check("2090-12-15", "C1")) + "\nnot json\n")
    res, text = run([ev])
    assert res["PASS"] == 1
    assert "no product code was changed" in text.lower()


def test_fail_finding_details_section_has_expected_vs_observed(tmp_path, run):
    ev = tmp_path / "ev"
    make_lane(ev, "A1", checks=[
        check("2090-12-16", "C3", "FAIL", expected="row is CLOUD_COMPLETE", observed="row was CLOUD_FAILED"),
        check("2090-12-17", "C4", "FINDING", expected="flow completes", observed="ExceptionGroup", finding="F-E1")])
    make_lane(ev, "lf")
    cat = [fake_scn("F1", "lf", expect=lambda r: ["scenario failure line"])]
    _, text = run([ev], catalog=cat)
    low = text.lower()
    assert "fail/finding details" in low
    sect = text[low.index("fail/finding details"):]
    assert "expected: row is CLOUD_COMPLETE" in sect and "observed: row was CLOUD_FAILED" in sect
    assert "expected: flow completes" in sect and "observed: ExceptionGroup" in sect
    assert "scenario failure line" in sect


def test_pass_checks_not_in_details(tmp_path, run):
    ev = tmp_path / "ev"
    make_lane(ev, "A1", checks=[check("2090-12-15", "C1", expected="UNIQUE_PASS_EXPECTED")])
    _, text = run([ev])
    low = text.lower()
    tail = text[low.index("fail/finding details"):] if "fail/finding details" in low else ""
    assert "UNIQUE_PASS_EXPECTED" not in tail


def test_mail_summary_counts_and_m3_limit(tmp_path, run):
    ev = tmp_path / "ev"
    mails = [("2090-12-18T08:00:00+05:30", "")]
    mails += [("2090-12-20T02:00:00+05:30", f"severity=warning;code=CODE_A;key=k{i}") for i in range(5)]
    mails += [("2090-12-21T02:00:00+05:30", "severity=info;code=rollover:activated:FY91-92;key=z")]
    make_lane(ev, "A1", mails=mails)
    make_lane(ev, "A2", mails=[("2090-12-20T02:00:00+05:30", "severity=warning;code=CODE_A;key=a")])
    _, text = run([ev])
    assert "severity WARNING: 5" in text
    assert "code CODE_A: 5" in text
    assert "severity INFO: 1" in text
    assert "max problem mails/day: 5 (limit 4)" in text
    assert "EXCEEDS" in text
    assert "max problem mails/day: 1 (limit 4)" in text
    # the lane within the limit is not flagged
    assert "EXCEEDS" not in line_with(text, "max problem mails/day: 1")


def test_lane_run_facts(tmp_path, run):
    ev = tmp_path / "ev"
    make_lane(ev, "A1", days=30, wall=226.1, status="completed", orphans=["robocopy.exe 99"], wake=48, shut=34,
              safety=(("S1", "ok"), ("S7", "FAIL")))
    _, text = run([ev])
    for s in ("days simulated: 30", "wall time: 226.1", "status: completed", "orphan_robocopy: 1",
              "wake calls: 48", "shutdown calls: 34", "S1: ok", "S7: FAIL"):
        assert s in text, s


def test_inputs_and_outputs_saved_section(tmp_path, run):
    ev = tmp_path / "ev"
    make_lane(ev, "A1", files={"checks.jsonl": 150893, "mail_index.csv": 2222})
    make_lane(ev, "A2", files={"inputs.jsonl": 4096})
    _, text = run([ev])
    low = text.lower()
    assert "inputs and outputs saved" in low
    sect = text[low.index("inputs and outputs saved"):]
    assert "150893" in line_with(sect, "checks.jsonl")
    assert "2222" in line_with(sect, "mail_index.csv")
    assert "4096" in line_with(sect, "inputs.jsonl")


def test_build_identity_and_no_product_change_statement(tmp_path, run):
    ev = tmp_path / "ev"
    make_lane(ev, "A1")
    make_lane(ev, "A2", commit="b" * 40)
    _, text = run([ev])
    assert f"Build tested: {COMMIT}" in text
    assert f"Build tested: {'b' * 40}" in text
    assert "no product code was changed" in text.lower()


def test_deterministic_bytes_and_root_order(tmp_path, monkeypatch):
    m = mr()
    monkeypatch.setattr(m, "load_catalog", lambda: [fake_scn("P1", "lane_a")])
    e1, e2 = tmp_path / "e1", tmp_path / "e2"
    make_lane(e1, "lane_a", checks=[check("2090-12-15", "C1")])
    make_lane(e2, "lane_b", checks=[check("2090-12-15", "C2", "FAIL")])
    out = tmp_path / "REPORT.md"
    m.build_report([e1, e2], out)
    first = out.read_bytes()
    m.build_report([e1, e2], out)
    assert out.read_bytes() == first
    m.build_report([e2, e1], out)
    assert out.read_bytes() == first


def test_lane_dir_given_directly_as_root(tmp_path, run):
    ev = tmp_path / "ev"
    d = make_lane(ev, "A1", checks=[check("2090-12-15", "C1")])
    res, text = run([d])
    assert res["PASS"] == 1 and "ev/A1/checks.jsonl:1" in text


def test_main_cli_writes_report(tmp_path, monkeypatch):
    m = mr()
    monkeypatch.setattr(m, "load_catalog", lambda: [])
    ev = tmp_path / "ev"
    make_lane(ev, "A1", checks=[check("2090-12-15", "C1")])
    out = tmp_path / "sub" / "REPORT.md"
    rc = m.main(["--evidence", str(ev), str(tmp_path / "nothing_here"), "--out", str(out)])
    assert rc == 0 and out.is_file()
    assert "| PASS | 1 |" in out.read_text(encoding="utf-8")


def test_default_catalog_loads_real_scenarios():
    cat = mr().load_catalog()
    ids = {s.id for s in cat}
    assert {"X09", "X18"} <= ids
    assert {s.id: s.expected_red for s in cat}["X09"] == "F-OWN-1"
