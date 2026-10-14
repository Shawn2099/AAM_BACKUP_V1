"""WAVE 1F â€” hypothesis property tests + parser forgery attempts.

Goal: try to BREAK the fail-closed contracts by generating adversarial inputs.
Every test records an evidence row; failures are findings, not pass/fail noise.
"""
from __future__ import annotations

import string
from pathlib import PureWindowsPath

import pytest
from hypothesis import HealthCheck, assume, given, settings
import hypothesis.strategies as st

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(600)

SET = settings(max_examples=250, deadline=None, suppress_health_check=list(HealthCheck))


# â”€â”€ classify_exit_code: exhaustive over the whole bit space â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_w1f_classify_exit_code_exhaustive():
    from core.lan_sync import classify_exit_code

    rows = []
    for code in list(range(-5, 40)) + [255, 256, 9999]:
        rows.append(f"{code:>6} -> {classify_exit_code(code)}")
    L.record("F.classify_exit_code", "robocopy exit-code classification is total & consistent",
             "code -> status:\n" + "\n".join(rows[:24]) + "\n...",
             "OK" if classify_exit_code(16).startswith("LAN_FAILED") else "unexpected")
    # documented mapping (v1.1.3: aligned with decide_lan_result — codes 0-7
    # with a finished pass are COMPLETE, only bit-3 copy errors are PARTIAL)
    assert classify_exit_code(0) == "LAN_COMPLETE"
    assert classify_exit_code(3) == "LAN_COMPLETE"
    assert classify_exit_code(4) == "LAN_COMPLETE"
    assert classify_exit_code(7) == "LAN_COMPLETE"
    assert classify_exit_code(8) == "LAN_PARTIAL"
    assert classify_exit_code(15) == "LAN_PARTIAL"
    assert classify_exit_code(16) == "LAN_FAILED"
    assert classify_exit_code(-1) == "LAN_FAILED"


# â”€â”€ decide_lan_result: the forged-success hunt â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@given(exit_code=st.integers(min_value=-3, max_value=40),
       lines=st.lists(st.text(alphabet=string.printable), min_size=0, max_size=12))
@SET
def test_w1f_lan_never_complete_without_completed_summary(exit_code, lines):
    """INVARIANT: COMPLETE requires BOTH the 'Files :' row AND the 'Ended :' line."""
    from core.lan_sync import decide_lan_result

    def has(pred):
        return any(pred(ln) for ln in lines)

    res = decide_lan_result(exit_code, "\n".join(lines))
    if res["status"] == "LAN_COMPLETE":
        # if it says COMPLETE, the log must prove completion
        assert res["log_complete"] is True
        assert res["termination"] == "normal"
    if res["status"] == "LAN_SUSPECT":
        assert res["termination"] == "abnormal"

    # targeted forge: a summary row without an "Ended :" line must never be COMPLETE
    forged = ["Files :    12    12    0    0    0    0"] + lines
    r2 = decide_lan_result(0, "\n".join(forged))
    if not has(lambda ln: ln.strip().startswith("Ended")):
        assert r2["status"] != "LAN_COMPLETE", f"FORGED COMPLETE with: {forged}"


@given(tail=st.text(alphabet=string.printable))
@SET
def test_w1f_lan_bit3_floor_and_failed_floor(tail):
    from core.lan_sync import failed_file_count

    assert failed_file_count(tail, 8) >= 1          # bit 3 -> at least one failure
    assert failed_file_count("", 8) == 1
    assert failed_file_count(tail, 0) >= 0
    assert failed_file_count("** FAILED: x\n" * 3, 0) >= 0


# â”€â”€ decide_cloud_verify_result: forged verification hunt â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@given(exit_code=st.integers(min_value=-1, max_value=4),
       body=st.text(alphabet=string.printable))
@SET
def test_w1f_verify_never_verified_with_contradiction(exit_code, body):
    """INVARIANT: verified=True needs exit 0 + a 'N differences found' summary with
    N==0 + no 'Failed to check' + no anchored ERROR/CRITICAL line."""
    from core.cloud_verify import decide_cloud_verify_result

    log = f"2026/09/06 18:18:52 NOTICE : x: 0 differences found\n{body}"
    res = decide_cloud_verify_result(exit_code, log)
    if isinstance(exit_code, int) and exit_code == 0:
        ok = True
    else:
        assert res["verified"] is False, f"verified with exit={exit_code}: {log[:200]}"

    # anchored ERROR line anywhere -> must fail closed
    err_log = "2026/09/06 18:18:52 ERROR : f.txt: sizes differ\n" \
              "2026/09/06 18:18:52 NOTICE : x: 0 differences found"
    assert decide_cloud_verify_result(0, err_log)["verified"] is False

    # divergence summary -> must fail closed
    div = "2026/09/06 18:18:52 NOTICE : Failed to check: 3 differences found"
    assert decide_cloud_verify_result(1, div)["verified"] is False


@given(name=st.text(alphabet=string.ascii_letters + "0123456789 .-_/:"))
@SET
def test_w1f_verify_filename_cannot_forge_summary(name):
    """A file *named* '0 differences found' must not verify a diverged run."""
    from core.cloud_verify import decide_cloud_verify_result

    poisoned = (f"2026/09/06 18:18:52 NOTICE : {name}: 7 differences found\n"
                f"2026/09/06 18:18:52 ERROR : {name}: md5 differ\n"
                f"2026/09/06 18:18:52 NOTICE : Failed to check with errors: 7 differences found")
    res = decide_cloud_verify_result(1, poisoned)
    assert res["verified"] is False, f"forged via filename {name!r}: {res}"


# â”€â”€ helpers: max-duration / broadcast / FY prefix â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@given(timeout=st.integers(min_value=1, max_value=200000), cfg=st.none() | st.integers(min_value=0, max_value=100000))
@SET
def test_w1f_resolve_max_duration(timeout, cfg):
    from core.cloud_sync import resolve_max_duration_seconds

    out = resolve_max_duration_seconds(timeout, cfg)
    if cfg is None:
        expected = timeout - 300 if timeout - 300 > 0 else None
    else:
        expected = cfg if cfg > 0 else None
    assert out == expected, (timeout, cfg, out)
    assert out is None or out > 0


@given(a=st.integers(0, 255), b=st.integers(0, 255), c=st.integers(0, 255))
@SET
def test_w1f_broadcast_address(a, b, c):
    import ipaddress

    from models.config import WolConfig

    w = WolConfig(enabled=False, mac_address="", server_ip=f"{a}.{b}.{c}.7")
    bc = w.get_broadcast_address()
    ip = ipaddress.IPv4Address(bc)
    assert str(ip) == f"{a}.{b}.{c}.255", bc


@given(y=st.integers(2000, 2100), m=st.integers(1, 12), d=st.integers(1, 28))
@SET
def test_w1f_fy_prefix(y, m, d):
    import datetime as dt

    from core.time_utils import get_fy_prefix

    out = get_fy_prefix(dt.date(y, m, d))
    assert out.startswith("FY")
    assert len(out) >= 7
    if (m, d) == (4, 1):
        assert out == f"FY{y % 100:02d}-{(y + 1) % 100:02d}"
    if (m, d) == (3, 31):
        assert out == f"FY{(y - 1) % 100:02d}-{y % 100:02d}"


# â”€â”€ diff/snapshot invariants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@given(before=st.dictionaries(st.text(min_size=1, max_size=8), st.tuples(st.integers(0, 10**9), st.floats())),
       after=st.dictionaries(st.text(min_size=1, max_size=8), st.tuples(st.integers(0, 10**9), st.floats())))
@SET
def test_w1f_diff_snapshots_partition(before, after):
    from core.lan_manifest import diff_snapshots

    d = diff_snapshots(before, after)
    added, modified, removed, unchanged = set(d["added"]), set(d["modified"]), set(d["removed"]), set(d["unchanged"])
    allkeys = set(before) | set(after)
    assert added | modified | removed | unchanged == allkeys
    for s in (added, modified, removed, unchanged):
        assert isinstance(s, set)
    # pairwise disjoint
    assert not (added & modified) and not (added & removed) and not (modified & removed)
    # membership semantics
    for p in added:
        assert p in after and p not in before
    for p in removed:
        assert p in before and p not in after


# â”€â”€ path normalisation never escapes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@given(p=st.text(alphabet=string.printable, max_size=40))
@SET
def test_w1f_clean_path_normalisation(p):
    from core.backup_repository import _clean_path

    out = _clean_path(p)
    if out:                                    # "" is the documented result for empty/dot/root
        assert not str(out).startswith("\\"), out
        assert not str(out).startswith("/"), out
        assert out != ".", out
        assert "\\" not in PureWindowsPath(out).as_posix()
        # idempotence: applying twice must be stable
        twice = _clean_path(out)
        if twice != out:
            L.record("F.clean_path_idempotence",
                     "_clean_path() is idempotent (normalising twice is a no-op)",
                     f"input={p!r} -> _clean_path={out!r} -> _clean_path again={twice!r}",
                     "MINOR: double application differs (trailing space/separator not stripped on pass 1)")
        assert _clean_path(twice) == twice


# â”€â”€ rclone log scan: can an error be hidden? â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@given(msg=st.text(alphabet=string.printable), lvl=st.sampled_from(["DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL"]))
@SET
def test_w1f_rclone_log_error_scan(msg, lvl):
    import json

    from core.cloud_sync import scan_rclone_log_for_errors

    line = json.dumps({"level": lvl, "msg": msg.replace('"', "'")})
    has_err, tail = scan_rclone_log_for_errors(line)
    if lvl in ("ERROR", "CRITICAL"):
        assert has_err is True, (lvl, line)
    else:
        assert has_err is False or "failed to" in line.lower(), (lvl, line)

    # a filename containing "failed to" in a *plaintext* line still flags (fail-closed)
    has2, _ = scan_rclone_log_for_errors("NOTICE: something failed to parse: /data/failed to sync.txt")
    assert has2 is True


# â”€â”€ integrity combined-file parsing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@given(paths=st.lists(st.text(alphabet=string.ascii_letters + string.digits + "./_-"), max_size=6),
       marks=st.lists(st.sampled_from(["+", "-", "*", "=", "!", "x", ""]), min_size=6, max_size=6))
@SET
def test_w1f_combined_file_parsing(paths, marks, sandbox):
    from core.integrity import _norm_rel, _parse_combined_file

    lines = []
    expect = {"+": 0, "-": 0, "*": 0, "=": 0, "!": 0}
    for p, m in zip(paths, marks):
        name = p or "f"
        lines.append(f"{m} {name}")
        # the parser skips entries whose normalised relative path is empty/"." —
        # mirror that in the expectation
        if m in expect and _norm_rel(name) not in ("", "."):
            expect[m] += 1
    diff_file = sandbox / "runtime" / "combined.txt"
    diff_file.write_text("\n".join(lines), encoding="utf-8")
    added, removed, modified, unchanged, errors, present = _parse_combined_file(str(diff_file))
    assert (len(added), len(removed), len(modified), len(unchanged), len(errors)) == (
        expect["+"], expect["-"], expect["*"], expect["="], expect["!"])
    assert present is True


# â”€â”€ lock file payloads: no input may crash the reader â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@given(payload=st.text())
@SET
def test_w1f_lock_reader_never_raises(payload, sandbox):
    from core.process import read_lock_alive

    lock = sandbox / "runtime" / "backup.lock"
    lock.write_text(payload, encoding="utf-8")
    alive, pid = read_lock_alive(lock)          # must not raise
    assert isinstance(alive, bool)
    assert pid is None or isinstance(pid, int)


# â”€â”€ cron rendering never raises â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@given(c=st.text(alphabet=string.printable))
@SET
def test_w1f_cron_to_human_never_raises(c):
    from core.time_utils import cron_to_human

    out = cron_to_human(c, "Asia/Kolkata")
    assert isinstance(out, str)

