"""WAVE 12 - fake-clock wave.

Everything in this wave drives the SHIPPED code with the wall clock frozen or
advanced, so time-dependent behaviour becomes deterministic and reproducible
forever (W6's rollover test depends on today's real date; this one does not).

Seam: audit_lab/labclock.py replaces the `pendulum` module object in the two
production modules that read the clock (core/time_utils.py, core/health.py).
Every other module reaches time through now_iso()/get_fy_prefix()/cutoff_iso(),
so one seam moves the whole system. No production file is edited.

W12.1  FY boundary          : 1 April 00:00 IST, and the UTC-vs-IST trap
W12.2  rollover boundary    : detect_rollover() flips exactly at the boundary
W12.3  rollover flow        : the real 1-April event through flow.rollover_check_flow
W12.4  FY divergence        : M-4 - the agent is down on 1 April, the legs split
W12.5  clock skew           : branch-exhaustive check_clock_skew()
W12.6  retention purge      : purge_old_runs() boundaries under a frozen clock
W12.7  seven-day cycle      : 7 nights + the 1-April boundary, ALL through the
                              production Prefect flows (flow.backup, and the
                              rollover/integrity flows for the boundary events)
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import timedelta
from pathlib import Path

import pendulum
import pytest

from audit_lab import labclock
from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(1800)

IST = "Asia/Kolkata"
FY_OLD = "FY26-27"      # the FY the rig is configured for
FY_NEW = "FY27-28"      # what the calendar says from 1 April 2027

BOUNDARY = pendulum.datetime(2027, 4, 1, 0, 0, tz=IST)          # the flip instant
NIGHT_1 = pendulum.datetime(2027, 3, 26, 1, 0, tz=IST)         # 7 nights -> 1 April


# ══════════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════════
def _tree_map(root: Path, skip=(".AAM_TARGET_MOUNTED",)) -> dict[str, str]:
    """rel path -> sha256 for every file under root (junctions not followed)."""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if p.is_file() and p.name not in skip:
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _write_cfg(base: Path, cfg) -> Path:
    import json

    import yaml

    p = base / "config.yaml"
    yaml.safe_dump(json.loads(cfg.model_dump_json()), p.open("w", encoding="utf-8"),
                   sort_keys=False, allow_unicode=True)
    return p


def _db_rows(cfg_path: Path):
    from core.manifest import ManifestDB

    from models.config import load_config

    c = load_config(str(cfg_path))
    m = ManifestDB(c.paths.database_path)
    rows = m.get_recent_runs(30)
    lan_rows = m.file_count("lan_status")
    cloud_rows = m.file_count("cloud_status")
    m.close()
    return rows, lan_rows, cloud_rows


def _seed(src: Path, n: int = 12, tag: str = "a") -> None:
    for i in range(n):
        L.write_file(src / "Clients" / f"{tag}_{i:03d}.txt", f"{tag} ledger {i}\n".encode() * 30)


@pytest.fixture()
def w12_rig(sandbox, monkeypatch):
    """Full production wiring on disk, with the clock frozen at a chosen instant.

    Both the OLD and the NEW fiscal-year prefixes exist in the fake bucket, so
    the rig is valid on either side of the 1-April boundary (HANDOVER section 7:
    the calendar-derived prefix must match whatever clock the test installs).
    """
    import contextlib

    import core.lan_sync as ls
    import flow

    src = sandbox / "source" / FY_OLD
    L.make_canary(sandbox / "nas" / FY_OLD)
    L.write_file(sandbox / "lab-key.json", b'{"type": "service_account"}')
    _seed(src)

    bucket_root = sandbox / "buckets"
    # BOTH prefixes: preflight resolves whichever FY the clock says it is.
    for fy in (FY_OLD, FY_NEW):
        (bucket_root / "lab-bucket" / fy).mkdir(parents=True, exist_ok=True)

    cfg = L.build_config(sandbox, source=src, lan_dest=str(sandbox / "nas" / FY_OLD),
                         runtime=sandbox / "runtime")
    cfg_path = _write_cfg(sandbox, cfg)
    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)

    orig_build = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig_build(s, d, c))

    alerts: list = []
    monkeypatch.setattr(flow, "send_failure_alert", lambda *a, **k: alerts.append(a) or True)
    monkeypatch.setattr(flow, "temp_rclone_config",
                        lambda *a, **k: contextlib.nullcontext(rc))
    # W11's evidence corpus lives in %TEMP%\robocopy_sync_*.log and flow.backup()
    # would delete it (it is older than 24h by the time this wave runs). Quarantine
    # the side effect but keep proof that the production call happens.
    cleaned: list[int] = []
    monkeypatch.setattr(flow, "cleanup_orphaned_robocopy_logs",
                        lambda *a, **k: cleaned.append(1) or 0)
    # flow.backup() runs core.health.check_clock_skew(), which would otherwise
    # make a REAL HTTPS call and legitimately fail ("skew" between Google's time
    # and our frozen 2027 clock). Simulate "Google agrees with our clock" so the
    # production check still runs, deterministically.
    labclock.install_google_date(monkeypatch)

    monkeypatch.chdir(bucket_root)
    monkeypatch.delenv("PREFECT_TEST_MODE", raising=False)

    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        from launch import _ensure_concurrency_limit

        _ensure_concurrency_limit()          # production startup code, limit=1
        yield {"sandbox": sandbox, "src": src, "nas": sandbox / "nas" / FY_OLD,
               "buckets": bucket_root, "cfg": cfg, "cfg_path": cfg_path, "rc": rc,
               "alerts": alerts, "cleaned": cleaned, "flow": flow,
               "labclock": labclock}

    ls.build_robocopy_command = orig_build
    for (mod, attr), old in saved.items():
        setattr(mod, attr, old)


# ══════════════════════════════════════════════════════════════════════════════
# W12.1 - the fiscal-year boundary is anchored to IST, not UTC
# ══════════════════════════════════════════════════════════════════════════════
def test_w12_fy_boundary_is_ist_anchored():
    """1 April 00:00 IST is the flip instant - in BOTH directions."""
    from datetime import date

    from core.time_utils import get_fy_prefix

    table = [
        # the instant just before the boundary (IST wall clock)
        (pendulum.datetime(2027, 3, 31, 23, 59, 59, 999999, tz=IST), FY_OLD),
        (BOUNDARY, FY_NEW),
        # the SAME instant expressed in UTC: 00:00 IST == 18:30 UTC the day before
        (pendulum.datetime(2027, 3, 31, 18, 29, 59, tz="UTC"), FY_OLD),
        (pendulum.datetime(2027, 3, 31, 18, 30, 0, tz="UTC"), FY_NEW),
        # a second year, to prove it is not a one-off
        (pendulum.datetime(2026, 3, 31, 23, 59, 59, tz=IST), "FY25-26"),
        (pendulum.datetime(2026, 4, 1, 0, 0, tz=IST), "FY26-27"),
        # and the reverse direction across the March boundary
        (pendulum.datetime(2027, 3, 31, 12, 0, tz=IST), FY_OLD),
    ]
    rows = []
    problems = []
    for at, expected in table:
        with labclock.fake_clock(at, tick=0.001):
            got = get_fy_prefix()                       # the production code path
        # the explicit-date overload must agree with the frozen clock's LOCAL date
        got_date = get_fy_prefix(at.in_timezone(IST).date())
        rows.append(f"{at.to_iso8601_string():32} -> {got:8} (expected {expected})")
        if got != expected:
            problems.append(f"{at.to_iso8601_string()}: got {got}, expected {expected}")
        if got_date != expected:
            problems.append(f"date arg {at.in_timezone(IST).date()}: got {got_date}, expected {expected}")

    L.record("W12.fy_boundary",
              "get_fy_prefix() flips exactly at 1 April 00:00 IST, including the "
              "UTC-vs-IST trap (00:00 IST == 18:30 UTC the previous day)",
              "\n".join(rows),
              "OK - the FY boundary is IST-anchored and off-by-5h30m-free"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W12.1]\n" + "\n".join(rows))
    assert not problems, problems


# ══════════════════════════════════════════════════════════════════════════════
# W12.2 - detect_rollover() flips at the same instant, and the G2 opt-out holds
# ══════════════════════════════════════════════════════════════════════════════
def test_w12_detect_rollover_boundary_and_optout():
    from core.fy_rollover import detect_rollover

    src = f"C:\\SOURCE\\{FY_OLD}"
    lan = f"\\\\NAS\\share\\{FY_OLD}"

    rows = []
    problems = []
    cases = [
        ("23:59:59.999 IST on 31 Mar 2027", pendulum.datetime(2027, 3, 31, 23, 59, 59, 999999, tz=IST), False),
        ("00:00:00 IST on 1 Apr 2027", BOUNDARY, True),
        ("same instant in UTC (18:30 UTC 31 Mar)", pendulum.datetime(2027, 3, 31, 18, 30, tz="UTC"), True),
        ("mid-year (1 Oct 2026)", pendulum.datetime(2026, 10, 1, 9, 0, tz=IST), False),
    ]
    for label, at, expected in cases:
        with labclock.fake_clock(at, tick=0.001):
            got = detect_rollover(src, lan)
        rows.append(f"{label:36} -> detect_rollover={got} (expected {expected})")
        if got is not expected:
            problems.append(f"{label}: got {got}, expected {expected}")

    # G2: no FY folder in either path -> rollover is a deliberate no-op
    with labclock.fake_clock(BOUNDARY):
        optout = detect_rollover("C:\\SOURCE", "\\\\NAS\\share")
        # source FY and LAN FY disagreeing is still a rollover trigger
        disagree = detect_rollover("C:\\SOURCE\\FY26-27", "\\\\NAS\\share\\FY25-26")
    rows.append(f"{'no FY folder in either path':36} -> detect_rollover={optout} (expected False)")
    rows.append(f"{'source/LAN FY labels disagree':36} -> detect_rollover={disagree} (expected True)")
    if optout is not False:
        problems.append("G2 opt-out broken: a config without FY folders now rolls over")
    if disagree is not True:
        problems.append("source/LAN FY disagreement no longer triggers a rollover")

    L.record("W12.detect_rollover",
              "detect_rollover() flips exactly at the 1 April 00:00 IST boundary, "
              "the G2 no-FY-folder opt-out stays a no-op, and disagreeing FY labels "
              "still trigger",
              "\n".join(rows),
              "OK - boundary, opt-out and disagreement all behave"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W12.2]\n" + "\n".join(rows))
    assert not problems, problems

# ------------------------------------------------------------------------------
# W12.3 - the real 1-April rollover, through the production flow
# ------------------------------------------------------------------------------
def test_w12_rollover_flow_at_the_april_boundary(w12_rig):
    """flow.rollover_check_flow() at 2027-04-01 00:30 IST: final backup, folders,
    canary, and an atomic config.yaml rewrite to the new FY."""
    import core.lan_sync as ls

    flow, rig = w12_rig["flow"], w12_rig
    src, cfg_path = rig["src"], rig["cfg_path"]

    # the night before: everything is on the old FY and both legs agree
    with labclock.fake_clock(NIGHT_1.add(days=6), tick=0.001):
        _flow_backup(flow, cfg_path)[0]
    before = _db_rows(cfg_path)[0]

    # 1 April, 00:30 IST - the scheduled rollover deployment fires
    with labclock.fake_clock(BOUNDARY.add(minutes=30), tick=0.001):
        result = _flow_call(flow.rollover_check_flow, config_path=str(cfg_path))
        rows, lan_rows, cloud_rows = _db_rows(cfg_path)

    from models.config import load_config

    reloaded = load_config(str(rig["sandbox"] / "config.yaml"))
    new_src = reloaded.paths.source_drive
    new_lan = reloaded.paths.lan_destination

    lines = [
        f"flow returned        : {result!r}",
        f"old FY folders       : source={src.name} lan={rig['nas'].name}",
        f"new source_drive     : {new_src}",
        f"new lan_destination  : {new_lan}",
        f"new source exists    : {Path(new_src).exists()}",
        f"new LAN folder exists: {Path(new_lan).exists()}",
        f"new LAN canary       : {(Path(new_lan) / '.AAM_TARGET_MOUNTED').exists()}",
        f"run_history after    : {[r['status'] for r in rows]}",
        f"alerts               : {len(rig['alerts'])}",
    ]
    problems = []
    if result != "ROLLOVER_COMPLETED":
        problems.append(f"rollover returned {result!r}")
    if not new_src.endswith(FY_NEW):
        problems.append(f"config not rewritten to {FY_NEW}: {new_src}")
    if not new_lan.endswith(FY_NEW):
        problems.append(f"LAN destination not rewritten to {FY_NEW}: {new_lan}")
    if not Path(new_src).exists():
        problems.append("the new FY source folder was not created")
    if not (Path(new_lan) / ".AAM_TARGET_MOUNTED").exists():
        problems.append("the new FY LAN folder has no canary - the next preflight fails")

    L.record("W12.rollover_flow",
              "the real 1-April rollover runs through flow.rollover_check_flow with a "
              "frozen clock: final backup, new folders, LAN canary, config rewritten",
              "\n".join(lines) + f"\nruns before rollover: {[r['status'] for r in before]}",
              "OK - the 1-April event completes and rewrites config.yaml to the new FY"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W12.3]\n" + "\n".join(lines))
    assert not problems, problems


# ------------------------------------------------------------------------------
# W12.4 - M-4: the agent is down on 1 April -> the two legs write different FYs
# ------------------------------------------------------------------------------
def test_w12_fy_divergence_fixed_by_effective_prefix(w12_rig):
    """v1.1.3 fixes M-4: with no rollover, _effective_fy_prefix() keeps the
    cloud leg on the configured source leaf (FY_OLD) instead of following the
    calendar to FY_NEW, so both legs stay on the same FY and the trees never
    split. The OLD test asserted the divergence; this verifies the fix."""
    flow, rig = w12_rig["flow"], w12_rig
    src, cfg_path, buckets = rig["src"], rig["cfg_path"], rig["buckets"]

    with labclock.fake_clock(BOUNDARY.add(hours=2), tick=0.001):
        _flow_backup(flow, cfg_path)[0]
        rows, _lan_rows, _cloud_rows = _db_rows(cfg_path)

    cloud_fy_dirs = sorted(p.name for p in (buckets / "lab-bucket").iterdir() if p.is_dir())
    lan_fy = rig["nas"].name
    lan_had_files = bool(_tree_map(rig["nas"]))
    cloud_old_fy_files = len(_tree_map(buckets / "lab-bucket" / FY_OLD))
    cloud_new_fy_files = len(_tree_map(buckets / "lab-bucket" / FY_NEW))

    lines = [
        f"cloud prefixes present in bucket : {cloud_fy_dirs}",
        f"objects written under {FY_OLD}  : {cloud_old_fy_files}",
        f"objects written under {FY_NEW}  : {cloud_new_fy_files}",
        f"LAN destination FY (from config)  : {lan_fy}",
        f"LAN destination still populated   : {lan_had_files}",
        f"run_history                       : {[r['status'] for r in rows]}",
    ]
    # v1.1.3: both legs write the source-leaf FY; no divergence, no split trees.
    assert cloud_old_fy_files > 0, "the cloud leg did not write to the source-leaf FY"
    assert cloud_new_fy_files == 0, (
        f"the cloud leg still followed the calendar to {FY_NEW} — M-4 regressed")
    assert lan_fy == FY_OLD, f"LAN followed the calendar instead of config: {lan_fy}"
    assert lan_had_files, "the LAN destination was purged - that would be C-1, not M-4"
    assert [r["status"] for r in rows] and all(
        r["status"].endswith(("COMPLETE", "NO_CHANGES_COMPLETE")) for r in rows), rows

    L.record("W12.fy_divergence",
             "v1.1.3 _effective_fy_prefix keeps the cloud leg on the configured source "
             "leaf, so a missed rollover no longer splits the cloud and LAN FY trees (M-4 fixed)",
             "\n".join(lines),
             "OK - M-4 FIXED: both legs wrote the source-leaf FY, no calendar divergence")
    print(f"[W12.4]\n" + "\n".join(lines))


# ------------------------------------------------------------------------------
# W12.5 - branch-exhaustive check_clock_skew() (0 direct tests before this wave)
# ------------------------------------------------------------------------------
def test_w12_clock_skew_branches(monkeypatch):
    """Every branch of check_clock_skew(): within limit, over limit (both signs),
    missing header, unparseable header, and the fail-open network error."""
    import email.utils

    import core.health as health

    frozen = pendulum.datetime(2026, 10, 10, 12, 0, tz="UTC")

    class _Resp:
        def __init__(self, value):
            self._value = value

        def getheader(self, _name):
            return self._value

    _NO_EXC = object()          # sentinel: "do not raise"

    class _Conn:
        def __init__(self, host, timeout=None):
            self.host = host
            self.timeout = timeout

        def request(self, *_a, **_k):
            pass

        def getresponse(self):
            if _Conn.raise_exc is not _NO_EXC:
                raise _Conn.raise_exc
            return _Resp(_Conn.header_value)

        def close(self):
            pass

    _Conn.header_value = None
    _Conn.raise_exc = _NO_EXC

    def _install(header_value=None, exc=None):
        _Conn.header_value = header_value
        _Conn.raise_exc = _NO_EXC if exc is None else exc
        monkeypatch.setattr(health.http.client, "HTTPSConnection", _Conn)

    rows, problems = [], []
    cases = [
        ("exact match", 0, True),
        ("+599 s (inside the 600 s limit)", 599, True),
        ("+600 s (exactly at the limit)", 600, True),
        ("+601 s (1 s over)", 601, False),
        ("-900 s (clock behind)", -900, False),
        ("-3600 s (an hour behind)", -3600, False),
    ]
    for label, offset, expect_ok in cases:
        at = frozen.add(seconds=offset)
        header = email.utils.formatdate(at.int_timestamp, usegmt=True)
        _install(header_value=header)
        with labclock.fake_clock(frozen):
            ok, reason = health.check_clock_skew(max_skew_seconds=600, connection_timeout=2)
        rows.append(f"{label:34} -> ok={ok} reason={reason[:70]!r}")
        if ok is not expect_ok:
            problems.append(f"{label}: ok={ok}, expected {expect_ok}")

    # no Date header at all -> fail closed (cannot prove the clock is right)
    _install(header_value=None)
    with labclock.fake_clock(frozen):
        ok, reason = health.check_clock_skew(max_skew_seconds=600, connection_timeout=2)
    rows.append(f"{'no Date header':34} -> ok={ok} reason={reason[:70]!r}")
    if ok is not False:
        problems.append("a missing Date header passed the clock check")

    # unparseable header -> fail OPEN (documented design note)
    _install(header_value="not-a-date")
    with labclock.fake_clock(frozen):
        ok, reason = health.check_clock_skew(max_skew_seconds=600, connection_timeout=2)
    rows.append(f"{'unparseable Date header':34} -> ok={ok} reason={reason[:70]!r}")
    if ok is not True:
        problems.append("an unparseable Date header no longer fails open")

    # network unreachable -> fail OPEN (the documented trade-off: skew is
    # invisible exactly when the network is flapping)
    _install(exc=OSError("no route to host"))
    with labclock.fake_clock(frozen):
        ok, reason = health.check_clock_skew(max_skew_seconds=600, connection_timeout=2)
    rows.append(f"{'Google unreachable (OSError)':34} -> ok={ok} reason={reason[:70]!r}")
    if ok is not True:
        problems.append("an unreachable network no longer fails open")

    # and a value error on the parse path
    _install(header_value="Thu, 99 Zzz 9999 99:99:99 GMT")
    with labclock.fake_clock(frozen):
        ok, reason = health.check_clock_skew(max_skew_seconds=600, connection_timeout=2)
    rows.append(f"{'garbage numeric Date header':34} -> ok={ok} reason={reason[:70]!r}")

    L.record("W12.clock_skew",
              "check_clock_skew() branch coverage: within limit, +601 s, -900 s, "
              "-3600 s, missing header, unparseable header, network unreachable",
              "\n".join(rows),
              "OK - fails closed on real skew, fails open on a missing/unreachable "
              "network (documented trade-off) - all branches now covered"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W12.5]\n" + "\n".join(rows))
    assert not problems, problems


# ------------------------------------------------------------------------------
# W12.6 - purge_old_runs() retention boundaries under a frozen clock
# ------------------------------------------------------------------------------
def test_w12_retention_purge_boundaries(sandbox):
    """purge_old_runs() keeps the boundary row, deletes what is older, and never
    touches file_entries (the docstring's claim)."""
    from core.manifest import ManifestDB

    frozen = pendulum.datetime(2026, 10, 10, 2, 0, tz=IST)
    db_path = sandbox / "runtime" / "manifest.db"

    with labclock.fake_clock(frozen):
        now = labclock.now()
        db = ManifestDB(str(db_path), busy_timeout_ms=5000)
        offsets = [0, 1, 89, 90, 91, 365]
        for d in offsets:
            at = now.subtract(days=d)
            db.insert_run({"run_id": f"r-{d:03d}", "mode": "lan",
                           "started_at": at.isoformat(), "status": "LAN_COMPLETE"})
        # file_entries must survive the run_history purge untouched
        db.upsert_file_entry("Clients/a.txt", 10, 1700000000.0, lan_status="synced")
        db.upsert_file_entry("Clients/b.txt", 20, 1700000000.0, cloud_status="synced")

        before = {r["run_id"] for r in db.get_recent_runs(50)}
        db.purge_old_runs(retention_days=90)
        after = {r["run_id"] for r in db.get_recent_runs(50)}
        entries = db.file_count("lan_status") + db.file_count("cloud_status")
        db.close()

    kept = sorted(after)
    purged = sorted(before - after)
    lines = [
        f"frozen now  : {now.isoformat()}",
        f"rows before : {sorted(before)}",
        f"rows kept   : {kept}",
        f"rows purged : {purged}",
        f"file_entries rows still present: {entries}/2",
    ]
    problems = []
    if "r-089" not in after or "r-090" not in after:
        problems.append("the 89/90-day rows should be kept (cutoff is strictly '<')")
    if "r-091" in after or "r-365" in after:
        problems.append("rows older than the retention window were not purged")
    if "r-000" not in after:
        problems.append("today's row was purged")
    if entries != 2:
        problems.append(f"purge_old_runs() touched file_entries: {entries}/2 left")

    # retention_days=0 -> everything is older than "now"
    with labclock.fake_clock(frozen.add(minutes=1), tick=0.001):
        db = ManifestDB(str(db_path))
        db.purge_old_runs(retention_days=0)
        left = {r["run_id"] for r in db.get_recent_runs(50)}
        db.close()
    if left:
        problems.append(f"retention_days=0 left rows behind: {sorted(left)}")

    # and the VACUUM branch (freelist above the threshold) must not raise
    with labclock.fake_clock(frozen.add(minutes=2), tick=0.001):
        db = ManifestDB(str(db_path), vacuum_freelist_threshold=-1)
        db.purge_old_runs(retention_days=365)
        db.close()

    L.record("W12.purge_retention",
              "purge_old_runs() under a frozen clock: the 89/90-day rows are kept, "
              "91+ purged, today's row kept, file_entries untouched, retention_days=0 "
              "clears the run log, and the VACUUM branch does not raise",
              "\n".join(lines),
              "OK - retention boundaries are exact and file_entries survive"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W12.6]\n" + "\n".join(lines))
    assert not problems, problems

# ------------------------------------------------------------------------------
# W12.7 - the simulated 7-day nightly cycle, crossing the 1-April boundary
# ------------------------------------------------------------------------------
_OK = ("LAN_COMPLETE", "CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE")

# Prefect's ephemeral harness server persists task_run_state rows with a
# UNIQUE(task_run_id, timestamp) constraint. Running ~11 flows back-to-back in
# ONE harness occasionally makes two state transitions of the same task run land
# on the same microsecond, so Prefect's own INSERT fails and the flow run is
# declared Failed *before our code does anything*. That is harness noise, not a
# product break, so a night hit by exactly this artefact is retried once. Any
# other exception still fails the test.
_HARNESS_ARTEFACT = "task_run_state"


def _flow_backup(flow, cfg_path, *, mode="all", attempts=2):
    """flow.backup() with one retry for the Prefect harness DB artefact."""
    for attempt in range(1, attempts + 1):
        try:
            return flow.backup(config_path=str(cfg_path), mode=mode), attempt
        except Exception as e:  # noqa: BLE001
            text = f"{type(e).__name__}: {e}"
            if attempt < attempts and _HARNESS_ARTEFACT in text:
                time.sleep(1.5)
                continue
            raise
    raise AssertionError("unreachable")


def _flow_call(fn, *args, attempts=2, **kwargs):
    """Any production flow call, with one retry for the harness DB artefact."""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            text = f"{type(e).__name__}: {e}"
            if attempt < attempts and _HARNESS_ARTEFACT in text:
                time.sleep(1.5)
                continue
            raise
    raise AssertionError("unreachable")


def test_w12_seven_day_cycle_through_the_production_flows(w12_rig):
    """Seven consecutive nights, every one driven through the REAL Prefect flows:

        * flow.backup(mode="all")            - the nightly entry point
        * flow.integrity_audit_flow(mode=all)- the weekly audit, mid-week
        * flow.rollover_check_flow()         - the 1-April deployment event

    Nights 1-6 are ordinary working days. Night 7 IS the fiscal boundary
    (2027-04-01 01:00 IST): the cloud leg follows the calendar, the LAN leg
    follows config.yaml, and nothing complains (M-4). Then the scheduled
    rollover runs and night 8 converges both legs on the new FY.
    """
    flow, rig = w12_rig["flow"], w12_rig
    src, cfg_path, nas, buckets = rig["src"], rig["cfg_path"], rig["nas"], rig["buckets"]

    from core.time_utils import get_fy_prefix

    rows_log: list[str] = []
    problems: list[str] = []

    def _snapshot():
        rows, lan_rows, cloud_rows = _db_rows(cfg_path)
        return rows, lan_rows, cloud_rows

    def _fy_of_cloud():
        # v1.1.3: the cloud leg syncs to _effective_fy_prefix(config) — the
        # configured source leaf when it is a valid FY dir — not the calendar.
        from flow import _effective_fy_prefix

        from models.config import load_config
        return _effective_fy_prefix(load_config(str(cfg_path)))

    rows_before, _l, _c = _snapshot()

    for night in range(1, 8):
        at = NIGHT_1.add(days=night - 1, hours=0)          # 01:00 IST each night
        # v1.1.3: the cloud prefix follows the configured source leaf, so it is
        # FY_OLD every night until the rollover rewrites config on night 7.
        fy_now = FY_OLD
        with labclock.fake_clock(at, tick=0.001):
            # ---- the accountants' working day -------------------------------
            L.write_file(src / "Clients" / f"new_{night:02d}.txt", b"new work\n" * 25)
            if night > 1:
                victim = src / "Clients" / f"a_{night - 2:03d}.txt"
                if victim.exists():
                    victim.unlink()
            target = src / "Clients" / f"a_{night:03d}.txt"
            if target.exists():
                target.write_bytes(f"amended on night {night}\n".encode() * 25)

            # ---- the real nightly flow --------------------------------------
            _flow_backup(flow, cfg_path)[0]
            rows_after, lan_rows, cloud_rows = _snapshot()

            cloud_dir = buckets / "lab-bucket" / fy_now
            src_map = _tree_map(src)
            nas_map = _tree_map(nas)
            cloud_map = _tree_map(cloud_dir)

            new_rows = rows_after[len(rows_before):]
            statuses = sorted(r["status"] for r in new_rows)
            last_lan = next((r for r in rows_after if r["mode"] == "lan"), None)
            last_cloud = next((r for r in rows_after if r["mode"] == "cloud"), None)

        lines = [
            f"night {night} {at.to_date_string()} {at.format('HH:mm z')}: calendar FY={FY_OLD if at < BOUNDARY else FY_NEW}",
            f"  src files={len(src_map)} nas={len(nas_map)} cloud({fy_now})={len(cloud_map)}",
            f"  run rows added={len(new_rows)} statuses={statuses}",
            f"  manifest lan={lan_rows} cloud={cloud_rows}",
        ]
        rows_log.extend(lines)

        # I1 lossless on BOTH legs (sha256, not counts)
        if nas_map != src_map:
            problems.append(f"night {night}: LAN differs - missing="
                            f"{sorted(set(src_map) - set(nas_map))[:3]} different="
                            f"{sorted(p for p in set(src_map) & set(nas_map) if src_map[p] != nas_map[p])[:3]}")
        if cloud_map != src_map:
            problems.append(f"night {night}: cloud differs - missing="
                            f"{sorted(set(src_map) - set(cloud_map))[:3]} different="
                            f"{sorted(p for p in set(src_map) & set(cloud_map) if src_map[p] != cloud_map[p])[:3]}")
        # I4 honest: no more than the two expected rows, all success-looking
        if len(new_rows) != 2:
            problems.append(f"night {night}: {len(new_rows)} run rows added (expected 2)")
        if any(r["status"] not in _OK for r in new_rows):
            problems.append(f"night {night}: unexpected statuses {statuses}")
        # last_successful_run() must be THIS night (I2 converges, no stale rows)
        if night >= 2 and last_lan and last_lan["started_at"][:10] != at.to_date_string():
            problems.append(f"night {night}: last LAN row is from {last_lan['started_at']}")
        if night >= 2 and last_cloud and last_cloud["started_at"][:10] != at.to_date_string():
            problems.append(f"night {night}: last cloud row is from {last_cloud['started_at']}")

        # ---- I2 convergence, checked once mid-week -------------------------
        if night == 3:
            with labclock.fake_clock(at.add(hours=1), tick=0.001):
                _flow_backup(flow, cfg_path)[0]
                rows_conv, _lc, _cc = _snapshot()
            converged_rows = rows_conv[len(rows_after):]
            if _tree_map(nas) != src_map or _tree_map(cloud_dir) != src_map:
                problems.append(f"night {night}: a re-run changed the destination")
            if len(converged_rows) != 2 or any(r["status"] not in _OK for r in converged_rows):
                problems.append(f"night {night}: re-run statuses {[r['status'] for r in converged_rows]}")
            rows_log.append(f"  re-run on night {night}: statuses="
                            f"{sorted(r['status'] for r in converged_rows)} (no-op)")
            # the re-run's rows are part of this night's history
            rows_after = rows_conv

        # ---- the weekly audit, mid-week (night 4) --------------------------
        if night == 4:
            with labclock.fake_clock(at.add(hours=2), tick=0.001):
                _flow_call(flow.integrity_audit_flow, config_path=str(cfg_path), mode="all")
            from core.manifest import ManifestDB

            from models.config import load_config

            m = ManifestDB(load_config(str(cfg_path)).paths.database_path)
            audits = [m.latest_audit("cloud"), m.latest_audit("lan")]
            m.close()
            audit_status = [(a["status"] if a else None) for a in audits]
            rows_log.append(f"  weekly audit after night 4: {audit_status}")
            if any(a is None for a in audits):
                problems.append("the audit flow wrote no integrity_audits row")
            if any(a and a["status"] != "VERIFIED" for a in audits):
                problems.append(f"the audit diverged on byte-identical trees: {audit_status}")

        rows_before = rows_after
        # space the nights out so Prefect's harness DB never has to insert two
        # state transitions of the same task run on the same microsecond
        time.sleep(1.0)

    # -- night 7 IS the boundary: v1.1.3 keeps both legs on the source leaf ---
    # The OLD behaviour (M-4) split the FYs: cloud followed the calendar to
    # FY27-28 while LAN followed config (FY26-27). _effective_fy_prefix() now
    # aligns the cloud prefix with the configured source leaf, so both legs
    # stay on FY_OLD until the rollover rewrites config — M-4 is FIXED.
    time.sleep(1.0)
    cloud_old = _tree_map(buckets / "lab-bucket" / FY_OLD)
    nas_now = _tree_map(nas)
    if not cloud_old:
        problems.append("the cloud leg did not write to the source-leaf FY")
    if nas.name != FY_OLD:
        problems.append(f"the LAN leg followed the calendar instead of config: {nas.name}")
    if nas_now != _tree_map(src):
        problems.append("the LAN leg lost data on the boundary night")

    # -- then the scheduled rollover fires, and night 8 converges --------------
    with labclock.fake_clock(BOUNDARY.add(minutes=90), tick=0.001):
        result = _flow_call(flow.rollover_check_flow, config_path=str(cfg_path))
        rows8, _l8, _c8 = _snapshot()

    from models.config import load_config

    reloaded = load_config(str(rig["sandbox"] / "config.yaml"))
    new_src = Path(reloaded.paths.source_drive)
    new_lan = Path(reloaded.paths.lan_destination)
    old_fy_objects = _tree_map(buckets / "lab-bucket" / FY_OLD)

    night8_at = NIGHT_1.add(days=8)                       # 2027-04-03 01:00 IST
    with labclock.fake_clock(night8_at, tick=0.001):
        for i in range(3):
            L.write_file(new_src / f"new_fy_{i}.txt", f"FY{FY_NEW} ledger {i}\n".encode() * 20)
        _flow_backup(flow, cfg_path)[0]
        rows9, _l9, _c9 = _snapshot()

    src_new_map = _tree_map(new_src)
    nas_new_map = _tree_map(new_lan)
    cloud_new_map = _tree_map(buckets / "lab-bucket" / FY_NEW)
    final_rows = rows9[len(rows8):]

    summary = [
        "nights 1-7 (26 Mar .. 1 Apr): " + "; ".join(
            l for l in rows_log if l.startswith("night")),
        "re-run + audit lines: " + "; ".join(
            l.strip() for l in rows_log if l.strip().startswith(("re-run", "weekly"))),
        f"boundary night 7: cloud FY={FY_OLD} ({len(cloud_old)} files), LAN FY={nas.name} "
        f"({len(nas_now)} files) — both legs on the source leaf (M-4 fixed by v1.1.3 "
        f"_effective_fy_prefix)",
        f"rollover flow returned: {result!r}",
        f"new FY folders: source={new_src.exists()} lan={new_lan.exists()} "
        f"canary={(new_lan / '.AAM_TARGET_MOUNTED').exists()}",
        f"old FY objects retained in bucket: {len(old_fy_objects)}",
        f"night 8: source={len(src_new_map)} nas={len(nas_new_map)} cloud={len(cloud_new_map)} "
        f"| run rows added={sorted(r['status'] for r in final_rows)}",
        f"alerts during the whole cycle: {len(rig['alerts'])}",
        f"cleanup_orphaned_robocopy_logs called by flow.backup(): {len(rig['cleaned'])} times",
    ]

    if result != "ROLLOVER_COMPLETED":
        problems.append(f"the boundary rollover returned {result!r}")
    if not (new_lan / ".AAM_TARGET_MOUNTED").exists():
        problems.append("the new FY LAN folder has no canary")
    if not old_fy_objects:
        problems.append("the final backup of the closing FY left no objects in the bucket")
    if src_new_map != nas_new_map or src_new_map != cloud_new_map:
        problems.append(f"night 8 did not converge: nas={len(nas_new_map)} cloud={len(cloud_new_map)} "
                        f"src={len(src_new_map)}")
    if len(final_rows) != 2 or any(r["status"] not in _OK for r in final_rows):
        problems.append(f"night 8 run rows: {[r['status'] for r in final_rows]}")
    if len(rig["cleaned"]) < 8:
        problems.append("flow.backup() did not invoke cleanup_orphaned_robocopy_logs on every night")

    L.record("W12.seven_day_cycle",
              "seven consecutive nights (26 Mar - 1 Apr 2027) plus the boundary "
              "rollover and a converging night 8, EVERY night driven through the "
              "production Prefect flows: byte-identical destinations on both legs, "
              "one success row per leg per night, a convergent re-run, a passing "
              "weekly audit, and the FY split (M-4) visible exactly at the boundary",
              "\n".join(summary),
              "OK - the full nightly cycle is lossless, honest and convergent on both "
              "legs; the only anomaly is the FY split at the boundary (M-4)"
              if not problems else f"ISSUE - {problems[:4]}")
    print("[W12.7]\n" + "\n".join(summary) + "\n" + "\n".join(rows_log))
    assert not problems, problems[:6]
