"""Tests for WS-B B7: versions space accounting and weekly report size (V11, V13)."""
from __future__ import annotations

from types import SimpleNamespace

import flow
from core import lan_versions, report


def test_v11_lan_access_task_includes_versions_growth_in_bytes_needed(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "nas" / "FY26-27"
    dst.mkdir(parents=True)
    dry_log = tmp_path / "plan.log"
    dry_log.write_text(
        '{"level":"notice","skipped":"move","object":"a.txt","size":1000}\n'
        '{"level":"notice","skipped":"move into backup dir","object":"b.txt","size":2000}\n'
        '{"level":"notice","skipped":"copy","object":"a.txt","size":500}\n',
        encoding="utf-8",
    )
    cfg = SimpleNamespace(
        paths=SimpleNamespace(source_drive=str(src), lan_destination=str(dst)),
        lan=SimpleNamespace(min_free_dest_gb=5.0, versions_enabled=True),
    )
    captured = {}
    monkeypatch.setattr(flow.permissions, "probe_source_read", lambda *a, **k: None)
    monkeypatch.setattr(flow.permissions, "probe_destination_write", lambda *a, **k: None)
    monkeypatch.setattr(
        flow.permissions, "check_destination_free_space",
        lambda d, *, bytes_needed=None, min_free_gb=0: captured.update(bytes_needed=bytes_needed),
    )

    flow.lan_access_task.fn(cfg, {"planned_bytes": 500, "log_path": str(dry_log)}, "run1")
    assert captured["bytes_needed"] == 3500


def test_v11_versions_size_summary_reports_bytes(tmp_path):
    nas = tmp_path / "nas"
    mirror = nas / "FY26-27"
    mirror.mkdir(parents=True)
    vdir = nas / "AAM_versions" / "2026-11-25_2101_FY26-27"
    vdir.mkdir(parents=True)
    (vdir / "old.bin").write_bytes(b"x" * 4096)

    cfg = SimpleNamespace(
        paths=SimpleNamespace(lan_destination=str(mirror)),
        lan=SimpleNamespace(enabled=True, versions_enabled=True, versions_folder="AAM_versions"),
    )
    assert lan_versions.versions_folder_size_bytes(nas / "AAM_versions") == 4096
    line = report.versions_size_summary(cfg)
    assert line is not None and "NAS versions folder" in line
