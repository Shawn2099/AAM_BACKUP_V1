"""AAM-DEL-01 on a folder rename: the shared G3 measurement treats a rename as a move.

Real rclone against temp dirs (deploy/bin/rclone.exe or PATH); skipped without a binary.
"""
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import flow
from core import guards as g
from core.cloud_sync import measure_planned_deletes

_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED = _ROOT / "deploy" / "bin" / "rclone.exe"
_RCLONE = str(_BUNDLED) if _BUNDLED.exists() else shutil.which("rclone")
needs_rclone = pytest.mark.skipif(not _RCLONE, reason="rclone binary not available")


@pytest.fixture
def local_rclone():
    with patch("core.cloud_sync.resolve_binary", return_value=_RCLONE):
        yield


def _mirror(tmp_path, n=20):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "Client_000" / "Invoices").mkdir(parents=True)
    dst.mkdir()
    for i in range(n):
        (src / "Client_000" / "Invoices" / f"inv{i}.txt").write_text(f"invoice number {i}")
    (src / "keep.txt").write_text("keep")
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return src, dst


def _measure(src, dst, **kw):
    return measure_planned_deletes(str(src), str(dst), modify_window_seconds=2, **kw)


@needs_rclone
def test_folder_rename_is_not_counted_as_planned_deletes(tmp_path, local_rclone):
    src, dst = _mirror(tmp_path)
    (src / "Client_000" / "Invoices").rename(src / "Client_000" / "Invoices_r1")
    assert _measure(src, dst)["planned_deletes"] == 0
    # without rename tracking it is the false mass delete that blocked a whole year
    assert _measure(src, dst, track_renames=False)["planned_deletes"] == 20


@needs_rclone
def test_really_deleted_source_files_still_count(tmp_path, local_rclone):
    src, dst = _mirror(tmp_path)
    for f in (src / "Client_000" / "Invoices").glob("inv1*.txt"):
        f.unlink()
    n = len(list((dst / "Client_000" / "Invoices").glob("inv1*.txt")))
    assert n > 1 and _measure(src, dst)["planned_deletes"] == n


@needs_rclone
def test_excluded_names_are_never_planned_deletes(tmp_path, local_rclone):
    src, dst = _mirror(tmp_path)
    (dst / ".AAM_TARGET_MOUNTED").write_text("")
    assert _measure(src, dst)["planned_deletes"] == 1
    assert _measure(src, dst, excludes=(".AAM_TARGET_MOUNTED",))["planned_deletes"] == 0


@needs_rclone
def test_failed_measurement_is_none(tmp_path, local_rclone):
    src, _ = _mirror(tmp_path)
    res = _measure(src, "remote_that_does_not_exist:bucket")
    assert res["planned_deletes"] is None and res["error"]


@needs_rclone
def test_g3_does_not_block_a_rename_but_blocks_real_loss(tmp_path, local_rclone):
    src, dst = _mirror(tmp_path, n=318)
    (src / "Client_000" / "Invoices").rename(src / "Client_000" / "Invoices_r1")
    renamed = _measure(src, dst)["planned_deletes"]
    assert g.g3_mass_delete(renamed, 319, 500, 10.0).passed
    shutil.rmtree(src / "Client_000")
    lost = _measure(src, dst)["planned_deletes"]
    assert lost == 318 and not g.g3_mass_delete(lost, 319, 500, 10.0).passed


def test_lan_preflight_task_uses_the_shared_measurement():
    cfg = MagicMock()
    cfg.paths.source_drive, cfg.paths.lan_destination = "S:\\", "\\\\nas\\share"
    cfg.cloud.track_renames = True
    cfg.lan.track_renames = True
    robocopy = {"ok": True, "planned_deletes": 316, "counts": {"extras": 316}}
    measured = {"planned_deletes": 0, "moves": 316, "error": None}
    with patch("flow.run_lan_dry_run", return_value=dict(robocopy)), \
         patch("flow.measure_planned_deletes", return_value=measured) as m:
        res = flow.lan_preflight_task.fn(cfg)
    assert res["planned_deletes"] == 0 and res["robocopy_planned_deletes"] == 316
    kw = m.call_args.kwargs
    assert kw["track_renames"] is True and ".AAM_TARGET_MOUNTED" in kw["excludes"]
    assert "fast_list" not in kw
    # a failed rclone measurement fails the run closed (never a silent 0)
    with patch("flow.run_lan_dry_run", return_value=dict(robocopy)), \
         patch("flow.measure_planned_deletes",
               return_value={"planned_deletes": None, "error": "boom"}), \
         pytest.raises(RuntimeError, match="boom"):
        flow.lan_preflight_task.fn(cfg)
