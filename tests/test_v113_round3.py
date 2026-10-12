"""v1.1.3 round-3 tests: 5h policy consistency, failure telemetry, P3 fixes.

NOT part of the 29-test v1.1.2 suite. Deterministic; no network/services.
"""
import json
from types import SimpleNamespace

import yaml


# Timeout policy: 18000 everywhere for the four stages, 300 for size
def test_r3_model_timeouts():
    from models.config import CloudConfig
    assert CloudConfig.model_fields["subprocess_timeout_seconds"].default == 18000
    assert CloudConfig.model_fields["manifest_timeout_seconds"].default == 18000
    assert CloudConfig.model_fields["verify_timeout_seconds"].default == 18000
    assert CloudConfig.model_fields["diff_timeout_seconds"].default == 18000
    assert CloudConfig.model_fields["cloud_size_timeout_seconds"].default == 300


def test_r3_repo_config_timeouts():
    with open("config.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    cloud = data["cloud"]
    assert cloud["subprocess_timeout_seconds"] == 18000
    assert cloud["manifest_timeout_seconds"] == 18000
    assert cloud["verify_timeout_seconds"] == 18000
    assert cloud["diff_timeout_seconds"] == 18000
    assert cloud["cloud_size_timeout_seconds"] == 300


def test_r3_example_config_timeouts():
    with open("config.example.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    cloud = data["cloud"]
    assert cloud["subprocess_timeout_seconds"] == 18000
    assert cloud["manifest_timeout_seconds"] == 18000
    assert cloud["verify_timeout_seconds"] == 18000
    assert cloud["diff_timeout_seconds"] == 18000
    assert cloud["cloud_size_timeout_seconds"] == 300


def test_r3_loaded_runtime_timeouts():
    from models.config import load_config
    cfg = load_config("config.yaml")
    assert cfg.cloud.subprocess_timeout_seconds == 18000
    assert cfg.cloud.manifest_timeout_seconds == 18000
    assert cfg.cloud.verify_timeout_seconds == 18000
    assert cfg.cloud.diff_timeout_seconds == 18000
    assert cfg.cloud.cloud_size_timeout_seconds == 300


# Failure-path telemetry helper
def test_r3_failure_telemetry_keeps_measured():
    import flow
    raw = flow._failure_telemetry({"sync_s": 412.3, "manifest_s": 10.0}, "verify")
    parsed = json.loads(raw)
    assert parsed["stage_seconds"] == {"sync_s": 412.3, "manifest_s": 10.0}
    assert parsed["failure_phase"] == "verify"
    assert "verified" not in parsed


def test_r3_failure_telemetry_empty_when_nothing_ran():
    import flow
    parsed = json.loads(flow._failure_telemetry({}, "pre"))
    assert parsed["stage_seconds"] == {}
    assert parsed["failure_phase"] == "pre"


# P3-B: GLOB literal underscore
def test_r3_last_successful_ignores_incomplete(tmp_path):
    from core.manifest import ManifestDB
    db = ManifestDB(str(tmp_path / "m.db"))
    db.insert_run({"run_id": "a", "mode": "cloud", "started_at": "2026-01-01T00:00:00",
                   "ended_at": "2026-01-01T00:01:00", "status": "INCOMPLETE",
                   "exit_code": 1, "duration_seconds": 1.0})
    assert db.last_successful_run("cloud") is None
    db.insert_run({"run_id": "b", "mode": "cloud", "started_at": "2026-01-02T00:00:00",
                   "ended_at": "2026-01-02T00:01:00", "status": "CLOUD_COMPLETE",
                   "exit_code": 0, "duration_seconds": 1.0})
    assert db.last_successful_run("cloud")["run_id"] == "b"


# P3-C: classifier alignment
def test_r3_classify_exit_codes():
    from core.lan_sync import classify_exit_code, decide_lan_result
    for code in (0, 1, 2, 3, 4, 5, 6, 7):
        assert classify_exit_code(code) == "LAN_COMPLETE"
    assert classify_exit_code(8) == "LAN_PARTIAL"
    assert classify_exit_code(16) == "LAN_FAILED"
    assert classify_exit_code(-1) == "LAN_FAILED"
    # Agreement with the authoritative decider on the previously split range
    log = "Started : Thursday\nEnded : Thursday\nTotal Copied Skipped Mismatch FAILED Extras\n  Dirs : 1 1 0 0 0 0\n  Files : 2 0 2 0 0 0\n"
    for code in (4, 5, 6, 7):
        assert decide_lan_result(code, log)["status"] == "LAN_COMPLETE"


# P3-D: effective prefix follows configured FY leaf
def test_r3_effective_prefix_prefers_configured_leaf(monkeypatch):
    import flow
    cfg = SimpleNamespace(paths=SimpleNamespace(source_drive="E:\\Data\\FY26-27"))
    monkeypatch.setattr(flow, "get_fy_prefix", lambda: "FY27-28")
    assert flow._effective_fy_prefix(cfg) == "FY26-27"


def test_r3_effective_prefix_falls_back_to_date(monkeypatch):
    import flow
    cfg = SimpleNamespace(paths=SimpleNamespace(source_drive="C:\\ChaosTest\\source"))
    monkeypatch.setattr(flow, "get_fy_prefix", lambda: "FY27-28")
    assert flow._effective_fy_prefix(cfg) == "FY27-28"
