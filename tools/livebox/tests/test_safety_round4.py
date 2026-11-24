r"""Red-first tests (round 4, review 3): box-safety and report-honesty fixes. Written before the code.

Origin: docs/LIVEBOX_OPUS_REVIEW3_2026-11-24.md (H1, H2, M1, M2 and section 3 items 2 and 4).

PINNED API (names are the contract)

H1  run_lane.LaneHooks._guard_s6_before_concurrency_limit()   after its `import launch` it pops PREFECT_API_URL from
        os.environ (launch.py sets it to the production :4200 at import; threads without the settings contextvar
        would inherit it).
    run_lane.LaneHooks.safety_check(ev)   raises SafetyRefusal rule "S6" when os.environ PREFECT_API_URL names the
        production :4200 server (before every event, independent of the settings value).
    run_lane.LaneHooks._safety_preflight()  S6 also refuses when os.environ PREFECT_API_URL is :4200 although the
        settings value is the lane server.
H2  safety.check_argv(argv, lane_root)  accepts a UNC path whose host is in 192.0.2.0/24 or ends with ".invalid"
        (the hosts rig.set_nas allows); every other UNC host (real NAS, 192.0.3.x, "x.invalid.example", plain
        "invalid") is still refused with S15.
M1  run_lane.build_parser()  gains --both-off (dest both_off); --cloud-only / --lan-only / --both-off are mutually
        exclusive (parse error).
    run_lane.lane_mode(a) -> "cloud" | "lan" | "off" | "both"
    run_lane.LaneHooks(..., mode="both")  new kwarg (from_args passes lane_mode(a)); open() calls
        rig.set_mode(mode) AFTER the resume config is written back (mode "off" included).
    rig.LaneRig.set_mode(mode)  also accepts "off" (cloud.enabled = lan.enabled = False).
    scenarios/w3.py _setup_cloud_only / _setup_lan_only  go through rig.set_mode (the on-disk config), so a stale
        in-memory rig.config can never revert a product change on disk.
    run_lane.build_days(a)  both_off -> neither leg expected.
M2  run_lane.LANE_MARKER  (str, file name inside the lane dir). rig.LaneRig.build() writes it. cleanup_lane refuses
        (SafetyRefusal "S2", deletes nothing) when the marker is missing.
make_report  a lane whose result.json is missing, or whose status != "completed", never gives a scenario row PASS:
        the row verdict is NOT_RUN or HARNESS-FAIL.
scenarios/results.verdict_for  with catalog.expected_red set, only the failure lines that NAME the finding id
        (substring) are masked; if any other failure line remains the verdict is FAIL and `failures` lists the
        unmasked lines. All lines covered -> FINDING. (So w1 X01's expect text must carry its finding id.)
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal
from tools.livebox import rig as rigmod
from tools.livebox import run_lane as rl
from tools.livebox import safety
from tools.livebox.tests.test_make_report import check, fake_scn, make_lane
from tools.livebox.tests.test_review_fixes_safety import (
    FakeRig, OrderRig, _hooks, _prep_preflight, _set_url, _stub_checks)

PROD = "http://127.0.0.1:4200/api"
WORKTREE = Path(__file__).resolve().parents[3]
DEV54 = WORKTREE / "agent_runs" / "livebox_dev54" / "w1_x01"
IST = cal.IST


def _event():
    return cal.Event(dt.datetime(2091, 2, 3, 2, 1, tzinfo=IST), cal.CLOUD)


# ============================================================================ H1
def test_h1_guard_drops_env_prefect_api_url_after_import_launch(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    monkeypatch.setenv("PREFECT_API_URL", "x")                     # so teardown restores "absent"
    monkeypatch.delenv("PREFECT_API_URL")
    monkeypatch.delitem(sys.modules, "launch", raising=False)      # fresh import: launch.py sets :4200 itself
    try:
        h._guard_s6_before_concurrency_limit()
        assert "PREFECT_API_URL" not in os.environ
    finally:
        h._stack.close()


def test_h1_guard_drops_a_prod_url_that_is_already_in_the_environment(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    monkeypatch.setenv("PREFECT_API_URL", PROD)
    try:
        h._guard_s6_before_concurrency_limit()
        assert "PREFECT_API_URL" not in os.environ
    finally:
        h._stack.close()


def test_h1_safety_check_refuses_when_env_points_at_prod(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    monkeypatch.setattr(safety, "check_shutdown_raisers", lambda *a, **k: None)
    monkeypatch.setenv("PREFECT_API_URL", PROD)
    with pytest.raises(safety.SafetyRefusal) as ei:
        h.safety_check(_event())
    assert ei.value.rule == "S6"


def test_h1_safety_check_passes_without_the_env_variable(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    monkeypatch.setattr(safety, "check_shutdown_raisers", lambda *a, **k: None)
    monkeypatch.setenv("PREFECT_API_URL", "x")
    monkeypatch.delenv("PREFECT_API_URL")
    h.safety_check(_event())


def test_h1_s6_preflight_refuses_prod_env_even_when_the_settings_value_is_the_lane(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    _stub_checks(monkeypatch, real=("check_prefect_api_url",))
    _prep_preflight(h, monkeypatch, FakeRig(h.lane_root, api_url="http://127.0.0.1:5000/api"))
    _set_url(monkeypatch, "http://127.0.0.1:5000/api")
    monkeypatch.setenv("PREFECT_API_URL", PROD)                    # settings fine, environment leaks :4200
    with pytest.raises(safety.SafetyRefusal) as ei:
        h._safety_preflight()
    assert ei.value.rule == "S6"


# ============================================================================ H2
LANE = r"C:\AAMLivebox\stamp1\laneA"


def _robo(dest):
    return ["robocopy.exe", LANE + r"\src\FY90-91", dest, "/MIR"]


@pytest.mark.parametrize("dest", [
    r"\\192.0.2.1\C$\AAMLivebox\stamp1\laneA\nas\FY90-91",
    r"\\192.0.2.254\share\x",
    r"\\nas.invalid\C$\x",
    r"\\a.b.INVALID\C$\x",
])
def test_h2_s15_accepts_the_hosts_rig_set_nas_allows(dest):
    safety.check_argv(_robo(dest), LANE)


@pytest.mark.parametrize("dest", [
    r"\\192.0.3.1\C$\x",
    r"\\192.168.1.20\backup\x",
    r"\\10.0.0.5\share",
    r"\\nas.example.com\x",
    r"\\nas.invalid.example.com\x",
    r"\\invalid\x",
    r"\\192.0.2.1.evil.net\x",
    r"C:\AAMBackup\x",
])
def test_h2_s15_still_refuses_every_other_host_and_path(dest):
    with pytest.raises(safety.SafetyRefusal) as ei:
        safety.check_argv(_robo(dest), LANE)
    assert ei.value.rule == "S15"


# ============================================================================ M1
def _args(*extra):
    return rl.build_parser().parse_args(["--stamp", "s1", "--lane", "l1", *extra])


@pytest.mark.parametrize("flags, mode", [
    ((), "both"), (("--cloud-only",), "cloud"), (("--lan-only",), "lan"), (("--both-off",), "off")])
def test_m1_lane_mode(flags, mode):
    a = _args(*flags)
    assert rl.lane_mode(a) == mode


def test_m1_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        _args("--cloud-only", "--both-off")


def test_m1_both_off_calendar_has_no_backup_rows():
    a = _args("--both-off", "--days", "3")
    days = rl.build_days(a)
    assert days and all(not d.expected_rows for d in days)


class ModeRig(OrderRig):
    def set_mode(self, mode):
        self.events.append(("set_mode", mode))


def _open(tmp_path, monkeypatch, mode, resume=False):
    events: list = []
    h = _hooks(tmp_path, monkeypatch, mode=mode, resume=resume)
    _stub_checks(monkeypatch, events)
    rig = ModeRig(h.lane_root, events)
    monkeypatch.setattr(rigmod.LaneRig, "create", classmethod(lambda cls, *a, **k: rig))
    import launch
    import prefect.testing.utilities as pu

    @contextlib.contextmanager
    def fake_harness():
        yield

    monkeypatch.setattr(pu, "prefect_test_harness", fake_harness)
    monkeypatch.setattr(launch, "_ensure_concurrency_limit", lambda: None)
    _prep_preflight(h, monkeypatch, rig)
    monkeypatch.setattr(h, "_seed", lambda: None)
    _set_url(monkeypatch, "http://127.0.0.1:5000/api")
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    try:
        h.open()
    finally:
        h._stack.close()
    return [e for e in events if isinstance(e, tuple)]


@pytest.mark.parametrize("mode", ["cloud", "lan", "off"])
def test_m1_open_sets_the_lane_config_through_rig_set_mode(tmp_path, monkeypatch, mode):
    assert ("set_mode", mode) in _open(tmp_path, monkeypatch, mode)


def _disk(rig):
    import yaml

    return yaml.safe_load(rig.config_path.read_text(encoding="utf-8"))


@pytest.fixture
def real_rig(tmp_path):
    r = rigmod.LaneRig(tmp_path / "stamp1" / "laneK").build()
    yield r
    r.remove()


def test_m1_rig_set_mode_off_disables_both_legs(real_rig):
    real_rig.set_mode("off")
    c = _disk(real_rig)
    assert c["cloud"]["enabled"] is False and c["lan"]["enabled"] is False


def test_m1_w3_setup_never_reverts_a_newer_config_on_disk(real_rig):
    """X34/X35 setup used the stale in-memory rig.config and rewrote the whole YAML (product FY switch lost)."""
    import yaml
    from tools.livebox.scenarios import w3

    c = _disk(real_rig)
    c["schedule"]["cloud_cron"] = "7 7 * * *"                    # a change the in-memory copy does not know
    real_rig.config_path.write_text(yaml.safe_dump(c, sort_keys=False), encoding="utf-8")
    w3._setup_cloud_only(real_rig)
    d = _disk(real_rig)
    assert d["schedule"]["cloud_cron"] == "7 7 * * *"
    assert d["lan"]["enabled"] is False and d["cloud"]["enabled"] is True


# ============================================================================ M2
def test_m2_cleanup_refuses_a_folder_without_the_lane_marker(tmp_path):
    root = tmp_path / "AAMLivebox"
    code = root / "harness" / "tools"
    code.mkdir(parents=True)
    (code / "run_lane.py").write_text("x", encoding="utf-8")
    with pytest.raises(safety.SafetyRefusal) as ei:
        rl.cleanup_lane(code, root, keep_evidence=False)
    assert ei.value.rule == "S2"
    assert (code / "run_lane.py").exists()


def test_m2_cleanup_works_when_the_marker_exists(tmp_path):
    root = tmp_path / "AAMLivebox"
    lane = root / "stamp1" / "laneA"
    lane.mkdir(parents=True)
    (lane / rl.LANE_MARKER).write_text("lane", encoding="utf-8")
    (lane / "x.txt").write_text("x", encoding="utf-8")
    rl.cleanup_lane(lane, root, keep_evidence=False)
    assert not lane.exists()


def test_m2_rig_build_writes_the_lane_marker(real_rig):
    assert (real_rig.lane_dir / rl.LANE_MARKER).is_file()


# ============================================================================ make_report: aborted lanes
def _report(tmp_path, monkeypatch, catalog):
    from tools.livebox import make_report as m

    monkeypatch.setattr(m, "load_catalog", lambda: list(catalog))
    out = tmp_path / "REPORT.md"
    res = m.build_report([tmp_path / "ev"], out)
    return res, out.read_text(encoding="utf-8")


def _row(text, sid):
    return next((ln for ln in text.splitlines() if ln.startswith(f"| {sid} |")), "")


def test_report_completed_lane_is_pass(tmp_path, monkeypatch):
    make_lane(tmp_path / "ev", "laneS1")
    res, text = _report(tmp_path, monkeypatch, [fake_scn("S1", "laneS1")])
    assert "PASS" in _row(text, "S1")


@pytest.mark.parametrize("status", ["aborted", "running", "failed"])
def test_report_unfinished_lane_is_not_pass(tmp_path, monkeypatch, status):
    make_lane(tmp_path / "ev", "laneS1", status=status)
    res, text = _report(tmp_path, monkeypatch, [fake_scn("S1", "laneS1")])
    row = _row(text, "S1")
    assert "PASS" not in row and ("NOT_RUN" in row or "HARNESS-FAIL" in row), row


def test_report_lane_without_result_json_is_not_pass(tmp_path, monkeypatch):
    d = make_lane(tmp_path / "ev", "laneS1", checks=[check("2091-02-03", "C1")])
    (d / "result.json").unlink()
    res, text = _report(tmp_path, monkeypatch, [fake_scn("S1", "laneS1")])
    row = _row(text, "S1")
    assert "PASS" not in row and ("NOT_RUN" in row or "HARNESS-FAIL" in row), row


# ============================================================================ expected_red masks only its finding
def _verdict(tmp_path, fails, red):
    from tools.livebox.scenarios import results

    scn = dataclasses.replace(fake_scn("S1", "l"), expect=lambda r: list(fails), expected_red=red)
    (tmp_path / "ev").mkdir(exist_ok=True)
    return results.verdict_for(scn, tmp_path / "ev")


def test_expected_red_masks_a_failure_that_names_the_finding(tmp_path):
    v = _verdict(tmp_path, ["S1: switch with the NAS unreachable (F-OWN-1)"], "F-OWN-1")
    assert v["verdict"] == "FINDING"


def test_expected_red_does_not_mask_an_unrelated_failure(tmp_path):
    v = _verdict(tmp_path, ["S1: LAN must stay green all days"], "F-OWN-1")
    assert v["verdict"] == "FAIL"
    assert any("LAN must stay green" in f for f in v["failures"])


def test_expected_red_mixed_failures_stay_fail(tmp_path):
    v = _verdict(tmp_path, ["S1: known thing (F-OWN-1)", "S1: something else broke"], "F-OWN-1")
    assert v["verdict"] == "FAIL"
    assert any("something else broke" in f for f in v["failures"])


needs_dev54 = pytest.mark.skipif(not (DEV54 / "checks.jsonl").is_file(), reason="evidence livebox_dev54 absent")


def _x01():
    from tools.livebox.scenarios import w1

    return {s.id: s for s in w1.SCENARIOS}["X01"]


@needs_dev54
def test_x01_real_evidence_is_the_known_finding():
    from tools.livebox.scenarios import results

    assert results.verdict_for(_x01(), DEV54)["verdict"] == "FINDING"


@needs_dev54
def test_x01_with_an_unrelated_failure_is_fail():
    from tools.livebox.scenarios import results

    orig = _x01()
    scn = dataclasses.replace(orig, expect=lambda r: list(orig.expect(r)) + [
        "X01: LAN must stay green all days, observed 3 non-success LAN rows"])
    v = results.verdict_for(scn, DEV54)
    assert v["verdict"] == "FAIL"
