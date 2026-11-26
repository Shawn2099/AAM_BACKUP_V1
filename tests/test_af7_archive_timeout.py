"""AF-7: rollover archive step timeout and skipping already-archived objects."""
from __future__ import annotations

import json
from unittest.mock import patch

from core import fy_rollover
from models.config import HealthConfig
from test_support.fake_tools import fake_run_tool


def test_af7_archive_timeout_default_and_bounds():
    cfg = HealthConfig()
    assert cfg.rollover_archive_timeout_seconds >= 1800
    cfg_max = HealthConfig(rollover_archive_timeout_seconds=21600)
    assert cfg_max.rollover_archive_timeout_seconds == 21600


def test_af7_fully_archived_prefix_makes_no_update_call(tmp_path):
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}', encoding="utf-8")
    listed = json.dumps([
        {"name": "FY25-26/a.txt", "storage_class": "ARCHIVE"},
        {"name": "FY25-26/b.txt", "storage_class": "ARCHIVE"},
    ])
    with patch("core.fy_rollover._resolve_gcloud", return_value="gcloud"), \
         fake_run_tool("core.fy_rollover", exit_code=0, stdout=listed) as calls:
        ok = fy_rollover.run_archive_transition("my-bucket", "FY25-26", str(key))
    assert ok is True
    update_calls = [c for c in calls if "update" in c["argv"]]
    assert update_calls == [], f"Expected no update calls when already ARCHIVE, got {update_calls}"


def test_af7_partial_prefix_updates_only_non_archived_objects(tmp_path):
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}', encoding="utf-8")
    listed = json.dumps([
        {"name": "FY25-26/already.txt", "storage_class": "ARCHIVE"},
        {"name": "FY25-26/todo1.txt", "storage_class": "STANDARD"},
        {"name": "FY25-26/todo2.txt", "storage_class": "NEARLINE"},
    ])
    with patch("core.fy_rollover._resolve_gcloud", return_value="gcloud"), \
         fake_run_tool("core.fy_rollover", exit_code=0, stdout=listed) as calls:
        ok = fy_rollover.run_archive_transition("my-bucket", "FY25-26", str(key))
    assert ok is True
    update_calls = [c for c in calls if "update" in c["argv"]]
    assert len(update_calls) == 1
    argv = update_calls[0]["argv"]
    assert "gs://my-bucket/FY25-26/already.txt" not in argv
    assert "gs://my-bucket/FY25-26/todo1.txt" in argv
    assert "gs://my-bucket/FY25-26/todo2.txt" in argv
    assert "--recursive" not in argv


def test_af7_timeout_returns_false_so_journal_stays_retryable(tmp_path):
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}', encoding="utf-8")
    with patch("core.fy_rollover._resolve_gcloud", return_value="gcloud"), \
         fake_run_tool("core.fy_rollover", timed_out=True):
        ok = fy_rollover.run_archive_transition("my-bucket", "FY25-26", str(key))
    assert ok is False
