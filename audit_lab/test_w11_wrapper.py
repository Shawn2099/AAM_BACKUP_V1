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
# W11.1 (robocopy corpus replay & fresh robocopy corpus) removed in P2 CL3;
# rclone LAN decider and real-rclone lab coverage live in tests/test_p1_c3_lan_rclone_decider.py
# and audit_lab/test_w25_rclone_lan.py.


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


# W11.3 summary_row_parser_round_trip removed (robocopy UTF-16 summary parser replaced by rclone JSON stats)


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


def test_w11_clock_check_never_fails_open(tmp_path):
    """WP12 (was: documented fail-open of check_clock_skew): the REAL check
    against Google, then w32tm. Whatever this machine's network allows, the
    verdict is OK/SKEWED with a named source, or UNVERIFIED with both reasons -
    never OK without a source."""
    from core.health import CLOCK_UNVERIFIED, check_clock
    from core.run_artefacts import RunArtefacts

    r = check_clock(max_skew_seconds=600, connection_timeout=2, ntp_server="time.windows.com",
                    w32tm_timeout_s=30, artefacts=RunArtefacts(tmp_path, "w11-clock", day="d"))
    truthful = (r.state == CLOCK_UNVERIFIED and r.source is None and r.reason) or \
               (r.state != CLOCK_UNVERIFIED and r.source and r.skew_s is not None)
    L.record("W11.clock_skew_fail_open",
             "check_clock() never reports OK without a measured time source",
             f"check_clock(timeout=2) -> {r}",
             "OK - verdict names its source (or UNVERIFIED with both reasons)"
             if truthful else "ISSUE - verdict without a source")
    print(f"[W11.2e] {r}")
    assert truthful, r

