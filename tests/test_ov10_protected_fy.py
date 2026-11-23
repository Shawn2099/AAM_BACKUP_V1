"""OV-10.1 (R-20) helper: upload_fy.py and restore.py refuse EVERY active FY - the configured year and, while the
overlap is on, the early next year - not only the configured one."""
from pathlib import Path

import pytest

from core import fy_overlap
from models.config import AppConfig


def _cfg(tmp_path, overlap=True, source="FY26-27", lan=r"\\nas\share\FY26-27"):
    return AppConfig(**{
        "paths": {"source_drive": str(tmp_path / "src" / source), "lan_destination": lan,
                  "runtime_dir": str(tmp_path / "rt"), "gcs_key_path": str(tmp_path / "k.json")},
        "cloud": {"enabled": True, "bucket": "aam-test-bucket", "project_number": "1"},
        "rollover": {"overlap_enabled": overlap},
        "wol": {"enabled": False}})


def test_protected_names_are_the_configured_and_the_next_year_while_overlap_is_on(tmp_path):
    assert fy_overlap.protected_fy_names(_cfg(tmp_path)) == ["FY26-27", "FY27-28"]
    assert fy_overlap.protected_fy_names(_cfg(tmp_path, overlap=False)) == ["FY26-27"]


def test_protected_names_wrap_the_century(tmp_path):
    cfg = _cfg(tmp_path, source="FY99-00", lan=r"\\nas\share\FY99-00")
    assert fy_overlap.protected_fy_names(cfg) == ["FY99-00", "FY00-01"]
