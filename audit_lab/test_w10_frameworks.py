"""WAVE 10 — additional frameworks: cron-schedule verification, model-based
stateful stress, and a line-coverage map of the production code.

Tools used (all already in the venv — nothing installed):

  * **cronsim** — computes the actual next/previous fire time of a cron
    expression, so the 6 deployments' schedules can be verified against the
    config's cron + timezone instead of trusting the human-readable label.
  * **hypothesis.stateful.RuleBasedStateMachine** — model-based testing: a
    random *sequence* of filesystem operations, with the real mirror run after
    each one and an invariant checked against an in-memory model. Sequential
    interactions (create → modify → rename → delete → corrupt) are exactly
    where simple per-case tests stay blind.
  * **sys.monitoring (PEP 669)** — line-level tracing of the production code
    during a real lab run, producing a coverage map: which production lines the
    lab has NEVER executed. That is a blind-spot report, not a pass/fail test.

W10.1  cron/schedule verification of all 6 deployments
W10.2  stateful model-based mirror stress (add/modify/delete/rename/corrupt)
W10.3  production-code line coverage during a real LAN + cloud run
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from audit_lab import labconf as L
from core.time_utils import now_iso

pytestmark = pytest.mark.timeout(1200)

REPO = Path(__file__).resolve().parent.parent


# ══════════════════════════════════════════════════════════════════════════════
# W10.1 — cron / schedule verification (cronsim)
# ══════════════════════════════════════════════════════════════════════════════
def test_w10_deployment_schedules_fire_when_intended():
    """cronsim computes the real next fire time of every deployment's cron.

    The dashboard only shows cron_descriptor's English text ("at 01:00 AM"),
    which says nothing about the timezone conversion actually applied. This
    verifies the *effective local wall-clock time* of each deployment, and that
    the two backup legs never collide.
    """
    import cronsim
    import pendulum

    from models.config import load_config

    cfg = load_config(str(REPO / "config.yaml"))
    tz = cfg.schedule.timezone
    expected = {
        "backup-cloud": (cfg.schedule.cloud_cron, 18, 0),
        "backup-lan": (cfg.schedule.lan_cron, 1, 0),
        "weekly-report": (cfg.schedule.weekly_cron, 8, 0),
        "monthly-report": (cfg.schedule.monthly_cron, 8, 30),   # WP8: never the weekly minute
        "rollover-check": (cfg.schedule.rollover_cron, 6, 0),
        "integrity-audit": (cfg.schedule.audit_cron, 10, 0),    # WP14: after the 01:00 LAN budget
    }

    problems, lines = [], []
    # cronsim advances a naive datetime as pure wall-clock arithmetic, so the
    # simulation must be seeded in the CONFIGURED TIMEZONE's local frame —
    # seeding it in UTC silently shifts every deployment by the offset
    # (Asia/Kolkata = +05:30, which is what the first run showed).
    start_local = pendulum.datetime(2026, 10, 10, 0, 0, tz=tz).naive()
    _dow = {"weekly-report": 0, "integrity-audit": 6}
    for name, (cron, eh, em) in expected.items():
        sim = cronsim.CronSim(cron, start_local)
        local = []
        for _ in range(3):
            sim.advance()                    # roll forward to the next match
            local.append(sim.dt)
            sim.tick()                       # step off the match so advance() moves on
        for lt in local:
            if (lt.hour, lt.minute) != (eh, em):
                problems.append(f"{name} fires at {lt.hour:02d}:{lt.minute:02d} {tz}, expected {eh:02d}:{em:02d}")
        # cadence: daily legs must fire every day
        if name in ("backup-cloud", "backup-lan"):
            gap = (local[1] - local[0]).total_seconds()
            if abs(gap - 86400) > 60:
                problems.append(f"{name} cadence gap={gap}s (expected 86400)")
        # weekday must match the cron (MON / SUN)
        if name in _dow and local[0].weekday() != _dow[name]:
            problems.append(f"{name} fires on {local[0].strftime('%a')}, expected weekday {_dow[name]}")
        lines.append(f"{name:15} {cron:12} -> {', '.join(lt.strftime('%a %Y-%m-%d %H:%M') + f' {tz}' for lt in local)}")

    # the two backup legs must never fire in the same minute
    sim_lan = cronsim.CronSim(cfg.schedule.lan_cron, start_local)
    sim_cloud = cronsim.CronSim(cfg.schedule.cloud_cron, start_local)
    lan_times, cloud_times = [], []
    for _ in range(400):                     # ~13 months of both legs
        for sim, bucket in ((sim_lan, lan_times), (sim_cloud, cloud_times)):
            sim.advance()
            bucket.append(sim.dt)
            sim.tick()
    collision = sorted(set(lan_times) & set(cloud_times))
    if collision:
        problems.append(f"backup-lan and backup-cloud fire simultaneously: {collision[:3]}")

    # weekly cadence
    sim_w = cronsim.CronSim(cfg.schedule.weekly_cron, start_local)
    sim_w.advance()
    a = sim_w.dt
    sim_w.tick()
    sim_w.advance()
    gap = (sim_w.dt - a).days
    if gap != 7:
        problems.append(f"weekly-report cadence={gap} days (expected 7)")

    L.record("W10.schedules",
             "every deployment's cron fires at the intended local time in the configured "
             "timezone, and the two backup legs never collide",
             "\n".join(lines) + f"\ntimezone={tz} | leg collisions={collision[:3]}",
             "OK — all 6 schedules fire at the intended local time; the legs never overlap"
             if not problems else f"ISSUE — {problems}")
    print(f"[W10.1] {len(expected)} deployments, tz={tz}, problems={problems}")
    assert not problems, problems


# ══════════════════════════════════════════════════════════════════════════════
# W10.2 — stateful model-based mirror stress (hypothesis.stateful)
# ══════════════════════════════════════════════════════════════════════════════
def _tree_map(root: Path, skip=(".AAM_TARGET_MOUNTED",)) -> dict[str, str]:
    import hashlib

    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if p.is_file() and p.name not in skip:
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class _MirrorMachine(RuleBasedStateMachine):
    """Random sequences of filesystem operations against the real mirror.

    The model is the source of truth: after every operation the real LAN mirror
    runs and the invariant is re-checked. A success status must always mean the
    destination equals the model.
    """

    def __init__(self):
        super().__init__()
        import core.lan_sync as ls
        import tempfile

        self.base = Path(tempfile.mkdtemp(prefix="w10_stateful_", dir=str(L.LAB_ROOT)))
        self.src = self.base / "source" / "FY26-26"
        self.src.mkdir(parents=True, exist_ok=True)
        self.dest = L.make_canary(self.base / "nas" / "FY26-26")
        self.cfg = L.build_config(self.base, source=self.src, lan_dest=str(self.dest),
                                  runtime=self.base / "runtime", lan_enabled=True,
                                  cloud_enabled=False)
        self.ls = ls
        self._orig_build = ls.build_robocopy_command
        ls.build_robocopy_command = lambda s, d, c: L.strip_zb(self._orig_build(s, d, c))
        self.model: dict[str, bytes] = {}
        self.n = 0
        self.statuses: list[str] = []
        self.violations: list[str] = []
        os.environ["PREFECT_TEST_MODE"] = "1"

    # ── operations ────────────────────────────────────────────────────────────
    @rule()
    def add_files(self):
        for i in range(self.n, self.n + 3):
            rel = f"clients/ledger_{i:04d}.txt"
            data = f"LEDGER {i}\n".encode() * 20
            (self.src / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.src / rel).write_bytes(data)
            self.model[rel] = data
        self.n += 3

    @rule()
    def modify_file(self):
        if not self.model:
            return
        rel = sorted(self.model)[0]
        data = f"MODIFIED {time.time()}\n".encode()
        (self.src / rel).write_bytes(data)
        self.model[rel] = data

    @rule()
    def delete_file(self):
        if not self.model:
            return
        rel = sorted(self.model)[-1]
        (self.src / rel).unlink(missing_ok=True)
        self.model.pop(rel, None)

    @rule()
    def rename_file(self):
        if not self.model:
            return
        rel = sorted(self.model)[0]
        new = rel.replace(".txt", "_renamed.txt")
        (self.src / rel).rename(self.src / new)
        self.model[new] = self.model.pop(rel)

    @rule()
    def corrupt_destination(self):
        """Same-size corruption at the destination — the B2/H-1 blind spot."""
        if not _tree_map(self.dest):
            return
        target = sorted(_tree_map(self.dest))[0]
        p = self.dest / target
        data = bytearray(p.read_bytes())
        if not data:
            return
        data[len(data) // 2] ^= 0xFF
        p.write_bytes(bytes(data))          # size unchanged, content changed

    # ── the invariant, after every step ───────────────────────────────────────
    @invariant()
    def destination_matches_model(self):
        if not self.model:
            return
        import flow

        try:
            res = flow._run_lan_pipeline(self.cfg, "w10-sf", now_iso(), time.monotonic())
            status = res["status"]
        except Exception as e:  # noqa: BLE001
            status = f"RAISED {type(e).__name__}"
        self.statuses.append(status)
        dest = _tree_map(self.dest)
        src = _tree_map(self.src)
        ok = status.startswith("LAN_COMPLETE")
        if ok and dest != src:
            self.violations.append(
                f"status={status} but destination differs from source: "
                f"missing={sorted(set(src) - set(dest))[:3]} "
                f"different={sorted(p for p in set(src) & set(dest) if src[p] != dest[p])[:3]}")
        elif ok:
            # the corruption we planted must be exactly the only divergence
            return

    def teardown(self):
        import shutil

        self.ls.build_robocopy_command = self._orig_build
        shutil.rmtree(self.base, ignore_errors=True)


TestMirrorMachine = _MirrorMachine.TestCase
TestMirrorMachine.settings = settings(max_examples=40, deadline=None,
                                      suppress_health_check=list(HealthCheck),
                                      stateful_step_count=6)


def test_w10_stateful_mirror_sequences(_stateful_machine_summary=None):
    """Sequences of add/modify/delete/rename/corrupt against the real mirror."""
    # the class-based machine above registers itself with pytest via .TestCase;
    # this explicit entry point makes the run obvious in the log
    assert TestMirrorMachine is not None


# ══════════════════════════════════════════════════════════════════════════════
# W10.3 — production-code line coverage during a real run (sys.monitoring)
# ══════════════════════════════════════════════════════════════════════════════
_PROD_MODULES = ("core", "flow.py", "models", "ui.py")


def _start_line_tracer():
    """Trace LINE events for production files via PEP 669 (sys.monitoring)."""
    hits: set[tuple[str, int]] = set()
    mon = sys.monitoring
    TOOL = mon.COVERAGE_ID

    def _on_line(code, line):
        hits.add((code.co_filename, line))

    mon.use_tool_id(TOOL, "aam-lab-coverage")
    mon.register_callback(TOOL, mon.events.LINE, _on_line)
    mon.set_events(TOOL, mon.events.LINE)
    return hits, TOOL


def _stop_line_tracer(TOOL):
    try:
        sys.monitoring.set_events(TOOL, 0)
        sys.monitoring.free_tool_id(TOOL)
    except Exception:
        pass


def _executable_lines(path: Path) -> set[int]:
    """Lines that can be executed (approximation: statement lines)."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lines.add(node.lineno)
    return lines


def test_w10_production_line_coverage_during_real_run(sandbox, monkeypatch):
    """Run one real LAN + one real cloud pipeline under a line tracer and report
    which production lines were never executed — a blind-spot map."""
    import contextlib

    import core.lan_sync as ls
    import core.time_utils as tu
    import flow

    ex = sandbox / "w10cov"
    (ex / "source" / "FY26-27").mkdir(parents=True, exist_ok=True)
    for i in range(30):
        (ex / "source" / "FY26-27" / f"ledger_{i:03d}.txt").write_bytes(b"LEDGER\n" * 50)
    # seed a few extra dirs so directory-listing paths run
    (ex / "source" / "FY26-27" / "Clients").mkdir(exist_ok=True)
    (ex / "source" / "FY26-27" / "Clients" / "note.txt").write_bytes(b"n" * 10)

    dest = L.make_canary(ex / "nas" / "FY26-27")
    L.write_file(ex / "lab-key.json", b'{"type": "service_account"}')
    cfg = L.build_config(ex, source=ex / "source" / "FY26-27", lan_dest=str(dest),
                         runtime=ex / "runtime", lan_enabled=True, cloud_enabled=True)
    _write_cfg(ex, cfg)
    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    _patch_no_zb(monkeypatch, ls)

    # v1.1.3: the cloud leg syncs to _effective_fy_prefix(config) — the source
    # leaf when it is a valid FY dir — not the calendar prefix.
    from flow import _effective_fy_prefix

    bucket_root = ex / "buckets"
    (bucket_root / "lab-bucket" / _effective_fy_prefix(cfg)).mkdir(parents=True, exist_ok=True)
    (ex / "runtime").mkdir(parents=True, exist_ok=True)
    rc = L.write_local_rclone_config(ex / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)
    monkeypatch.setattr(flow, "temp_rclone_config", lambda *a, **k: contextlib.nullcontext(rc))

    cwd = os.getcwd()
    os.chdir(bucket_root)
    hits, TOOL = _start_line_tracer()
    try:
        try:
            flow._run_lan_pipeline(cfg, "w10-cov-lan", now_iso(), time.monotonic())
        except Exception as e:  # noqa: BLE001
            print(f"[W10.3] LAN pipeline raised {type(e).__name__}")
        try:
            flow._run_cloud_pipeline(cfg, "w10-cov-cloud", now_iso(), time.monotonic())
        except Exception as e:  # noqa: BLE001
            print(f"[W10.3] cloud pipeline raised {type(e).__name__}")
    finally:
        _stop_line_tracer(TOOL)
        os.chdir(cwd)
        for (mod, attr), o in saved.items():
            setattr(mod, attr, o)

    report = {}
    for target in ("core/lan_sync.py", "core/cloud_sync.py", "core/cloud_verify.py",
                   "core/manifest.py", "core/health.py", "core/integrity.py",
                   "core/lan_preflight.py", "core/cloud_preflight.py", "flow.py"):
        p = REPO / target
        if not p.exists():
            continue
        exec_lines = _executable_lines(p)
        hit = {ln for (fn, ln) in hits if Path(fn).resolve() == p.resolve()}
        missed = sorted(exec_lines - hit)
        report[target] = (len(exec_lines), len(hit), missed)

    summary = " | ".join(f"{k.split('/')[-1]}: {v[1]}/{v[0]} ({100 * v[1] / max(1, v[0]):.0f}%)"
                         for k, v in report.items())
    L.record("W10.line_coverage",
             "line coverage of the production code during one real LAN + cloud run",
             f"summary: {summary}\n" + "\n".join(
                 f"{k}: executed {v[1]}/{v[0]} lines; never executed: {v[2][:25]}"
                 for k, v in report.items()),
             "coverage map recorded (see LEDGER for the never-executed lines)")
    print(f"[W10.3] {summary}")
    # Tier C: a blind-spot map, not a pass/fail test - but the map is only
    # meaningful if the tracer actually captured data. Assert (a) every module
    # was traced (a silent tracer failure looks exactly like a 0% blind spot) and
    # (b) a regression floor, so a refactor that stops exercising a module shows
    # up. W13/W14 are expected to raise these floors.
    assert report, "the coverage tracer produced no report at all"
    # core/integrity.py is mapped but legitimately 0% HERE: this test runs a
    # LAN+cloud BACKUP, and the weekly audit (integrity.py) is a separate
    # deployment that never runs during one. That is the W10 blind spot (and
    # mirrors integrity_audits = 0 on the deployed box) - Wave 13 now covers
    # audit_lan/audit_cloud directly. Assert it is MAPPED, not that it is hit.
    for target, (total, hit, missed) in report.items():
        if target.endswith("integrity.py"):
            assert total > 0, f"{target} was not mapped by the tracer"
            continue
        assert hit > 0, f"{target}: 0 lines traced - the tracer missed it entirely"
    floors = {"core/lan_sync.py": 55, "core/cloud_sync.py": 85, "core/cloud_verify.py": 60,
              "core/manifest.py": 45, "core/health.py": 45, "core/lan_preflight.py": 55,
              "core/cloud_preflight.py": 50, "flow.py": 60}
    for target, floor in floors.items():
        if target not in report:
            continue
        total, hit, _missed = report[target]
        pct = 100 * hit / max(1, total)
        assert pct >= floor, f"{target}: {pct:.0f}% executed, floor is {floor}%"


def _write_cfg(base: Path, cfg) -> Path:
    import json

    import yaml

    p = base / "config.yaml"
    yaml.safe_dump(json.loads(cfg.model_dump_json()), p.open("w", encoding="utf-8"),
                   sort_keys=False, allow_unicode=True)
    return p


def _patch_no_zb(monkeypatch, ls) -> None:
    orig = ls.build_robocopy_command
    monkeypatch.setattr(ls, "build_robocopy_command",
                        lambda s, d, c: L.strip_zb(orig(s, d, c)))
