"""v1.1.3 round-3 tests: 5h policy consistency, failure telemetry, P3 fixes.

NOT part of the 29-test v1.1.2 suite. Deterministic; no network/services.
"""
import json
from pathlib import Path
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


# The repo-root config.yaml is gitignored, operator-owned runtime state: the
# FY-rollover flow rewrites it and its timeouts are tuned per deployment (the
# HDD test rig pins 21600/14400/1800/900 — every one legal under the schema's
# ge/le bounds). The 5h policy is pinned by the *tracked* artifacts instead:
# the model defaults (above), the shipped template (test_r3_example_config_timeouts)
# and the core fallback signatures
# (test_v113_remediation.test_k_fallbacks_match_model). Asserting 18000 against
# the rig file went stale the moment an operator tuned a value; what the
# runtime file must still guarantee is that nothing it pins is rejected or
# silently clamped by the schema, and that the loader applies it verbatim.
_RIG_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"
_POLICY_DEFAULTS = {
    "subprocess_timeout_seconds": 18000,
    "verify_timeout_seconds": 18000,
    "diff_timeout_seconds": 18000,
    "manifest_timeout_seconds": 18000,
    "cloud_size_timeout_seconds": 300,
}


def _schema_bounds():
    """{field: (min, max|None)} declared by CloudConfig for each timeout field."""
    from models.config import CloudConfig
    bounds = {}
    for name, field in CloudConfig.model_fields.items():
        if not name.endswith("_timeout_seconds"):
            continue
        lo = max((m.ge for m in field.metadata if getattr(m, "ge", None) is not None), default=None)
        hi = min((m.le for m in field.metadata if getattr(m, "le", None) is not None), default=None)
        bounds[name] = (lo, hi)
    return bounds


def test_r3_repo_config_timeouts():
    """Every cloud timeout the rig config.yaml pins must sit inside the bounds
    CloudConfig declares — a hand-edited value the schema would reject (or
    clamp) is a config error, not a tuning choice."""
    with open(_RIG_CONFIG, encoding="utf-8") as f:
        pinned = yaml.safe_load(f)["cloud"]
    bounds = _schema_bounds()
    for field in _POLICY_DEFAULTS:
        if field not in pinned:
            continue  # omitted → inherits the model default, checked below
        lo, hi = bounds[field]
        assert pinned[field] >= lo, f"{field}={pinned[field]} below schema minimum {lo}"
        assert hi is None or pinned[field] <= hi, f"{field}={pinned[field]} above schema maximum {hi}"


def test_r3_loaded_runtime_timeouts(tmp_path):
    """The loader honours whatever a runtime config pins and falls back to the
    5h policy only for keys it omits — no silent reversion to a stale value.

    Direction 1 runs against the deployed rig file (operator-tuned numbers);
    direction 2 against a minimal config that omits every timeout (policy
    defaults)."""
    from models.config import AppConfig, load_config

    # 1. rig config: pinned values load verbatim
    raw = _RIG_CONFIG.read_text(encoding="utf-8")
    pinned = yaml.safe_load(raw)["cloud"]
    data = yaml.safe_load(raw)
    for removed in ("api_key", "auth_enabled"):          # keys of an older release (plan E)
        data.get("dashboard", {}).pop(removed, None)
    rig = AppConfig(**data).cloud
    for field, policy_default in _POLICY_DEFAULTS.items():
        assert getattr(rig, field) == pinned.get(field, policy_default)

    # 2. a config that omits the timeouts inherits the 5h policy defaults
    minimal = tmp_path / "config.yaml"
    minimal.write_text(
        "paths:\n"
        "  source_drive: E:\\SOURCE\\FY26-27\n"
        "  lan_destination: \\\\nas\\share\\FY26-27\n"
        "  gcs_key_path: k.json\n"
        "cloud:\n"
        "  enabled: false\n"
        "wol:\n"
        "  enabled: false\n"
        "dashboard: {}\n"
    )
    defaults = load_config(str(minimal)).cloud
    for field, policy_default in _POLICY_DEFAULTS.items():
        assert getattr(defaults, field) == policy_default


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
    from core import fy_rollover   # WP9: one effective-FY function for flow + dashboard
    monkeypatch.setattr(fy_rollover, "get_fy_prefix", lambda: "FY27-28")
    assert flow._effective_fy_prefix(cfg) == "FY27-28"
