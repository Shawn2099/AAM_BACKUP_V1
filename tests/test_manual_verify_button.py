"""The dashboard's "Check now" / "Check both copies now" buttons start the existing read-only audit."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import ui

ROOT = Path(ui.__file__).parent
TEMPLATE = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")


def test_the_page_has_one_check_button_per_copy_and_one_for_both():
    for leg in ("lan", "cloud", "all"):
        assert f'id="btn-verify-{leg}"' in TEMPLATE and f"triggerVerify('{leg}')" in TEMPLATE
    assert 'id="verify-note"' in TEMPLATE


def test_the_script_posts_to_the_existing_audit_trigger_after_a_confirmation():
    body = JS.split("async function triggerVerify", 1)[1].split("function updateVerifyUi", 1)[0]
    assert "confirm(" in body and "'/trigger/verify/' + mode" in body and "method: 'POST'" in body
    assert "Nothing is changed" in body


def test_buttons_are_disabled_while_a_backup_or_a_check_runs():
    body = JS.split("function updateVerifyUi", 1)[1].split("// ── Recent backups", 1)[0]
    assert "data.verify_running" in body and "busyBackup" in body and "btn.disabled" in body


@pytest.mark.parametrize("running", [True, False])
def test_status_reports_whether_a_safety_check_is_running(monkeypatch, running):
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=running))
    data = TestClient(ui.app).get("/status").json()
    assert data["verify_running"] is running


def test_an_unreachable_scheduler_does_not_claim_a_check_is_running(monkeypatch):
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=None))
    assert TestClient(ui.app).get("/status").json()["verify_running"] is False
