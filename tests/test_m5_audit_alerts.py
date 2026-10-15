"""M5 - the weekly integrity audit always records and emails a leg that could not run.

Finding (M5 review, 2026-10-16): integrity_audit_flow only alerted on the
verdicts it collected. An EXCEPTION in a leg - the NAS not waking
(WolTimeout from wol_check_task, the likeliest Sunday failure), the GCS key
missing (temp_rclone_config), the audit helper raising - or the backup slot
not being obtained escaped with NO email and NO audit row; a cloud exception
also skipped the LAN audit. Prefect showed Failed, nobody was told.

Fix: an exception in a leg is that leg's AUDIT_INCOMPLETE ("could not run")
row; the other leg still runs; a slot/lock failure records AUDIT_INCOMPLETE
for every requested leg; every non-VERIFIED outcome goes into the ONE audit
alert, and the flow still fails.
"""

from unittest.mock import MagicMock

import pytest

import flow
from core.wol import WolTimeout

H = 3600
VERIFIED = {"status": "VERIFIED", "scope": "full", "files_checked": 10, "bytes_checked": None,
            "mismatches": 0, "detail": "verified clean"}


def _setup(monkeypatch, cloud=True, lan=True):
    recorded = []
    db = MagicMock()
    db.record_audit.side_effect = recorded.append
    cfg = MagicMock()
    cfg.paths.source_drive = "E:\\FY26-27"
    cfg.paths.lan_destination = "\\\\nas\\share\\FY26-27"
    cfg.cloud.enabled = cloud
    cfg.lan.enabled = lan
    cfg.budget.audit_max_hours = 12
    cfg.budget.lan_post_reserve_minutes = 30
    cfg.lan.subprocess_timeout_seconds = 4 * H
    cfg.cloud.verify_timeout_seconds = 4 * H
    cfg.lan.shutdown_after_backup = True
    cfg.wol.enabled = True
    monkeypatch.setattr(flow, "load_config", lambda p: cfg)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow.ManifestDB, "from_config", lambda c: db)
    monkeypatch.setattr(flow, "_backup_slot", MagicMock())
    monkeypatch.setattr(flow, "_open_run_artefacts", lambda c, i: None)
    monkeypatch.setattr(flow, "temp_rclone_config", MagicMock())
    m = {"notify": MagicMock(), "wol": MagicMock(), "shutdown": MagicMock(),
         "audit_cloud": MagicMock(return_value=dict(VERIFIED)),
         "audit_lan": MagicMock(return_value=dict(VERIFIED))}
    monkeypatch.setattr(flow, "notify", m["notify"])
    monkeypatch.setattr(flow, "wol_check_task", m["wol"])
    monkeypatch.setattr(flow, "lan_shutdown_task", m["shutdown"])
    monkeypatch.setattr("core.integrity.audit_cloud", m["audit_cloud"])
    monkeypatch.setattr("core.integrity.audit_lan", m["audit_lan"])
    return cfg, recorded, m


def _one_alert(m):
    assert m["notify"].call_count == 1
    event = m["notify"].call_args.args[1]
    assert event.kind == "FAILURE" and event.mode == "audit"
    return event


def test_nas_not_waking_is_recorded_and_emailed(monkeypatch):
    _, recorded, m = _setup(monkeypatch, cloud=False)
    m["wol"].side_effect = WolTimeout("Backup server 10.0.0.9 did not become usable over SMB")
    with pytest.raises(Exception):
        flow.integrity_audit_flow.fn("x.yaml", "lan")
    [row] = recorded
    assert row["mode"] == "lan" and row["status"] == "AUDIT_INCOMPLETE"
    assert "could not run" in row["detail"] and "did not become usable" in row["detail"]
    assert "did not become usable" in _one_alert(m).message
    m["audit_lan"].assert_not_called()
    m["shutdown"].assert_not_called()          # the NAS never woke - nothing to power off


def test_cloud_exception_still_audits_lan_and_alerts_once(monkeypatch):
    _, recorded, m = _setup(monkeypatch)
    flow.temp_rclone_config.side_effect = FileNotFoundError("aam-gcs-key.json not found")
    with pytest.raises(Exception):
        flow.integrity_audit_flow.fn("x.yaml", "all")
    rows = {r["mode"]: r for r in recorded}
    assert rows["cloud"]["status"] == "AUDIT_INCOMPLETE"
    assert "aam-gcs-key.json" in rows["cloud"]["detail"]
    assert rows["lan"]["status"] == "VERIFIED"
    m["audit_lan"].assert_called_once()
    assert "aam-gcs-key.json" in _one_alert(m).message


def test_slot_not_obtained_records_every_requested_leg_and_alerts(monkeypatch):
    _, recorded, m = _setup(monkeypatch)
    flow._backup_slot.side_effect = TimeoutError("Attempt to acquire concurrency slots timed out")
    with pytest.raises(Exception):
        flow.integrity_audit_flow.fn("x.yaml", "all")
    assert sorted((r["mode"], r["status"]) for r in recorded) == [
        ("cloud", "AUDIT_INCOMPLETE"), ("lan", "AUDIT_INCOMPLETE")]
    assert all("timed out" in r["detail"] for r in recorded)
    assert "timed out" in _one_alert(m).message
    m["wol"].assert_not_called()


def test_shutdown_failure_never_replaces_the_lan_verdict(monkeypatch):
    _, recorded, m = _setup(monkeypatch, cloud=False)
    m["shutdown"].side_effect = OSError("shutdown.exe failed")
    flow.integrity_audit_flow.fn("x.yaml", "lan")      # VERIFIED audit: the flow succeeds
    [row] = recorded
    assert row["status"] == "VERIFIED"
    m["shutdown"].assert_called_once()


def test_clean_audit_sends_nothing(monkeypatch):
    _, recorded, m = _setup(monkeypatch)
    flow.integrity_audit_flow.fn("x.yaml", "all")
    assert [r["status"] for r in recorded] == ["VERIFIED", "VERIFIED"]
    m["notify"].assert_not_called()
