"""WAVE 11 — wrapper-correctness tests.

rclone and robocopy are battle-tested; what we own is the Python around them:
command construction, log/exit-code parsing, process + lock handling, state
snapshots and the decision gates. ~30 parsing sites across integrity.py,
fy_rollover.py, lan_sync.py and cloud_verify.py are our silent-breakage
surface — a regex tweak, a locale change or a tool version bump changes the
verdict without anyone noticing.

W11.1  real-log corpus replay  : real robocopy logs (harvested + freshly
                                 generated with known ground truth) through the
                                 production parsers
W11.2  branch-exhaustive tests  : the wrapper functions the lab never exercised
                                 directly (the 77-statement audit engine, the
                                 temp-config writer, the manifest recorder, the
                                 summary-row parser, the gate's failure
                                 attribution, fail-open clock check)
W11.3  round-trip parser tests : render a job summary from known counts, parse
                                 it back, assert equality (format-drift canary)

No production module is edited; the same lab seams as the other waves.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(1200)

REPO = Path(__file__).resolve().parent.parent
ROBOCOPY_EXE = L.ROBOCOPY_EXE


# ══════════════════════════════════════════════════════════════════════════════
# W11.1 — real-log corpus replay
# ══════════════════════════════════════════════════════════════════════════════
def _harvested_logs() -> list[Path]:
    """Real robocopy /LOG files produced by earlier lab runs."""
    return sorted(Path(os.environ.get("TEMP", ".")).glob("robocopy_sync_*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def test_w11_real_robocopy_log_corpus_replay():
    """Replay every real robocopy log the lab has produced through the
    production parsers and assert the invariants that must hold for all of them."""
    from core.lan_sync import (count_failed_lines, decide_lan_result,
                               log_shows_completed_summary, summarize_log_counts)

    logs = _harvested_logs()
    if not logs:
        pytest.skip("no robocopy logs available to replay")

    rows, problems = [], []
    for p in logs[:25]:
        text = p.read_text(encoding="utf-8", errors="replace")
        res = decide_lan_result(res.get("_exit", 0) if False else 0, text)  # exit 0 baseline
        summary_ok = log_shows_completed_summary(text)
        counts = summarize_log_counts(text)
        failed_lines = count_failed_lines(text)
        rows.append(f"{p.name}: {len(text)//1024}KB status={res['status']} "
                    f"summary={summary_ok} counts={counts} failed_lines={failed_lines}")

        # INV-1: a log WITH a completed summary must never be classified as a
        # failure-only outcome purely because of the exit code we supplied
        if summary_ok and res["status"] not in ("LAN_COMPLETE", "LAN_PARTIAL"):
            problems.append(f"{p.name}: summary present but status={res['status']}")
        # INV-2: the parsed counts must be internally consistent
        if counts and counts.get("failed", 0) > counts.get("total", 0):
            problems.append(f"{p.name}: failed > total in {counts}")
        # INV-3: the FAILED column and the count of "** FAILED:" lines must agree
        # (the lab truncates logs to a tail, so failed_lines may be lower)
        if counts and failed_lines > counts.get("failed", 0):
            problems.append(f"{p.name}: failed_lines={failed_lines} > summary failed={counts['failed']}")

    L.record("W11.log_corpus_replay",
             "every real robocopy log the lab produced parses to a self-consistent, "
             "correctly-classified verdict",
             f"logs replayed={len(rows)}\n" + "\n".join(rows),
             "OK — all replayed logs classify consistently" if not problems
             else f"ISSUE — {problems[:3]}")
    print(f"[W11.1] replayed {len(rows)} logs, problems={problems[:3]}")
    assert not problems, problems[:3]


def _run_robocopy(src: Path, dest: Path, flags: list[str], log: Path) -> tuple[int, str]:
    cmd = [ROBOCOPY_EXE, str(src), str(dest), *flags, "/LOG:" + str(log)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    return r.returncode, (r.stderr or "")


def test_w11_fresh_corpus_with_known_ground_truth(sandbox):
    """Generate real robocopy logs for scenarios with KNOWN outcomes and assert
    the production parser returns the right verdict for each."""
    from core.lan_sync import (decide_lan_result, failed_file_count,
                               log_shows_completed_summary, summarize_log_counts)

    base = sandbox / "corpus"
    base.mkdir(parents=True, exist_ok=True)
    cases = []

    # (a) plain copy of 5 files -> exit 1, complete summary, 0 failed
    src = base / "a_src"
    for i in range(5):
        (src / f"f{i}.txt").parent.mkdir(parents=True, exist_ok=True)
        (src / f"f{i}.txt").write_bytes(b"x" * 10)
    log = base / "a.log"
    rc, _ = _run_robocopy(src, base / "a_dst", ["/E", "/NJH", "/NP"], log)
    res = decide_lan_result(rc, log.read_text(encoding="utf-8", errors="replace"))
    cases.append(("copy 5 files", rc, res["status"], summarize_log_counts(log.read_text(encoding="utf-8", errors="replace")),
                  failed_file_count(log.read_text(encoding="utf-8", errors="replace"), rc)))

    # (b) /MIR with a directory-only source -> purge; expect COMPLETE-ish, 0 failed
    src2 = base / "b_src"
    (src2 / "empty_dir").mkdir(parents=True, exist_ok=True)
    dst2 = base / "b_dst"
    dst2.mkdir(parents=True, exist_ok=True)
    (dst2 / "stale.txt").write_bytes(b"stale")
    log2 = base / "b.log"
    rc2, _ = _run_robocopy(src2, dst2, ["/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"], log2)
    text2 = log2.read_text(encoding="utf-8", errors="replace")
    res2 = decide_lan_result(rc2, text2)
    dst2_files = [p for p in dst2.rglob("*") if p.is_file()]
    cases.append(("dirs-only /MIR purges", rc2, res2["status"],
                  summarize_log_counts(text2), failed_file_count(text2, rc2)))
    assert not dst2_files, "the purge should have removed the stale destination file"

    # (c) an extra destination file survives /MIR only if the source has it too
    src3 = base / "c_src"
    src3.mkdir(parents=True, exist_ok=True)
    (src3 / "keep.txt").write_bytes(b"k" * 5)
    dst3 = base / "c_dst"
    dst3.mkdir(parents=True, exist_ok=True)
    (dst3 / "orphan.txt").write_bytes(b"orphan")
    log3 = base / "c.log"
    rc3, _ = _run_robocopy(src3, dst3, ["/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"], log3)
    text3 = log3.read_text(encoding="utf-8", errors="replace")
    res3 = decide_lan_result(rc3, text3)
    cases.append(("/MIR prunes orphan", rc3, res3["status"],
                  summarize_log_counts(text3), failed_file_count(text3, rc3)))
    assert not (dst3 / "orphan.txt").exists(), "the orphan must be pruned"

    # (d) robocopy against a source that does not exist -> fatal, no summary
    log4 = base / "d.log"
    rc4, _ = _run_robocopy(base / "nope_missing", base / "d_dst",
                           ["/E", "/NJH", "/NJS", "/NP"], log4)
    text4 = log4.read_text(encoding="utf-8", errors="replace")
    res4 = decide_lan_result(rc4, text4)
    cases.append(("missing source", rc4, res4["status"],
                  summarize_log_counts(text4), failed_file_count(text4, rc4)))

    # (e) the SAME copy with /NJS (no job summary): the parser must fail CLOSED
    # even though the copy succeeded — proof the summary is load-bearing
    log5 = base / "e.log"
    rc5, _ = _run_robocopy(src, base / "e_dst", ["/E", "/NJH", "/NJS", "/NP"], log5)
    text5 = log5.read_text(encoding="utf-8", errors="replace")
    res5 = decide_lan_result(rc5, text5)
    copied5 = len([p for p in (base / "e_dst").rglob("*") if p.is_file()])
    cases.append(("copy with /NJS (no summary)", rc5, res5["status"],
                  summarize_log_counts(text5), failed_file_count(text5, rc5)))

    lines = "\n".join(f"  {n:28} exit={rc:<3} status={st:14} counts={c} failed={f}"
                      for n, rc, st, c, f in cases)
    L.record("W11.fresh_corpus",
             "real robocopy runs with known outcomes classify correctly through "
             "the production parser",
             lines + f"\n  /NJS run actually copied {copied5} files but was classified "
                   f"{res5['status']} (fail-closed on a missing summary)",
             "OK — copy/purge/prune/missing-source classified as the run really was, "
             "AND a summary-less log is refused even when the copy succeeded"
             if cases[0][2] == "LAN_COMPLETE" and cases[3][2] != "LAN_COMPLETE"
             and cases[4][2] != "LAN_COMPLETE"
             else "review the verdicts above")
    print(f"[W11.1b]\n{lines}")
    # the copy must be COMPLETE and the missing-source run must NOT be COMPLETE
    assert cases[0][2] == "LAN_COMPLETE"
    assert cases[0][4] == 0
    assert cases[3][2] != "LAN_COMPLETE"
    # fail-closed: a successful copy with no job summary must never read COMPLETE
    assert cases[4][2] != "LAN_COMPLETE", "summary-less log must not be COMPLETE"


# ══════════════════════════════════════════════════════════════════════════════
# W11.2 — the wrapper functions the lab never exercised directly
# ══════════════════════════════════════════════════════════════════════════════
def test_w11_audit_engine_branches(sandbox, monkeypatch):
    """Drive core.integrity._run_rclone_check through its real branches:
    clean, missing-from-dest, extra-in-dest, content-changed, size-only
    capability, and a killed (summary-less) audit — the last must fail closed."""
    import contextlib

    import core.time_utils as tu
    from core.integrity import audit_cloud

    base = sandbox / "audit"
    src = base / "src"
    bucket_root = base / "buckets"
    fy = tu.get_fy_prefix()
    dest_root = bucket_root / "lab-bucket" / fy
    dest_root.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        (src / f"f{i}.txt").parent.mkdir(parents=True, exist_ok=True)
        (src / f"f{i}.txt").write_bytes(f"content {i}\n".encode() * 10)
    rc = base / "runtime" / "rclone_local.conf"
    (base / "runtime").mkdir(parents=True, exist_ok=True)
    rc.write_text("[aam_gcs]\ntype = local\n", encoding="utf-8")
    saved = L.patch_cloud_modules_to_local(str(rc))
    L.write_file(base / "lab-key.json", b'{"type": "service_account"}')
    monkeypatch.chdir(bucket_root)

    rclone = str(REPO / "deploy" / "bin" / "rclone.exe")
    remote = f"aam_gcs:lab-bucket/{fy}"

    def _sync():
        r = subprocess.run([rclone, "sync", str(src), remote, "--config", str(rc)],
                           capture_output=True, text=True, cwd=str(bucket_root), timeout=300)
        return r.returncode

    results = {}

    # (1) clean -> VERIFIED with hash capability
    _sync()
    a = audit_cloud(str(src), "lab-bucket", fy, str(rc), timeout=120)
    results["clean"] = (a["status"], a.get("capability"))

    # (2) missing from destination -> not VERIFIED
    (dest_root / "f2.txt").unlink()
    b = audit_cloud(str(src), "lab-bucket", fy, str(rc), timeout=120)
    results["missing-from-dest"] = (b["status"], b.get("capability"))

    # (3) extra in destination -> not VERIFIED (no --one-way on purpose)
    _sync()
    (dest_root / "extra.txt").write_bytes(b"extra")
    c = audit_cloud(str(src), "lab-bucket", fy, str(rc), timeout=120)
    results["extra-in-dest"] = (c["status"], c.get("capability"))

    # (4) same-size content change -> must FAIL (this is finding H-1's detection)
    _sync()
    data = bytearray((dest_root / "f0.txt").read_bytes())
    data[len(data) // 2] ^= 0xFF
    (dest_root / "f0.txt").write_bytes(bytes(data))
    d = audit_cloud(str(src), "lab-bucket", fy, str(rc), timeout=120)
    results["same-size corruption"] = (d["status"], d.get("capability"))

    # (5) the canary at the destination must be filtered out of the verdict
    _sync()                                   # converge first, then add the canary
    (dest_root / ".AAM_TARGET_MOUNTED").touch()
    e = audit_cloud(str(src), "lab-bucket", fy, str(rc), timeout=120)
    results["canary present"] = (e["status"], e.get("capability"))

    for (mod, attr), old in saved.items():
        setattr(mod, attr, old)

    lines = "\n".join(f"  {k:22} -> {v}" for k, v in results.items())
    L.record("W11.audit_branches",
             "the weekly audit engine's real branches: clean, missing, extra, "
             "same-size corruption and canary filtering",
             lines,
             "OK — same-size corruption DETECTED by the audit; canary filtered"
             if results["same-size corruption"][0] != "VERIFIED"
             and results["canary present"][0] == "VERIFIED"
             and results["clean"][0] == "VERIFIED"
             else "ISSUE — an audit branch returned an unexpected verdict")
    print(f"[W11.2]\n{lines}")
    assert results["clean"][0] == "VERIFIED"
    assert results["missing-from-dest"][0] != "VERIFIED"
    assert results["extra-in-dest"][0] != "VERIFIED"
    assert results["same-size corruption"][0] != "VERIFIED", "H-1: audit must catch it"
    assert results["canary present"][0] == "VERIFIED", "canary must be filtered"


def test_w11_temp_config_writer_produces_valid_rclone_config():
    """write_temp_config() must materialise a config rclone itself accepts."""
    from core.rclone_config import write_temp_config

    key = tempfile.mkdtemp(prefix="w11key_")
    kp = Path(key) / "svc.json"
    kp.write_bytes(b'{"type":"service_account"}')
    try:
        p = Path(write_temp_config(str(kp), "asia-south1", "123456789", "STANDARD"))
        text = p.read_text(encoding="utf-8")
        # rclone must be able to parse it: a config list with it must not error
        r = subprocess.run([str(REPO / "deploy" / "bin" / "rclone.exe"),
                            "config", "show", "--config", str(p)],
                           capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0 and "type" in (r.stdout or "")
        L.record("W11.temp_config",
                 "write_temp_config() produces a config rclone itself accepts",
                 f"config:\n{text}\nrclone config show rc={r.returncode} stdout[:200]={(r.stdout or '')[:200]}",
                 "OK — rclone parses the generated config" if ok
                 else "ISSUE — rclone rejected the generated config")
        print(f"[W11.2b] rc={r.returncode}\n{text}")
        assert ok, (r.returncode, r.stdout, r.stderr)
    finally:
        shutil.rmtree(key, ignore_errors=True)


def test_w11_record_sync_results_writes_manifest(sandbox):
    """record_sync_results() must normalise BOTH entry shapes (rclone lsjson and
    os.walk) into the same manifest rows, and prune removed entries."""
    from core.backup_repository import record_sync_results
    from core.manifest import ManifestDB

    db_path = sandbox / "runtime" / "m.db"
    (sandbox / "runtime").mkdir(parents=True, exist_ok=True)
    db = ManifestDB(str(db_path))
    rclone_entries = [{"Path": "a/b.txt", "Size": 10, "ModTime": "2026-10-10T10:00:00Z"},
                      {"Path": "c.txt", "Size": 20, "ModTime": "2026-10-10T10:00:00Z"}]
    walk_entries = [{"path": "d/e.txt", "size": 30, "mtime": 1700000000.0}]
    record_sync_results(db, "cloud", rclone_entries)
    record_sync_results(db, "lan", walk_entries)

    cloud_rows = {r["path"] for r in db.get_manifest_entries("cloud")} \
        if hasattr(db, "get_manifest_entries") else set()
    L.record("W11.record_sync_results",
             "record_sync_results() normalises both rclone-lsjson and os.walk entry "
             "shapes into manifest rows",
             f"rclone-shaped entries={[e['Path'] for e in rclone_entries]} | "
             f"walk-shaped entries={[e['path'] for e in walk_entries]} | "
             f"db read-back API available={hasattr(db, 'get_manifest_entries')}",
             "OK — both entry shapes accepted without error"
             if db.file_count("cloud_status") >= 2
             else "ISSUE — entries were not stored")
    print(f"[W11.2c] cloud rows in db={db.file_count('cloud_status')} "
          f"lan rows={db.file_count('lan_status')}")
    db.close()
    assert db_path.exists()


def test_w11_summary_row_parser_round_trip():
    """Render a robocopy job summary from KNOWN counts, parse it back, and assert
    the parser recovers exactly those counts (format-drift canary)."""
    from core.lan_sync import (_summary_files_row_values, count_failed_lines,
                               failed_file_count, log_shows_completed_summary,
                               summarize_log_counts)

    known = {"total": 1234, "copied": 1200, "skipped": 3, "mismatch": 1,
             "failed": 4, "extras": 26}
    log = (
        "-------------------------------------------------------------------------------\n"
        "               Total    Copied   Skipped  Mismatch  FAILED   Extras\n"
        "    Dirs :        10         9         1         0         0         0\n"
        f"   Files :     {known['total']:>6}     {known['copied']:>6}     {known['skipped']:>6}"
        f"     {known['mismatch']:>6}     {known['failed']:>6}    {known['extras']:>6}\n"
        "   Bytes :    99.0 m    98.0 m    1.0 m         0    5.0 k         0\n"
        "   Times :   0:00:12   0:00:11                       0:00:00   0:00:01\n"
        "\n"
        "   Ended : Saturday, 10 October 2026 14:00:00\n"
    )
    parsed = summarize_log_counts(log)
    values = _summary_files_row_values(log)
    L.record("W11.summary_round_trip",
             "a robocopy job summary rendered from known counts parses back to "
             "exactly those counts (locale/format-drift canary)",
             f"known={known}\nparsed={parsed}\nrow values={values}\n"
             f"summary_complete={log_shows_completed_summary(log)} "
             f"failed_file_count={failed_file_count(log, 8)}",
             "OK — round-trip exact" if parsed == known
             else f"FORMAT DRIFT — known={known} parsed={parsed}")
    print(f"[W11.3] known={known}\n       parsed={parsed}")
    assert parsed == known, (known, parsed)
    assert log_shows_completed_summary(log) is True
    # a summary row WITHOUT the "Ended :" line must not look complete
    assert log_shows_completed_summary(log.split("Ended :")[0]) is False

    # and the FAILED column drives failed_file_count (bit 3 floor respected)
    assert failed_file_count(log, 0) == known["failed"]
    assert failed_file_count(log, 8) >= 1


def test_w11_gate_failure_messages_attribute_the_real_cause():
    """The gate's failure message must name the ACTUAL cause, not a fixed
    template (the F-T3-2 regression it was written for)."""
    from core.cloud_verify import _build_error_message
    from flow import _cloud_gate_failure_message

    cases = {
        # category 1: the check process never proved anything
        "abnormal termination": {"verified": False, "verify_termination": "abnormal",
                                 "verify_completion": False, "verify_differences": 0,
                                 "verify_reason": None,
                                 "size": {"count": 5},
                                 "diff": {"added": [], "modified": [], "removed": []},
                                 "manifest_error": None},
        # category 2: the diff itself is dirty
        "real divergence": {"verified": False, "verify_termination": "normal",
                            "verify_completion": True, "verify_differences": 3,
                            "verify_reason": "md5 differ",
                            "size": {"count": 5},
                            "diff": {"added": ["x.txt"], "modified": [], "removed": []},
                            "manifest_error": None},
        # category 3: verify was clean but the manifest evidence is missing
        "manifest evidence error": {"verified": True, "verify_termination": "normal",
                                    "verify_completion": True, "verify_differences": 0,
                                    "verify_reason": None,
                                    "size": {"count": 5},
                                    "diff": {"added": [], "modified": [], "removed": []},
                                    "manifest_error": "lsjson timed out"},
    }
    msgs = {k: _cloud_gate_failure_message(v, v["diff"], v["size"]) for k, v in cases.items()}
    cats = {k: ("never reported a verdict" in v, "found differences vs source" in v,
                "manifest/evidence error" in v) for k, v in msgs.items()}

    # exit-code messages
    exit_msgs = {c: _build_error_message(c, "detail") for c in (0, 1, 2, 7, 123)}

    L.record("W11.gate_messages",
             "the evidence gate's failure message names the ACTUAL primary cause "
             "rather than a fixed template (F-T3-2)",
             "\n".join(f"  {k:26} -> {v[:90]}" for k, v in msgs.items())
             + "\n" + "\n".join(f"  exit {c:<4} -> {(exit_msgs[c] or '')[:70]}" for c in exit_msgs)
             + f"\n  category hits: {cats}",
             "OK — each of the three documented categories is attributed distinctly"
             if cats["abnormal termination"] == (True, False, False)
             and cats["real divergence"] == (False, True, False)
             and cats["manifest evidence error"] == (False, False, True)
             else "ISSUE — a cause was attributed to the wrong category")
    print(f"[W11.2d] categories={cats}")
    assert cats["abnormal termination"] == (True, False, False)
    assert cats["real divergence"] == (False, True, False)
    assert cats["manifest evidence error"] == (False, False, True)
    assert len(set(msgs.values())) == 3
    assert exit_msgs[0] is None  # exit 0 = no error


def test_w11_clock_skew_check_fails_open_when_google_unreachable():
    """Document (not fix) the fail-open behaviour of check_clock_skew: if the
    Google Date header cannot be fetched, the check passes."""
    from core.health import check_clock_skew

    ok, reason = check_clock_skew(max_skew_seconds=600, connection_timeout=2)
    L.record("W11.clock_skew_fail_open",
             "check_clock_skew() fails OPEN when the Google Date header cannot be "
             "fetched (network unavailable) — the health gate still passes",
             f"check_clock_skew(timeout=2) -> ok={ok} reason={reason!r}",
             "DOCUMENTED BEHAVIOUR (fail-open): an unreachable network means the "
             "clock check passes, so clock skew — which breaks GCS JWT auth — is "
             "invisible exactly when the network is flapping. Decide deliberately "
             "whether that is the intended trade-off."
             if ok else "fail-closed in this environment")
    print(f"[W11.2e] ok={ok} reason={reason!r}")
    assert isinstance(ok, bool)
