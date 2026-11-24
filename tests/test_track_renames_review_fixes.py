"""Opus review fixes for the shared rename-aware G3 measurement (2026-11-25).

Real rclone where it matters (deploy/bin/rclone.exe or PATH); skipped without a binary.
"""
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import flow
from core import faults
from core import guards as g
from core.cloud_sync import count_planned_moves, final_stats, measure_planned_deletes
from models.config import GuardsConfig, LanConfig

_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED = _ROOT / "deploy" / "bin" / "rclone.exe"
_RCLONE = str(_BUNDLED) if _BUNDLED.exists() else shutil.which("rclone")
needs_rclone = pytest.mark.skipif(not _RCLONE, reason="rclone binary not available")


@pytest.fixture
def local_rclone():
    with patch("core.cloud_sync.resolve_binary", return_value=_RCLONE):
        yield


def _mirror(tmp_path, n=5):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "Client_000" / "Invoices").mkdir(parents=True)
    dst.mkdir()
    for i in range(n):
        (src / "Client_000" / "Invoices" / f"inv{i}.txt").write_text(f"invoice number {i}")
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return src, dst


def _measure(src, dst, **kw):
    return measure_planned_deletes(str(src), str(dst), modify_window_seconds=2, **kw)


def _lan_cfg(tmp_path, with_file=True):
    src = tmp_path / "lsrc"
    src.mkdir(exist_ok=True)
    if with_file:
        (src / "a.txt").write_text("x")
    cfg = MagicMock()
    cfg.paths.source_drive, cfg.paths.lan_destination = str(src), "\\\\nas\\share"
    cfg.cloud.track_renames = True
    cfg.lan.track_renames = True
    cfg.lan.rclone_measure_timeout_seconds = 1234
    cfg.guards = GuardsConfig()
    return cfg


def _preflight(cfg, robocopy_deletes, measured):
    robocopy = {"ok": True, "planned_deletes": robocopy_deletes,
                "counts": {"extras": robocopy_deletes}}
    with patch("flow.run_lan_dry_run", return_value=robocopy), \
         patch("flow.measure_planned_deletes", return_value=measured) as m:
        return flow.lan_preflight_task.fn(cfg), m


def test_count_planned_moves_reads_rclone_move_lines():
    log = ('{"skipped":"move to b/f1.txt"}\n{"skipped":"delete"}\nnot json\n'
           '{"skipped":"move to b/f2.txt"}\n')
    assert count_planned_moves(log) == 2 and count_planned_moves("") == 0


@needs_rclone
def test_measure_reports_rename_moves(tmp_path, local_rclone):
    src, dst = _mirror(tmp_path)
    (src / "Client_000" / "Invoices").rename(src / "Client_000" / "Invoices_r1")
    res = _measure(src, dst)
    assert res["planned_deletes"] == 0 and res["moves"] == 5


# 1. LAN invariant
def test_lan_preflight_fails_closed_when_robocopy_deletes_exceed_rclone_plus_moves(tmp_path):
    cfg = _lan_cfg(tmp_path)
    with pytest.raises(RuntimeError, match="disagree") as ei:
        _preflight(cfg, 316, {"planned_deletes": 0, "moves": 3, "error": None})
    assert faults.code_of(ei.value) == "AAM-LAN-01"
    res, _ = _preflight(cfg, 316, {"planned_deletes": 0, "moves": 316, "error": None})
    assert res["planned_deletes"] == 0 and res["rclone_moves"] == 316
    assert res["robocopy_planned_deletes"] == 316
    res, _ = _preflight(cfg, 12, {"planned_deletes": 10, "moves": 2, "error": None})
    assert res["planned_deletes"] == 10


def test_lan_guards_extras_message_names_robocopy_not_rclone():
    dry = {"ok": True, "counts": {"total": 3, "extras": 1}, "planned_deletes": 0,
           "robocopy_planned_deletes": 5, "canary_present": True}
    cfg = MagicMock()
    with pytest.raises(RuntimeError, match=r"5 '\*EXTRA File' lines"):
        flow.lan_guards_task.fn(cfg, "FY26-27", dry, 10)


# 2. retry already_deleted
def test_lan_deletes_of_ignores_rename_halves():
    assert flow.lan_deletes_of({"files_deleted": 20}, 20) == 0
    assert flow.lan_deletes_of({"files_deleted": 25}, 20) == 5
    assert flow.lan_deletes_of({"files_deleted": None}, 20) is None
    assert flow.lan_deletes_of({}, 0) is None


def test_lan_retry_after_partly_mirrored_rename_is_not_reblocked():
    """Attempt 1 deleted 20 old-name halves of a renamed 20-file folder, then failed."""
    seen = []
    results = iter([{"status": "LAN_FAILED", "exit_code": 8, "files_deleted": 20},
                    {"status": "LAN_COMPLETE", "files_deleted": 0}])

    def run_once():
        r = next(results)
        if r["status"] == "LAN_FAILED":
            raise flow.SyncFailed("failed", r)
        return r

    with patch("flow.time.sleep"):
        flow._sync_with_retries(
            "lan", run_once=run_once, remeasure=lambda n: seen.append(n),
            deletes_of=lambda r: flow.lan_deletes_of(r, 20), retryable=lambda r: True,
            max_attempts=2, delay_s=1, budget=None, reserve_s=0, state={})
    assert seen == [0]   # rename halves are not this run's deletions


@needs_rclone
def test_cloud_stats_deletes_exclude_renames(tmp_path):
    """Cloud retry: already_deleted comes from rclone's stats 'deletes'; renames are separate."""
    src, dst = _mirror(tmp_path, n=4)
    (src / "Client_000" / "Invoices").rename(src / "Client_000" / "Invoices_r1")
    out = subprocess.run([_RCLONE, "sync", str(src), str(dst), "--track-renames",
                          "--use-json-log", "--stats", "1s", "--log-level", "INFO"],
                         capture_output=True, text=True)
    stats = final_stats(out.stdout + out.stderr)
    assert stats["deletes"] == 0 and stats["renames"] == 4


# 3. case-insensitive excludes
@needs_rclone
def test_lan_excludes_match_recycle_bin_case_insensitively(tmp_path, local_rclone):
    src, dst = _mirror(tmp_path, n=3)
    (dst / "$Recycle.Bin").mkdir()
    (dst / "$Recycle.Bin" / "x.txt").write_text("x")
    ex = ("$RECYCLE.BIN/**",)
    assert _measure(src, dst, excludes=ex)["planned_deletes"] == 1
    assert _measure(src, dst, excludes=ex, ignore_case=True)["planned_deletes"] == 0


# 4 + 6. lan keys
def test_lan_measurement_uses_lan_keys_not_cloud_keys(tmp_path):
    cfg = _lan_cfg(tmp_path)
    cfg.cloud.track_renames, cfg.lan.track_renames = True, False
    _, m = _preflight(cfg, 0, {"planned_deletes": 0, "moves": 0, "error": None})
    kw = m.call_args.kwargs
    assert kw["track_renames"] is False
    assert kw["timeout"] == 1234
    assert kw["ignore_case"] is True


def test_new_lan_keys_have_defaults_and_bounds():
    f = LanConfig.model_fields
    assert f["track_renames"].default is True
    assert f["rclone_measure_timeout_seconds"].default == 1800
    with pytest.raises(Exception):
        LanConfig(rclone_measure_timeout_seconds=1)


# 6. empty source still reaches G1 first
def test_lan_empty_source_reaches_g1_before_the_rclone_failure(tmp_path):
    cfg = _lan_cfg(tmp_path, with_file=False)
    res, _ = _preflight(cfg, 0, {"planned_deletes": None, "moves": 0, "error": "boom"})
    assert res["planned_deletes"] is None and "boom" in res["rclone_measure_error"]
    res["counts"] = {"total": 0, "copied": 0, "skipped": 0, "mismatch": 0, "failed": 0, "extras": 0}
    with patch("flow.ManifestDB"):
        with pytest.raises(g.GuardBlocked) as ei:
            flow.lan_guards_task.fn(cfg, "FY26-27", res, 5)
    assert "G1_SOURCE_HAS_FILES" in repr(ei.value.record)


def test_lan_nonempty_source_rclone_failure_fails_closed(tmp_path):
    cfg = _lan_cfg(tmp_path)
    with pytest.raises(RuntimeError, match="boom") as ei:
        _preflight(cfg, 0, {"planned_deletes": None, "moves": 0, "error": "boom"})
    assert faults.code_of(ei.value) == "AAM-LAN-01"
