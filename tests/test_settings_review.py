"""Every settings save is reviewed first; important changes need the typed word APPROVE."""
from pathlib import Path

import pytest

import ui
from core import config_edit as ce
from models.config import load_config

ROOT = Path(ui.__file__).parent
JS = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")


def test_the_typed_word_is_approve_for_one_or_many_important_settings():
    assert ce.confirmation_phrase(["paths.lan_destination"]) == "APPROVE"
    assert ce.confirmation_phrase(["guards.max_delete_files", "wol.enabled"]) == "APPROVE"


@pytest.mark.parametrize("key, word", [
    ("schedule.lan_cron", "when backups"),
    ("budget.lan_max_hours", "how long"),
])
def test_notable_settings_carry_a_plain_note_but_need_no_typed_word(key, word):
    assert word in ce.note_for(key) and not ce.is_risky(key)


@pytest.mark.parametrize("key, word", [
    ("paths.lan_destination", "what is backed up"),
    ("guards.max_delete_files", "safety check"),
    ("wol.enabled", "NAS is switched"),
    ("notifications.send_on_failure", "failure emails"),
    ("notifications.recipients", "who receives alerts"),
    ("notifications.smtp_host", "who receives alerts"),
    ("notifications.smtp_password", "who receives alerts"),
    ("notifications.weekly_enabled", "which emails are sent"),
])
def test_risky_settings_carry_a_plain_note_and_need_the_word(key, word):
    assert word in ce.note_for(key) and ce.is_risky(key)


def test_an_ordinary_setting_has_no_note():
    assert ce.note_for("firm_name") == ""


def test_the_catalogue_sends_the_note_to_the_page(tmp_path):
    from test_m4_settings import _write_config
    sections = ce.catalog(load_config(str(_write_config(tmp_path))))
    fields = {f["key"]: f for s in sections for f in s["fields"]}
    assert fields["schedule.lan_cron"]["note"] and fields["paths.lan_destination"]["note"]
    assert fields["firm_name"]["note"] == ""


def test_the_save_button_opens_the_review_and_the_dialog_exists():
    assert 'onclick="reviewAndSave()"' in HTML and 'id="review-dialog"' in HTML
    for part in ("review-list", "review-phrase", "review-ok", "review-error"):
        assert f'id="{part}"' in HTML
    body = JS.split("function reviewAndSave", 1)[1].split("function closeReview", 1)[0]
    assert "showModal" in body and "original[k]" in body and "c[k]" in body


def test_a_refused_word_keeps_the_dialog_open_instead_of_a_browser_prompt():
    body = JS.split("async function saveSettings", 1)[1].split("async function loadSettings", 1)[0]
    assert "reviewOpen()" in body and "review-error" in body
