"""AF-17 (notify): render_alert and support bundle must not run for suppressed/disabled alerts."""
from __future__ import annotations

from unittest.mock import patch

from core import notify as n
from core.manifest import ManifestDB
from core.time_utils import now_iso
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, WolConfig


def test_af17_suppressed_duplicate_does_not_render_alert(tmp_path):
    src = tmp_path / "FY26-27"
    src.mkdir()
    db_path = tmp_path / "m.db"
    cfg = AppConfig(
        paths=PathsConfig(
            source_drive=str(src),
            lan_destination=r"\\192.168.1.50\backup\FY26-27",
            database_path=str(db_path),
        ),
        cloud=CloudConfig(enabled=False, bucket="test-bucket", project_number="123456"),
        lan=LanConfig(enabled=True),
        wol=WolConfig(enabled=False),
    )
    db = ManifestDB(str(db_path))
    try:
        db.record_notification({
            "event_key": "backup:cloud:CLOUD_FAILED",
            "mode": "cloud",
            "kind": "FAILURE",
            "status": "SENT",
            "subject": "old",
            "recipient": "a@example.com",
            "created_at": now_iso(),
        })
        ev = n.AlertEvent(
            key="backup:cloud:CLOUD_FAILED",
            kind="FAILURE",
            mode="cloud",
            title="Cloud failed",
            message="boom",
            status="CLOUD_FAILED",
            occurred_at=now_iso(),
        )
        with patch("core.notify.render_alert", side_effect=AssertionError("should not render")) as ra:
            res = n._notify(cfg, ev, db)
        assert res.status == n.SUPPRESSED
        ra.assert_not_called()
    finally:
        db.close()
