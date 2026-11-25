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
# 2. retry already_deleted
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
def test_new_lan_keys_have_defaults_and_bounds():
    f = LanConfig.model_fields
    assert f["track_renames"].default is True
    assert f["rclone_measure_timeout_seconds"].default == 1800
    with pytest.raises(Exception):
        LanConfig(rclone_measure_timeout_seconds=1)


# 6. empty source still reaches G1 first
