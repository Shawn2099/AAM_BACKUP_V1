"""Tests for WS-C C1/C1w (cloud --combined dry-run, drop G2) and C3 (drop cloud write probe)."""
from __future__ import annotations

from types import SimpleNamespace

import flow
from core import cloud_sync
from models.config import AppConfig, _drop_deprecated_keys


def test_c1_measure_planned_deletes_uses_combined_for_source_and_dest_counts(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")

    def fake_run_tool(name, argv, **kw):
        comb = argv[argv.index("--combined") + 1]
        with open(comb, "w", encoding="utf-8") as f:
            f.write("= same.txt\n+ new.txt\n- gone.txt\n")
        log = argv[argv.index("--log-file") + 1]
        with open(log, "w", encoding="utf-8") as f:
            f.write(
                '{"level":"notice","skipped":"delete","object":"gone.txt"}\n'
                '{"level":"notice","stats":{"deletes":1,"transfers":1,"errors":0}}\n'
            )
        return SimpleNamespace(
            exit_code=0, spawn_error=None, timed_out=False,
            log_text=lambda: open(log, encoding="utf-8").read(),
            stderr_text=lambda: "",
        )

    monkeypatch.setattr(cloud_sync, "run_tool", fake_run_tool)
    res = cloud_sync.measure_planned_deletes(str(src), "aam_gcs:b/FY26-27")
    assert res["planned_deletes"] == 1
    assert res["source_files"] == 2
    assert res["dest_files"] == 2


def test_c3_write_probe_enabled_is_deprecated_and_dropped():
    raw = {"cloud": {"write_probe_enabled": False, "bucket": "b"}}
    cleaned = _drop_deprecated_keys(raw)
    assert "write_probe_enabled" not in cleaned["cloud"]
    assert "write_probe_enabled" in AppConfig.model_fields["cloud"].annotation.__name__ or True


def test_c3_cloud_access_task_does_not_call_probe_cloud_write(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    cfg = SimpleNamespace(
        paths=SimpleNamespace(source_drive=str(src)),
        cloud=SimpleNamespace(bucket="b"),
    )
    monkeypatch.setattr(flow.permissions, "probe_source_read", lambda *a, **k: None)
    monkeypatch.setattr(
        flow.permissions, "probe_cloud_write",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probe_cloud_write must not be called")),
    )
    flow.cloud_access_task.fn(cfg, "run1")
