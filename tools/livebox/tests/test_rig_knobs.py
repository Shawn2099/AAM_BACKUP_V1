r"""Red-first tests for the scenario knobs of tools.livebox.rig.LaneRig (packs w1-w4 call them by these names).

PINNED API (all methods of LaneRig; every one is reversible and lane-local, nothing outside the lane dir is touched;
the tests use tmp dirs only, never the real config, never a real NAS):

    rig._lane_path(p) -> Path               resolve p (absolute, or relative to rig.lane_dir); RigError if the
                                            resolved path is not inside rig.lane_dir (also for ".." and junction escapes)
    rig.set_nas(host: str | None) -> None   host: paths.lan_destination becomes \\<host>\<rest of the lane share>;
                                            None restores the original lane value. rig.config and the YAML stay in step
                                            (still valid for models.config.AppConfig).
    rig.make_junction(name, target) -> Path junction (mklink /J) lane\src\<name> -> target. target is relative to
                                            rig.lane_dir (created if missing) or absolute inside it; otherwise RigError.
    rig.delete_source_folder(fy) -> None    remove lane\src\<fy>; a junction is unlinked only (target survives);
                                            fy with a path separator or ".." -> RigError.
    rig.deny_source_parent_write(flag: bool)  icacls deny of create-file/create-folder on lane\src for the current
                                            user (True) / remove exactly that deny (False).
    rig.deny_config_write(flag: bool)       same for the config file (write + delete denied).
    rig.fail_archive(flag: bool)            seams.archive_result False/True (archive recorder still records the call).
    rig.kill_child_after(step: str)         arm: rig.kill_armed == step.
    rig.fire_step(step) -> list[int]        if armed for `step`: kill_lane_processes(), disarm, return the pids;
                                            otherwise [] and nothing is killed.
    rig.kill_lane_processes() -> list[int]  taskkill /T /F ONLY of processes whose cmdline contains str(rig.lane_dir)
                                            (never our own pid or parents).
    rig.tamper_nas_file(fy) -> Path         flip bytes of one NAS file of that FY: same size, same mtime (ns), new content.
    rig.approve(phrase, *, client=None) -> dict
                                            client = dashboard TestClient (default: built from the lane config).
                                            GET /approvals -> first open request; POST /approvals/<id>/approve json
                                            {"phrase": phrase}. Appends one JSON row to <evidence_dir>/approvals.jsonl
                                            with keys phrase, http_status, applied (http 200), fy, leg, status; returns it.
                                            No open request: http_status None, applied False.
    rig.set_mode(mode)                      "cloud" | "lan" | "both": cloud.enabled / lan.enabled in the lane config
                                            (the real keys of models.config).
    rig.set_lan_manual(flag: bool)          lan.scheduled = not flag (real key); config stays valid.
    rig.clock_jump(delta: timedelta) -> datetime   clock.move_to(clock.now + delta); returns the new instant
    rig.clock_set(instant: datetime) -> datetime   clock.move_to(instant); RigError without a clock
    rig.fire_trigger(leg, *, client=None) -> int | None   POST /trigger/<leg> on the dashboard client, returns the
                                            HTTP status (X37: fired while a flow is running).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.livebox import rig as rigmod

NAS_FY = "FY90-91"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="icacls / junctions are Windows only")


@pytest.fixture
def rig(tmp_path):
    r = rigmod.LaneRig(tmp_path / "stamp1" / "laneK").build()
    yield r
    r.remove()


def _cfg_on_disk(rig):
    import yaml

    return yaml.safe_load(rig.config_path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ _lane_path
def test_lane_path_accepts_inside_and_refuses_outside(rig, tmp_path):
    assert rig._lane_path("src") == (rig.lane_dir / "src").resolve()
    for bad in (tmp_path / "outside", "..", str(rig.lane_dir) + "_sibling", rig.lane_dir / ".." / "x"):
        with pytest.raises(rigmod.RigError):
            rig._lane_path(bad)


# ------------------------------------------------------------------------------------------------ set_nas
def test_set_nas_points_at_unreachable_host_and_restores(rig):
    orig = rig.config["paths"]["lan_destination"]
    rig.set_nas("192.0.2.1")
    assert rig.config["paths"]["lan_destination"].startswith("\\\\192.0.2.1\\")
    assert _cfg_on_disk(rig)["paths"]["lan_destination"] == rig.config["paths"]["lan_destination"]
    rig.load_config()                                           # still valid for the product model
    rig.set_nas(None)
    assert rig.config["paths"]["lan_destination"] == orig
    assert _cfg_on_disk(rig)["paths"]["lan_destination"] == orig


def test_set_nas_twice_still_restores_original(rig):
    orig = rig.config["paths"]["lan_destination"]
    rig.set_nas("192.0.2.1")
    rig.set_nas("192.0.2.2")
    rig.set_nas(None)
    assert rig.config["paths"]["lan_destination"] == orig


# ------------------------------------------------------------------------------------------------ junction
@windows_only
def test_make_junction_links_into_lane_target(rig):
    j = rig.make_junction(NAS_FY, "elsewhere")
    assert j == rig.lane_dir / "src" / NAS_FY or j == rig.source_dir.parent / NAS_FY
    assert os.path.isjunction(j)
    (rig.lane_dir / "elsewhere" / "probe.txt").write_text("x")
    assert (j / "probe.txt").read_text() == "x"


@windows_only
def test_make_junction_refuses_target_outside_lane(rig, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(rigmod.RigError):
        rig.make_junction("FY91-92", outside)
    with pytest.raises(rigmod.RigError):
        rig.make_junction("FY91-92", "..\\..\\escape")
    assert not (rig.lane_dir / "src" / "FY91-92").exists()


@windows_only
def test_make_junction_refuses_name_with_separator(rig):
    with pytest.raises(rigmod.RigError):
        rig.make_junction("..\\evil", "elsewhere")


# ------------------------------------------------------------------------------------------------ delete_source_folder
def test_delete_source_folder_removes_tree(rig):
    d = rig.lane_dir / "src" / "FY91-92"
    d.mkdir(parents=True)
    (d / "a.txt").write_text("a")
    rig.delete_source_folder("FY91-92")
    assert not d.exists()
    assert rig.source_dir.exists()                              # other FY untouched


@windows_only
def test_delete_source_folder_unlinks_junction_but_keeps_target(rig):
    j = rig.make_junction("FY91-92", "elsewhere")
    (rig.lane_dir / "elsewhere" / "keep.txt").write_text("k")
    rig.delete_source_folder("FY91-92")
    assert not os.path.lexists(j)
    assert (rig.lane_dir / "elsewhere" / "keep.txt").read_text() == "k"


@pytest.mark.parametrize("bad", ["..", "..\\x", "a/b", "C:\\Windows"])
def test_delete_source_folder_refuses_paths(rig, bad):
    with pytest.raises(rigmod.RigError):
        rig.delete_source_folder(bad)


# ------------------------------------------------------------------------------------------------ icacls denies
@windows_only
def test_deny_source_parent_write_is_reversible(rig):
    src = rig.lane_dir / "src"
    try:
        rig.deny_source_parent_write(True)
        with pytest.raises(PermissionError):
            (src / "newfy").mkdir()
    finally:
        rig.deny_source_parent_write(False)
    (src / "newfy").mkdir()                                     # works again


@windows_only
def test_deny_config_write_is_reversible(rig):
    try:
        rig.deny_config_write(True)
        with pytest.raises(PermissionError):
            rig.config_path.write_text("x", encoding="utf-8")
    finally:
        rig.deny_config_write(False)
    before = rig.config_path.read_text(encoding="utf-8")
    rig.config_path.write_text(before, encoding="utf-8")


def test_deny_refuses_when_lane_dir_escaped(rig, tmp_path):
    rig.config_path = tmp_path / "outside.yaml"                 # config path outside the lane
    rig.config_path.write_text("x")
    with pytest.raises(rigmod.RigError):
        rig.deny_config_write(True)


# ------------------------------------------------------------------------------------------------ fail_archive
def test_fail_archive_toggles_recorder_result(rig):
    import core.fy_rollover as fr

    with rig:                                                   # install(): seams
        rig.fail_archive(True)
        assert not fr.run_archive_transition("b", "FY90-91", "k")
        rig.fail_archive(False)
        assert fr.run_archive_transition("b", "FY90-91", "k")
        assert len(rig.seams.archive_calls) == 2                # the failing call was recorded too


# ------------------------------------------------------------------------------------------------ kill_child_after
def _spawn(folder: str, image: str = "rclone.exe"):
    """A harmless long-running process whose image name is `image` and whose path (so cmdline) is inside `folder`."""
    import shutil

    d = Path(folder)
    d.mkdir(parents=True, exist_ok=True)
    exe = d / image
    shutil.copy2(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe", exe)
    return subprocess.Popen([str(exe), "-n", "120", "127.0.0.1"], stdout=subprocess.DEVNULL)


def test_kill_lane_processes_kills_only_lane_cmdlines(rig):
    inside = _spawn(str(rig.lane_dir / "tools1"))
    outside = _spawn(str(rig.lane_dir.parent / "not-the-lane"))
    python_in_lane = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", str(rig.lane_dir)])
    try:
        time.sleep(0.5)
        pids = rig.kill_lane_processes()
        assert inside.pid in pids and outside.pid not in pids and python_in_lane.pid not in pids
        inside.wait(timeout=10)
        assert inside.poll() is not None
        assert outside.poll() is None
        assert python_in_lane.poll() is None               # only robocopy.exe / rclone.exe images may be killed
        assert os.getpid() not in pids
    finally:
        for p in (inside, outside, python_in_lane):
            if p.poll() is None:
                p.kill()


def test_kill_child_after_arms_and_fires_once_on_matching_step(rig):
    victim = _spawn(str(rig.lane_dir / "tools1"))
    try:
        time.sleep(0.5)
        rig.kill_child_after("final_backups")
        assert rig.kill_armed == "final_backups"
        assert rig.fire_step("other_step") == [] and victim.poll() is None
        assert victim.pid in rig.fire_step("final_backups")
        victim.wait(timeout=10)
        assert rig.kill_armed is None
        assert rig.fire_step("final_backups") == []             # disarmed
    finally:
        if victim.poll() is None:
            victim.kill()


# ------------------------------------------------------------------------------------------------ tamper
def test_tamper_nas_file_keeps_size_and_mtime_changes_bytes(rig):
    d = rig.lane_dir / "nas" / "backup" / NAS_FY
    f = d / "docs" / "a.bin"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"0123456789" * 10)
    os.utime(f, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    before = f.stat()
    got = rig.tamper_nas_file(NAS_FY)
    after = got.stat()
    assert got == f
    assert after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns
    assert f.read_bytes() != b"0123456789" * 10


def test_tamper_nas_file_never_picks_the_marker_or_other_lanes(rig):
    with pytest.raises(rigmod.RigError):                        # only the marker file exists
        rig.tamper_nas_file(NAS_FY)
    with pytest.raises(rigmod.RigError):
        rig.tamper_nas_file("..")


# ------------------------------------------------------------------------------------------------ approve / trigger
class FakeClient:
    def __init__(self, open_requests, post_status=200):
        self.open, self.post_status, self.posts = open_requests, post_status, []

    def get(self, url, **_k):
        assert url == "/approvals"
        return SimpleNamespace(status_code=200, json=lambda: {"approvals": self.open})

    def post(self, url, json=None, **_k):
        self.posts.append((url, json))
        return SimpleNamespace(status_code=self.post_status, json=lambda: {"ok": self.post_status == 200})


def test_approve_posts_phrase_and_appends_evidence_row(rig):
    c = FakeClient([{"approval_id": "ap1", "leg": "lan", "status": "open", "fy": "FY91-92"}])
    row = rig.approve("APPROVE", client=c)
    assert c.posts == [("/approvals/ap1/approve", {"phrase": "APPROVE"})]
    assert row == {"phrase": "APPROVE", "http_status": 200, "applied": True, "fy": "FY91-92", "leg": "lan",
                   "status": "open"}
    lines = (rig.evidence_dir / "approvals.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == [row]


def test_approve_wrong_phrase_rejected_is_recorded_not_applied(rig):
    c = FakeClient([{"approval_id": "ap1", "leg": "lan", "status": "open", "fy": "FY91-92"}], post_status=400)
    rig.approve("approve ", client=c)
    rig.approve("APPROVE", client=c)
    rows = [json.loads(x) for x in (rig.evidence_dir / "approvals.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["phrase"] for r in rows] == ["approve ", "APPROVE"]           # appended, never overwritten
    assert all(r["http_status"] == 400 and r["applied"] is False for r in rows)


def test_approve_without_open_request_records_none(rig):
    row = rig.approve("APPROVE", client=FakeClient([]))
    assert row["http_status"] is None and row["applied"] is False


def test_fire_trigger_returns_http_status(rig):
    c = FakeClient([], post_status=409)
    assert rig.fire_trigger("lan", client=c) == 409
    assert c.posts[0][0] == "/trigger/lan"


# ------------------------------------------------------------------------------------------------ set_mode / manual
@pytest.mark.parametrize("mode,cloud,lan", [("cloud", True, False), ("lan", False, True), ("both", True, True)])
def test_set_mode_uses_real_config_keys_and_stays_valid(rig, mode, cloud, lan):
    rig.set_mode(mode)
    c = rig.load_config()
    assert (c.cloud.enabled, c.lan.enabled) == (cloud, lan)
    assert _cfg_on_disk(rig)["cloud"]["enabled"] is cloud and rig.config["lan"]["enabled"] is lan


def test_set_mode_rejects_unknown(rig):
    with pytest.raises(ValueError):
        rig.set_mode("nas")


def test_set_lan_manual_toggles_scheduled(rig):
    rig.set_lan_manual(True)
    assert rig.load_config().lan.scheduled is False
    rig.set_lan_manual(False)
    assert rig.load_config().lan.scheduled is True


# ------------------------------------------------------------------------------------------------ clock
class FakeClock:
    def __init__(self, now):
        self.now, self.moves = now, []

    def move_to(self, instant):
        self.moves.append(instant)
        self.now = instant


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def test_clock_jump_and_set(rig):
    t0 = dt.datetime(2091, 4, 1, 6, 0, tzinfo=IST)
    rig.clock = FakeClock(t0)
    assert rig.clock_jump(dt.timedelta(hours=-3)) == t0 - dt.timedelta(hours=3)
    assert rig.clock_jump(dt.timedelta(days=2)) == t0 - dt.timedelta(hours=3) + dt.timedelta(days=2)
    target = dt.datetime(2091, 4, 5, 1, 0, tzinfo=IST)
    assert rig.clock_set(target) == target
    assert rig.clock.moves[-1] == target


def test_clock_knobs_need_a_clock(rig):
    rig.clock = None
    with pytest.raises(rigmod.RigError):
        rig.clock_jump(dt.timedelta(hours=1))
    with pytest.raises(rigmod.RigError):
        rig.clock_set(dt.datetime(2091, 4, 5, tzinfo=IST))
