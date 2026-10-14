"""M2 2.4 — preflight permission checks (plan WP4, R7, gates CL3/CL13/LA7-LA9).

Each failure message names the missing right and how to fix it. These are
environment faults to repair, so they FAIL the run (never BLOCK it).
"""

import contextlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core import permissions as perm
from core.run_artefacts import RunArtefacts
from core.time_utils import now_iso
from core.tool_runner import ToolResult
from models.config import CloudConfig, LanConfig

# Real `whoami /priv /fo csv /nh` lines (Windows 11, 2026-10-15) + a held
# backup privilege as the service account shows it (state may be Disabled:
# robocopy enables a held privilege itself).
WHOAMI_NO_BACKUP = (
    '"SeShutdownPrivilege","Shut down the system","Disabled"\r\n'
    '"SeChangeNotifyPrivilege","Bypass traverse checking","Enabled"\r\n'
    '"SeUndockPrivilege","Remove computer from docking station","Disabled"\r\n'
)
WHOAMI_WITH_BACKUP_LOCALIZED = (
    '"SeBackupPrivilege","Sauvegarder les fichiers et les répertoires","Désactivé"\r\n'
    '"SeRestorePrivilege","Restaurer les fichiers et les répertoires","Désactivé"\r\n'
)


# ── Config ────────────────────────────────────────────────────────────────

def test_new_config_keys_and_defaults():
    lan, cloud = LanConfig(), CloudConfig(enabled=False)
    assert lan.use_backup_mode is True                       # DEC-8
    assert lan.min_free_dest_gb == 5
    assert lan.privilege_check_timeout_seconds == 60
    assert cloud.write_probe_enabled is True                 # DEC-5


def test_backup_mode_switch_controls_zb(tmp_path):
    from core.lan_sync import build_robocopy_command
    (tmp_path / "f.txt").write_text("x")
    assert "/ZB" in build_robocopy_command(str(tmp_path), "\\\\nas\\s", LanConfig())
    off = build_robocopy_command(str(tmp_path), "\\\\nas\\s", LanConfig(use_backup_mode=False))
    assert "/ZB" not in off and "/Z" in off


# ── SeBackupPrivilege ─────────────────────────────────────────────────────

def test_parse_privileges_reads_names_only_any_language():
    assert "SeBackupPrivilege" not in perm.parse_privileges(WHOAMI_NO_BACKUP)
    assert {"SeBackupPrivilege", "SeRestorePrivilege"} <= perm.parse_privileges(
        WHOAMI_WITH_BACKUP_LOCALIZED)
    with_header = '"Privilege Name","Description","State"\r\n' + WHOAMI_NO_BACKUP
    assert perm.parse_privileges(with_header) == perm.parse_privileges(WHOAMI_NO_BACKUP)


def _fake_tool(results):
    """Patch core.permissions.run_tool with one ToolResult per call."""
    calls = []

    def fake(name, argv, *, timeout_s, artefacts, log_path=None, env=None, cwd=None):
        spec = results[len(calls)]
        calls.append([str(a) for a in argv])
        out = artefacts.new_path(f"{name}.stdout", "txt")
        err = artefacts.new_path(f"{name}.stderr", "txt")
        out.write_text(spec.get("stdout", ""), encoding="utf-8")
        err.write_text(spec.get("stderr", ""), encoding="utf-8")
        timed_out = spec.get("timed_out", False)
        return ToolResult(name=name, argv=calls[-1],
                          exit_code=None if timed_out else spec.get("exit_code", 0),
                          timed_out=timed_out, spawn_error=None, stdout_path=out,
                          stderr_path=err, log_path=None, started_at=now_iso(),
                          ended_at=now_iso(), duration_s=0.0, timeout_s=timeout_s)
    return patch("core.permissions.run_tool", side_effect=fake), calls


def test_backup_privilege_held(tmp_path):
    p, calls = _fake_tool([{"stdout": WHOAMI_WITH_BACKUP_LOCALIZED}])
    with p:
        perm.check_backup_privilege(RunArtefacts(tmp_path, "r"), timeout_s=60)
    assert calls[0][0].lower().endswith(os.path.join("system32", "whoami.exe"))
    assert calls[0][1:] == ["/priv", "/fo", "csv", "/nh"]


def test_backup_privilege_missing_names_the_right_and_the_fix(tmp_path):
    p, _ = _fake_tool([{"stdout": WHOAMI_NO_BACKUP}])
    with p, pytest.raises(perm.PermissionCheckError) as ei:
        perm.check_backup_privilege(RunArtefacts(tmp_path, "r"), timeout_s=60)
    msg = str(ei.value)
    assert "SeBackupPrivilege" in msg and "secpol.msc" in msg
    assert "lan.use_backup_mode" in msg


@pytest.mark.parametrize("spec", [{"exit_code": 1, "stderr": "ERROR"}, {"timed_out": True},
                                  {"stdout": ""}])
def test_privilege_check_that_cannot_run_fails_closed(tmp_path, spec):
    p, _ = _fake_tool([spec])
    with p, pytest.raises(perm.PermissionCheckError, match="could not be checked"):
        perm.check_backup_privilege(RunArtefacts(tmp_path, "r"), timeout_s=60)


# ── Destination write probe ───────────────────────────────────────────────

def test_destination_write_probe_leaves_nothing_behind(tmp_path):
    perm.probe_destination_write(str(tmp_path), "run-1")
    assert list(tmp_path.iterdir()) == []


def test_destination_write_denied_names_the_error_and_the_fix(tmp_path):
    real_open = open

    def denied(path, mode="r", *a, **k):
        if "w" in mode:
            raise PermissionError(13, "Access is denied", str(path))
        return real_open(path, mode, *a, **k)

    with patch("builtins.open", denied), pytest.raises(perm.PermissionCheckError) as ei:
        perm.probe_destination_write(str(tmp_path), "run-1")
    msg = str(ei.value)
    assert "Access is denied" in msg and "service account" in msg and str(tmp_path) in msg


def test_destination_readback_mismatch_fails_and_cleans_up(tmp_path):
    with patch("core.permissions._PROBE_PAYLOAD", b"expected"), \
         patch("core.permissions._read_back", return_value=b"garbled"), \
         pytest.raises(perm.PermissionCheckError, match="read back"):
        perm.probe_destination_write(str(tmp_path), "run-1")
    assert list(tmp_path.iterdir()) == []


# ── Source read probe ─────────────────────────────────────────────────────

def test_source_read_probe_reads_a_real_file(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "ledger.xlsx").write_bytes(b"data")
    assert perm.probe_source_read(str(tmp_path)).endswith("ledger.xlsx")


def test_source_read_denied_fails(tmp_path):
    (tmp_path / "ledger.xlsx").write_bytes(b"data")
    with patch("builtins.open", side_effect=PermissionError(13, "Access is denied")), \
         pytest.raises(perm.PermissionCheckError, match="Access is denied"):
        perm.probe_source_read(str(tmp_path))


# ── Destination free space ────────────────────────────────────────────────

GB = 1024 ** 3


def test_free_space_ok_and_short(tmp_path):
    with patch("core.permissions.shutil.disk_usage", return_value=SimpleNamespace(free=10 * GB)):
        perm.check_destination_free_space(str(tmp_path), bytes_needed=2 * GB, min_free_gb=5)
    with patch("core.permissions.shutil.disk_usage", return_value=SimpleNamespace(free=6 * GB)), \
         pytest.raises(perm.PermissionCheckError) as ei:
        perm.check_destination_free_space(str(tmp_path), bytes_needed=2 * GB, min_free_gb=5)
    assert "6.0 GB free" in str(ei.value) and "7.0 GB" in str(ei.value)


def test_free_space_unmeasurable_fails(tmp_path):
    with patch("core.permissions.shutil.disk_usage", side_effect=OSError("WinError 53")), \
         pytest.raises(perm.PermissionCheckError, match="could not be measured"):
        perm.check_destination_free_space(str(tmp_path), bytes_needed=1, min_free_gb=5)
    with pytest.raises(perm.PermissionCheckError, match="could not be measured"):
        perm.check_destination_free_space(str(tmp_path), bytes_needed=None, min_free_gb=5)


# ── robocopy /L byte count ────────────────────────────────────────────────

def test_dry_run_uses_exact_bytes_and_excludes_probe_files(tmp_path):
    from core import lan_preflight
    from core.lan_sync import summarize_log_bytes

    # real robocopy 10.0 output with /BYTES (probe 2026-10-15)
    out = ("   Files :         2         2         0         0         0         1\n"
           "   Bytes :   3501234   3501234         0         0         0         2\n")
    assert summarize_log_bytes(out)["copied"] == 3501234
    assert summarize_log_bytes("   Bytes :    3.33 m    3.33 m   0  0  0  2\n") is None
    dest = tmp_path / "d"
    dest.mkdir()
    (dest / ".AAM_TARGET_MOUNTED").write_text("")
    with patch.object(lan_preflight.subprocess, "run",
                      return_value=MagicMock(returncode=1, stdout=out, stderr="")) as run:
        res = lan_preflight.run_lan_dry_run(str(tmp_path), str(dest))
    argv = run.call_args.args[0]
    assert "/BYTES" in argv and perm.PROBE_PATTERN in argv
    assert res["bytes"]["copied"] == 3501234


def test_sync_command_excludes_probe_files(tmp_path):
    from core.lan_sync import build_robocopy_command
    (tmp_path / "f.txt").write_text("x")
    assert perm.PROBE_PATTERN in build_robocopy_command(str(tmp_path), "\\\\n\\s", LanConfig())


# ── Cloud write probe ─────────────────────────────────────────────────────

def test_cloud_write_probe_touches_and_deletes_outside_the_fy_prefix(tmp_path):
    p, calls = _fake_tool([{}, {}])
    with p:
        perm.probe_cloud_write("bkt", "run-1", "/c.conf", 60, RunArtefacts(tmp_path, "r"))
    assert calls[0][1:3] == ["touch", "aam_gcs:bkt/_aam_preflight/probe_run-1"]
    assert calls[1][1:3] == ["deletefile", "aam_gcs:bkt/_aam_preflight/probe_run-1"]


def test_cloud_write_probe_names_the_cause(tmp_path):
    p, _ = _fake_tool([{"exit_code": 7, "stderr":
                        "Failed to touch: x509: certificate signed by unknown authority"}])
    with p, pytest.raises(perm.PermissionCheckError) as ei:
        perm.probe_cloud_write("bkt", "run-1", "/c.conf", 60, RunArtefacts(tmp_path, "r"))
    assert "TLS" in str(ei.value) or "intercepted" in str(ei.value)


def test_cloud_probe_delete_failure_fails(tmp_path):
    p, _ = _fake_tool([{}, {"exit_code": 7, "stderr": "403 Forbidden"}])
    with p, pytest.raises(perm.PermissionCheckError, match="delete"):
        perm.probe_cloud_write("bkt", "run-1", "/c.conf", 60, RunArtefacts(tmp_path, "r"))


# ── Pipeline wiring ───────────────────────────────────────────────────────

def test_lan_health_task_checks_privilege_only_in_backup_mode():
    import flow

    cfg = MagicMock()
    for mode_on, expected in ((True, 1), (False, 0)):
        cfg.lan.use_backup_mode = mode_on
        with patch("flow.pre_backup_health"), \
             patch("flow.permissions.check_backup_privilege") as chk:
            flow.health_check_task.fn(cfg, "lan", None)
        assert chk.call_count == expected
    with patch("flow.pre_backup_health"), patch("flow.permissions.check_backup_privilege") as chk:
        flow.health_check_task.fn(cfg, "cloud", None)
    chk.assert_not_called()


def test_lan_access_task_runs_all_three_checks(tmp_path):
    import flow

    cfg = MagicMock()
    cfg.paths.source_drive = "S"
    cfg.paths.lan_destination = "D"
    cfg.lan.min_free_dest_gb = 5
    with patch("flow.permissions") as p:
        flow.lan_access_task.fn(cfg, {"bytes": {"copied": 123}}, "run-1")
    p.probe_source_read.assert_called_once_with("S")
    p.probe_destination_write.assert_called_once_with("D", "run-1")
    p.check_destination_free_space.assert_called_once_with("D", bytes_needed=123, min_free_gb=5)


def test_cloud_access_task_respects_the_switch(tmp_path):
    import flow

    cfg = MagicMock()
    cfg.paths.source_drive = "S"
    cfg.cloud.bucket = "bkt"

    @contextlib.contextmanager
    def fake_cfg(*a, **k):
        yield "/c.conf"

    for enabled, expected in ((True, 1), (False, 0)):
        cfg.cloud.write_probe_enabled = enabled
        with patch("flow.permissions") as p, patch("flow.temp_rclone_config", fake_cfg):
            flow.cloud_access_task.fn(cfg, "run-1", None)
        p.probe_source_read.assert_called_once_with("S")
        assert p.probe_cloud_write.call_count == expected


def _pcfg():
    cfg = MagicMock()
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    cfg.lan.shutdown_on_all_retries_exhausted = False
    cfg.cloud.max_attempts = 2
    cfg.cloud.retry_delay_seconds = 60
    return cfg


@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.lan_sync_task")
@patch("flow.lan_access_task", side_effect=perm.PermissionCheckError("Access is denied"))
@patch("flow.lan_guards_task")
@patch("flow.lan_snapshot_before_task", return_value={})
@patch("flow.lan_preflight_task")
@patch("flow.wol_check_task")
@patch("flow.health_check_task")
@patch("flow._record_run")
def test_lan_access_failure_is_lan_failed_and_nothing_copied(
    rec, _h, _w, _pre, _snap, _g, _acc, sync, _art,
):
    import flow

    with pytest.raises(perm.PermissionCheckError):
        flow._run_lan_pipeline(_pcfg(), "run-p", now_iso())
    sync.with_options.return_value.assert_not_called()
    assert rec.call_args.args[4] == "LAN_FAILED"


@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.cloud_sync_task")
@patch("flow.cloud_access_task", side_effect=perm.PermissionCheckError("403"))
@patch("flow.cloud_guards_task")
@patch("flow.cloud_preflight_task")
@patch("flow.health_check_task")
@patch("flow.ManifestDB")
@patch("flow._record_run")
def test_cloud_access_failure_is_cloud_failed_and_nothing_synced(rec, _db, _h, _pre, _g, _acc,
                                                                 sync, _art):
    import flow

    with pytest.raises(perm.PermissionCheckError):
        flow._run_cloud_pipeline(_pcfg(), "run-c", now_iso())
    sync.with_options.return_value.assert_not_called()
    assert rec.call_args.args[4] == "CLOUD_FAILED"
