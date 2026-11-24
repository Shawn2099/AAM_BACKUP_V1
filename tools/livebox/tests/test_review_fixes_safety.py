"""Red-first tests for the Opus review must-fix items 1-5 (safety / rig / run_lane).

Assumed names for the fix (stated, since the review gives no API):
  * rig.LaneRig.prefect_api_url : the lane harness's OWN api url, set by prefect_harness(); S6 compares
    PREFECT_API_URL against it (None / missing -> S6 refuses, fail closed).
  * run_lane.LaneHooks(..., code_root=Path) and the CLI flag --code-root (default run_lane.CODE_ROOT): the BOX checkout
    used for S1 (git head) and S5 (flow.__file__); with --code-root the evidence root defaults under the lane folder.
Everything runs on tmp_path; no Prefect server, no real tools.
"""
from __future__ import annotations

import contextlib
import importlib
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


def rl():
    return importlib.import_module("tools.livebox.run_lane")


def rigmod():
    return importlib.import_module("tools.livebox.rig")


def safety():
    return importlib.import_module("tools.livebox.safety")


# ================================================================== item 1a: _Patches.setattr on a missing attribute
def test_patches_setattr_missing_attribute_raises_rigerror_and_creates_nothing():
    r = rigmod()
    mod = types.ModuleType("dummy_mod")
    mod.present = 1
    p = r._Patches()
    with pytest.raises(r.RigError):
        p.setattr(mod, "wol_send", lambda: None)
    assert not hasattr(mod, "wol_send")


def test_patches_setattr_existing_attribute_still_patches_and_restores():
    r = rigmod()
    mod = types.ModuleType("dummy_mod")
    mod.present = 1
    p = r._Patches()
    p.setattr(mod, "present", 2)
    assert mod.present == 2
    p.restore()
    assert mod.present == 1


# ================================================================== shared helpers for the preflight tests
CHECKS = ("check_box_head", "check_sandbox_paths", "check_not_production_paths", "check_fy_leaves",
          "check_config_binding", "check_prefect_api_url", "check_notifications", "check_rclone_config",
          "check_shutdown_raisers", "check_production_lock", "check_production_schedule", "check_resources",
          "check_lane_process_count", "check_production_slot")


def _cfg(lane: Path):
    return SimpleNamespace(
        paths=SimpleNamespace(source_drive=str(lane / "src" / "FY90-91"), lan_destination=str(lane / "nas" / "FY90-91"),
                              runtime_dir=str(lane / "runtime"), database_path=str(lane / "runtime" / "m.db"),
                              log_directory=str(lane / "runtime" / "logs"),
                              backup_lock_path=str(lane / "runtime" / "backup.lock")),
        run_logs_dir=str(lane / "runtime" / "run_logs"),
        notifications=SimpleNamespace(smtp_host="127.0.0.1", smtp_auth=False, recipients=["a@livebox.invalid"],
                                      report_recipients=["b@livebox.invalid"]))


def _hooks(tmp_path, monkeypatch=None, **kw):
    lane = tmp_path / "stamp1" / "laneA"
    lane.mkdir(parents=True, exist_ok=True)
    if monkeypatch is not None:                       # S2 looks at these two environment folders
        monkeypatch.setenv("PREFECT_HOME", str(lane / "prefect_home"))
        monkeypatch.setenv("TEMP", str(lane / "tmp"))
    h = rl().LaneHooks(lane, lane="laneA", stamp="stamp1", days=[], agent_runs=tmp_path / "ar",
                       allow_dev_path=True, **kw)
    h.ev_dir.mkdir(parents=True, exist_ok=True)
    return h


def _stub_checks(monkeypatch, events=None, real=()):
    """Replace the safety.check_* functions (except `real`) with recorders; return the call log."""
    s = safety()
    calls = []
    for name in CHECKS:
        if name in real or not hasattr(s, name):
            continue

        def rec(*a, _n=name, **k):
            calls.append((_n, a, k))
            if events is not None:
                events.append(_n)

        monkeypatch.setattr(s, name, rec)
    return calls


class FakeRig:
    """Stands in for a built LaneRig inside the hooks (preflight-only tests)."""

    def __init__(self, lane, seams=None, api_url=None):
        self.lane_dir = lane
        self.config_path = lane / "config.livebox.yaml"
        self.bucket_root = lane / "bucket"
        self.seams = seams if seams is not None else rigmod().Seams(str(lane), None, None)   # not installed
        self.prefect_api_url = api_url
        self._cfg = _cfg(lane)

    def load_config(self):
        return self._cfg


def _prep_preflight(h, monkeypatch, rig):
    h.rig = rig
    monkeypatch.setattr(h, "_git", lambda *a: "deadbeef\n")
    monkeypatch.setattr(h, "_count_lane_processes", lambda: 1)
    monkeypatch.setattr(h, "_resources", lambda: None)


# ================================================================== item 2: S6 / S8 are not tautological
def _set_url(monkeypatch, url):
    import prefect.settings as ps

    monkeypatch.setattr(ps, "PREFECT_API_URL", SimpleNamespace(value=lambda: url))
    monkeypatch.setenv("PREFECT_API_URL", url or "")


@pytest.mark.parametrize("url, lane_url", [
    ("http://127.0.0.1:6000/api", "http://127.0.0.1:5000/api"),     # a different loopback server (not :4200)
    ("http://127.0.0.1:4200/api", "http://127.0.0.1:5000/api"),     # the production port
    ("http://127.0.0.1:4200/api", "http://127.0.0.1:4200/api"),     # even when both say :4200
    ("http://127.0.0.1:6000/api", None),                            # the harness url is unknown: fail closed
])
def test_s6_compares_against_the_lane_harness_url(tmp_path, monkeypatch, url, lane_url):
    h = _hooks(tmp_path, monkeypatch)
    _stub_checks(monkeypatch, real=("check_prefect_api_url",))
    _prep_preflight(h, monkeypatch, FakeRig(h.lane_root, api_url=lane_url))
    _set_url(monkeypatch, url)
    with pytest.raises(safety().SafetyRefusal) as ei:
        h._safety_preflight()
    assert ei.value.rule == "S6"


def test_s6_accepts_the_lane_harness_url(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    _stub_checks(monkeypatch, real=("check_prefect_api_url",))
    _prep_preflight(h, monkeypatch, FakeRig(h.lane_root, api_url="http://127.0.0.1:5000/api"))
    _set_url(monkeypatch, "http://127.0.0.1:5000/api")
    h._safety_preflight()


@pytest.fixture
def seamed(tmp_path, monkeypatch):
    """Capture the real objects, then install the real seams (raisers, spies, local rclone)."""
    import core.cloud_preflight as cpre
    import core.cloud_sync as csync
    import core.shutdown as sd
    import core.wol as wol
    import flow

    orig = SimpleNamespace(
        wol_ensure=wol.ensure_server_online, wol_send=wol._send_magic_packet, sd_from_config=sd.shutdown_from_config,
        sd_server=sd.shutdown_server, flow_temp=flow.temp_rclone_config, csync_temp=csync.temp_rclone_config,
        cpre_temp=cpre.temp_rclone_config)
    lane = tmp_path / "stamp1" / "laneA"
    lane.mkdir(parents=True, exist_ok=True)
    seams = rigmod().install_seams(lane)
    try:
        yield SimpleNamespace(seams=seams, orig=orig, flow=flow, wol=wol, sd=sd, csync=csync, cpre=cpre)
    finally:
        seams.uninstall()


def _run_with_real(tmp_path, monkeypatch, seamed, real):
    h = _hooks(tmp_path, monkeypatch)
    _stub_checks(monkeypatch, real=real)
    _prep_preflight(h, monkeypatch, FakeRig(h.lane_root, seams=seamed.seams, api_url="http://127.0.0.1:5000/api"))
    _set_url(monkeypatch, "http://127.0.0.1:5000/api")
    h._safety_preflight()


def test_s8_and_s9_pass_when_every_seam_is_installed(tmp_path, monkeypatch, seamed):
    _run_with_real(tmp_path, monkeypatch, seamed, real=("check_rclone_config", "check_shutdown_raisers"))


@pytest.mark.parametrize("mod", ["flow", "csync", "cpre"])
def test_s8_refuses_when_a_temp_rclone_config_is_not_the_seam(tmp_path, monkeypatch, seamed, mod):
    real_fn = {"flow": seamed.orig.flow_temp, "csync": seamed.orig.csync_temp, "cpre": seamed.orig.cpre_temp}[mod]
    monkeypatch.setattr(getattr(seamed, mod), "temp_rclone_config", real_fn)
    with pytest.raises(safety().SafetyRefusal) as ei:
        _run_with_real(tmp_path, monkeypatch, seamed, real=("check_rclone_config", "check_shutdown_raisers"))
    assert ei.value.rule == "S8"


# ================================================================== item 1b: S9 checks what the code really calls
def _clone_with_globals(fn, **override):
    g = dict(fn.__globals__)
    g.update(override)
    clone = types.FunctionType(fn.__code__, g, fn.__name__, fn.__defaults__, fn.__closure__)
    clone.__kwdefaults__ = fn.__kwdefaults__
    return clone


def _s9(tmp_path, monkeypatch, seamed):
    with pytest.raises(safety().SafetyRefusal) as ei:
        _run_with_real(tmp_path, monkeypatch, seamed, real=("check_shutdown_raisers",))
    assert ei.value.rule == "S9"


def test_s9_refuses_when_flow_shutdown_from_config_is_the_real_function(tmp_path, monkeypatch, seamed):
    monkeypatch.setattr(seamed.flow, "shutdown_from_config", seamed.orig.sd_from_config)
    _s9(tmp_path, monkeypatch, seamed)


def test_s9_refuses_when_flow_ensure_server_online_is_the_real_function(tmp_path, monkeypatch, seamed):
    monkeypatch.setattr(seamed.flow, "ensure_server_online", seamed.orig.wol_ensure)
    _s9(tmp_path, monkeypatch, seamed)


def test_s9_checks_the_send_the_real_ensure_server_online_would_call(tmp_path, monkeypatch, seamed):
    """core.wol.ensure_server_online.__globals__['_send_magic_packet'] must be the raiser."""
    leaky = _clone_with_globals(seamed.orig.wol_ensure, _send_magic_packet=seamed.orig.wol_send)
    monkeypatch.setattr(seamed.wol, "ensure_server_online", leaky)
    _s9(tmp_path, monkeypatch, seamed)


def test_s9_checks_the_shutdown_the_real_shutdown_from_config_would_call(tmp_path, monkeypatch, seamed):
    """core.shutdown.shutdown_from_config.__globals__['shutdown_server'] must be the raiser."""
    leaky = _clone_with_globals(seamed.orig.sd_from_config, shutdown_server=seamed.orig.sd_server)
    monkeypatch.setattr(seamed.sd, "shutdown_from_config", leaky)
    _s9(tmp_path, monkeypatch, seamed)


# ================================================================== item 3: local rclone remote must not depend on cwd
def test_local_rclone_remote_is_absolute_or_the_guard_refuses_outside_cwd(tmp_path, monkeypatch):
    lane = tmp_path / "stamp1" / "laneA"
    bucket = lane / "bucket"
    seams = rigmod().Seams(str(lane), str(bucket), None)          # not installed: only writes rclone_local.conf
    text = seams.rclone_conf.read_text(encoding="utf-8")
    lines = {ln.split("=", 1)[0].strip(): ln.split("=", 1)[1].strip() for ln in text.splitlines() if "=" in ln}
    absolute = (lines.get("type") == "alias" and Path(lines.get("remote", "x")).is_absolute()
                and safety().is_under(lines["remote"], lane))
    if absolute:
        return
    # alternative fix: the run_tool guard refuses rclone whose cwd is outside the lane
    import core.tool_runner as tr

    calls = []
    monkeypatch.setattr(tr, "run_tool", lambda *a, **k: calls.append(a))
    h = _hooks(tmp_path)
    try:
        h._install_run_tool_guard()
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.chdir(outside)
        with pytest.raises(safety().SafetyRefusal):
            tr.run_tool("t", ["rclone.exe", "lsd", "aam_gcs:bucket", str(lane / "x")])
        assert not calls
    finally:
        h._stack.close()


# ================================================================== item 4: order of the checks
class OrderRig(FakeRig):
    """Real LaneRig.prefect_harness (and its _ensure_concurrency_limit call) on an otherwise fake rig."""

    prefect_harness = rigmod().LaneRig.prefect_harness

    def __init__(self, lane, events):
        super().__init__(lane, api_url="http://127.0.0.1:5000/api")
        self.events = events
        self.smtp = SimpleNamespace(port=1)

    def build(self):
        self.events.append("build")

    def install(self):
        self.events.append("install")
        return self

    def remove(self):
        pass


def _open_with_events(tmp_path, monkeypatch):
    events: list[str] = []
    h = _hooks(tmp_path, monkeypatch)
    _stub_checks(monkeypatch, events)
    rig = OrderRig(h.lane_root, events)
    monkeypatch.setattr(rigmod().LaneRig, "create", classmethod(lambda cls, *a, **k: rig))
    import launch
    import prefect.testing.utilities as pu

    @contextlib.contextmanager
    def fake_harness():
        events.append("harness")
        yield

    monkeypatch.setattr(pu, "prefect_test_harness", fake_harness)
    monkeypatch.setattr(launch, "_ensure_concurrency_limit", lambda: events.append("ensure_limit"))
    _prep_preflight(h, monkeypatch, rig)
    monkeypatch.setattr(h, "_seed", lambda: None)
    orig_s2 = h._s2

    def s2(paths):
        events.append("S2")
        return orig_s2(paths)

    monkeypatch.setattr(h, "_s2", s2)
    _set_url(monkeypatch, "http://127.0.0.1:5000/api")
    try:
        h.open()
    finally:
        h._stack.close()
    return events


def test_s6_is_checked_before_the_concurrency_limit_is_created(tmp_path, monkeypatch):
    ev = _open_with_events(tmp_path, monkeypatch)
    assert "check_prefect_api_url" in ev and "ensure_limit" in ev
    assert ev.index("check_prefect_api_url") < ev.index("ensure_limit"), ev


@pytest.mark.parametrize("check", ["check_box_head", "check_config_binding", "check_notifications"])
def test_s1_s5_s7_are_checked_before_rig_install(tmp_path, monkeypatch, check):
    ev = _open_with_events(tmp_path, monkeypatch)
    assert check in ev and "install" in ev
    assert ev.index(check) < ev.index("install"), ev


def test_s2_s3_are_checked_before_rig_install(tmp_path, monkeypatch):
    ev = _open_with_events(tmp_path, monkeypatch)
    assert ev.index("S2") < ev.index("install")
    assert ev.index("check_not_production_paths") < ev.index("install")


# ================================================================== item 5: separate box code root
def test_parse_args_code_root_default_and_override(tmp_path):
    a = rl().parse_args(["--stamp", "s1", "--lane", "A"])
    assert Path(a.code_root) == rl().CODE_ROOT
    b = rl().parse_args(["--stamp", "s1", "--lane", "A", "--code-root", str(tmp_path / "box")])
    assert Path(b.code_root) == tmp_path / "box"


def test_hooks_use_the_given_code_root_for_s1_and_s5(tmp_path, monkeypatch):
    box = tmp_path / "boxcode"
    box.mkdir()
    h = _hooks(tmp_path, monkeypatch, code_root=box)
    seen = {}
    import subprocess

    def fake_run(cmd, *a, **k):
        seen["git"] = [str(c) for c in cmd]
        return SimpleNamespace(returncode=0, stdout="abc\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    h._git("rev-parse", "HEAD")
    assert str(box) in seen["git"]
    calls = _stub_checks(monkeypatch)
    _prep_preflight(h, monkeypatch, FakeRig(h.lane_root, api_url="http://127.0.0.1:5000/api"))
    monkeypatch.setattr(h, "_git", lambda *a: "abc\n")
    _set_url(monkeypatch, "http://127.0.0.1:5000/api")
    h._safety_preflight()
    s5 = next(c for c in calls if c[0] == "check_config_binding")
    assert str(box) in [str(x) for x in s5[1]] + [str(v) for v in s5[2].values()]


def test_evidence_defaults_under_the_lane_when_a_box_root_is_given(tmp_path, monkeypatch):
    lane_root = tmp_path / "root" / "s1" / "A"
    captured = {}
    r = rl()
    monkeypatch.setattr(r, "prepare_env", lambda *a, **k: None)

    def from_args(cls, a, lane_root, days, ar):
        captured["ar"] = Path(ar)
        return SimpleNamespace()

    monkeypatch.setattr(r.LaneHooks, "from_args", classmethod(from_args))
    monkeypatch.setattr(r, "run_lane", lambda *a, **k: r.LaneResult())
    monkeypatch.setattr(r, "build_days", lambda a: [])
    box = tmp_path / "boxcode"
    r.main(["--stamp", "s1", "--lane", "A", "--root", str(tmp_path / "root"), "--allow-dev-path",
            "--code-root", str(box)])
    ar = captured["ar"]
    assert safety().is_under(ar, lane_root), ar
    assert not safety().is_under(ar, box), ar
    assert not safety().is_under(ar, r.CODE_ROOT)
