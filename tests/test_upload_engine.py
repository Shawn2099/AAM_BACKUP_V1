"""Tests for the bulk FY upload engine (local rclone backend)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.upload import list_local_fy_folders, upload_fy


@pytest.fixture
def upload_lab(tmp_path, monkeypatch):
    """A local rclone remote named aam_gcs plus a source FY tree."""
    monkeypatch.chdir(tmp_path)
    bucket = tmp_path / "fakebucket"
    (bucket / "FY26-27").mkdir(parents=True)  # existing live FY
    source = tmp_path / "archive"
    fy = source / "FY24-25"
    (fy / "nested").mkdir(parents=True)
    (fy / "a.txt").write_text("alpha", encoding="utf-8")
    (fy / "nested" / "b.bin").write_bytes(b"\x00\x01" * 500)
    (fy / "nested" / ".AAM_TARGET_MOUNTED").write_text("canary", encoding="utf-8")
    cfg = tmp_path / "rclone.conf"
    cfg.write_text("[aam_gcs]\ntype = local\n", encoding="utf-8")
    return {"bucket": bucket, "source": source, "fy": fy, "cfg": cfg, "root": tmp_path}


def _upload(lab, **kwargs):
    return upload_fy(
        lab["fy"],
        bucket="fakebucket",
        fy="FY24-25",
        storage_class="ARCHIVE",
        rclone_config_path=str(lab["cfg"]),
        **kwargs,
    )


def test_upload_is_verified_and_excludes_control_file(upload_lab):
    result = _upload(upload_lab)

    assert result["status"] == "UPLOADED_VERIFIED"
    assert result["ok"] is True
    assert result["source_count"] == 2  # canary excluded
    assert result["matched"] == 2
    assert result["missing_at_remote"] == 0
    assert result["modified_at_remote"] == 0
    dest = upload_lab["bucket"] / "FY24-25"
    assert (dest / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (dest / "nested" / "b.bin").read_bytes() == b"\x00\x01" * 500
    assert not (dest / "nested" / ".AAM_TARGET_MOUNTED").exists()
    assert Path(result["report_path"]).is_file()


def test_dry_run_writes_nothing(upload_lab):
    result = _upload(upload_lab, dry_run=True)

    assert result["status"] == "DRY_RUN"
    assert result["ok"] is True
    assert not (upload_lab["bucket"] / "FY24-25").exists()


def test_verify_only_detects_missing_object(upload_lab):
    assert _upload(upload_lab)["status"] == "UPLOADED_VERIFIED"
    (upload_lab["bucket"] / "FY24-25" / "a.txt").unlink()

    result = _upload(upload_lab, verify_only=True)

    assert result["status"] == "FAILED"
    assert result["verified"] is False
    assert result["missing_at_remote"] == 1


def test_verify_only_passes_after_upload(upload_lab):
    assert _upload(upload_lab)["status"] == "UPLOADED_VERIFIED"

    result = _upload(upload_lab, verify_only=True)

    assert result["status"] == "VERIFIED"
    assert result["ok"] is True
    assert result["matched"] == 2


def test_wrong_fy_is_refused(upload_lab):
    result = upload_fy(
        upload_lab["fy"],
        bucket="fakebucket",
        fy="FY26-27",
        storage_class="ARCHIVE",
        rclone_config_path=str(upload_lab["cfg"]),
    )

    assert result["status"] == "FAILED"
    assert "not found" in result["error"] or "not FY26-27" in result["error"]


def test_list_local_fy_folders(upload_lab):
    listing = list_local_fy_folders(upload_lab["source"])
    assert [f["name"] for f in listing["folders"]] == ["FY24-25"]
