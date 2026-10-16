"""M5 - a run whose process died is recorded ABANDONED and alerted, whatever its age.

Finding (M5 trial-box review, 2026-10-16): the RUNNING row was only turned
into *_ABANDONED at agent start, and only when older than
maintenance.abandon_running_after_hours (24 h). A Windows Update reboot at
03:00 during the 02:00 cloud run restarts the agent minutes later - the row
is 1 h old, so it stayed "Running" until some later agent restart, and no
alert was ever sent for the interrupted night. A flow-run process dying while
the agent stays up was never swept at all.

Root fix: the RUNNING row records its owner process ("pid:create_time", the
lock-file identity, safe against PID reuse); a row whose owner is provably
gone is ABANDONED (+ one FAILURE alert) at agent start AND at every pipeline
start. A live owner is never touched; an unknown owner keeps the age backstop.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pendulum
import psutil

import flow
from core import backup_repository as repo
from core import process
from core.manifest import ManifestDB
from core.time_utils import IST, now_iso
from models.config import MaintenanceConfig

REPO = Path(__file__).resolve().parents[1]


def _dead_token() -> str:
    """Token of a real process that has exited."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    ct = psutil.Process(p.pid).create_time()
    p.wait()
    return f"{p.pid}:{ct:.6f}"


def _rows(db) -> dict:
    return {r["run_id"]: r for r in db.get_recent_runs(50)}


# -- process identity ------------------------------------------------------

def test_token_alive_semantics():
    me = process.process_token()
    assert me.startswith(f"{os.getpid()}:")
    assert process.token_alive(me) is True
    assert process.token_alive(_dead_token()) is False
    pid, ct = me.split(":")
    assert process.token_alive(f"{pid}:{float(ct) - 1000:.6f}") is False   # PID reused
    for unknown in (None, "", "garbage", "123", "-5:1.0", "x:y"):
        assert process.token_alive(unknown) is None


def test_token_alive_access_denied_is_unknown_not_dead():
    with patch.object(process.psutil, "Process", side_effect=psutil.AccessDenied(4)):
        assert process.token_alive("4:123.0") is None


def test_lock_file_uses_the_same_identity(tmp_path):
    lock = tmp_path / "backup.lock"
    process.write_lock(lock)
    assert lock.read_text() == process.process_token()


# -- the sweep -------------------------------------------------------------

def test_abandon_dead_runs_by_owner_not_by_age(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    fresh = pendulum.now(IST).subtract(hours=1).isoformat()
    old = pendulum.now(IST).subtract(hours=30).isoformat()
    dead, live = _dead_token(), process.process_token()
    try:
        for run_id, mode, started, status, owner in (
            ("reboot", "cloud", fresh, "CLOUD_RUNNING", dead),   # old code: left RUNNING
            ("alive", "lan", fresh, "LAN_RUNNING", live),
            ("alive-old", "lan", old, "LAN_RUNNING", live),       # a live process is the truth
            ("legacy-fresh", "lan", fresh, "LAN_RUNNING", None),
            ("legacy-old", "cloud", old, "CLOUD_RUNNING", None),
            ("done", "lan", fresh, "LAN_COMPLETE", dead),
        ):
            db.insert_run({"run_id": run_id, "mode": mode, "started_at": started,
                           "status": status,
                           "extended_metrics": json.dumps({"owner": owner}) if owner else None})
        changed = db.abandon_dead_runs(process.token_alive, older_than_hours=24)
        assert sorted(r["run_id"] for r in changed) == ["legacy-old", "reboot"]
        rows = _rows(db)
        assert rows["reboot"]["status"] == "CLOUD_ABANDONED"
        assert "process ended" in rows["reboot"]["error_message"] and rows["reboot"]["ended_at"]
        assert rows["legacy-old"]["status"] == "CLOUD_ABANDONED"
        assert "24" in rows["legacy-old"]["error_message"]
        for keep, status in (("alive", "LAN_RUNNING"), ("alive-old", "LAN_RUNNING"),
                             ("legacy-fresh", "LAN_RUNNING"), ("done", "LAN_COMPLETE")):
            assert rows[keep]["status"] == status
        assert db.abandon_dead_runs(process.token_alive, older_than_hours=24) == []
    finally:
        db.close()


def _cfg(db_path):
    cfg = MagicMock()
    cfg.paths.database_path = db_path
    cfg.maintenance = MaintenanceConfig()
    return cfg


def test_sweep_sends_one_failure_alert_per_abandoned_run(tmp_path):
    db_path = str(tmp_path / "m.db")
    db = ManifestDB(db_path)
    try:
        db.insert_run({"run_id": "r1", "mode": "cloud", "started_at": now_iso(),
                       "status": "CLOUD_RUNNING",
                       "extended_metrics": json.dumps({"owner": _dead_token()})})
        with patch.object(repo, "notify") as notify:
            rows = repo.sweep_abandoned_runs(_cfg(db_path), db)
    finally:
        db.close()
    assert [r["run_id"] for r in rows] == ["r1"]
    (cfg, event), kwargs = notify.call_args
    assert notify.call_count == 1
    assert event.kind == "FAILURE" and event.key == "backup:cloud:CLOUD_ABANDONED"
    assert event.status == "CLOUD_ABANDONED" and "did not finish" in event.message


def test_pipeline_start_sweeps_then_writes_its_own_owned_row(tmp_path):
    """A flow-run process that died while the agent kept running: the next
    pipeline start records it (no agent restart needed)."""
    db_path = str(tmp_path / "m.db")
    db = ManifestDB(db_path)
    db.insert_run({"run_id": "crashed", "mode": "lan", "started_at": now_iso(),
                   "status": "LAN_RUNNING",
                   "extended_metrics": json.dumps({"owner": _dead_token()})})
    db.close()
    cfg = _cfg(db_path)
    with patch("core.backup_repository.notify") as notify, \
            patch("flow.ManifestDB.from_config", side_effect=lambda c: ManifestDB(db_path)):
        flow._record_running(cfg, "cloud", "new-run", now_iso())
    db = ManifestDB(db_path)
    try:
        rows = _rows(db)
    finally:
        db.close()
    assert rows["crashed"]["status"] == "LAN_ABANDONED"
    assert notify.call_count == 1
    assert rows["new-run"]["status"] == "CLOUD_RUNNING"
    assert json.loads(rows["new-run"]["extended_metrics"])["owner"] == process.process_token()


def test_sweep_failure_never_blocks_the_running_row(tmp_path):
    db_path = str(tmp_path / "m.db")
    with patch("flow.sweep_abandoned_runs", side_effect=OSError("db locked")), \
            patch("flow.ManifestDB.from_config", side_effect=lambda c: ManifestDB(db_path)):
        flow._record_running(_cfg(db_path), "lan", "run-x", now_iso())
    db = ManifestDB(db_path)
    try:
        assert db.last_run("lan")["status"] == "LAN_RUNNING"
    finally:
        db.close()


def _closed_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                       # nothing listens: every SMTP connect is refused
    return port


def test_mail_server_down_never_stops_the_backup_and_the_failed_alert_is_recorded(tmp_path):
    """Code review 2026-10-16 (verification): the sweep emails inline at every
    pipeline start. With the mail server down, the backup must still get its
    RUNNING row, and the undelivered alert must be recorded (FAILED) so the
    dashboard's undelivered-alerts banner shows it."""
    from models.config import AppConfig

    cfg = AppConfig(
        paths={"source_drive": str(tmp_path), "lan_destination": "\\\\nas\\backup\\FY26-27",
               "runtime_dir": str(tmp_path / "rt"), "gcs_key_path": str(tmp_path / "key.json")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"auth_enabled": False},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": _closed_port(),
                       "sender": "b@test.local", "recipients": ["ops@test.local"],
                       "smtp_security": "none", "smtp_auth": False, "retry_attempts": 3,
                       "retry_base_seconds": 0.01, "smtp_timeout_seconds": 2,
                       "send_on_failure": True},
    )
    db = ManifestDB.from_config(cfg)
    try:
        db.insert_run({"run_id": "crashed", "mode": "lan", "started_at": now_iso(),
                       "status": "LAN_RUNNING",
                       "extended_metrics": json.dumps({"owner": _dead_token()})})
    finally:
        db.close()

    flow._record_running(cfg, "lan", "new-run", now_iso())

    db = ManifestDB.from_config(cfg)
    try:
        rows = _rows(db)
        alert = db.last_notification("backup:lan:LAN_ABANDONED")
    finally:
        db.close()
    assert rows["crashed"]["status"] == "LAN_ABANDONED"
    assert rows["new-run"]["status"] == "LAN_RUNNING"          # the backup goes ahead
    assert alert["status"] == "FAILED" and alert["attempts"] == 3


def test_real_killed_run_is_abandoned_at_agent_start(tmp_path):
    """End to end with a real process: it writes its RUNNING row exactly as a
    pipeline does, then dies without an outcome (os._exit = killed/reboot).
    The agent start, minutes later, records it ABANDONED and alerts."""
    db_path = str(tmp_path / "m.db")
    child = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from core.manifest import ManifestDB\n"
        "from core.backup_repository import start_run_row\n"
        f"db = ManifestDB({db_path!r})\n"
        "start_run_row(db, run_id='killed', mode='cloud', started_at=sys.argv[1])\n"
        "db.close()\n"
        "os._exit(1)\n"
    )
    subprocess.run([sys.executable, "-c", child, now_iso()], check=False, timeout=120,
                   cwd=str(REPO))
    db = ManifestDB(db_path)
    try:
        assert db.last_run("cloud")["status"] == "CLOUD_RUNNING"
    finally:
        db.close()

    import launch
    with patch("models.config.load_config", return_value=_cfg(db_path)), \
            patch("core.manifest.ManifestDB.from_config", side_effect=lambda c: ManifestDB(db_path)), \
            patch("core.backup_repository.notify") as notify:
        launch._mark_abandoned_runs()
    db = ManifestDB(db_path)
    try:
        row = db.last_run("cloud")
    finally:
        db.close()
    assert row["status"] == "CLOUD_ABANDONED"
    assert notify.call_count == 1
