"""Two recipient lists - the Settings page (plan A3)."""
from __future__ import annotations

from pathlib import Path

from core import config_edit as ce
from models.config import AppConfig
from tests.test_recipients_callers import _cfg

JS = (Path(__file__).resolve().parent.parent / "static" / "js" / "settings.js").read_text(encoding="utf-8")


def _fields(tmp_path):
    return {x["key"]: x for s in ce.catalog(_cfg(tmp_path, 25)) for x in s["fields"]}


def test_both_lists_are_basic_email_lists_with_plain_names(tmp_path):
    f = _fields(tmp_path)
    for key in ("notifications.recipients", "notifications.report_recipients"):
        assert f[key]["level"] == "basic" and f[key]["widget"] == "email_list", key
    assert f["notifications.recipients"]["label"] == "Who gets problem alerts"
    assert f["notifications.report_recipients"]["label"] == "Who gets reports"
    assert f["notifications.recipients"]["value"] == ["ops@firm.example", "both@firm.example"]
    assert f["notifications.report_recipients"]["value"] == ["boss@firm.example", "both@firm.example"]
    # the help says what an empty list does
    assert "empty" in f["notifications.recipients"]["help"].lower()
    assert "empty" in f["notifications.report_recipients"]["help"].lower()


def test_the_page_checks_duplicates_ignoring_case_in_both_places():
    assert JS.count("toLowerCase()") >= 2
    assert "a.indexOf(x) !== i" not in JS


def test_saving_the_report_list_validates_each_list_on_its_own(tmp_path):
    from tests.test_m4_settings_ux import _write_config

    path = str(_write_config(tmp_path))
    # the same address in both lists is fine ...
    ok = ce.validate_changes(path, {"notifications.recipients": ["a@x.com"],
                                    "notifications.report_recipients": ["a@x.com"]})
    assert ok["ok"], ok
    # ... twice in one list (any letter case) is not, and the message names the list
    bad = ce.validate_changes(path, {"notifications.report_recipients": ["a@x.com", "A@x.com"]})
    assert not bad["ok"]
    text = " ".join(e["message"] for e in bad["errors"])
    assert "report list" in text and "duplicate" in text.lower()
    # a malformed address in the report list is rejected too
    worse = ce.validate_changes(path, {"notifications.report_recipients": ["nope"]})
    assert not worse["ok"]
