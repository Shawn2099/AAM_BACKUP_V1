"""M4 4.7 follow-up (owner feedback 2026-10-15): settings page "never able to mess up".

- recipients / sender are validated as email addresses (server side - the
  page AND config.yaml), duplicates refused;
- /api/settings/validate gives the exact verdict a save would, live;
- fixed-set values are drop-downs (storage class, timezone), schedules use a
  day/time picker that writes cron (round-trip tested in Node);
- every client-facing time is IST (format_ist; dashboard formatter).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from core import config_edit as ce
from core.time_utils import format_ist
from models.config import NotificationConfig, load_config

ROOT = Path(__file__).resolve().parent.parent


def _write_config(tmp_path, extra=""):
    src = tmp_path / "src" / "FY26-27"
    src.mkdir(parents=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "paths:\n"
        f"  source_drive: '{src}'\n"
        "  lan_destination: '\\\\nas\\share\\FY26-27'\n"
        f"  runtime_dir: '{tmp_path / 'rt'}'\n"
        "  gcs_key_path: ''\n"
        "cloud:\n  enabled: false\n"
        "wol:\n  enabled: false\n"
        "notifications:\n  recipients: ['a@firm.com']\n"
        "dashboard:\n  auth_enabled: true\n  api_key: k\n" + extra, encoding="utf-8")
    return cfg


class TestEmailValidation:
    @pytest.mark.parametrize("good", [["a@firm.com"], ["a@firm.com", "b.c+x@sub.firm.co.in"], []])
    def test_valid_recipients(self, good):
        assert NotificationConfig(recipients=good).recipients == good

    @pytest.mark.parametrize("bad", [["not-an-email"], ["a@firm"], ["a@firm.com, b@firm.com"],
                                     ["a @firm.com"], [""]])
    def test_invalid_recipients_are_refused(self, bad):
        with pytest.raises(ValidationError, match="not an email address"):
            NotificationConfig(recipients=bad)

    def test_duplicates_are_refused(self):
        with pytest.raises(ValidationError, match="more than once"):
            NotificationConfig(recipients=["a@firm.com", "a@firm.com"])

    def test_sender_must_be_an_address_when_set(self):
        assert NotificationConfig(sender="").sender == ""
        with pytest.raises(ValidationError, match="not an email address"):
            NotificationConfig(sender="backup-server")


class TestLiveValidation:
    def test_valid_change_reports_ok_and_the_phrase_it_will_need(self, tmp_path):
        cfg = _write_config(tmp_path)
        before = cfg.read_bytes()
        out = ce.validate_changes(str(cfg), {"guards.max_delete_files": 100})
        assert out == {"ok": True, "changes": 1, "risky": ["guards.max_delete_files"],
                       "confirm_phrase": "CHANGE guards.max_delete_files"}
        assert cfg.read_bytes() == before           # nothing written

    def test_invalid_email_is_reported_on_its_key(self, tmp_path):
        out = ce.validate_changes(str(_write_config(tmp_path)),
                                  {"notifications.recipients": ["a@firm.com", "oops"]})
        assert out["ok"] is False
        assert out["errors"][0]["key"] == "notifications.recipients"
        assert "oops" in out["errors"][0]["message"]

    def test_schedule_clash_is_a_general_error(self, tmp_path):
        out = ce.validate_changes(str(_write_config(tmp_path)),
                                  {"schedule.lan_cron": "0 21 * * *",
                                   "schedule.audit_cron": "0 22 * * SUN"})
        assert out["ok"] is False and "run budgets" in out["errors"][0]["message"]

    def test_locked_key_is_reported(self, tmp_path):
        out = ce.validate_changes(str(_write_config(tmp_path)), {"dashboard.api_key": "x"})
        assert out["ok"] is False and "config.yaml on the server" in out["errors"][0]["message"]


class TestWidgets:
    def test_catalog_tells_the_page_how_to_draw_each_field(self, tmp_path):
        sections = ce.catalog(load_config(str(_write_config(tmp_path))))
        f = {x["key"]: x for s in sections for x in s["fields"]}
        assert f["notifications.recipients"]["widget"] == "email_list"
        assert f["notifications.sender"]["widget"] == "email"
        assert f["schedule.lan_cron"]["widget"] == "schedule"
        assert f["lan.enabled"]["widget"] == "toggle"
        assert f["cloud.storage_class"]["choices"] == ["STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"]
        assert "Asia/Kolkata" in f["schedule.timezone"]["choices"]


class TestIST:
    @pytest.mark.parametrize("iso", ["2026-10-15T21:05:00+05:30", "2026-10-15T15:35:00+00:00",
                                     "2026-10-15T15:35:00Z"])
    def test_any_offset_is_shown_as_ist(self, iso):
        assert format_ist(iso) == "2026-10-15 21:05 IST"

    def test_missing_and_garbage_never_raise(self):
        assert format_ist(None) == "-" and format_ist("garbage") == "garbage"


def _node(script: str) -> str:
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60,
                         env={"TZ": "America/New_York", "PATH": __import__("os").environ["PATH"]})
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _js_function(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    end = text.index("\n}\n", start) + 2
    return text[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
class TestBrowserCode:
    def test_schedule_picker_round_trips_every_shape(self):
        js = ROOT / "static" / "js" / "settings.js"
        consts = "\n".join(line for line in js.read_text(encoding="utf-8").splitlines()
                           if line.startswith("const DOW") or line.startswith("             ['FRI"))
        script = (consts + "\n" + _js_function(js, "parseCron") + "\n" + _js_function(js, "buildCron") +
                  "\nconst crons = ['0 21 * * *', '30 8 1 * *', '0 10 * * SUN', '5 2 * * 0', '0 9 * * MON',"
                  " '*/15 * * * *', '0 8 31 * *'];"
                  "\nconsole.log(JSON.stringify(crons.map(c => [c, parseCron(c), buildCron(parseCron(c))])));")
        rows = json.loads(_node(script))
        out = {c: (p, built) for c, p, built in rows}
        assert out["0 21 * * *"][0] == {"freq": "daily", "time": "21:00"}
        assert out["30 8 1 * *"][0] == {"freq": "monthly", "time": "08:30", "dom": 1}
        assert out["0 10 * * SUN"][0]["dow"] == "SUN"
        assert out["5 2 * * 0"][0]["dow"] == "SUN" and out["5 2 * * 0"][1] == "5 2 * * SUN"
        assert out["*/15 * * * *"][0]["freq"] == "custom"        # never mangled
        assert out["0 8 31 * *"][0]["freq"] == "custom"          # day 29-31 skips months
        for c in ("0 21 * * *", "30 8 1 * *", "0 10 * * SUN", "0 9 * * MON"):
            assert out[c][1] == c

    def test_dashboard_dates_are_ist_even_on_a_foreign_browser(self):
        fn = _js_function(ROOT / "static" / "js" / "dashboard.js", "formatFullDate")
        out = _node(fn + "\nconsole.log(formatFullDate('2026-10-15T15:35:00Z'));")
        assert out == "15 Oct 2026 at 9:05 PM IST"
