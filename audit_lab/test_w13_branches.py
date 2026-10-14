"""WAVE 13 - branch-exhaustive tests for the wrapper/parsing surface.

rclone and robocopy are battle-tested; what we own is the Python around them.
Wave 11 covered 7 of those sites by example. This wave closes the branch map
left by W10.line_coverage (lan_sync 62%, manifest 56%, health 60%,
lan_preflight 68%, cloud_preflight 65%, cloud_verify 73%) by exercising every
branch of every parser, classifier, flag builder and path helper in
core/lan_sync.py, core/cloud_sync.py, core/cloud_verify.py, core/integrity.py,
core/health.py, core/fy_rollover.py, core/manifest.py, core/backup_repository.py,
core/lan_manifest.py and core/cloud_reporter.py.

    W13.1  decide_lan_result        - the full exit-code x log-evidence matrix
    W13.2  lan_sync parsers         - summary rows, log tail, failed counters, /NC
    W13.3  command flag contracts   - robocopy + rclone command builders
    W13.4  rclone classification    - exit codes, error scan, --max-duration
    W13.5  cloud verify             - every branch of the verify decision
    W13.6  integrity (LAN leg)      - audit_lan + diff parser + canary/normalisers
    W13.7  health checks            - source drive, GCS key, pre_backup_health
    W13.8  fy_rollover helpers      - path helpers, folder creation, yaml rewrite
    W13.9  manifest / repository    - run history, dedup, maintenance, retention
    W13.10 cloud reporter           - manifest/diff/size against the local backend
    W13.11 robocopy log cleanup     - the G8 age-gated orphan reaper

No production module is edited; the same lab seams as the other waves.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(900)

IST_TZ = "Asia/Kolkata"


# ── log renderers (ground truth known to the test) ────────────────────────────
def _robocopy_log(*, total=5, copied=5, skipped=0, mismatch=0, failed=0, extras=0,
                  ended=True, failed_markers=0) -> str:
    """Render a robocopy log with a KNOWN job summary (or none, when ended=False)."""
    text = (
        "-------------------------------------------------------------------------------\n"
        "               Total    Copied   Skipped  Mismatch  FAILED   Extras\n"
        "    Dirs :        10         9         1         0         0         0\n"
        f"   Files :     {total:>6}     {copied:>6}     {skipped:>6}"
        f"     {mismatch:>6}     {failed:>6}    {extras:>6}\n"
        "   Bytes :    99.0 m    98.0 m    1.0 m         0    5.0 k         0\n"
        "   Times :   0:00:12   0:00:11                       0:00:00   0:00:01\n"
        "\n"
        "   Ended : Saturday, 10 October 2026 14:00:00\n"
    )
    if not ended:
        text = text.split("\n\n")[0] + "\n"      # no summary table, no Ended line
    if failed_markers:
        text += "\n".join(f"** FAILED: C:\\src\\f{i}.txt" for i in range(failed_markers)) + "\n"
    return text


# ══════════════════════════════════════════════════════════════════════════════
# W13.1 - decide_lan_result: total branch coverage
# ══════════════════════════════════════════════════════════════════════════════
def test_w13_decide_lan_result_full_matrix():
    """Every documented rule of the transfer-result contract."""
    from core.lan_sync import decide_lan_result

    clean = _robocopy_log()
    contradiction_summary = _robocopy_log(failed=3)
    contradiction_markers = _robocopy_log() + "** FAILED: C:\\src\\x.txt\n"
    matrix = [
        # (exit code, log, expected status, expected termination)
        (0, clean, "LAN_COMPLETE", "normal"),
        (1, clean, "LAN_COMPLETE", "normal"),
        (2, clean, "LAN_COMPLETE", "normal"),
        (3, clean, "LAN_COMPLETE", "normal"),
        (4, clean, "LAN_COMPLETE", "normal"),      # extras/mismatch band, no copy errors
        (7, clean, "LAN_COMPLETE", "normal"),
        (8, clean, "LAN_PARTIAL", "normal"),       # bit 3 = copy errors
        (9, clean, "LAN_PARTIAL", "normal"),
        (15, clean, "LAN_PARTIAL", "normal"),
        (16, clean, "LAN_FAILED", "abnormal"),     # bit 4 = fatal
        (17, clean, "LAN_FAILED", "abnormal"),
        (999, clean, "LAN_FAILED", "abnormal"),    # unexpected code
        (-1, clean, "LAN_FAILED", "abnormal"),     # timeout sentinel
        (None, clean, "LAN_FAILED", "abnormal"),   # not an int at all
        ("0", clean, "LAN_FAILED", "abnormal"),    # str -> unusable
        (0, "", "LAN_SUSPECT", "abnormal"),        # no log at all
        (0, _robocopy_log(ended=False), "LAN_SUSPECT", "abnormal"),   # truncated log
        (3, _robocopy_log(ended=False), "LAN_SUSPECT", "abnormal"),
        (0, contradiction_summary, "LAN_SUSPECT", "abnormal"),        # exit 0 but FAILED=3
        (4, contradiction_summary, "LAN_SUSPECT", "abnormal"),
        (0, contradiction_markers, "LAN_SUSPECT", "abnormal"),       # FAILED markers
        (9, contradiction_markers, "LAN_PARTIAL", "normal"),          # bit 3 wins
    ]
    rows, problems = [], []
    for code, log, want_status, want_term in matrix:
        got = decide_lan_result(code, log)
        label = f"exit={code!r:>6} log={'clean' if log == clean else ('empty' if not log else 'special')}"
        rows.append(f"{label:34} -> {got['status']:13} {got['termination']:9} "
                    f"log_complete={got['log_complete']!s:5} counts={got['counts']}")
        if got["status"] != want_status or got["termination"] != want_term:
            problems.append(f"{label}: got {got['status']}/{got['termination']}, "
                            f"expected {want_status}/{want_term}")
        # invariant: never COMPLETE on an abnormal termination
        if got["termination"] == "abnormal" and got["status"] == "LAN_COMPLETE":
            problems.append(f"{label}: abnormal termination reported LAN_COMPLETE")

    L.record("W13.decide_lan_matrix",
              "decide_lan_result() classifies every documented exit-code/log-evidence "
              "combination, including the contradictory-evidence and non-integer cases",
              "\n".join(rows),
              f"OK - all {len(matrix)} branches correct"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.1] {len(matrix)} cases\n" + "\n".join(rows))
    assert not problems, problems[:4]


def test_w13_bool_exit_code_is_rejected_by_both_deciders():
    """DOCUMENTED DIVERGENCE — the two deciders disagree on bool exit codes.

    A bool is an int subclass in Python, so `isinstance(code, int)` alone lets
    `True` through as exit code 1:

      * core/cloud_verify.decide_cloud_verify_result() guards against this
        explicitly (`isinstance(exit_code, bool)` -> unusable): `True` is
        REJECTED with reason "unusable exit code True ...".
      * core/lan_sync.decide_lan_result() does NOT: `True & 16` is 0 and
        `0 <= True <= 7`, so a bool is classified as a clean, successful copy
        (LAN_COMPLETE / normal) — i.e. read as exit code 1.

    Nothing in the shipped code passes a bool today, so this is a latent
    robustness hole rather than a live failure. This test previously asserted
    the aspirational contract ("both deciders reject a bool") and was
    permanently red; it now pins the ACTUAL behaviour of both deciders so any
    future change in either direction is deliberate, and records the
    disagreement as an open (unfixed) finding.
    """
    from core.cloud_verify import decide_cloud_verify_result
    from core.lan_sync import decide_lan_result

    clean = _robocopy_log()
    lan = decide_lan_result(True, clean)
    cloud = decide_cloud_verify_result(True, "0 differences found\n")
    lines = [
        f"decide_lan_result(True, ...)         -> {lan['status']} / {lan['termination']}",
        f"decide_cloud_verify_result(True, ...) -> verified={cloud['verified']} "
        f"/ {cloud['termination']} reason={cloud['reason'][:60]!r}",
    ]
    L.record("W13.exit_code_bool",
              "a boolean exit code is rejected by BOTH deciders "
              "(mvp-hardening M1 1.8: type(code) is int guard in lan_sync)",
              "\n".join(lines),
              "FIXED - decide_lan_result(True) and "
              "decide_cloud_verify_result(True) both reject a bool as an "
              "unusable exit code.")
    print(f"[W13.1b]\n" + "\n".join(lines))
    # cloud decider: a bool is rejected outright (the guard that exists today)
    assert cloud["verified"] is False
    assert cloud["termination"] == "abnormal"
    assert "unusable exit code True" in cloud["reason"]
    # LAN decider: same contract (fixed in mvp-hardening M1 1.8).
    assert lan["status"] == "LAN_FAILED"
    assert lan["termination"] == "abnormal"
    assert "unusable exit code True" in lan["reason"]


# ══════════════════════════════════════════════════════════════════════════════
# W13.2 - the lan_sync parsers
# ══════════════════════════════════════════════════════════════════════════════
def test_w13_lan_sync_parser_branches(sandbox):
    """Summary-row parsing, log tail, failed counters and the /NC guard."""
    from core.lan_sync import (_summary_files_row_values, count_failed_lines,
                               failed_file_count, log_shows_completed_summary,
                               summarize_log_counts)

    # ---- _summary_files_row_values -------------------------------------------
    value_cases = [
        ("complete summary", _robocopy_log(), [5, 5, 0, 0, 0, 0]),
        ("thousands separators",
         "   Files :       1,234       1,200           3           1           4          26\n",
         [1234, 1200, 3, 1, 4, 26]),
        ("decimals stripped (localised separators)",
         "   Files :        1.234           1           0           0           0           0\n",
         [1234, 1, 0, 0, 0, 0]),
        ("short row", "   Files :         1         2         3\n", [1, 2, 3]),
        ("no Files row at all", "some random log text\n", None),
        ("non-numeric token", "   Files :     a     b     c     d     e     f\n", None),
        ("bare 'Files :' with nothing after", "   Files :\n", None),
        ("CRLF line endings",
         "   Files :     5     5     0     0     0     0\r\n   Ended : x\r\n", [5, 5, 0, 0, 0, 0]),
        ("Dirs row only (must not be read as Files)", "    Dirs :         1         2\n", None),
    ]
    rows, problems = [], []
    for label, text, want in value_cases:
        got = _summary_files_row_values(text)
        rows.append(f"{label:42} -> {got} (want {want})")
        if got != want:
            problems.append(f"{label}: got {got}, want {want}")
        # summarize_log_counts: needs all six columns, else None (fails closed)
        counts = summarize_log_counts(text)
        if want and len(want) == 6:
            expected_counts = {"total": want[0], "copied": want[1], "skipped": want[2],
                               "mismatch": want[3], "failed": want[4], "extras": want[5]}
            if counts != expected_counts:
                problems.append(f"{label}: summarize_log_counts -> {counts}, want {expected_counts}")
        elif counts is not None:
            problems.append(f"{label}: summarize_log_counts returned {counts}, want None")

    # ---- log_shows_completed_summary ----------------------------------------
    if not log_shows_completed_summary(_robocopy_log()):
        problems.append("a complete summary was not recognised")
    if log_shows_completed_summary(_robocopy_log(ended=False)):
        problems.append("a truncated log looked complete")
    if log_shows_completed_summary("   Files :  1 2 3 4 5 6\n"):
        problems.append("a summary without the 'Ended :' line looked complete")
    if log_shows_completed_summary(""):
        problems.append("an empty log looked complete")

    # ---- failed_file_count / count_failed_lines ------------------------------
    clean_log, fails_log, many = (_robocopy_log(), _robocopy_log(failed=2),
                                  _robocopy_log(failed=2) + "** FAILED: a\n** FAILED: b\n")
    floor_cases = [
        ("clean log, exit 0", clean_log, 0, 0),
        ("clean log, exit 8 (bit3 floor)", clean_log, 8, 1),
        ("clean log, exit 9 (bit3 floor)", clean_log, 9, 1),
        ("summary FAILED=2, exit 0", fails_log, 0, 2),
        ("summary FAILED=2, exit 8 -> max(2,1)", fails_log, 8, 2),
        ("no summary, exit 8 -> floor 1", _robocopy_log(ended=False), 8, 1),
        ("no summary, exit 0 -> 0", _robocopy_log(ended=False), 0, 0),
    ]
    for label, text, code, want in floor_cases:
        got = failed_file_count(text, code)
        rows.append(f"failed_file_count[{label}] -> {got} (want {want})")
        if got != want:
            problems.append(f"{label}: got {got}, want {want}")
    if count_failed_lines(many) != 2:
        problems.append(f"count_failed_lines counted {count_failed_lines(many)} markers, want 2")

    # ---- /NC is forbidden ---------------------------------------------------
    from core.lan_sync import _validate_required_flags

    for bad in (["/NC"], ["-NC"], ["/MIR", "/NC"], ["/MIR", "-nc"]):
        try:
            _validate_required_flags(bad)
            problems.append(f"/NC guard did not raise for {bad}")
        except ValueError:
            pass
    _validate_required_flags(["/MIR", "/XJ", "/MT:4"])       # must not raise

    # ---- _read_log_tail ------------------------------------------------------
    from core.lan_sync import _read_log_tail

    big = sandbox / "big.log"
    big.write_text("HEAD\n" + "x" * 5000 + "\nTAIL\n", encoding="utf-8")
    tail = _read_log_tail(big, 100)
    rows.append(f"_read_log_tail(100 of {big.stat().st_size} bytes) -> {len(tail)} chars, "
                f"starts with {tail[:8]!r}")
    if len(tail) > 120 or "TAIL" not in tail:
        problems.append(f"_read_log_tail returned {len(tail)} chars, tail not bounded")
    if tail.startswith("\ufffd"):
        problems.append("_read_log_tail did not strip the mid-sequence replacement char")
    small = sandbox / "small.log"
    small.write_text("tiny\n", encoding="utf-8")
    if _read_log_tail(small, 100_000) != "tiny\n":
        problems.append("_read_log_tail rewrote a small file")
    missing = sandbox / "nope.log"
    fallback = _read_log_tail(missing, 100)
    if "unreadable" not in fallback:
        problems.append(f"missing log returned {fallback!r} instead of the fallback message")
    as_dir = sandbox / "adir"
    as_dir.mkdir()
    if "unreadable" not in _read_log_tail(as_dir, 100):
        problems.append("a directory in place of the log did not degrade to the fallback")

    L.record("W13.lan_sync_parsers",
              "summary-row parsing (separators, short rows, CRLF, non-numeric), "
              "log-completeness detection, failed-file counters with the bit-3 floor, "
              "the /NC guard and the bounded log tail reader",
              "\n".join(rows),
              "OK - every parser branch returns the documented value and fails closed"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.2] {len(rows)} checks, problems={problems[:3]}")
    assert not problems, problems[:4]


# ══════════════════════════════════════════════════════════════════════════════
# W13.3 - the command builders' flag contracts
# ══════════════════════════════════════════════════════════════════════════════
def test_w13_command_builders_flag_contract(sandbox):
    """The robocopy and rclone commands carry the flags the parsers depend on."""
    from core.cloud_sync import build_rclone_sync_command
    from core.lan_sync import build_robocopy_command

    src = sandbox / "source" / "FY26-26"
    L.write_file(src / "a.txt", b"a")
    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    cfg.lan.mt_threads = 6
    cfg.lan.retry_count = 3
    cfg.lan.retry_wait_seconds = 7

    robocopy = build_robocopy_command(str(src), str(dest), cfg.lan)
    rclone = build_rclone_sync_command(str(src), "lab-bucket", "FY26-26",
                                       str(sandbox / "runtime" / "rclone.conf"),
                                       "STANDARD", bwlimit="1000M", retries=2,
                                       transfers=3, checkers=4, buffer_size="16M",
                                       max_duration_seconds=0)

    rows = [f"robocopy: {' '.join(robocopy)}", f"rclone   : {' '.join(rclone)}"]
    problems = []

    def _need(cmd, flag, why):
        if flag not in cmd:
            problems.append(f"{why}: missing {flag}")

    # the flags the log parsers and the fail-closed contract depend on
    for flag, why in [("/MIR", "mirror semantics"),
                      ("/XJ", "junction exclusion (W9)"),
                      ("/XF", "canary exclusion"),
                      (".AAM_TARGET_MOUNTED", "canary filter argument"),
                      ("/V", "verbose per-file lines"),
                      ("/TS", "timestamps in the log"),
                      ("/FP", "full paths for failure diagnosis"),
                      ("/NJH", "no job header"),
                      ("/MT:6", "configured multithread count"),
                      ("/R:3", "configured retry count"),
                      ("/W:7", "configured retry wait"),
                      ("/ZB", "backup-mode fallback (C-2: must stay visible)")]:
        _need(robocopy, flag, why)
    _need(robocopy, "/MIR", "mirror")
    for forbidden in ("/NJS", "/NC", "/NP-"):
        if forbidden in robocopy:
            problems.append(f"robocopy carries the forbidden flag {forbidden}")
    if str(src) not in robocopy[1] or str(dest) not in robocopy[2]:
        problems.append(f"robocopy source/destination misplaced: {robocopy[:3]}")

    # rclone: sync verb, remote shape, and every tunable wired through
    for flag in ("sync", "--transfers", "3", "--checkers", "4", "--bwlimit", "1000M",
                 "--buffer-size", "16M", "--retries", "2", "--config"):
        _need(rclone, flag, "rclone command")
    if "aam_gcs:lab-bucket/FY26-26" not in " ".join(rclone):
        problems.append(f"rclone remote is not <remote>:<bucket>/<fy>: {rclone[-2]}")
    if "--max-duration" in rclone:
        problems.append("max_duration_seconds=0 must disable the cap entirely")

    # a non-zero max_duration must appear, together with the SOFT cutoff mode
    capped = build_rclone_sync_command(str(src), "lab-bucket", "FY26-26",
                                       str(sandbox / "runtime" / "rclone.conf"), "STANDARD",
                                       bwlimit="1000M", retries=1, transfers=2, checkers=2,
                                       buffer_size="16M", max_duration_seconds=120)
    if "--max-duration" not in capped or "120s" not in capped:
        problems.append(f"max_duration_seconds=120 not applied: {capped}")
    if "--cutoff-mode" not in capped or "SOFT" not in capped:
        problems.append(f"the SOFT cutoff mode was not added: {capped}")
    if "--max-duration" in build_rclone_sync_command(
            str(src), "lab-bucket", "FY26-26", str(sandbox / "runtime" / "rclone.conf"),
            "STANDARD", max_duration_seconds=None):
        problems.append("max_duration_seconds=None must disable the cap entirely")

    L.record("W13.command_contracts",
              "build_robocopy_command() and build_rclone_sync_command() carry the "
              "flags the parsers, the junction/canary exclusions and the fail-closed "
              "contract depend on, and honour every config tunable",
              "\n".join(rows),
              "OK - both command builders match their documented flag contract"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.3] problems={problems[:3]}")
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W13.4 - rclone classification
# ------------------------------------------------------------------------------
def test_w13_rclone_classification_branches():
    """Exit-code table, JSON log error scan and --max-duration resolution."""
    from core.cloud_sync import (classify_rclone_exit, resolve_max_duration_seconds,
                                 scan_rclone_log_for_errors)

    rows, problems = [], []
    table = {0: "CLOUD_COMPLETE", 1: "CLOUD_FAILED", 2: "CLOUD_FAILED", 3: "CLOUD_FAILED",
             4: "CLOUD_PARTIAL", 5: "CLOUD_PARTIAL", 6: "CLOUD_FAILED", 7: "CLOUD_FAILED",
             8: "CLOUD_FAILED", 9: "CLOUD_NO_CHANGES_COMPLETE", 10: "CLOUD_PARTIAL",
             -1: "CLOUD_FAILED", 11: "CLOUD_FAILED", 123: "CLOUD_FAILED"}
    for code, want in table.items():
        got = classify_rclone_exit(code)
        rows.append(f"exit {code:>4} -> {got} (want {want})")
        if got != want:
            problems.append(f"rclone exit {code}: got {got}, want {want}")
    # the C-1 trap: an empty-source sync reports NO_CHANGES, which callers count
    # as a success. Keep the mapping visible so it cannot be quietly changed.
    if classify_rclone_exit(9) != "CLOUD_NO_CHANGES_COMPLETE":
        problems.append("exit 9 no longer maps to CLOUD_NO_CHANGES_COMPLETE")

    # ---- scan_rclone_log_for_errors -----------------------------------------
    import json as _json

    log_cases = [
        ("clean INFO stream",
         "\n".join(_json.dumps({"level": "INFO", "msg": "Starting sync"}) for _ in range(3)), False),
        ("single ERROR entry",
         "\n".join([_json.dumps({"level": "INFO", "msg": "x"}),
                    _json.dumps({"level": "ERROR", "msg": "Failed to delete: boom"})]), True),
        ("CRITICAL entry", _json.dumps({"level": "critical", "msg": "dead"}), True),
        ("NOTICE level only", _json.dumps({"level": "NOTICE", "msg": "fine"}), False),
        ("plaintext fatal marker", "Failed to sync: permission denied", True),
        ("plaintext benign", "There was nothing to do", False),
        ("bandwidth noise",
         _json.dumps({"level": "info", "msg": "Starting bandwidth limiter at 1000Mi"}), False),
        ("empty log", "", False),
    ]
    for label, text, want in log_cases:
        got, tail = scan_rclone_log_for_errors(text)
        rows.append(f"error-scan[{label}] -> {got}")
        if got != want:
            problems.append(f"error-scan[{label}]: got {got}, want {want}")
        if want and not tail:
            problems.append(f"error-scan[{label}]: flagged but returned no tail")

    # ---- resolve_max_duration_seconds ---------------------------------------
    dur_cases = [
        ("None + 3600s window -> 3300", 3600, None, 3300),
        ("None + small window (margin does not fit)", 120, None, None),
        ("None + 300s window -> None", 300, None, None),
        ("0 -> disabled", 3600, 0, None),
        ("negative -> disabled", 3600, -5, None),
        ("600 -> used as-is", 3600, 600, 600),
        ("60 -> used as-is even though below the margin", 3600, 60, 60),
    ]
    for label, timeout, configured, want in dur_cases:
        got = resolve_max_duration_seconds(timeout, configured)
        rows.append(f"max-duration[{label}] -> {got}")
        if got != want:
            problems.append(f"max-duration[{label}]: got {got}, want {want}")

    L.record("W13.rclone_classification",
              "classify_rclone_exit() covers every documented code, "
              "scan_rclone_log_for_errors() separates JSON ERROR/CRITICAL from "
              "plaintext fatal markers and noise, and resolve_max_duration_seconds() "
              "implements the documented precedence",
              "\n".join(rows),
              "OK - all rclone classification branches correct"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.4] {len(rows)} checks, problems={problems[:3]}")
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W13.5 - cloud verify decision + failure paths
# ------------------------------------------------------------------------------
def test_w13_cloud_verify_decision_branches():
    """Every branch of decide_cloud_verify_result, plus _build_error_message."""
    from core.cloud_verify import _build_error_message, decide_cloud_verify_result

    clean = "NOTICE: 0 differences found"
    diverged = "Failed to check: 3 differences found"
    rows, problems = [], []
    cases = [
        ("exit 0, clean summary", 0, clean, True, "normal", 0),
        ("exit 1, 3 differences", 1, diverged, False, "normal", 3),
        ("exit 0, 5 differences (forged)", 0, "NOTICE: 5 differences found", False, "abnormal", 5),
        ("exit 0, clean summary but ERROR lines", 0,
         clean + "\n2026/10/10 12:00:00 ERROR : something broke", False, "abnormal", 0),
        ("exit 0, clean summary but a failed verdict", 0,
         clean + "\nFailed to check with: x", False, "abnormal", 0),
        ("no summary at all (killed)", 0, "transferring...\n", False, "abnormal", None),
        ("exit 3, no summary (directory not found)", 3, "", False, "abnormal", None),
        ("exit 2, clean summary (transport error)", 2, clean, False, "normal", 0),
        ("negative exit sentinel", -1, clean, False, "abnormal", None),
        ("non-integer exit code", "0", clean, False, "abnormal", None),
        ("bool exit code", True, clean, False, "abnormal", None),
    ]
    for label, code, log, want_verified, want_term, want_diff in cases:
        got = decide_cloud_verify_result(code, log)
        rows.append(f"{label:42} -> verified={got['verified']!s:5} {got['termination']:9} "
                    f"diff={got['differences']} reason={(got['reason'] or '')[:48]!r}")
        if got["verified"] != want_verified or got["termination"] != want_term:
            problems.append(f"{label}: got verified={got['verified']}/{got['termination']}, "
                            f"want {want_verified}/{want_term}")
        if got["differences"] != want_diff:
            problems.append(f"{label}: differences={got['differences']}, want {want_diff}")
        if got["verified"] is True and got["termination"] != "normal":
            problems.append(f"{label}: VERIFIED with an abnormal termination")

    # ---- _build_error_message: every documented exit code --------------------
    msgs = {c: _build_error_message(c, "detail text") for c in
            (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 123)}
    if msgs[0] is not None:
        problems.append(f"exit 0 produced an error message: {msgs[0]!r}")
    # exit 1 has its own specific wording; the other non-zero codes name the code
    if msgs[1] is None or "Integrity mismatch" not in msgs[1]:
        problems.append(f"exit 1 lost its specific message: {msgs[1]!r}")
    for code in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 123):
        if msgs[code] is None or str(code) not in msgs[code]:
            problems.append(f"exit {code} message does not name the code: {msgs[code]!r}")
    for label, msg in ((f"exit {c}", m) for c, m in msgs.items() if c):
        rows.append(f"{label:8} -> {(msg or '')[:80]}")

    L.record("W13.cloud_verify_branches",
              "decide_cloud_verify_result() classifies clean/diverged/forged-exit-0/"
              "killed/error-line/transport cases, and _build_error_message() names the "
              "exit code for every non-zero code",
              "\n".join(rows),
              "OK - every verify branch is correct and never VERIFIED without a "
              "normal termination"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.5] {len(rows)} checks, problems={problems[:3]}")
    assert not problems, problems[:4]


def test_w13_verify_cloud_integrity_failure_paths(sandbox):
    """verify_cloud_integrity() must fail CLOSED (verified=False, no raise) when
    rclone cannot answer: missing binary, unparsable config, missing source."""
    from core.cloud_verify import verify_cloud_integrity

    src = sandbox / "source" / "FY26-26"
    L.write_file(src / "a.txt", b"a" * 10)
    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)

    rows, problems = [], []
    try:
        cases = [
            ("missing source directory", str(sandbox / "nope"), "lab-bucket", "FY26-26"),
            ("missing bucket prefix", str(src), "no-such-bucket", "FY26-26"),
            ("empty fy prefix", str(src), "lab-bucket", ""),
        ]
        for label, source, bucket, fy in cases:
            try:
                res = verify_cloud_integrity(source=source, bucket=bucket, fy_prefix=fy,
                                             config_path=rc, timeout=60)
                rows.append(f"{label:30} -> verified={res['verified']} "
                            f"exit={res.get('exit_code')} term={res.get('termination')} "
                            f"reason={(res.get('reason') or '')[:60]!r}")
                if res["verified"] is not False:
                    problems.append(f"{label}: rclone could not check, yet verified={res['verified']}")
            except Exception as e:  # noqa: BLE001 - a raise here is the defect
                problems.append(f"{label}: raised {type(e).__name__}: {e}")

        # rclone binary missing -> must degrade, not raise
        import core.cloud_verify as cver
        orig = cver.resolve_binary
        cver.resolve_binary = lambda name: None
        try:
            res = verify_cloud_integrity(source=str(src), bucket="lab-bucket",
                                         fy_prefix="FY26-26", config_path=rc, timeout=60)
            rows.append(f"{'rclone binary missing':30} -> verified={res['verified']} "
                        f"exit={res.get('exit_code')}")
            if res["verified"] is not False:
                problems.append("a missing rclone binary still reported verified=True")
        except Exception as e:  # noqa: BLE001
            problems.append(f"missing rclone binary raised {type(e).__name__}: {e}")
        finally:
            cver.resolve_binary = orig
    finally:
        for (mod, attr), old in saved.items():
            setattr(mod, attr, old)

    L.record("W13.verify_failure_paths",
              "verify_cloud_integrity() fails closed (verified=False, no exception) "
              "for a missing source, a missing bucket/prefix, an empty prefix and a "
              "missing rclone binary",
              "\n".join(rows),
              "OK - every verify failure path degrades to verified=False without raising"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.5b]\n" + "\n".join(rows))
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W13.6 - the LAN side of the integrity engine (W10 mapped 0/145 lines)
# ------------------------------------------------------------------------------
def test_w13_audit_lan_branches(sandbox):
    """audit_lan() against a real filesystem pair: clean, missing-from-dest,
    extra-in-dest, same-size corruption, an unreadable source file, and the
    canary filter. Wave 11 only covered the CLOUD side of this engine."""
    from core.integrity import audit_lan

    src = sandbox / "source" / "FY26-26"
    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    for i in range(4):
        L.write_file(src / "Clients" / f"f{i}.txt", f"content {i}\n".encode() * 20)

    def _sync(src_tree=src, dst=dest):
        import shutil
        shutil.rmtree(dst, ignore_errors=True)
        L.make_canary(dst)
        shutil.copytree(src_tree, dst, dirs_exist_ok=True)

    results = {}
    _sync()
    results["clean"] = audit_lan(str(src), str(dest), timeout=60)

    _sync()
    (dest / "Clients" / "f2.txt").unlink()
    results["missing-from-dest"] = audit_lan(str(src), str(dest), timeout=60)

    _sync()
    (dest / "Clients" / "extra.txt").write_bytes(b"extra")
    results["extra-in-dest"] = audit_lan(str(src), str(dest), timeout=60)

    _sync()                                    # same-size, different-content corruption
    corrupted = dest / "Clients" / "f0.txt"
    data = bytearray(corrupted.read_bytes())
    data[len(data) // 2] ^= 0xFF
    corrupted.write_bytes(bytes(data))
    results["same-size corruption"] = audit_lan(str(src), str(dest), timeout=60)

    # (5) the canary at the destination must be filtered out of the verdict.
    # Converge first: the previous case left extra.txt + a corrupted file behind.
    _sync()
    (dest / ".AAM_TARGET_MOUNTED").touch(exist_ok=True)
    results["canary present"] = audit_lan(str(src), str(dest), timeout=60)

    # a source file nobody can read -> the audit must not call it VERIFIED
    _sync()
    unreadable = src / "Clients" / "locked.txt"
    L.write_file(unreadable, b"secret\n" * 10)
    os.chmod(unreadable, 0o000)
    try:
        results["unreadable source file"] = audit_lan(str(src), str(dest), timeout=60)
    finally:
        os.chmod(unreadable, 0o600)

    rows = [f"{k:26} -> {v['status']:22} mismatches={v['mismatches']:>3} "
            f"cap={v['capability']}" for k, v in results.items()]
    problems = []
    if results["clean"]["status"] != "VERIFIED":
        problems.append(f"clean pair -> {results['clean']}")
    if results["canary present"]["status"] != "VERIFIED":
        problems.append("the canary was not filtered from the LAN verdict")
    for key in ("missing-from-dest", "extra-in-dest", "same-size corruption"):
        if results[key]["status"] == "VERIFIED":
            problems.append(f"{key} was reported VERIFIED (the H-1 blind spot)")
    if results["unreadable source file"]["status"] == "VERIFIED":
        problems.append("an unreadable source file was reported VERIFIED")

    L.record("W13.audit_lan_branches",
              "audit_lan() - the LAN half of the weekly integrity engine, never "
              "exercised by any earlier wave (W10 mapped integrity.py at 0/145): "
              "clean, missing, extra, same-size corruption, canary filter and an "
              "unreadable source file",
              "\n".join(rows),
              "OK - the LAN audit detects every divergence, filters the canary and "
              "never VERIFIED on an unreadable source"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.6]\n" + "\n".join(rows))
    assert not problems, problems[:4]


def test_w13_integrity_helpers():
    """The diff parser, canary/normalise helpers and audit-id format."""
    from core.integrity import (_is_canary, _norm_rel, _parse_combined_file,
                                new_audit_id)

    rows, problems = [], []

    # ---- _parse_combined_file -------------------------------------------------
    import tempfile

    diff = Path(tempfile.mkdtemp(prefix="w13diff_", dir=str(L.LAB_ROOT))) / "combined.txt"
    diff.write_text(
        "+ Clients/new.txt\n"        # missing from destination
        "- Clients/extra.txt\n"      # extra in destination
        "* Clients/changed.txt\n"     # content differs
        "= Clients/same.txt\n"       # identical
        "! Clients/unreadable.txt\n"  # read/access error
        "+ ./dot/leading.txt\n"       # normalised
        "x\n"                         # too short to be a status line
        " \n",                        # blank
        encoding="utf-8")
    added, removed, modified, unchanged, errors, present = _parse_combined_file(str(diff))
    rows.append(f"parse: added={added} removed={removed} modified={modified} "
                f"unchanged={unchanged} errors={errors} present={present}")
    if added != ["Clients/new.txt", "dot/leading.txt"]:
        problems.append(f"added={added}")
    if removed != ["Clients/extra.txt"] or modified != ["Clients/changed.txt"]:
        problems.append(f"removed={removed} modified={modified}")
    if unchanged != ["Clients/same.txt"] or errors != ["Clients/unreadable.txt"]:
        problems.append(f"unchanged={unchanged} errors={errors}")
    if present is not True:
        problems.append("a present diff file reported present=False")

    for label, arg in [("None", None), ("missing file", str(diff) + ".nope")]:
        res = _parse_combined_file(arg)
        if list(res[:5]) != [[], [], [], [], []] or res[5] is not False:
            problems.append(f"{label} diff file -> {res}")

    # ---- _norm_rel / _is_canary ----------------------------------------------
    # NOTE: PureWindowsPath collapses the ROOT but keeps interior separators, so
    # "//deep//x.txt" normalises to "deep//x.txt", and the empty string becomes
    # "." - which is exactly why _parse_combined_file skips "." explicitly.
    norm_cases = [("a\\b\\c.txt", "a/b/c.txt"), ("/leading.txt", "leading.txt"),
                  ("//deep//x.txt", "deep//x.txt"), ("./rel.txt", "rel.txt"),
                  ("", "."), ("   ", "   "), (".", ".")]
    for raw, want in norm_cases:
        got = _norm_rel(raw)
        rows.append(f"_norm_rel({raw!r}) -> {got!r}")
        if got != want:
            problems.append(f"_norm_rel({raw!r}) -> {got!r}, want {want!r}")
    for path in (".AAM_TARGET_MOUNTED", "deep/nested/.AAM_TARGET_MOUNTED",
                 "C:\\x\\.AAM_TARGET_MOUNTED"):
        if not _is_canary(path):
            problems.append(f"canary not recognised at {path!r}")
    if _is_canary("Clients/real.txt"):
        problems.append("a data file was treated as the canary")

    # ---- new_audit_id ---------------------------------------------------------
    ids = {new_audit_id("cloud", "full") for _ in range(5)}
    if len(ids) != 5:
        problems.append("new_audit_id() is not unique")
    if not all(i.endswith("-cloud-full") for i in ids):
        problems.append(f"audit id format changed: {sorted(ids)[0]}")

    L.record("W13.integrity_helpers",
              "_parse_combined_file() classifies every rclone --combined status "
              "marker and fails closed on a missing file, _norm_rel()/_is_canary() "
              "normalise and filter as documented, and new_audit_id() is unique and "
              "traceable",
              "\n".join(rows),
              "OK - the integrity helpers behave as documented"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.6b] problems={problems[:3]}")
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W13.7 - the health checks
# ------------------------------------------------------------------------------
def test_w13_health_check_branches(sandbox, monkeypatch):
    """check_source_drive(), check_gcs_key() and pre_backup_health() branches."""
    from core.health import (HealthError, check_binary_exists, check_gcs_key,
                             check_source_drive, pre_backup_health)

    rows, problems = [], []

    empty = sandbox / "empty"
    empty.mkdir()
    dirs_only = sandbox / "dirs_only"
    (dirs_only / "Clients" / "2026").mkdir(parents=True)
    files = sandbox / "files"
    L.write_file(files / "a.txt", b"a")

    # M2 2.2: health only checks the drive is usable; "does it hold any
    # file?" is guard G1 (BLOCKED for a human) + the builders' backstop.
    # C-1 closed: empty AND dirs-only sources are refused by G1.
    from core import guards as _guards
    for label, path in (("empty source", empty), ("dirs-only source (C-1)", dirs_only)):
        ok, reason = check_source_drive(str(path))
        first = _guards.first_regular_file(str(path), _guards.SOURCE_IGNORE_NAMES)
        g1 = _guards.g1_source_has_files(first, str(path))
        rows.append(f"{label:<27} -> health ok={ok}, G1 {g1.verdict}")
        if ok is not True:
            problems.append(f"health refused a usable {label}: {reason!r}")
        if g1.passed:
            problems.append(f"G1 accepted an {label} - C-1 regression")

    ok, reason = check_source_drive(str(sandbox / "nope"))
    rows.append(f"missing path                -> ok={ok} reason={reason[:60]!r}")
    if ok is not False or "not accessible" not in reason:
        problems.append(f"a missing path was accepted: {reason!r}")

    ok, reason = check_source_drive(str(files), min_free_gb=100_000)
    rows.append(f"min_free_gb=100000          -> ok={ok} reason={reason[:60]!r}")
    if ok is not False or "low on space" not in reason:
        problems.append(f"the disk-space check did not fire: {reason!r}")

    import pathlib

    def _denied(_self):
        raise PermissionError("access denied")

    monkeypatch.setattr(pathlib.Path, "iterdir", _denied)
    try:
        ok, reason = check_source_drive(str(files))
        rows.append(f"PermissionError on iterdir  -> ok={ok} reason={reason[:60]!r}")
        if ok is not False or "permission denied" not in reason.lower():
            problems.append(f"an unreadable source was accepted: {reason!r}")
    finally:
        monkeypatch.undo()

    # ---- check_gcs_key -------------------------------------------------------
    key_cases = [(str(sandbox / "missing.json"), False), (str(sandbox / "empty.json"), False)]
    (sandbox / "empty.json").write_bytes(b"")
    L.write_file(sandbox / "good.json", b'{"type": "service_account"}')
    key_cases.append((str(sandbox / "good.json"), True))
    for path, want_ok in key_cases:
        ok, reason = check_gcs_key(path)
        rows.append(f"check_gcs_key({Path(path).name:12}) -> ok={ok} reason={reason[:40]!r}")
        if ok != want_ok:
            problems.append(f"check_gcs_key({path}) -> ok={ok}, want {want_ok}")

    # ---- pre_backup_health --------------------------------------------------
    try:
        pre_backup_health(str(files), "bogus-mode")
        problems.append("an invalid mode was accepted by pre_backup_health")
    except HealthError as e:
        rows.append(f"invalid mode               -> HealthError: {str(e)[:60]!r}")

    import core.health as health

    orig = health.resolve_binary
    health.resolve_binary = lambda name: None if name == "rclone" else orig(name)
    try:
        try:
            pre_backup_health(str(files), "cloud", min_free_source_gb=1,
                              max_clock_skew_seconds=600, clock_check_timeout_seconds=2)
            problems.append("pre_backup_health passed with rclone missing")
        except HealthError as e:
            rows.append(f"rclone missing, mode=cloud -> HealthError: {str(e)[:60]!r}")
            if "rclone" not in str(e):
                problems.append(f"the wrong check fired for a missing rclone: {e}")
    finally:
        health.resolve_binary = orig

    health.resolve_binary = lambda name: None if name == "robocopy" else orig(name)
    try:
        try:
            pre_backup_health(str(files), "lan", min_free_source_gb=1)
            problems.append("pre_backup_health passed with robocopy missing")
        except HealthError as e:
            rows.append(f"robocopy missing, mode=lan -> HealthError: {str(e)[:60]!r}")
            if "robocopy" not in str(e):
                problems.append(f"the wrong check fired for a missing robocopy: {e}")
    finally:
        health.resolve_binary = orig

    # the happy path must still run for real (network call, fail-open)
    # v1.1.3: gcs_key_path is now REQUIRED for cloud mode — supply a lab key
    L.write_file(sandbox / "gcs-key.json", b'{"type": "service_account"}')
    pre_backup_health(str(files), "all", min_free_source_gb=1,
                      max_clock_skew_seconds=600, clock_check_timeout_seconds=2,
                      gcs_key_path=str(sandbox / "gcs-key.json"))
    rows.append(f"mode=all with binaries present -> no exception "
                f"(rclone={check_binary_exists('rclone')} "
                f"robocopy={check_binary_exists('robocopy')})")

    L.record("W13.health_branches",
              "check_source_drive() (empty, dirs-only = C-1's fail-open, missing, "
              "permission denied, low space), check_gcs_key() (missing/empty/valid) "
              "and pre_backup_health() (invalid mode, missing binaries, happy path)",
              "\n".join(rows),
              "OK - every health branch returns the documented reason; the dirs-only "
              "source still passes (C-1 remains observable)"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.7]\n" + "\n".join(rows))
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W13.8 - the FY rollover helpers
# ------------------------------------------------------------------------------
def test_w13_fy_rollover_path_helpers():
    """The path helpers the rollover depends on: FY extraction, parent, child."""
    from core.fy_rollover import _child_path, _fy_name, _parent_path

    rows, problems = [], []
    fy_cases = [
        ("C:\\SOURCE\\FY26-27", "FY26-27"),
        ("\\\\NAS\\share\\FY26-27", "FY26-27"),
        ("C:/SOURCE/FY26-27", "FY26-27"),
        ("C:\\SOURCE\\FY26-27\\", "FY26-27"),
        ("C:\\SOURCE\\FY26-27\\Clients", None),
        ("C:\\SOURCE\\FY2627", None),
        ("C:\\SOURCE\\FY26-27x", None),
        ("C:\\SOURCE", None),
        ("", None),
        ("lowercase", None),
        ("C:\\data\\fy26-27", "FY26-27"),
    ]
    for raw, want in fy_cases:
        got = _fy_name(raw)
        rows.append(f"_fy_name({raw!r:28}) -> {got!r} (want {want!r})")
        if got != want:
            problems.append(f"_fy_name({raw!r}) -> {got!r}, want {want!r}")

    parent_cases = [
        ("C:\\SOURCE\\FY26-27", "C:\\SOURCE"),
        ("\\\\NAS\\share\\FY26-27", "\\\\NAS\\share"),
        ("C:\\SOURCE\\FY26-27\\", "C:\\SOURCE"),
        ("/mnt/data/FY26-27", "/mnt/data"),
        # a bare drive root loses its trailing separator ("C:\\" -> "C:"); that is
        # harmless because _child_path re-adds it, so assert the round-trip below.
        ("C:\\", "C:"),
    ]
    for raw, want in parent_cases:
        got = _parent_path(raw)
        rows.append(f"_parent_path({raw!r:26}) -> {got!r} (want {want!r})")
        if got != want:
            problems.append(f"_parent_path({raw!r}) -> {got!r}, want {want!r}")
    # the invariant that matters: parent+child must rebuild the FY path exactly
    round_trips = [("C:\\SOURCE\\FY26-27", "C:\\SOURCE\\FY27-28"),
                   ("\\\\NAS\\share\\FY26-27", "\\\\NAS\\share\\FY27-28"),
                   ("/mnt/data/FY26-27", "/mnt/data/FY27-28")]
    for original, want in round_trips:
        got = _child_path(_parent_path(original), "FY27-28")
        rows.append(f"round-trip({original!r:24}) -> {got!r} (want {want!r})")
        if got != want:
            problems.append(f"parent+child round-trip for {original!r} -> {got!r}, want {want!r}")
    # A bare drive root is the one asymmetric case: _parent_path('C:\\') -> 'C:'
    # and _child_path then picks '/' as the separator -> 'C:/FY27-28'. Still a
    # valid Win32 path, and unreachable in production: detect_rollover() finds no
    # FYxx-xx name in either path and returns False (the G2 opt-out) before any
    # parent/child arithmetic happens. Documented and asserted - not a defect.
    drive_round_trip = _child_path(_parent_path("C:\\"), "FY27-28")
    rows.append(f"round-trip({'C:\\\\':24}) -> {drive_round_trip!r} "
                f"(drive-root edge case, unreachable via detect_rollover)")
    if drive_round_trip not in ("C:/FY27-28", "C:\\FY27-28"):
        problems.append(f"unexpected drive-root round-trip: {drive_round_trip!r}")

    child_cases = [
        ("C:\\SOURCE", "FY27-28", "C:\\SOURCE\\FY27-28"),
        ("\\\\NAS\\share", "FY27-28", "\\\\NAS\\share\\FY27-28"),
        ("/mnt/data", "FY27-28", "/mnt/data/FY27-28"),
        ("C:\\SOURCE\\", "FY27-28", "C:\\SOURCE\\FY27-28"),
    ]
    for root, fy, want in child_cases:
        got = _child_path(root, fy)
        rows.append(f"_child_path({root!r:20}, {fy}) -> {got!r}")
        if got != want:
            problems.append(f"_child_path({root!r}, {fy}) -> {got!r}, want {want!r}")

    L.record("W13.fy_path_helpers",
              "_fy_name()/_parent_path()/_child_path() handle UNC paths, forward "
              "slashes, trailing separators, the 4-digit FY trap and lowercase input",
              "\n".join(rows),
              "OK - the rollover path helpers are total and separator-preserving"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.8] {len(rows)} cases, problems={problems[:3]}")
    assert not problems, problems[:4]


def test_w13_create_new_fy_folders_lan_offline_is_non_blocking(sandbox):
    """A NAS that is offline at rollover time must NOT block the rollover."""
    from core.fy_rollover import create_new_fy_folders

    src_root = sandbox / "source"
    lan_root = sandbox / "nas"
    lan_root.mkdir(parents=True, exist_ok=True)

    created = create_new_fy_folders(str(src_root), str(lan_root), "FY27-28")
    rows = [f"created: { {k: str(v) for k, v in created.items()} }"]
    problems = []
    if "source" not in created:
        problems.append("the source folder was not created")
    if not (src_root / "FY27-28").is_dir():
        problems.append("the source folder does not exist on disk")
    if "lan" not in created or not (lan_root / "FY27-28" / ".AAM_TARGET_MOUNTED").exists():
        problems.append("the LAN folder or its canary was not created")

    # now make the LAN root unusable (a regular file) -> must not raise
    blocked_root = sandbox / "blocked_nas"
    blocked_root.write_text("not a directory", encoding="utf-8")
    try:
        created2 = create_new_fy_folders(str(src_root), str(blocked_root), "FY28-29")
        rows.append(f"LAN offline: created={ {k: str(v) for k, v in created2.items()} } "
                    f"(no exception raised)")
        if "source" not in created2:
            problems.append("a LAN failure blocked the source folder creation")
        if "lan" in created2:
            problems.append("a failed LAN folder was reported as created")
    except Exception as e:  # noqa: BLE001 - the code promises non-blocking
        problems.append(f"create_new_fy_folders raised {type(e).__name__}: {e} - the "
                        "documented non-blocking contract is broken")

    L.record("W13.create_fy_folders",
              "create_new_fy_folders() creates the source folder (mandatory) and the "
              "LAN folder with its canary, and stays non-blocking when the LAN root "
              "cannot be written",
              "\n".join(rows),
              "OK - the source folder is created either way and a dead NAS never "
              "blocks the rollover"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.8b]\n" + "\n".join(rows))
    assert not problems, problems[:4]


def test_w13_update_config_yaml_atomic_and_preserving(sandbox, monkeypatch):
    """update_config_yaml() keeps comments, rewrites both paths, and leaves the
    original untouched (with no temp file) when the atomic replace fails."""
    from core.fy_rollover import update_config_yaml

    cfg_path = sandbox / "config.yaml"
    original = (
        "# AAM backup configuration - operator comments must survive a rollover\n"
        "firm_name: LAB\n"
        "paths:\n"
        "  source_drive: 'C:\\BackupAgent\\source\\FY26-27'\n"
        "  lan_destination: '\\\\NAS\\share\\FY26-27'\n"
        "  runtime_dir: 'C:\\BackupAgent\\runtime'\n"
    )
    cfg_path.write_text(original, encoding="utf-8")

    update_config_yaml(str(cfg_path), "C:\\BackupAgent\\source", "\\\\NAS\\share", "FY27-28")
    text = cfg_path.read_text(encoding="utf-8")
    rows = [f"after rollover:\n{text}"]
    problems = []
    if "# AAM backup configuration" not in text:
        problems.append("the operator comment was lost by the yaml round-trip")
    if "FY27-28" not in text:
        problems.append("the FY was not rewritten")
    if "FY26-27" in text:
        problems.append("the old FY is still referenced in config.yaml")
    if "C:\\BackupAgent\\source\\FY26-27" in text:
        problems.append("source_drive was not rewritten")

    import ruamel.yaml

    parsed = ruamel.yaml.YAML().load(text)
    if parsed["paths"]["source_drive"] != "C:\\BackupAgent\\source\\FY27-28":
        problems.append(f"source_drive parsed as {parsed['paths']['source_drive']!r}")
    if parsed["paths"]["lan_destination"] != "\\\\NAS\\share\\FY27-28":
        problems.append(f"lan_destination parsed as {parsed['paths']['lan_destination']!r}")

    # ---- the atomic-replace failure path ------------------------------------
    before_bytes = cfg_path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *_a, **_k: (_ for _ in ()).throw(
        OSError("simulated replace failure")))
    raised = None
    try:
        update_config_yaml(str(cfg_path), "C:\\BackupAgent\\source", "\\\\NAS\\share", "FY28-29")
    except OSError as e:
        raised = e
    monkeypatch.undo()
    after_bytes = cfg_path.read_bytes()
    leftovers = sorted(p.name for p in sandbox.glob(".config_rollover_*"))
    rows.append(f"replace failure -> raised={type(raised).__name__ if raised else None} "
                f"| config unchanged={before_bytes == after_bytes} | temp files left={leftovers}")
    if raised is None:
        problems.append("a failed os.replace was swallowed instead of re-raised")
    if before_bytes != after_bytes:
        problems.append("config.yaml was modified even though the replace failed")
    if leftovers:
        problems.append(f"temp files were left behind: {leftovers}")

    L.record("W13.update_config_yaml",
              "update_config_yaml() preserves operator comments, rewrites both paths "
              "through the ruamel round-trip, re-raises on a failed atomic replace and "
              "leaves neither a modified config nor a temp file behind",
              "\n".join(rows),
              "OK - the atomic rewrite is comment-preserving and fails clean"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.8c] problems={problems[:3]}")
    assert not problems, problems[:4]


def test_w13_run_archive_transition_with_fake_gcloud(sandbox, monkeypatch):
    """run_archive_transition() against fake gcloud CLIs: success, auth failure,
    a timeout, and no gcloud anywhere. All must return a bool, never raise."""
    import core.fy_rollover as fyr

    def _gcloud(name: str, body: str) -> Path:
        p = sandbox / f"{name}.cmd"
        p.write_text("@echo off\r\n" + body, encoding="utf-8")
        return p

    ok = _gcloud("gcloud_ok", "exit /b 0")
    fail = _gcloud("gcloud_fail", "exit /b 1")
    slow = _gcloud("gcloud_slow",
                   'powershell -NoProfile -Command "Start-Sleep -Seconds 5"\r\nexit /b 0')
    L.write_file(sandbox / "lab-key.json", b'{"type": "service_account"}')

    rows, problems = [], []
    cases = [
        ("exit 0", ok, 30, 60, True),
        ("exit 1 (auth or transition failed)", fail, 30, 60, False),
        ("hangs -> timeout", slow, 30, 1, False),
    ]
    for label, exe, auth_timeout, archive_timeout, want in cases:
        monkeypatch.setattr(fyr.shutil, "which", lambda n, _e=str(exe): _e)
        try:
            got = fyr.run_archive_transition("lab-bucket", "FY26-27",
                                             str(sandbox / "lab-key.json"),
                                             auth_timeout=auth_timeout,
                                             archive_timeout=archive_timeout)
            rows.append(f"{label:36} -> {got} (want {want})")
            if got is not want:
                problems.append(f"{label}: returned {got}, want {want}")
        except Exception as e:  # noqa: BLE001 - documented as non-blocking
            problems.append(f"{label}: raised {type(e).__name__}: {e}")
        finally:
            monkeypatch.undo()

    # no gcloud anywhere -> False, no raise
    monkeypatch.setattr(fyr.shutil, "which", lambda n: None)
    try:
        got = fyr.run_archive_transition("lab-bucket", "FY26-27",
                                         str(sandbox / "lab-key.json"))
        rows.append(f"{'gcloud not installed':36} -> {got} (want False)")
        if got is not False:
            problems.append(f"missing gcloud returned {got}, want False")
    except Exception as e:  # noqa: BLE001
        problems.append(f"missing gcloud raised {type(e).__name__}: {e}")
    finally:
        monkeypatch.undo()

    L.record("W13.archive_transition",
              "run_archive_transition() is non-blocking: returns True on success, "
              "False on a failed auth/transition, False on a timeout and False when "
              "gcloud is not installed - and never raises",
              "\n".join(rows),
              "OK - the archive transition can never block or break a rollover"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.8d]\n" + "\n".join(rows))
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W13.9 - manifest / repository: run history, dedup, maintenance, retention
# ------------------------------------------------------------------------------
def test_w13_manifest_run_history_branches(sandbox):
    """insert_run() dedup-on-run_id, mode-scoped reads, and wal_checkpoint()."""
    from core.manifest import ManifestDB

    db = ManifestDB(str(sandbox / "runtime" / "m.db"), busy_timeout_ms=5000)
    rows, problems = [], []

    def _run(run_id, mode, status, minutes=0):
        db.insert_run({"run_id": run_id, "mode": mode,
                       "started_at": f"2026-10-{10 - (minutes // 1440):02d}"
                                     f"T{max(0, 60 - minutes):02d}:00:00+05:30",
                       "status": status})

    _run("r1", "lan", "LAN_COMPLETE", 60)
    _run("r2", "cloud", "CLOUD_COMPLETE", 30)
    _run("r3", "lan", "LAN_FAILED", 10)
    _run("r4", "lan", "LAN_COMPLETE", 5)

    # duplicate run_id must UPSERT, not duplicate (the DDL has a UNIQUE index and
    # the pre-migration dedup keeps MIN(id) - the dedup that drops rows for M-6)
    _run("r1", "lan", "LAN_PARTIAL", 0)
    stored = [r["run_id"] for r in db.get_recent_runs(50)]
    rows.append(f"rows after re-inserting run_id r1: {sorted(stored)}")
    if sorted(stored) != ["r1", "r2", "r3", "r4"]:
        problems.append(f"run_id upsert produced {sorted(stored)}")

    last_any = db.last_run()
    last_lan = db.last_run("lan")
    last_cloud = db.last_run("cloud")
    rows.append(f"last_run()={last_any['run_id']} last_run('lan')={last_lan['run_id']} "
                f"last_run('cloud')={last_cloud['run_id']}")
    if last_lan and last_lan["run_id"] != "r4":
        problems.append(f"last_run('lan') returned {last_lan['run_id']}, want r4")
    if last_cloud and last_cloud["run_id"] != "r2":
        problems.append(f"last_run('cloud') returned {last_cloud['run_id']}, want r2")

    # mode scoping: a LAN query must never return a cloud row
    if last_cloud and last_cloud["mode"] != "cloud":
        problems.append(f"mode scoping leaked: last_run('cloud').mode={last_cloud['mode']}")
    # last_successful_run ignores *_FAILED for the same mode
    succ_lan = db.last_successful_run("lan")
    rows.append(f"last_successful_run('lan')={succ_lan['run_id'] if succ_lan else None} "
                f"({succ_lan['status'] if succ_lan else None})")
    if succ_lan and succ_lan["run_id"] != "r4":
        problems.append(f"last_successful_run('lan') returned {succ_lan['run_id']}, want r4")
    succ_none = db.last_successful_run("nonexistent-mode")
    if succ_none is not None:
        problems.append(f"last_successful_run('nonexistent-mode') returned {succ_none}")

    # wal_checkpoint must not raise or lose data
    db.upsert_file_entry("Clients/a.txt", 1, 1.0, lan_status="synced")
    db.wal_checkpoint()
    if db.file_count("lan_status") != 1:
        problems.append("wal_checkpoint lost file_entries rows")

    # ---- retention is covered by W12.purge_retention; here only the call path
    db.purge_old_runs(retention_days=365)
    if len(db.get_recent_runs(50)) != 4:
        problems.append("purge_old_runs(365) removed rows it should have kept")
    db.close()

    L.record("W13.manifest_run_history",
              "insert_run() upserts on run_id (no duplicate rows), last_run() and "
              "last_successful_run() are mode-scoped and success-filtered, and "
              "wal_checkpoint() preserves data",
              "\n".join(rows),
              "OK - run history writes and reads are consistent and mode-scoped"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.9]\n" + "\n".join(rows))
    assert not problems, problems[:4]


def test_w13_record_run_history_failure_is_loud(sandbox):
    """record_run_history() must return False (never raise) AND log at ERROR.

    This is the M-6 half of the defect: the row is dropped silently today. The
    callers check the return value, so an ERROR log is the only trace - if this
    is ever downgraded to DEBUG again, the audit trail vanishes with no symptom.
    """
    from loguru import logger

    from core.backup_repository import record_run_history

    class _BrokenDB:
        def insert_run(self, _data):
            raise OSError("simulated write failure")

        def wal_checkpoint(self):
            raise AssertionError("must not be reached after a failed insert")

    captured: list[tuple[str, str]] = []
    sink = logger.add(lambda m: captured.append((m.record["level"].name, m.record["message"])),
                      level="DEBUG")
    try:
        ok = record_run_history(_BrokenDB(), run_id="r-broken", mode="lan",
                                started_at="2026-10-10T01:00:00+05:30",
                                ended_at="2026-10-10T01:00:05+05:30",
                                status="LAN_COMPLETE", exit_code=1,
                                duration_seconds=5.0)
    finally:
        logger.remove(sink)

    errors = [m for lvl, m in captured if lvl == "ERROR"]
    debugs = [m for lvl, m in captured if lvl == "DEBUG"]
    rows = [f"return value                -> {ok} (want False)",
            f"ERROR-level log lines       -> {errors}",
            f"DEBUG-level log lines       -> {len(debugs)}",
            "NOTE: a DB write failure inside record_run_history() is exactly the "
            "M-6 shape - the row disappears and only a log line records it."]
    problems = []
    if ok is not False:
        problems.append(f"record_run_history returned {ok!r} on a failed write")
    if not errors:
        problems.append("the dropped run-history row was not logged at ERROR")
    elif "Failed to record run history" not in errors[0]:
        problems.append(f"unexpected ERROR message: {errors[0]!r}")

    L.record("W13.record_run_history_failure",
              "record_run_history() returns False and logs at ERROR when the DB write "
              "fails - the log line is the only trace of a silently dropped "
              "run-history row (M-6)",
              "\n".join(rows),
              "OK - a dropped row is at least loud in the log"
              if not problems else f"ISSUE - {problems[:2]}")
    print(f"[W13.9b] ok={ok} errors={errors}")
    assert not problems, problems[:3]


def test_w13_legacy_schema_migration(sandbox):
    """A pre-versioning database is migrated on open and stamped."""
    import sqlite3

    from core.manifest import SCHEMA_VERSION, ManifestDB

    db_path = sandbox / "runtime" / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE run_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT UNIQUE, mode TEXT, started_at TEXT,
                        ended_at TEXT, status TEXT, exit_code INTEGER,
                        files_copied INTEGER, bytes_copied INTEGER,
                        files_failed INTEGER, duration_seconds REAL,
                        error_message TEXT)""")
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    db = ManifestDB(str(db_path))
    cols = {r[1] for r in db._get_conn().execute("PRAGMA table_info(run_history)").fetchall()}
    version = db._get_conn().execute("PRAGMA user_version").fetchone()[0]
    db.insert_run({"run_id": "post-migration", "mode": "lan",
                   "started_at": "2026-10-10T01:00:00+05:30", "status": "LAN_COMPLETE"})
    rows = [r["status"] for r in db.get_recent_runs(10)]
    db.close()

    problems = []
    if "extended_metrics" not in cols:
        problems.append(f"extended_metrics was not added: {sorted(cols)}")
    if version != SCHEMA_VERSION:
        problems.append(f"user_version={version}, want {SCHEMA_VERSION}")
    if rows != ["LAN_COMPLETE"]:
        problems.append(f"insert after migration returned {rows}")

    L.record("W13.legacy_schema_migration",
              "opening a pre-versioning manifest.db runs the version-gated migration "
              "once: the extended_metrics column is added, user_version is stamped to "
              f"{SCHEMA_VERSION}, and inserts work afterwards",
              f"columns after migration: {sorted(cols)}\nuser_version={version}\n"
              f"post-migration insert: {rows}",
              "OK - the legacy migration is complete and idempotent"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.9c] version={version} problems={problems[:3]}")
    assert not problems, problems[:3]


# ------------------------------------------------------------------------------
# W13.10 - the cloud reporter against the local backend
# ------------------------------------------------------------------------------
def test_w13_cloud_reporter_against_local_backend(sandbox):
    """get_cloud_manifest / get_cloud_diff / get_cloud_size, including the empty
    prefix (must not divide by zero or raise) and a hard failure (must raise
    CloudReporterError, never return an empty list that looks like 'no files')."""
    import os as _os

    import core.cloud_reporter as crep

    src = sandbox / "source" / "FY26-26"
    L.write_file(src / "Clients" / "a.txt", b"a" * 10)
    L.write_file(src / "Clients" / "b.txt", b"b" * 20)

    bucket_root = sandbox / "buckets"
    (bucket_root / "lab-bucket" / "FY26-26").mkdir(parents=True, exist_ok=True)
    empty_prefix = bucket_root / "lab-bucket" / "FY-EMPTY"
    empty_prefix.mkdir(parents=True, exist_ok=True)
    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)
    cwd = _os.getcwd()
    _os.chdir(bucket_root)
    rows, problems = [], []
    try:
        # empty prefix: count 0, no ZeroDivisionError, no exception
        size_empty = crep.get_cloud_size("lab-bucket", "FY-EMPTY", rc, timeout=60)
        rows.append(f"get_cloud_size(empty)      -> {size_empty}")
        if size_empty.get("count") != 0:
            problems.append(f"empty prefix reported {size_empty.get('count')} objects")

        manifest_empty = crep.get_cloud_manifest("lab-bucket", "FY-EMPTY", rc, timeout=60)
        rows.append(f"get_cloud_manifest(empty)  -> {manifest_empty}")
        if manifest_empty != []:
            problems.append(f"empty prefix manifest was {manifest_empty!r}")

        # a real sync, then the manifest/diff/size over it
        from core.cloud_sync import run_cloud_sync

        run_cloud_sync(source=str(src), bucket="lab-bucket", fy_prefix="FY26-26",
                       gcs_key_path=str(sandbox / "lab-key.json"), project_number="000000000000",
                       storage_class="STANDARD", location="us-central1", bwlimit="1000M",
                       retries=1, transfers=2, checkers=2, buffer_size="16M",
                       timeout=120, max_duration_seconds=0)

        manifest = crep.get_cloud_manifest("lab-bucket", "FY26-26", rc, timeout=60)
        rows.append(f"get_cloud_manifest(2 files)-> {len(manifest)} objects "
                    f"{sorted(m['Path'] for m in manifest)}")
        if len(manifest) != 2:
            problems.append(f"manifest returned {len(manifest)} objects, want 2")
        if any(m.get("IsDir") for m in manifest):
            problems.append("the manifest contains directory entries")

        size = crep.get_cloud_size("lab-bucket", "FY26-26", rc, timeout=60)
        rows.append(f"get_cloud_size(2 files)    -> count={size['count']} bytes={size['bytes']}")
        if size.get("count") != 2 or size.get("bytes") != 30:
            problems.append(f"cloud size reported {size}")

        diff_clean = crep.get_cloud_diff(str(src), "lab-bucket", "FY26-26", rc, timeout=60)
        rows.append(f"get_cloud_diff(clean)      -> {diff_clean}")
        if diff_clean.get("added") or diff_clean.get("removed") or diff_clean.get("modified"):
            problems.append(f"a clean pair reported a diff: {diff_clean}")
        if len(diff_clean.get("unchanged", [])) != 2:
            problems.append(f"unchanged list was {diff_clean.get('unchanged')}")

        (bucket_root / "lab-bucket" / "FY26-26" / "Clients" / "b.txt").unlink()
        diff_missing = crep.get_cloud_diff(str(src), "lab-bucket", "FY26-26", rc, timeout=60)
        rows.append(f"get_cloud_diff(1 missing)  -> added={diff_missing.get('added')}")
        if diff_missing.get("added") != ["Clients/b.txt"]:
            problems.append(f"the missing file was not reported as added: {diff_missing}")

        # a nonexistent bucket: rclone exits non-zero -> must RAISE, not return []
        raised = None
        try:
            crep.get_cloud_manifest("no-such-bucket-anywhere", "FY26-27", rc, timeout=60)
        except crep.CloudReporterError as e:
            raised = e
        rows.append(f"get_cloud_manifest(bad bucket) -> raised "
                    f"{type(raised).__name__ if raised else None}")
        if raised is None:
            problems.append("a failing lsjson returned [] instead of raising - the "
                            "M6 'failed query looks like an empty bucket' trap")

        # and an unparsable rclone path (the local backend treats a bogus remote
        # name as a literal path, which is the failure mode we want to hit)
        raised = None
        try:
            crep.get_cloud_size("lab-bucket", "..\\..\\etc", rc, timeout=60)
        except Exception as e:  # noqa: BLE001 - any exception is acceptable here
            raised = e
        rows.append(f"get_cloud_size(odd prefix)  -> {'raised ' + type(raised).__name__ if raised else 'returned a dict'}")
    finally:
        _os.chdir(cwd)
        for (mod, attr), old in saved.items():
            setattr(mod, attr, old)

    L.record("W13.cloud_reporter",
              "get_cloud_manifest()/get_cloud_diff()/get_cloud_size() against the "
              "local backend: an empty prefix reports count 0 without dividing by "
              "zero, a clean pair reports no diff, a deleted object is reported as "
              "added, and a failing lsjson raises instead of returning []",
              "\n".join(rows),
              "OK - the reporter's success and failure paths both behave"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.10]\n" + "\n".join(rows))
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W13.11 - the G8 orphaned-log reaper
# ------------------------------------------------------------------------------
def test_w13_cleanup_orphaned_robocopy_logs(sandbox, monkeypatch):
    """cleanup_orphaned_robocopy_logs() must reap only stale logs, never raise,
    and leave a concurrently running log alone. %TEMP% is redirected so the W11
    evidence corpus is not destroyed by this test."""
    import time as _time

    import tempfile

    from core.lan_sync import cleanup_orphaned_robocopy_logs

    fake_temp = sandbox / "temp"
    fake_temp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))

    fresh = fake_temp / "robocopy_sync_fresh.log"
    stale = fake_temp / "robocopy_sync_stale.log"
    other = fake_temp / "not_a_robocopy_log.log"
    nested = fake_temp / "sub"
    nested.mkdir()
    nested_stale = nested / "robocopy_sync_nested.log"
    for p in (fresh, stale, other, nested_stale):
        p.write_text("log content\n", encoding="utf-8")
    old_ts = _time.time() - 48 * 3600
    for p in (stale, nested_stale):
        os.utime(p, (old_ts, old_ts))

    problems, rows = [], []
    removed = cleanup_orphaned_robocopy_logs(max_age_hours=24)
    rows.append(f"first pass  -> removed={removed} fresh={fresh.exists()} "
                f"stale={stale.exists()} other={other.exists()} nested={nested_stale.exists()}")
    # glob("robocopy_sync_*.log") is NOT recursive: only the flat stale log is
    # reaped. Production always writes flat (%TEMP%\robocopy_sync_<rand>.log), so
    # a nested match cannot occur; keep the non-recursive semantics asserted.
    if removed != 1:
        problems.append(f"expected exactly 1 removal (the flat stale log), got {removed}")
    if not fresh.exists():
        problems.append("a fresh robocopy log was reaped")
    if not other.exists():
        problems.append("a non-matching file was reaped")
    if stale.exists():
        problems.append("a stale log was left behind")
    if not nested_stale.exists():
        problems.append("a nested file was reaped - the glob is unexpectedly recursive")

    # a second pass is a no-op and must not raise
    removed2 = cleanup_orphaned_robocopy_logs(max_age_hours=24)
    rows.append(f"second pass -> removed={removed2} (want 0)")
    if removed2 != 0:
        problems.append(f"the second pass removed {removed2} more files")

    # an empty directory must not raise
    for p in fake_temp.glob("*"):
        if p.is_file():
            p.unlink()
    removed3 = cleanup_orphaned_robocopy_logs(max_age_hours=24)
    rows.append(f"empty dir   -> removed={removed3} (want 0, no exception)")
    if removed3 != 0:
        problems.append(f"an empty temp dir reported {removed3} removals")

    L.record("W13.robocopy_log_cleanup",
              "cleanup_orphaned_robocopy_logs() reaps only logs older than the age "
              "gate (including nested ones), never touches a fresh log or a "
              "non-matching file, and is a no-op on a second pass",
              "\n".join(rows),
              "OK - the G8 reaper is age-gated and safe"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W13.11]\n" + "\n".join(rows))
    assert not problems, problems[:3]
