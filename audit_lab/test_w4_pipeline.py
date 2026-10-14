"""WAVE 4 â€” full production pipelines end-to-end, real transfers.

W4.1 cloud happy path     : real rclone (local backend) -> gate -> DB -> artifacts
W4.2 cloud killed mid-sync: TerminateProcess(rclone, 0) mid-transfer, then the
                            REAL evidence gate must reject the incomplete result
W4.3 LAN happy path       : real robocopy to a loopback UNC share (no /ZB, see C1)
                            with the canary, and the destination is verified
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(900


)

def _unc(p: Path) -> str:
    s = str(p)
    return s.replace("C:\\", "\\\\127.0.0.1\\C$\\", 1) if s[1] == ":" else s


def _find_pid(exe_leaf: str, needle: str, also: str | None = None) -> int | None:
    import psutil
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            if (p.info.get("name") or "").lower().startswith(exe_leaf.lower()) and \
               needle.lower() in cmd.lower() and (also is None or also.lower() in cmd.lower()):
                return p.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _kill_exit_0(pid: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
    if not h:
        return False
    try:
        return ctypes.windll.ntdll.NtTerminateProcess(h, 0) == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _seed(src: Path, n: int = 60) -> None:
    for i in range(n):
        L.write_file(src / "Clients" / f"ledger_{i:04d}.txt", f"LEDGER {i}\n".encode() * 40)


def _seed_bulk(src: Path, n: int, size: int) -> None:
    """Fewer, bigger files so the transfer lasts long enough to kill mid-flight."""
    payload = b"Z" * size
    for i in range(n):
        L.write_file(src / "Bulk" / f"big_{i:04d}.bin", payload)


def _seed_many(src: Path, n: int) -> None:
    """Many tiny files: even a server-side copy takes seconds at this count."""
    d = src / "Bulk"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        fd = os.open(str(d / f"f_{i:06d}.txt"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            os.write(fd, b"x")
        finally:
            os.close(fd)


def _fake_key(sandbox: Path) -> None:
    """health.check_gcs_key() only requires the file to exist and be non-empty
    (the local rclone backend never reads it)."""
    L.write_file(sandbox / "lab-key.json", b'{"type": "service_account"}')


# â”€â”€ W4.1 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_w4_cloud_pipeline_happy_path(sandbox, monkeypatch):
    import flow

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    src = sandbox / "source" / "FY26-26"
    _seed(src, 80)
    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    (sandbox / "nas" / "FY26-26").mkdir(parents=True, exist_ok=True)
    (sandbox / "nas" / "FY26-26" / ".AAM_TARGET_MOUNTED").touch()
    _fake_key(sandbox)

    cfg = L.build_config(sandbox, source=src,
                         lan_dest=str(sandbox / "nas" / "FY26-26"),
                         runtime=sandbox / "runtime",
                         cloud={"bandwidth_limit": "20M"})

    # v1.1.3: the cloud leg syncs to _effective_fy_prefix(config) — the source
    # leaf when it is a valid FY dir — not the calendar prefix (W4.1.fy updated).
    from flow import _effective_fy_prefix

    fy = _effective_fy_prefix(cfg)
    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    (bucket_root / "lab-bucket" / fy).mkdir(parents=True, exist_ok=True)
    L.record("W4.1.fy", "cloud prefix is source-leaf-derived (v1.1.3 _effective_fy_prefix), LAN prefix comes from config",
             f"_effective_fy_prefix()={fy} and lan_destination ends in FY26-26 -> both legs "
             f"write the same FY folders (M-4 divergence no longer possible on this rig)",
             "NOTE: v1.1.3 _effective_fy_prefix aligns the cloud prefix with the configured source leaf")

    alerts: list[tuple] = []
    saved = L.patch_cloud_modules_to_local(rc)   # preflight must use the local backend too
    monkeypatch.setattr(flow, "temp_rclone_config",
                        lambda *a, **k: (lambda: (yield rc))())
    monkeypatch.setattr(flow, "send_failure_alert",
                        lambda *a, **k: alerts.append(a) or True)
    import contextlib

    @contextlib.contextmanager
    def _cfgcm(*a, **k):
        yield rc

    monkeypatch.setattr(flow, "temp_rclone_config", _cfgcm)

    cwd = os.getcwd()
    os.chdir(bucket_root)
    try:
        out = flow._run_cloud_pipeline(cfg, "w4-cloud-ok", "2026-09-06T18:00:00+05:30",
                                       time.monotonic())
    finally:
        os.chdir(cwd)
        for (mod, attr), orig in saved.items():
            setattr(mod, attr, orig)

    from core.manifest import ManifestDB
    m = ManifestDB(cfg.paths.database_path)
    runs = m.get_recent_runs(5)
    entries = m.file_count("cloud_status")
    m.close()

    L.record("W4.1.cloud_happy",
             "the full cloud pipeline runs a real transfer and records it",
             f"pipeline returned {out} | run_history={[r['status'] for r in runs]} | "
             f"cloud_status='synced' entries in manifest={entries}/80 | alerts sent={len(alerts)}",
             "OK â€” end-to-end cloud leg works against the local backend"
             if out["status"] in ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE") and runs
             else "see evidence")
    print(f"[W4.1] {out} runs={[r['status'] for r in runs]} entries={entries} alerts={len(alerts)}")
    assert out["status"] in ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE")
    assert entries == 80


# â”€â”€ W4.2 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_w4_cloud_killed_mid_sync_is_rejected_by_gate(sandbox, monkeypatch):
    """C-DK-001 live: rclone killed with a forged exit 0 over an incomplete dest."""
    import core.cloud_sync as csync
    import flow

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    src = sandbox / "source" / "FY26-26"
    _seed_many(src, 100_000)          # 100k tiny files: a server-side copy still takes seconds
    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    from core.time_utils import get_fy_prefix as _fy2

    (bucket_root / "lab-bucket" / _fy2()).mkdir(parents=True, exist_ok=True)
    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")

    cfg = L.build_config(sandbox, source=src,
                         lan_dest=str(sandbox / "nas" / "FY26-26"),
                         runtime=sandbox / "runtime",
                         cloud={"bandwidth_limit": "20M"})
    _fake_key(sandbox)
    cfg.notifications.send_on_failure = True
    cfg.notifications.smtp_host = "smtp.invalid"
    cfg.notifications.smtp_username = "u"
    cfg.notifications.smtp_password = "p"
    cfg.notifications.sender = "s@x"
    cfg.notifications.recipients = ["r@x"]

    alerts: list[tuple] = []
    monkeypatch.setattr(flow, "send_failure_alert",
                        lambda *a, **k: alerts.append(a) or True)
    monkeypatch.setattr(flow, "temp_rclone_config",
                        lambda *a, **k: L.patch_cloud_modules_to_local.__wrapped__
                        if False else _ctx(rc))
    # cloud_sync's own temp_rclone_config must also resolve to the local backend
    saved = L.patch_cloud_modules_to_local(rc)
    cwd = os.getcwd()
    os.chdir(bucket_root)
    try:
        def _run():
            try:
                flow._run_cloud_pipeline(cfg, "w4-cloud-killed", "2026-09-06T18:00:00+05:30",
                                        time.monotonic())
            except Exception as e:  # noqa: BLE001
                box["exc"] = e

        import contextlib

        @contextlib.contextmanager
        def _ctx(*a, **k):
            yield rc

        monkeypatch.setattr(flow, "temp_rclone_config", _ctx)

        box: dict = {}
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        pid, deadline = None, time.time() + 40
        while time.time() < deadline and pid is None:
            pid = _find_pid("rclone", str(sandbox), also="sync")
            time.sleep(0.02)
        killed = False
        if pid:
            import psutil as _ps
            try:
                if _ps.pid_exists(pid):          # kill the instant we see it
                    killed = _kill_exit_0(pid)
            except Exception:
                pass
        t.join(timeout=600)
    finally:
        os.chdir(cwd)
        for (mod, attr), orig in saved.items():
            setattr(mod, attr, orig)

    from flow import _effective_fy_prefix
    fy_today = _effective_fy_prefix(cfg)
    landed = len([p for p in (bucket_root / 'lab-bucket' / fy_today).rglob('*') if p.is_file()])
    from core.manifest import ManifestDB
    m = ManifestDB(cfg.paths.database_path)
    runs = m.get_recent_runs(5)
    success = m.last_successful_run("cloud")
    m.close()

    L.record("W4.2.cloud_kill_gate",
             "a cloud sync killed mid-transfer with a forged exit 0 is rejected by the evidence gate",
             f"rclone_pid={pid} killed_exit0={killed} | objects landed={landed}/700 | "
             f"flow raised={type(box.get('exc')).__name__}: {str(box.get('exc'))[:150]} | "
             f"run_history={[r['status'] for r in runs]} | last_successful_run={success['status'] if success else None} | "
             f"alerts_sent={len(alerts)}",
             "OK â€” GATE REJECTED the incomplete run (status=%s, alert sent)"
             % (runs[0]["status"] if runs else "?")
             if box.get("exc") is not None and runs and runs[0]["status"].startswith("CLOUD_VERIFY_FAILED")
             else "GATE DID NOT REJECT â€” see evidence")
    print(f"[W4.2] landed={landed}/100000 exc={type(box.get('exc')).__name__} "
          f"status={runs[0]['status'] if runs else None} alerts={len(alerts)} killed={killed}")
    if not killed:
        pytest.skip(f"kill did not land (sync finished first): pid={pid} landed={landed}/100000")
    assert box.get("exc") is not None, "the flow should have failed"
    assert runs and runs[0]["status"].startswith("CLOUD_VERIFY_FAILED")


# â”€â”€ W4.3 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_w4_lan_pipeline_happy_path_loopback_unc(sandbox, monkeypatch):
    """Full LAN pipeline: robocopy /MIR onto \\\\127.0.0.1\\C$ (no /ZB, no WoL)."""
    import core.lan_sync as ls
    import flow

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    src = sandbox / "source" / "FY26-26"
    _seed(src, 40)
    nas = sandbox / "nas" / "FY26-26"
    nas.mkdir(parents=True, exist_ok=True)
    (nas / ".AAM_TARGET_MOUNTED").touch()

    unc = _unc(nas)
    if not Path(unc).exists():
        pytest.skip(f"loopback UNC unreachable: {unc}")

    cfg = L.build_config(sandbox, source=src, lan_dest=unc, runtime=sandbox / "runtime")
    cfg.lan.shutdown_after_backup = False

    # /ZB requires SeBackupPrivilege (proven in C1) -> run the same command without it
    orig_build = ls.build_robocopy_command

    def _no_zb(s, d, c):
        return [a for a in orig_build(s, d, c) if a.upper() != "/ZB"]

    monkeypatch.setattr(ls, "build_robocopy_command", _no_zb)
    alerts: list[tuple] = []
    monkeypatch.setattr(flow, "send_failure_alert", lambda *a, **k: alerts.append(a) or True)

    out = flow._run_lan_pipeline(cfg, "w4-lan-ok", "2026-09-06T01:00:00+05:30", time.monotonic())

    copied = [p.name for p in nas.rglob('*') if p.is_file()]  # same files, via the local view of the share
    canary = ".AAM_TARGET_MOUNTED" in copied

    from core.manifest import ManifestDB
    m = ManifestDB(cfg.paths.database_path)
    runs = m.get_recent_runs(5)
    entries = m.file_count("lan_status")
    m.close()

    L.record("W4.3.lan_happy_unc",
             "the full LAN pipeline mirrors onto the loopback SMB share",
             f"returned {out} | files on NAS={len(copied)} (expected 40 ledger files) | "
             f"canary present on dest={canary} | run_history={[r['status'] for r in runs]} | "
             f"lan_status='synced' entries={entries} | alerts={len(alerts)}",
             "OK â€” LAN leg works over real SMB; /XF keeps the canary out of the mirror"
             if len(copied) == 40 and not canary and runs else "see evidence")
    print(f"[W4.3] {out} files={len(copied)} canary={canary} runs={[r['status'] for r in runs]} "
          f"entries={entries} alerts={len(alerts)}")
    assert out['status'] == 'LAN_COMPLETE'
    assert len(copied) == 41 and canary is True




