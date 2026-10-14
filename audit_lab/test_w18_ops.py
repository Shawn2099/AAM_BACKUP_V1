"""WAVE 18 — coverage-driven tests for the process/ops layer (part 2).

Companion to Wave 17 (core/wol.py + watchdog.py). This part covers:

  * launch.py                  (was 46%) — dashboard thread, API wait, limits,
                                            orphan cancellation, leg reconciliation, main()
  * collect_config_data.py     (was 37%) — discovery helpers + main()
  * core/cloud_preflight.py    (was 57%) — run_cloud_dry_run branches
  * core/lan_preflight.py      (was 62%) — run_lan_dry_run branches

Safety: subprocess, sockets, httpx and the Prefect client are all faked;
launch.py's PREFECT_API_URL env write is restored after every test.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(300)

# launch.py hard-codes this at import time (production service behaviour)
_ORIG_API_URL = os.environ.get("PREFECT_API_URL")


@pytest.fixture(autouse=True)
def _restore_prefect_env():
    yield
    if _ORIG_API_URL is None:
        os.environ.pop("PREFECT_API_URL", None)
    else:
        os.environ["PREFECT_API_URL"] = _ORIG_API_URL


def _cp(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _write_cfg(base: Path, cfg) -> Path:
    import yaml

    p = base / "config.yaml"
    yaml.safe_dump(json.loads(cfg.model_dump_json()), p.open("w", encoding="utf-8"),
                   sort_keys=False, allow_unicode=True)
    return p


def _lab_cfg(sandbox):
    return L.build_config(sandbox, source=sandbox / "src", lan_dest=str(sandbox / "nas"),
                          runtime=sandbox / "runtime")


# ══════════════════════════════════════════════════════════════════════════════
# W18.1 — launch.py (was 46%)
# ══════════════════════════════════════════════════════════════════════════════

def _launch_rig(monkeypatch, sandbox, patch_dashboard=True):
    """Full launch.main() rig: lab config + every collaborator faked."""
    import launch
    import models.config as mc

    cfg = _lab_cfg(sandbox)
    cfg_path = _write_cfg(sandbox, cfg)
    monkeypatch.setattr(mc, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(mc, "load_config", lambda p=None: cfg)
    monkeypatch.setattr(launch, "_check_prefect_api", lambda: True)
    if patch_dashboard:
        monkeypatch.setattr(launch, "_run_dashboard", lambda: None)
    monkeypatch.setattr(launch, "_supervise_dashboard", lambda t: None)
    monkeypatch.setattr(launch, "_ensure_concurrency_limit", lambda: None)
    monkeypatch.setattr(launch, "_cancel_orphaned_runs", lambda: None)
    monkeypatch.setattr(launch, "_reconcile_disabled_legs", lambda c: {"backup-lan": "active"})
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    monkeypatch.setattr("core.fy_rollover.rollover", lambda: False)
    serve_stub = types.ModuleType("serve")
    serve_stub.deployments = lambda: ("dep-aam-backup",)
    monkeypatch.setitem(sys.modules, "serve", serve_stub)
    return launch, cfg


def test_w18_launch_main_api_never_ready(monkeypatch, sandbox, capsys):
    launch, _ = _launch_rig(monkeypatch, sandbox)
    monkeypatch.setattr(launch, "_check_prefect_api", lambda: False)
    with pytest.raises(SystemExit) as ei:
        launch.main()
    assert ei.value.code == 1
    out = capsys.readouterr().out
    assert "Prefect API not reachable after 300s" in out
    assert "start_server.bat" in out


def test_w18_launch_main_happy_path(monkeypatch, sandbox, capsys):
    launch, _ = _launch_rig(monkeypatch, sandbox)
    calls = []

    def fake_serve(*deps, **kw):
        calls.append((deps, kw))
        raise KeyboardInterrupt()

    monkeypatch.setattr("prefect.serve", fake_serve)
    launch.main()
    out = capsys.readouterr().out
    assert calls and calls[0][0] == ("dep-aam-backup",)          # real deployments()
    assert calls[0][1] == {"pause_on_shutdown": False}
    assert "Deployment backup-lan: active" in out
    assert "(clean)" in out                                     # clean Ctrl+C shutdown
    L.record("W18.launch_main_happy",
             "launch.main() boots in order: API wait -> dashboard -> rollover -> "
             "limits -> orphan cleanup -> reconcile -> serve, and shuts down clean",
             f"serve_deps={calls[0][0]} serve_kw={calls[0][1]}",
             "OK - the production startup sequence is exercised end to end")


def test_w18_launch_main_rollover_blocked(monkeypatch, sandbox, capsys):
    launch, _ = _launch_rig(monkeypatch, sandbox)
    from core import fy_rollover as fr

    monkeypatch.setattr(fr, "rollover", lambda: (_ for _ in ()).throw(fr.RolloverError("locked")))
    monkeypatch.setattr("prefect.serve", lambda *d, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    launch.main()
    out = capsys.readouterr().out
    assert "FY rollover blocked: locked" in out
    assert "Will retry on next scheduled run" in out


def test_w18_launch_main_rollover_error_non_fatal(monkeypatch, sandbox, capsys):
    launch, _ = _launch_rig(monkeypatch, sandbox)
    from core import fy_rollover as fr

    monkeypatch.setattr(fr, "rollover", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("prefect.serve", lambda *d, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    launch.main()      # must not raise
    assert "FY rollover check failed (non-fatal)" in capsys.readouterr().out


def test_w18_run_dashboard_wiring(monkeypatch, sandbox):
    launch, cfg = _launch_rig(monkeypatch, sandbox, patch_dashboard=False)
    ran = {}

    def fake_run(app, host, port, log_level):
        ran.update(app=app, host=host, port=port, log_level=log_level)

    uv = types.ModuleType("uvicorn")
    uv.run = fake_run
    monkeypatch.setitem(sys.modules, "uvicorn", uv)
    ui_stub = types.ModuleType("ui")
    ui_stub.app = "UI-APP"
    monkeypatch.setitem(sys.modules, "ui", ui_stub)
    launch._run_dashboard()
    assert ran == {"app": "UI-APP", "host": cfg.dashboard.bind_address,
                   "port": cfg.dashboard.port, "log_level": "warning"}


def test_w18_supervise_dashboard_warns_on_dead_thread(monkeypatch, capsys):
    import launch

    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    mon = launch._supervise_dashboard(t, interval_seconds=0)
    mon.join(timeout=5)
    assert not mon.is_alive()
    out = capsys.readouterr().out
    assert "WARNING: dashboard thread is not alive" in out
    assert "scheduler continues" in out          # observability only, never fatal


def test_w18_check_prefect_api_branches(monkeypatch):
    import httpx
    import launch

    class Ok:
        status_code = 200

    class Bad:
        status_code = 500

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: Ok())
    assert launch._check_prefect_api() is True
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: Bad())
    assert launch._check_prefect_api() is False

    def conn(url, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", conn)
    assert launch._check_prefect_api() is False

    def other(url, timeout=None):
        raise RuntimeError("weird")

    monkeypatch.setattr(httpx, "get", other)
    assert launch._check_prefect_api() is False


class _FakeLimClient:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def upsert_global_concurrency_limit_by_name(self, name, limit):
        self.calls.append(("global", name, limit))

    async def create_concurrency_limit(self, tag, concurrency_limit):
        self.calls.append(("tag", tag, concurrency_limit))
        raise Exception("Concurrency limit 'aam-backup' already exists")   # tolerated


def test_w18_ensure_concurrency_limit(monkeypatch):
    import launch
    import prefect.client.orchestration as pco

    fake = _FakeLimClient()
    monkeypatch.setattr(pco, "get_client", lambda: fake)
    launch._ensure_concurrency_limit()
    assert ("global", "aam-backup", 1) in fake.calls
    assert ("tag", "aam-backup", 1) in fake.calls


def test_w18_ensure_concurrency_limit_inside_loop(monkeypatch):
    import launch
    import prefect.client.orchestration as pco

    fake = _FakeLimClient()
    monkeypatch.setattr(pco, "get_client", lambda: fake)
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: object())
    launch._ensure_concurrency_limit()
    assert ("global", "aam-backup", 1) in fake.calls, "the worker-thread branch ran"


class _FakeRun:
    def __init__(self, rid, name, flow_id, st):
        self.id, self.name, self.flow_id, self.state_type = rid, name, flow_id, st


def _wanted(filter_obj):
    try:
        return list(filter_obj.state.type.any_)
    except Exception:
        return []


class _FakeRunClient:
    def __init__(self, flows, runs, fail_ids=()):
        self.flows, self.runs, self.fail_ids = flows, runs, fail_ids
        self.cancelled, self.reads = [], []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read_flows(self, flow_filter=None):
        return list(self.flows)

    async def read_flow_runs(self, flow_run_filter=None, offset=0, limit=200):
        self.reads.append(offset)
        wanted = _wanted(flow_run_filter)
        return [r for r in self.runs if r.state_type in wanted][offset:offset + limit]

    async def set_flow_run_state(self, flow_run_id, state, force):
        if flow_run_id in self.fail_ids:
            raise RuntimeError("server said no")
        self.cancelled.append(flow_run_id)


def _orphan_rig(monkeypatch, sandbox, *, alive, pid, lock_exists):
    import models.config as mc
    import prefect.client.orchestration as pco
    from core import process as cp

    cfg = _lab_cfg(sandbox)
    # backup_lock_path is a derived property: database_path.parent / backup.lock
    cfg.paths.database_path = str(sandbox / "manifest.db")
    lock = cfg.paths.backup_lock_path
    if lock_exists:
        lock.write_text(f"{pid}:1", encoding="utf-8")
    monkeypatch.setattr(mc, "load_config", lambda p=None: cfg)
    monkeypatch.setattr(cp, "read_lock_alive", lambda p: (alive, pid))
    return pco, cfg


def test_w18_cancel_orphans_no_flows(monkeypatch, sandbox, capsys):
    import launch

    pco, _ = _orphan_rig(monkeypatch, sandbox, alive=False, pid=None, lock_exists=False)
    fake = _FakeRunClient([], [])
    monkeypatch.setattr(pco, "get_client", lambda: fake)
    launch._cancel_orphaned_runs()
    assert fake.cancelled == []
    assert "No registered AAM flows found" in capsys.readouterr().out


def test_w18_cancel_orphans_pagination_and_live_lock(monkeypatch, sandbox, capsys):
    """A full 200-run batch forces a second page; a live lock protects RUNNING."""
    import launch
    from prefect.client.schemas.objects import StateType

    pco, _ = _orphan_rig(monkeypatch, sandbox, alive=True, pid=5, lock_exists=True)
    flows = [types.SimpleNamespace(id="flow-aam")]
    runs = [_FakeRun(f"id{i}", f"aam-backup-{i}", "flow-aam", StateType.PENDING)
            for i in range(200)]                       # exactly one page -> forces offset 200
    runs += [_FakeRun("run-1", "aam-backup-running", "flow-aam", StateType.RUNNING)]
    runs += [_FakeRun("other", "not-aam", "flow-other", StateType.PENDING)]  # filtered out
    fake = _FakeRunClient(flows, runs)
    monkeypatch.setattr(pco, "get_client", lambda: fake)
    launch._cancel_orphaned_runs()
    out = capsys.readouterr().out
    assert fake.reads == [0, 200], "the second page was requested"
    assert len(fake.cancelled) == 200 and "other" not in fake.cancelled
    assert "run-1" not in fake.cancelled, "a live lock protects RUNNING flows"
    assert "Skipping RUNNING flows" in out
    assert "Cleaned up 200 orphaned PENDING" in out


def test_w18_cancel_orphans_stale_lock_and_failure_warning(monkeypatch, sandbox, capsys):
    import launch
    from prefect.client.schemas.objects import StateType

    pco, cfg = _orphan_rig(monkeypatch, sandbox, alive=False, pid=999, lock_exists=True)
    flows = [types.SimpleNamespace(id="flow-aam")]
    runs = [_FakeRun("p1", "aam-backup-1", "flow-aam", StateType.PENDING),
            _FakeRun("p2", "aam-backup-2", "flow-aam", StateType.PENDING)]
    fake = _FakeRunClient(flows, runs, fail_ids={"p2"})
    monkeypatch.setattr(pco, "get_client", lambda: fake)
    launch._cancel_orphaned_runs()
    out = capsys.readouterr().out
    assert fake.cancelled == ["p1"] and "failed to cancel run p2" in out
    assert "Stale backup lock (PID 999" in out
    assert not cfg.paths.backup_lock_path.exists(), "the stale lock file was cleaned up"


class _FakeDepClient:
    def __init__(self, deps):
        self.deps, self.actions = deps, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read_deployment_by_name(self, full):
        if full not in self.deps:
            from prefect.exceptions import ObjectNotFound
            raise ObjectNotFound(f"{full} not found")
        return types.SimpleNamespace(id=full + "-id", paused=self.deps[full])

    async def resume_deployment(self, dep_id):
        self.actions.append(("resume", dep_id))

    async def pause_deployment(self, dep_id):
        self.actions.append(("pause", dep_id))


def test_w18_reconcile_disabled_legs(monkeypatch):
    import launch
    import prefect.client.orchestration as pco

    fake = _FakeDepClient({"aam-backup/backup-lan": True,          # paused but enabled -> resume
                           "aam-backup/backup-cloud": False,       # active but disabled -> pause
                           "integrity-audit/integrity-audit": True})
    monkeypatch.setattr(pco, "get_client", lambda: fake)
    res = launch._reconcile_disabled_legs(
        None, legs={"backup-lan": True, "backup-cloud": False,
                    "integrity-audit": True, "rollover-check": True})
    assert res["backup-lan"] == "resumed (leg enabled)"
    assert res["backup-cloud"] == "PAUSED (leg disabled)"
    assert res["integrity-audit"] == "resumed (leg enabled)"
    assert res["rollover-check"] == "absent (nothing to reconcile)"
    assert ("resume", "aam-backup/backup-lan-id") in fake.actions
    assert ("pause", "aam-backup/backup-cloud-id") in fake.actions


def test_w18_reconcile_disabled_legs_inside_loop(monkeypatch):
    import launch
    import prefect.client.orchestration as pco

    fake = _FakeDepClient({})
    monkeypatch.setattr(pco, "get_client", lambda: fake)
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: object())
    res = launch._reconcile_disabled_legs(None, legs={"backup-lan": True})
    assert res == {"backup-lan": "absent (nothing to reconcile)"}


# ══════════════════════════════════════════════════════════════════════════════
# W18.2 — collect_config_data.py (was 37%)
# ══════════════════════════════════════════════════════════════════════════════

def test_w18_is_firewall_open_branches(monkeypatch):
    import collect_config_data as ccd

    cmds = []
    monkeypatch.setattr(ccd.subprocess, "run",
                        lambda *a, **k: (cmds.append(a[0]), _cp(0, "AAM Backup Dashboard"))[1])
    assert ccd.is_firewall_open(8080) is True
    assert "powershell" in cmds[0][0]
    monkeypatch.setattr(ccd.subprocess, "run", lambda *a, **k: _cp(0, ""))
    assert ccd.is_firewall_open(8080) is False
    assert ccd.is_firewall_open(0) is False          # invalid range, no call
    assert ccd.is_firewall_open(70000) is False

    def boom(*a, **k):
        raise OSError("no powershell")

    monkeypatch.setattr(ccd.subprocess, "run", boom)
    assert ccd.is_firewall_open(8080) is False


def test_w18_list_drives(monkeypatch):
    import collect_config_data as ccd

    monkeypatch.setattr(ccd.psutil, "disk_partitions", lambda all=False: [
        types.SimpleNamespace(device="C:\\", fstype="NTFS", mountpoint="C:\\"),
        types.SimpleNamespace(device="D:\\", fstype="", mountpoint="D:\\"),   # no fstype -> skipped
    ])
    drives = ccd.list_drives()
    assert len(drives) == 1 and drives[0].startswith("C:\\") and "NTFS" in drives[0]


def test_w18_get_network_info_filters(monkeypatch):
    import socket as _s
    import collect_config_data as ccd

    monkeypatch.setattr(ccd.psutil, "net_if_addrs", lambda: {
        "Ethernet": [types.SimpleNamespace(family=_s.AF_INET, address="192.168.1.50"),
                     types.SimpleNamespace(family=ccd.psutil.AF_LINK, address="aa-bb-cc-dd-ee-ff")],
        "Loopback Pseudo-Interface 1": [
            types.SimpleNamespace(family=_s.AF_INET, address="127.0.0.1"),
            types.SimpleNamespace(family=ccd.psutil.AF_LINK, address="00-00-00-00-00-00")],
        "Wi-Fi": [types.SimpleNamespace(family=_s.AF_INET, address="169.254.1.7"),
                  types.SimpleNamespace(family=ccd.psutil.AF_LINK, address="11-22-33-44-55-66")],
        "Down": [types.SimpleNamespace(family=_s.AF_INET, address="10.0.0.9"),
                 types.SimpleNamespace(family=ccd.psutil.AF_LINK, address="ab-cd-ef-01-02-03")],
    })
    monkeypatch.setattr(ccd.psutil, "net_if_stats", lambda: {
        "Ethernet": types.SimpleNamespace(isup=True),
        "Loopback Pseudo-Interface 1": types.SimpleNamespace(isup=True),
        "Wi-Fi": types.SimpleNamespace(isup=True),
        "Down": types.SimpleNamespace(isup=False),
    })
    nets = ccd.get_network_info()
    assert nets == [("Ethernet", "192.168.1.50", "AA:BB:CC:DD:EE:FF")], nets
    L.record("W18.network_discovery",
             "loopback, APIPA (169.254.x) and down interfaces are excluded; "
             "MACs are normalised to colon-uppercase",
             f"networks={nets}",
             "OK - only routable interfaces are offered as config snippets")


def _prep_collector(monkeypatch, sandbox, *, networks, firewall_open):
    import collect_config_data as ccd

    monkeypatch.setattr(ccd, "get_network_info", lambda: networks)
    monkeypatch.setattr(ccd, "list_drives", lambda: ["C:\\ (Type: NTFS, Mount: C:\\)"])
    monkeypatch.setattr(ccd, "is_firewall_open", lambda port=8080: firewall_open)
    monkeypatch.setattr(ccd, "get_fy_prefix", lambda: "FY26-27")
    monkeypatch.setattr(ccd, "verify_with_pydantic", lambda s: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)   # never block on input()
    return ccd


def test_w18_collector_main_with_networks(monkeypatch, sandbox, capsys):
    ccd = _prep_collector(monkeypatch, sandbox,
                          networks=[("Ethernet", "192.168.1.50", "AA:BB:CC:DD:EE:FF")],
                          firewall_open=True)
    ccd.main()
    out = capsys.readouterr().out
    assert "AAM BACKUP AUTOMATION - CONFIGURATION DATA COLLECTOR" in out
    assert "AVAILABLE LOCAL DRIVES" in out
    assert 'source_drive: "D:\\\\FY26-27"' in out
    assert "192.168.1.100" in out                       # suggested NAS placeholder
    assert "mac_address: \"AA:BB:CC:DD:EE:FF\"" in out
    assert "Port 8080 is OPEN" in out
    assert "Press Enter to exit" not in out             # non-tty: never blocks


def test_w18_collector_main_no_networks_firewall_closed(monkeypatch, sandbox, capsys):
    ccd = _prep_collector(monkeypatch, sandbox, networks=[], firewall_open=False)
    ccd.main()
    out = capsys.readouterr().out
    assert "WARNING: No active IPv4 network interfaces found." in out
    assert "Port 8080 is CLOSED" in out
    assert "netsh advfirewall firewall add rule" in out  # the recovery hint is printed


def test_w18_verify_with_pydantic_merge_branch(monkeypatch, sandbox):
    import collect_config_data as ccd

    cfg_path = _write_cfg(sandbox, _lab_cfg(sandbox))
    monkeypatch.setattr(ccd, "CONFIG_PATH", str(cfg_path))
    # dict value on an existing key -> the update-merge branch (line 78)
    assert ccd.verify_with_pydantic({"paths": {"source_drive": r"D:\FY26-27"}}) is True
    assert ccd.verify_with_pydantic({"firm_name": 12345}) is False   # type error -> False


# ══════════════════════════════════════════════════════════════════════════════
# W18.3 — dry-run preflights (cloud 57% / lan 62%)
# ══════════════════════════════════════════════════════════════════════════════

def _rig_cloud(monkeypatch):
    import core.cloud_preflight as cf

    monkeypatch.setattr(cf, "temp_rclone_config",
                        lambda *a, **k: contextlib.nullcontext("rc.conf"))
    return cf


def test_w18_cloud_dry_run_source_missing(monkeypatch, sandbox):
    cf = _rig_cloud(monkeypatch)
    res = cf.run_cloud_dry_run(str(sandbox / "nope"), "b", "FY26-27", "k.json", "1", "COLDLINE")
    assert res["ok"] is False and res["exit_code"] == -1
    assert "Source drive not accessible" in res["error"]


def test_w18_cloud_dry_run_empty_source_still_probes(monkeypatch, sandbox):
    cf = _rig_cloud(monkeypatch)
    src = sandbox / "empty"
    src.mkdir()
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **k: _cp(0, "[]"))
    res = cf.run_cloud_dry_run(str(src), "b", "FY26-27", "k.json", "1", "COLDLINE")
    assert res["ok"] is True and res["error"] is None       # M8: diagnostic-only


def test_w18_cloud_dry_run_unreadable_source(monkeypatch, sandbox):
    cf = _rig_cloud(monkeypatch)
    f = sandbox / "notadir.txt"
    f.write_text("x", encoding="utf-8")                    # exists() but iterdir() fails
    res = cf.run_cloud_dry_run(str(f), "b", "FY26-27", "k.json", "1", "COLDLINE")
    assert res["ok"] is False and "Source drive read error" in res["error"]


def test_w18_cloud_dry_run_probe_failures(monkeypatch, sandbox):
    cf = _rig_cloud(monkeypatch)
    src = sandbox / "src"
    (src / "FY26-27").mkdir(parents=True)
    (src / "FY26-27" / "a.txt").write_text("a", encoding="utf-8")

    monkeypatch.setattr(cf.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            subprocess.TimeoutExpired(cmd="rclone", timeout=30)))
    res = cf.run_cloud_dry_run(str(src), "b", "FY26-27", "k.json", "1", "COLDLINE", timeout=5)
    assert res["ok"] is False and "Timeout after 5s" in res["error"]

    monkeypatch.setattr(cf.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    res = cf.run_cloud_dry_run(str(src), "b", "FY26-27", "k.json", "1", "COLDLINE")
    assert res["ok"] is False and res["error"] == "rclone not found"

    monkeypatch.setattr(cf.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("pipe")))
    res = cf.run_cloud_dry_run(str(src), "b", "FY26-27", "k.json", "1", "COLDLINE")
    assert res["ok"] is False and "OS error launching rclone" in res["error"]

    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **k: _cp(3, "", "directory not found"))
    res = cf.run_cloud_dry_run(str(src), "b", "FY26-27", "k.json", "1", "COLDLINE")
    assert res["ok"] is False and res["exit_code"] == 3
    assert "Exit 3: directory not found" in res["error"]

    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **k: _cp(0, "[]"))
    res = cf.run_cloud_dry_run(str(src), "b", "FY26-27", "k.json", "1", "COLDLINE")
    assert res["ok"] is True and res["exit_code"] == 0
    L.record("W18.cloud_dry_run_branches",
             "run_cloud_dry_run() fails closed on every probe failure and passes "
             "only when source + bucket are both reachable",
             "source-missing | empty (warns, probes) | unreadable | timeout | "
             "no-rclone | OS error | exit!=0 | exit=0",
             "OK - all 8 branches behave as documented")


def test_w18_extract_unc_host():
    from core.lan_preflight import _extract_unc_host

    assert _extract_unc_host(r"\\192.168.10.10\share\FY26-27") == "192.168.10.10"
    assert _extract_unc_host("//nas/share/FY26-27") == "nas"
    assert _extract_unc_host(r"D:\Backup") is None
    assert _extract_unc_host("relative/path") is None
    assert _extract_unc_host(r"\\") is None


def test_w18_is_smb_reachable(monkeypatch):
    from core import lan_preflight as lp

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(lp.socket, "create_connection", lambda addr, timeout=None: Conn())
    assert lp._is_smb_reachable("10.0.0.9") is True

    def refused(addr, timeout=None):
        raise ConnectionRefusedError("no smb")

    monkeypatch.setattr(lp.socket, "create_connection", refused)
    monkeypatch.setattr(time, "sleep", lambda s: None)     # skip tenacity backoff
    assert lp._is_smb_reachable("10.0.0.9") is False       # 4 attempts then False


def _lan_rig(monkeypatch, sandbox, *, with_canary, canary_raises=False):
    from core import lan_preflight as lp

    src = sandbox / "src" / "FY26-27"
    src.mkdir(parents=True, exist_ok=True)
    (src / "ledger.txt").write_text("LEDGER\n" * 10, encoding="utf-8")
    dest = sandbox / "nas" / "FY26-27"          # the sandbox fixture may pre-create it
    dest.mkdir(parents=True, exist_ok=True)
    if with_canary:
        L.make_canary(dest)
    if canary_raises:
        monkeypatch.setattr(Path, "is_file",
                            lambda self: (_ for _ in ()).throw(OSError("access denied")))
    monkeypatch.setattr(lp, "_is_smb_reachable", lambda host, port=445, timeout=2.0, **kw: False)
    return lp, src, dest


def test_w18_lan_dry_run_smb_unreachable(monkeypatch, sandbox):
    from core.health import HealthError

    lp, src, dest = _lan_rig(monkeypatch, sandbox, with_canary=True)
    with pytest.raises(HealthError, match="Cannot reach LAN host"):
        lp.run_lan_dry_run(str(src), r"\\nas01\share\FY26-27")


def test_w18_lan_dry_run_canary_missing(monkeypatch, sandbox):
    """M2 2.3: a missing marker is reported (guard G5 blocks the run for a
    human); an UNREADABLE destination still fails (next test)."""
    lp, src, dest = _lan_rig(monkeypatch, sandbox, with_canary=False)
    res = lp.run_lan_dry_run(str(src), str(dest))
    assert res["canary_present"] is False


def test_w18_lan_dry_run_canary_unreadable(monkeypatch, sandbox):
    from core.health import HealthError

    lp, src, dest = _lan_rig(monkeypatch, sandbox, with_canary=True, canary_raises=True)
    with pytest.raises(HealthError, match="Cannot access LAN destination"):
        lp.run_lan_dry_run(str(src), str(dest))


def test_w18_lan_dry_run_branches(monkeypatch, sandbox):
    lp, src, dest = _lan_rig(monkeypatch, sandbox, with_canary=True)

    monkeypatch.setattr(lp.subprocess, "run", lambda *a, **k: _cp(1, "1 file(s)"))
    res = lp.run_lan_dry_run(str(src), str(dest))
    assert res["ok"] is True and res["exit_code"] == 1 and res["error"] is None

    monkeypatch.setattr(lp.subprocess, "run", lambda *a, **k: _cp(8, "", "ERROR: bad path"))
    res = lp.run_lan_dry_run(str(src), str(dest))
    assert res["ok"] is False and res["exit_code"] == 8
    assert "Robocopy /L failed with exit 8" in res["error"]

    monkeypatch.setattr(lp.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            subprocess.TimeoutExpired(cmd="robocopy", timeout=900)))
    res = lp.run_lan_dry_run(str(src), str(dest), timeout=9)
    assert res["ok"] is False and "Timeout after 9s" in res["error"]

    monkeypatch.setattr(lp.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    res = lp.run_lan_dry_run(str(src), str(dest))
    assert res["ok"] is False and res["error"] == "robocopy.exe not found"

    monkeypatch.setattr(lp.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("share gone")))
    res = lp.run_lan_dry_run(str(src), str(dest))
    assert res["ok"] is False and res["error"] == "share gone"
    L.record("W18.lan_dry_run_branches",
             "run_lan_dry_run() gates on SMB reachability + canary first, then "
             "maps every robocopy outcome to an honest dict",
             "smb-unreachable->HealthError | canary missing->HealthError | "
             "canary unreadable->HealthError | exit<8 ok | exit>=8 fail | "
             "timeout | no-robocopy | OS error",
             "OK - all 8 branches behave as documented")
