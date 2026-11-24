r"""Red-first tests for round-2 review items of tools.livebox.rig (new tests only; nothing existing is edited).

PINNED API (all on tools.livebox.rig.LaneRig unless noted)
  set_nas(host)             host must be an IPv4 address inside 192.0.2.0/24 (TEST-NET-1) or a name ending in ".invalid"
                            (something before the dot); anything else (incl. 127.0.0.1, 10.x, *.local, bad octets,
                            share suffixes, "") -> RigError and the config (memory and disk) is unchanged.
                            set_nas(None) restores ONLY the host part (back to the lane's original host); the rest of
                            the UNC path is taken from the CURRENT config on disk, so a product FY switch survives.
  _save_config(mutate)      re-reads the YAML from disk before applying `mutate` (never works from the stale in-memory
                            copy); self.config is refreshed from what was written.
  deny_config_write(True)   blocks WRITE/DELETE only: the config can still be READ with open() (Windows only).
  revert_denies()           strips every deny this rig applied (src and config); idempotent; rig._denied == {} after.
                            remove() calls it, and run_lane.cleanup_lane strips denies below the lane before deleting
                            (a crashed process leaves the ACLs behind).
  kill_lane_processes()     only images robocopy.exe / rclone.exe (case-insensitive) whose cmdline contains the lane dir
                            (or that descend from a lane process); never python.exe / notepad.exe / other images, never
                            another lane's robocopy.
  Seams rclone config       the generated local-remote text has real keys type = alias and an absolute remote inside the
                            lane, and does not contain the literal text "type = local" anywhere (not even in a comment).
"""
from __future__ import annotations

import configparser
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.livebox import rig as rigmod

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="icacls is Windows only")


@pytest.fixture
def rig(tmp_path):
    r = rigmod.LaneRig(tmp_path / "stamp1" / "laneK").build()
    yield r
    r.remove()


def _disk(rig):
    import yaml

    return yaml.safe_load(rig.config_path.read_text(encoding="utf-8"))


def _edit_disk(rig, fn):
    """Simulate the product editing the config file behind the rig's back."""
    import yaml

    raw = _disk(rig)
    fn(raw)
    rig.config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


# ------------------------------------------------------------------------------------------------ set_nas hosts
@pytest.mark.parametrize("host", ["192.0.2.1", "192.0.2.254", "192.0.2.0", "nas1.invalid", "a.b.invalid", "NAS.INVALID"])
def test_set_nas_accepts_testnet_and_invalid_names(rig, host):
    rig.set_nas(host)
    assert rig.config["paths"]["lan_destination"].lower().startswith("\\\\" + host.lower() + "\\")
    assert _disk(rig)["paths"]["lan_destination"] == rig.config["paths"]["lan_destination"]


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "192.0.3.1", "192.0.20.1", "192.0.2.256", "192.0.2",
                                  "nas.local", "invalid", ".invalid", "evil.invalid.com", "192.0.2.1\\share", "",
                                  "8.8.8.8", "localhost"])
def test_set_nas_refuses_everything_else_and_changes_nothing(rig, host):
    before_mem, before_disk = rig.config["paths"]["lan_destination"], _disk(rig)["paths"]["lan_destination"]
    with pytest.raises(rigmod.RigError):
        rig.set_nas(host)
    assert rig.config["paths"]["lan_destination"] == before_mem
    assert _disk(rig)["paths"]["lan_destination"] == before_disk


def test_set_nas_none_restores_host_only_and_keeps_the_switched_config(rig):
    orig = rig.config["paths"]["lan_destination"]
    orig_host = orig.lstrip("\\").split("\\", 1)[0]
    rig.set_nas("192.0.2.9")

    def product_switch(raw):                      # the product moved the lane to the next FY while the NAS was "down"
        for key in ("source_drive", "lan_destination"):
            raw["paths"][key] = raw["paths"][key].replace("FY90-91", "FY91-92")

    _edit_disk(rig, product_switch)
    rig.set_nas(None)
    on_disk = _disk(rig)["paths"]
    assert on_disk["lan_destination"].lstrip("\\").split("\\", 1)[0] == orig_host
    assert "FY91-92" in on_disk["lan_destination"] and "FY90-91" not in on_disk["lan_destination"]
    assert "FY91-92" in on_disk["source_drive"]


def test_set_nas_host_change_after_a_switch_keeps_the_switched_rest(rig):
    def product_switch(raw):
        raw["paths"]["lan_destination"] = raw["paths"]["lan_destination"].replace("FY90-91", "FY91-92")

    _edit_disk(rig, product_switch)
    rig.set_nas("192.0.2.5")
    assert _disk(rig)["paths"]["lan_destination"].startswith("\\\\192.0.2.5\\")
    assert "FY91-92" in _disk(rig)["paths"]["lan_destination"]


# ------------------------------------------------------------------------------------------------ _save_config
def test_save_config_rereads_disk_so_a_knob_does_not_undo_a_switch(rig):
    def product_switch(raw):
        raw["paths"]["source_drive"] = raw["paths"]["source_drive"].replace("FY90-91", "FY91-92")

    _edit_disk(rig, product_switch)
    assert "FY90-91" in rig.config["paths"]["source_drive"]          # the in-memory copy is stale
    rig.set_mode("cloud")                                            # any knob that goes through _save_config
    assert "FY91-92" in _disk(rig)["paths"]["source_drive"]
    assert "FY91-92" in rig.config["paths"]["source_drive"]
    assert _disk(rig)["lan"]["enabled"] is False                     # and the knob itself still applied


def test_save_config_mutate_sees_the_disk_values(rig):
    _edit_disk(rig, lambda raw: raw["paths"].__setitem__("log_directory", str(rig.lane_dir / "runtime" / "marker")))
    seen = {}
    rig._save_config(lambda c: seen.update(v=c["paths"].get("log_directory")))
    assert seen["v"] == str(rig.lane_dir / "runtime" / "marker")


# ------------------------------------------------------------------------------------------------ denies
def _force_strip(rig):
    user = rig._acl_user()
    for p in (rig.config_path, rig.lane_dir / "src"):
        subprocess.run(["icacls", str(p), "/remove:d", user], capture_output=True, text=True)


@windows_only
def test_deny_config_write_blocks_writes_but_not_reads(rig):
    try:
        rig.deny_config_write(True)
        with open(rig.config_path, "r", encoding="utf-8") as fh:          # the product (and the harness) must still read it
            assert "paths" in fh.read()
        with pytest.raises(PermissionError):
            with open(rig.config_path, "a", encoding="utf-8") as fh:
                fh.write("# x\n")
    finally:
        _force_strip(rig)


@windows_only
def test_revert_denies_strips_both_denies(rig):
    try:
        rig.deny_config_write(True)
        rig.deny_source_parent_write(True)
        assert set(rig._denied) == {"config", "src"}
        rig.revert_denies()
        assert rig._denied == {}
        with open(rig.config_path, "a", encoding="utf-8") as fh:
            fh.write("# ok\n")
        (rig.lane_dir / "src" / "newdir").mkdir()
        rig.revert_denies()                                              # idempotent
    finally:
        _force_strip(rig)


@windows_only
def test_remove_calls_revert_denies(rig, monkeypatch):
    calls = []
    monkeypatch.setattr(rig, "revert_denies", lambda: calls.append(1), raising=False)
    rig.remove()
    assert calls, "LaneRig.remove() must call revert_denies()"


@windows_only
def test_cleanup_lane_strips_leftover_denies_of_a_crashed_process(rig):
    from tools.livebox import run_lane

    root = rig.lane_dir.parent.parent
    try:
        rig.deny_config_write(True)
        rig.deny_source_parent_write(True)
        rig._denied.clear()                      # the crashed process is gone: nobody remembers the denies
        run_lane.cleanup_lane(rig.lane_dir, root, keep_evidence=False)
        assert not rig.lane_dir.exists()
    finally:
        if rig.lane_dir.exists():
            _force_strip(rig)


# ------------------------------------------------------------------------------------------------ kill restriction
class _FakeProc:
    def __init__(self, pid, name, cmdline, ppid=1):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "cmdline": cmdline, "ppid": ppid}

    def parent(self):
        return None


def test_kill_lane_processes_only_robocopy_and_rclone_images(rig, monkeypatch):
    import psutil

    lane = str(rig.lane_dir)
    other = str(rig.lane_dir.parent / "laneOther")
    procs = [
        _FakeProc(900001, "robocopy.exe", ["robocopy", lane + "\\src", lane + "\\nas"]),
        _FakeProc(900002, "RCLONE.EXE", ["rclone", "sync", lane + "\\src", "aam_gcs:b"]),
        _FakeProc(900003, "python.exe", ["python", "-c", "x", lane]),                       # image not allowed
        _FakeProc(900004, "notepad.exe", ["notepad", lane + "\\notes.txt"]),                # image not allowed
        _FakeProc(900005, "robocopy.exe", ["robocopy", other + "\\src", other + "\\nas"]),  # another lane
        _FakeProc(900006, "powershell.exe", ["powershell", "-Command", "ls " + lane]),      # image not allowed
        _FakeProc(900007, "robocopy.exe", ["robocopy", "C:\\elsewhere", "D:\\x"]),          # no lane path
    ]
    taskkilled: list[int] = []

    def fake_run(cmd, *a, **k):
        if cmd and str(cmd[0]).lower().startswith("taskkill"):
            taskkilled.append(int(cmd[cmd.index("/PID") + 1]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rigmod.subprocess, "run", fake_run)
    killed = rig.kill_lane_processes()
    assert sorted(taskkilled) == [900001, 900002]
    assert sorted(killed) == [900001, 900002]


# ------------------------------------------------------------------------------------------------ rclone config
def test_generated_local_remote_is_a_real_alias_to_an_absolute_folder_inside_the_lane(tmp_path):
    lane = tmp_path / "stamp1" / "laneR"
    bucket = lane / "bucket"
    seams = rigmod.Seams(str(lane), str(bucket), None)
    text = seams.rclone_conf.read_text(encoding="utf-8")
    cp = configparser.RawConfigParser()
    cp.read_string(text)
    assert cp.sections() == ["aam_gcs"]
    assert cp["aam_gcs"]["type"] == "alias"
    remote = Path(cp["aam_gcs"]["remote"])
    assert remote.is_absolute()
    from tools.livebox import safety

    assert safety.is_under(remote, lane)
    assert "type = local" not in text, "no 'type = local' text, not even inside a comment"
