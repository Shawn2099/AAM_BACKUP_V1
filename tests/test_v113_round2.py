"""v1.1.3 round-2 tests: 5h timeouts, stage telemetry, disabled legs, dashboard truthfulness.

NOT part of the 29-test v1.1.2 suite. Deterministic; no network/services.
"""
import asyncio
import inspect
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import ui


def _fake_request(query=""):
    scope = {
        "type": "http",
        "query_string": query.encode(),
        "client": ("127.0.0.1", 55555),
        "headers": [],
    }
    return Request(scope)


def _patch_auth(monkeypatch):
    monkeypatch.setattr(ui, "_require_auth", lambda request: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)


def _fake_cfg(cloud_enabled=True, lan_enabled=True):
    return SimpleNamespace(
        cloud=SimpleNamespace(enabled=cloud_enabled),
        lan=SimpleNamespace(enabled=lan_enabled),
    )


def _body(resp):
    return json.loads(bytes(resp.body).decode())


# A — 5h timeouts
def test_a_model_defaults_are_5h():
    from models.config import CloudConfig
    assert CloudConfig.model_fields["subprocess_timeout_seconds"].default == 18000
    assert CloudConfig.model_fields["manifest_timeout_seconds"].default == 18000
    assert CloudConfig.model_fields["verify_timeout_seconds"].default == 18000
    assert CloudConfig.model_fields["diff_timeout_seconds"].default == 18000


def test_a_bounds_allow_5h():
    from models.config import CloudConfig
    cfg = CloudConfig(enabled=True, bucket="bkt-abc", project_number="123")
    assert cfg.subprocess_timeout_seconds == 18000
    assert cfg.manifest_timeout_seconds == 18000
    assert cfg.verify_timeout_seconds == 18000
    assert cfg.diff_timeout_seconds == 18000


def test_a_fallbacks_match_model():
    from core import cloud_reporter as cr
    from core import cloud_sync as cs
    from core import cloud_verify as cv
    from core import integrity as integ
    from models.config import CloudConfig
    assert inspect.signature(cs.run_cloud_sync).parameters["timeout"].default == 18000
    assert inspect.signature(cv.verify_cloud_integrity).parameters["timeout"].default == 18000
    assert inspect.signature(cr.get_cloud_manifest).parameters["timeout"].default == CloudConfig.model_fields["manifest_timeout_seconds"].default == 18000
    assert inspect.signature(cr.get_cloud_diff).parameters["timeout"].default == CloudConfig.model_fields["diff_timeout_seconds"].default == 18000
    assert inspect.signature(integ.audit_cloud).parameters["timeout"].default == 18000


def test_a_untouched_timeouts_stable():
    from core import cloud_preflight as cp
    from core import cloud_reporter as cr
    from core import lan_preflight as lp
    from models.config import CloudConfig, LanConfig
    assert inspect.signature(cp.run_cloud_dry_run).parameters["timeout"].default == 30
    assert inspect.signature(cr.get_cloud_size).parameters["timeout"].default == 300
    assert inspect.signature(lp.run_lan_dry_run).parameters["timeout"].default == 900
    assert CloudConfig.model_fields["preflight_timeout_seconds"].default == 300
    assert LanConfig.model_fields["dry_run_timeout_seconds"].default == 900


# B — stage telemetry carries through verify task
def test_b_verify_task_reports_stage_seconds(monkeypatch):
    import flow

    @contextmanager
    def fake_cfg_ctx(*a, **k):
        yield "fake-rclone-cfg"

    monkeypatch.setattr(flow, "temp_rclone_config", fake_cfg_ctx)
    monkeypatch.setattr(flow, "verify_cloud_integrity", lambda **k: {"verified": True})
    monkeypatch.setattr(flow, "get_cloud_size", lambda *a, **k: {"count": 2, "bytes": 20})
    monkeypatch.setattr(flow, "get_cloud_manifest", lambda *a, **k: [])
    monkeypatch.setattr(flow, "get_cloud_diff", lambda *a, **k: {})
    cfg = SimpleNamespace(
        paths=SimpleNamespace(source_drive="E:\\FY26-27", gcs_key_path="k"),
        cloud=SimpleNamespace(bucket="b", location="l", project_number="p", storage_class="s",
                              verify_timeout_seconds=1, cloud_size_timeout_seconds=1,
                              manifest_timeout_seconds=1, diff_timeout_seconds=1),
    )
    out = flow.cloud_verify_and_report_task.fn(cfg, "FY26-27")
    stages = out["stage_seconds"]
    assert set(stages) == {"verify_s", "size_s", "manifest_s", "diff_s"}
    assert all(isinstance(v, float) and v >= 0 for v in stages.values())


def test_b_extended_metrics_schema_has_timings():
    raw = json.dumps({"stage_seconds": {"sync_s": 1.5, "verify_s": 2.0}, "verified": True})
    parsed = json.loads(raw)
    assert parsed["stage_seconds"]["sync_s"] == 1.5


# C — disabled legs
def test_c_trigger_cloud_disabled_rejected(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(cloud_enabled=False))

    async def boom(name, timeout=None):
        raise AssertionError("arun_deployment must not run for a disabled leg")

    monkeypatch.setattr(ui, "arun_deployment", boom)
    resp = asyncio.run(ui.trigger_cloud(_fake_request()))
    assert resp.status_code == 409
    assert _body(resp)["status"] == "disabled"


def test_c_trigger_lan_disabled_rejected(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(lan_enabled=False))

    async def boom(name, timeout=None):
        raise AssertionError("arun_deployment must not run for a disabled leg")

    monkeypatch.setattr(ui, "arun_deployment", boom)
    resp = asyncio.run(ui.trigger_lan(_fake_request()))
    assert resp.status_code == 409
    assert _body(resp)["status"] == "disabled"


def test_c_trigger_enabled_legs_proceed(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(ui, "_cfg", lambda: _fake_cfg(True, True))
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    run = SimpleNamespace(id="aaa", state=SimpleNamespace(type=None, is_final=lambda: False))
    monkeypatch.setattr(ui, "arun_deployment", AsyncMock(return_value=run))
    assert asyncio.run(ui.trigger_cloud(_fake_request())).status_code == 202
    assert asyncio.run(ui.trigger_lan(_fake_request())).status_code == 202


def test_c_flow_refuses_explicit_disabled_leg(tmp_path):
    import flow

    def _write(name, cloud_on, lan_on):
        p = tmp_path / name
        p.write_text(
            "firm_name: 'T'\n"
            "paths:\n"
            f"  source_drive: 'E:\\FY26-27'\n"
            f"  lan_destination: '\\\\\\\\srv\\\\sh\\\\FY26-27'\n"
            f"  runtime_dir: '{str(tmp_path).replace(chr(92), chr(92) + chr(92))}'\n"
            "  gcs_key_path: 'k'\n"
            f"lan:\n  enabled: {str(lan_on).lower()}\n"
            "wol:\n  enabled: false\n"
            f"cloud:\n  enabled: {str(cloud_on).lower()}\n  bucket: 'bkt-abc'\n  project_number: '123'\n"
            "dashboard:\n  auth_enabled: false\n",
            encoding="utf-8",
        )
        return str(p)

    with pytest.raises(ValueError, match="disabled"):
        flow.backup.fn(_write("c1.yaml", False, True), mode="cloud")
    with pytest.raises(ValueError, match="disabled"):
        flow.backup.fn(_write("c2.yaml", True, False), mode="lan")


def test_c_flow_all_mode_runs_enabled_leg_only(tmp_path, monkeypatch):
    import flow
    p = tmp_path / "c3.yaml"
    p.write_text(
        "firm_name: 'T'\npaths:\n  source_drive: 'E:\\FY26-27'\n"
        "  lan_destination: '\\\\\\\\srv\\\\sh\\\\FY26-27'\n"
        f"  runtime_dir: '{str(tmp_path)}'\n  gcs_key_path: ''\n"
        "lan:\n  enabled: true\nwol:\n  enabled: false\n"
        "cloud:\n  enabled: false\n  bucket: ''\n  project_number: ''\n"
        "dashboard:\n  auth_enabled: false\n",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(flow, "_run_cloud_pipeline", lambda *a, **k: calls.append("cloud"))
    monkeypatch.setattr(flow, "_run_lan_pipeline", lambda *a, **k: calls.append("lan"))
    monkeypatch.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda **k: None)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow, "configure_prefect_bridge", lambda: None)
    flow.backup.fn(str(p), mode="all")
    assert calls == ["lan"]


# D–H — dashboard truthfulness (backend payload + template + JS contract)
def _fake_db():
    return SimpleNamespace(
        get_recent_runs=lambda n: [],
        last_run=lambda mode: None,
        last_successful_run=lambda mode: None,
        latest_audit=lambda mode: None,
        file_count=lambda status: 0,
    )


def _status_payload(monkeypatch, tmp_path, cloud_on=True, lan_on=True):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(ui, "_require_auth", lambda request: None)
    fake_cfg = SimpleNamespace(
        firm_name="T",
        paths=SimpleNamespace(source_drive=str(tmp_path), database_path=str(tmp_path / "m.db")),
        schedule=SimpleNamespace(cloud_cron="0 1 * * *", lan_cron="0 2 * * *", timezone="UTC"),
        cloud=SimpleNamespace(enabled=cloud_on),
        lan=SimpleNamespace(enabled=lan_on),
        dashboard=SimpleNamespace(auth_enabled=False, api_key=""),
    )
    monkeypatch.setattr(ui, "_cfg", lambda: fake_cfg)
    monkeypatch.setattr(ui, "get_db", lambda: _fake_db())
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    client = TestClient(ui.app, raise_server_exceptions=False)
    return client.get("/status")


def test_d_status_exposes_enabled_integrity_runstate(monkeypatch, tmp_path):
    resp = _status_payload(monkeypatch, tmp_path)
    assert resp.status_code == 200
    data = resp.json()
    for leg in ("cloud", "lan"):
        assert data[leg]["enabled"] is True
        assert data[leg]["integrity"]["status"] == "NOT_VERIFIED"
        assert data[leg]["run_state"] == "idle"
        assert data[leg]["last_run"] is None


def test_d_status_marks_disabled_leg(monkeypatch, tmp_path):
    data = _status_payload(monkeypatch, tmp_path, cloud_on=False).json()
    assert data["cloud"]["enabled"] is False
    assert data["lan"]["enabled"] is True


def test_d_unknown_runstate_surfaced(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    _status_payload(monkeypatch, tmp_path)  # warms patches via fresh client below
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=None))
    client = TestClient(ui.app, raise_server_exceptions=False)
    data = client.get("/status").json()
    assert data["cloud"]["run_state"] == "unknown"
    assert data["cloud"]["running"] is False


def test_e_status_401_unauthorized(monkeypatch):
    from fastapi.testclient import TestClient
    # Real auth path (not patched): enabled dashboard, no session cookie/key.
    fake_cfg = SimpleNamespace(dashboard=SimpleNamespace(auth_enabled=True, api_key="secret-key"))
    monkeypatch.setattr(ui, "_cfg", lambda: fake_cfg)
    client = TestClient(ui.app, raise_server_exceptions=False)
    resp = client.get("/status", headers={"Accept": "application/json"})
    assert resp.status_code == 401


def test_f_template_hides_disabled_trigger_and_shows_integrity(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(ui, "_require_auth", lambda request: None)
    fake_cfg = SimpleNamespace(
        schedule=SimpleNamespace(cloud_cron="0 1 * * *", lan_cron="0 2 * * *", timezone="UTC"),
        cloud=SimpleNamespace(enabled=False),
        lan=SimpleNamespace(enabled=True),
        dashboard=SimpleNamespace(auth_enabled=False),
    )
    monkeypatch.setattr(ui, "_cfg", lambda: fake_cfg)
    html = TestClient(ui.app, raise_server_exceptions=False).get("/").text
    assert "disabled in configuration" in html
    assert 'id="integrity-cloud"' in html
    assert 'id="integrity-lan"' in html
    assert 'id="session-banner"' in html


def test_h_js_contract():
    from pathlib import Path
    js = Path("static/js/dashboard.js").read_text(encoding="utf-8")
    assert "response.status === 401" in js
    assert "setSessionExpired" in js
    assert "integrityText" in js
    assert "NOT VERIFIED" in js
    assert "VERIFICATION FAILED" in js
    assert "data.cloud.enabled === false" in js
    assert "data.lan.enabled === false" in js
    assert "run_state" in js
