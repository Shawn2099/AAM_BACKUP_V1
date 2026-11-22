"""Tests for the whole-FY cloud restore engine.

A local rclone backend (`type = local`) stands in for GCS so the tests run
without network access. The production path was also validated against the
real demo bucket (FY25-26 and FY26-27, 690 files, MD5-identical).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.restore import fy_info, list_fy_folders, restore_fy, valid_folder_name
from restore import main as restore_main


@pytest.fixture
def fake_bucket(tmp_path, monkeypatch):
    """A local rclone remote named aam_gcs that maps to tmp_path/fakebucket."""
    monkeypatch.chdir(tmp_path)
    bucket = tmp_path / "fakebucket"
    fy = bucket / "FY26-27"
    (fy / "nested").mkdir(parents=True)
    (fy / "one.txt").write_text("hello", encoding="utf-8")
    (fy / "nested" / "two.bin").write_bytes(b"\x00\x01\x02" * 1000)
    (fy / "zero.dat").write_bytes(b"")
    # Control-plane marker must never be restored.
    (fy / "nested" / ".AAM_TARGET_MOUNTED").write_text("canary", encoding="utf-8")
    old = bucket / "FY25-26"
    old.mkdir()
    (old / "old.txt").write_text("old year", encoding="utf-8")
    (bucket / "not_an_fy").mkdir()
    (bucket / "not_an_fy" / "x.txt").write_text("x", encoding="utf-8")
    cfg = tmp_path / "rclone.conf"
    cfg.write_text("[aam_gcs]\ntype = local\n", encoding="utf-8")
    return {"bucket": bucket, "fy": fy, "old": old, "cfg": cfg, "root": tmp_path}


def _restore(fake_bucket, target, **kwargs):
    return restore_fy(
        target,
        fy=kwargs.pop("fy", "FY26-27"),
        bucket="fakebucket",
        rclone_config_path=str(fake_bucket["cfg"]),
        skip_space_check=True,
        forbidden_paths=[str(fake_bucket["bucket"])],
        **kwargs,
    )


def test_restore_whole_fy_is_verified_and_excludes_control_file(fake_bucket):
    target = fake_bucket["root"] / "restore"
    target.mkdir()
    result = _restore(fake_bucket, target)

    assert result["status"] == "RESTORED_VERIFIED"
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["missing_at_target"] == 0
    assert result["modified_at_target"] == 0
    assert result["extra_at_target"] == 0
    assert result["remote_count"] == 3  # canary excluded
    restored = target / "FY26-27"
    assert (restored / "one.txt").read_text(encoding="utf-8") == "hello"
    assert (restored / "nested" / "two.bin").read_bytes() == b"\x00\x01\x02" * 1000
    assert (restored / "zero.dat").stat().st_size == 0
    assert not (restored / "nested" / ".AAM_TARGET_MOUNTED").exists()
    report = Path(result["report_path"])
    assert report.is_file()
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "RESTORED_VERIFIED"


def test_refuses_non_empty_target_without_force(fake_bucket):
    target = fake_bucket["root"] / "restore"
    (target / "FY26-27").mkdir(parents=True)
    keep = target / "FY26-27" / "keep.txt"
    keep.write_text("keep", encoding="utf-8")

    result = _restore(fake_bucket, target)

    assert result["status"] == "FAILED"
    assert "not empty" in result["error"]
    assert keep.read_text(encoding="utf-8") == "keep"


def test_dry_run_writes_nothing(fake_bucket):
    target = fake_bucket["root"] / "dry"
    target.mkdir()
    result = _restore(fake_bucket, target, dry_run=True)

    assert result["status"] == "DRY_RUN"
    assert result["ok"] is True
    assert not (target / "FY26-27").exists()


def test_verify_only_detects_missing_file(fake_bucket):
    target = fake_bucket["root"] / "restore"
    target.mkdir()
    assert _restore(fake_bucket, target)["status"] == "RESTORED_VERIFIED"
    (target / "FY26-27" / "one.txt").unlink()

    result = _restore(fake_bucket, target, verify_only=True)

    assert result["status"] == "FAILED"
    assert result["verified"] is False
    assert result["missing_at_target"] == 1


def test_force_merge_preserves_extra_files(fake_bucket):
    target = fake_bucket["root"] / "restore"
    target.mkdir()
    assert _restore(fake_bucket, target)["status"] == "RESTORED_VERIFIED"
    extra = target / "FY26-27" / "extra.txt"
    extra.write_text("keep me", encoding="utf-8")

    result = _restore(fake_bucket, target, allow_non_empty=True)

    assert result["status"] == "RESTORED_VERIFIED"
    assert result["extra_at_target"] == 1
    assert extra.read_text(encoding="utf-8") == "keep me"


def test_refuses_target_that_contains_protected_path(fake_bucket):
    result = _restore(fake_bucket, fake_bucket["root"], allow_non_empty=True, into=True)

    assert result["status"] == "FAILED"
    assert "protected path" in result["error"]


def test_old_fy_can_be_restored(fake_bucket):
    target = fake_bucket["root"] / "old-restore"
    target.mkdir()
    result = _restore(fake_bucket, target, fy="FY25-26")

    assert result["status"] == "RESTORED_VERIFIED"
    assert (target / "FY25-26" / "old.txt").read_text(encoding="utf-8") == "old year"


def test_list_fy_folders_and_info(fake_bucket):
    listing = list_fy_folders(
        bucket="fakebucket",
        rclone_config_path=str(fake_bucket["cfg"]),
    )
    names = [item["name"] for item in listing["folders"]]
    assert names == ["not_an_fy", "FY26-27", "FY25-26"]
    flags = {item["name"]: item["is_fy"] for item in listing["folders"]}
    assert flags["FY26-27"] is True
    assert flags["not_an_fy"] is False
    assert listing["other_folders"] == []

    info = fy_info(
        "FY26-27",
        bucket="fakebucket",
        rclone_config_path=str(fake_bucket["cfg"]),
    )
    assert info["count"] == 3
    assert info["bytes"] == len(b"hello") + len(b"\x00\x01\x02" * 1000)


def test_restore_any_top_level_folder(fake_bucket):
    target = fake_bucket["root"] / "any-restore"
    target.mkdir()
    result = _restore(fake_bucket, target, fy="not_an_fy")

    assert result["status"] == "RESTORED_VERIFIED"
    assert result["folder"] == "not_an_fy"
    assert (target / "not_an_fy" / "x.txt").read_text(encoding="utf-8") == "x"


def test_valid_folder_name_rejects_paths():
    assert valid_folder_name("FY26-27")
    assert valid_folder_name("archive")
    assert not valid_folder_name("")
    assert not valid_folder_name("..")
    assert not valid_folder_name("../escape")
    assert not valid_folder_name(r"archive\escape")
    assert not valid_folder_name("folder/child")


def test_cli_restores_a_non_fy_folder(fake_bucket, tmp_path):
    target = tmp_path / "cli-restore"
    target.mkdir()
    code = restore_main([
        "--config", str(tmp_path / "missing.yaml"),
        "--bucket", "fakebucket",
        "--rclone-config", str(fake_bucket["cfg"]),
        "--fy", "not_an_fy",
        "--target", str(target),
        "--yes",
        "--verbose",
        "--log-dir", str(tmp_path / "logs"),
    ])

    assert code == 0
    assert (target / "not_an_fy" / "x.txt").read_text(encoding="utf-8") == "x"
