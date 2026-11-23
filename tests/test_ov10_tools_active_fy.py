"""OV-10.1 (R-20): upload_fy.py and restore.py refuse EVERY active FY - the configured year and, while the
overlap is on, the early next year - not only the configured one."""
from pathlib import Path

import pytest

import restore
import upload_fy
from core import fy_overlap
from models.config import AppConfig


def _cfg(tmp_path, overlap=True, source="FY26-27", lan=r"\\nas\share\FY26-27"):
    return AppConfig(**{
        "paths": {"source_drive": str(tmp_path / "src" / source), "lan_destination": lan,
                  "runtime_dir": str(tmp_path / "rt"), "gcs_key_path": str(tmp_path / "k.json")},
        "cloud": {"enabled": True, "bucket": "aam-test-bucket", "project_number": "1"},
        "rollover": {"overlap_enabled": overlap},
        "wol": {"enabled": False}})


def _upload(tmp_path, monkeypatch, fy, overlap=True):
    (tmp_path / "src" / "FY26-27").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "FY27-28").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "FY25-26").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(upload_fy, "_load_config", lambda path: _cfg(tmp_path, overlap=overlap))
    return upload_fy.main(["--fy", fy, "--tier", "STANDARD", "--source", str(tmp_path / "src"),
                           "--bucket", "b", "--key", "k", "--dry-run"])


@pytest.mark.parametrize("fy", ["FY26-27", "FY27-28", "fy27-28"])
def test_upload_refuses_every_active_year(tmp_path, monkeypatch, capsys, fy):
    assert _upload(tmp_path, monkeypatch, fy) == 2
    err = capsys.readouterr().err
    assert "live" in err and "FY2" in err


def test_upload_allows_the_early_year_when_overlap_is_off(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(upload_fy, "upload_fy_folder", lambda *a, **k: calls.append(1) or {"status": "OK"},
                        raising=False)
    try:
        _upload(tmp_path, monkeypatch, "FY27-28", overlap=False)
    except Exception:           # whatever happens after the refusal check is not this test's business
        pass
    assert "live" not in capsys.readouterr().err


def test_restore_never_writes_into_an_active_year_folder(tmp_path):
    forbidden = restore._forbidden_paths(_cfg(tmp_path), None)
    assert str(tmp_path / "src" / "FY26-27") in forbidden
    assert str(tmp_path / "src" / "FY27-28") in forbidden                  # the early folder
    assert r"\\nas\share\FY27-28" in forbidden                             # and its NAS twin
    off = restore._forbidden_paths(_cfg(tmp_path, overlap=False), None)
    assert str(tmp_path / "src" / "FY27-28") not in off
