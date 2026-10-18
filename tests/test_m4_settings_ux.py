"""M4 4.7 follow-up (owner feedback 2026-10-15): settings page "never able to mess up".

- recipients / sender are validated as email addresses (server side - the
  page AND config.yaml), duplicates refused;
- /api/settings/validate gives the exact verdict a save would, live;
- fixed-set values are drop-downs (storage class, timezone), schedules use a
  day/time picker that writes cron (round-trip tested in Node);
- every client-facing time is IST (format_ist; dashboard formatter).
"""

from test_support import ui_login  # noqa: E402
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
        + ui_login.DASHBOARD_YAML + extra, encoding="utf-8")
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
        out = ce.validate_changes(str(_write_config(tmp_path)), {"dashboard.bind_address": "0.0.0.0"})
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

    def test_a_time_without_offset_is_local_ist_not_utc(self):
        # regression: it was read as UTC and shown 5.5 h late
        assert format_ist("2026-10-13T10:00:00") == "2026-10-13 10:00 IST"

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


# ── owner feedback 2: basic / advanced split + "Run FY rollover now" ─────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import ui  # noqa: E402
from core import fy_rollover  # noqa: E402
from core.manifest import ManifestDB  # noqa: E402


def test_basic_tab_holds_what_a_client_changes_the_rest_is_advanced(tmp_path):
    f = {x["key"]: x for s in ce.catalog(load_config(str(_write_config(tmp_path))))
         for x in s["fields"]}
    for key in ("notifications.recipients", "schedule.lan_cron", "budget.cloud_max_hours",
                "paths.lan_destination", "wol.mac_address", "firm_name"):
        assert f[key]["level"] == "basic", key
    for key in ("guards.max_delete_percent", "watchdog.failure_threshold", "cloud.verify_timeout_seconds",
                "maintenance.sqlite_synchronous", "schedule.timezone", "dashboard.bind_address"):
        assert f[key]["level"] == "advanced", key
    assert all(k in f for k in ce.BASIC_KEYS)        # no typo in the basic list


class TestManualRollover:
    @pytest.mark.parametrize("configured,current,kind,can_run", [
        ("FY26-27", "FY26-27", fy_rollover.NONE, False),
        ("FY26-27", "FY27-28", fy_rollover.FORWARD, True),
        ("FY25-26", "FY27-28", fy_rollover.JUMP, True),          # runs, then asks for approval
        ("FY27-28", "FY26-27", fy_rollover.CLOCK_BEHIND, False),  # never forceable
    ])
    def test_rules(self, configured, current, kind, can_run):
        s = fy_rollover.manual_run_state(configured, current)
        assert (s["kind"], s["can_run"]) == (kind, can_run)

    def test_no_fy_folder_means_no_rollover(self):
        assert fy_rollover.manual_run_state(None, "FY26-27")["can_run"] is False


@pytest.fixture
def rollover_rig(monkeypatch, tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(**ui_login.DASHBOARD),
        paths=SimpleNamespace(source_drive="E:\DATA\FY26-27",
                              lan_destination="\\nas\share\FY26-27"))
    calls = []

    async def fake_arun_deployment(name, parameters=None, timeout=None):
        calls.append(name)
        return SimpleNamespace(id="fr-1", state=None)

    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "arun_deployment", fake_arun_deployment)
    yield SimpleNamespace(client=TestClient(ui.app, raise_server_exceptions=False), calls=calls,
                          db=db, monkeypatch=monkeypatch)
    db.close()


H = {}      # the TestClient is signed in already (tests/conftest.py)


class TestRolloverEndpoints:
    def test_not_due_is_refused(self, rollover_rig):
        rollover_rig.monkeypatch.setattr(ui, "get_fy_prefix", lambda: "FY26-27")
        assert rollover_rig.client.get("/api/rollover", headers=H).json()["can_run"] is False
        r = rollover_rig.client.post("/api/rollover/run?wait=false", headers=H)
        assert r.status_code == 409 and "Not due" in r.json()["detail"]
        assert rollover_rig.calls == []

    def test_due_starts_the_normal_rollover_check(self, rollover_rig):
        rollover_rig.monkeypatch.setattr(ui, "get_fy_prefix", lambda: "FY27-28")
        status = rollover_rig.client.get("/api/rollover", headers=H).json()
        assert status["can_run"] is True and status["configured_fy"] == "FY26-27"
        r = rollover_rig.client.post("/api/rollover/run?wait=false", headers=H)
        assert r.status_code == 202 and r.json()["status"] == "running", r.text   # started
        assert rollover_rig.calls == ["rollover-check/rollover-check"]

    def test_clock_behind_is_never_started(self, rollover_rig):
        rollover_rig.monkeypatch.setattr(ui, "get_fy_prefix", lambda: "FY25-26")
        r = rollover_rig.client.post("/api/rollover/run?wait=false", headers=H)
        assert r.status_code == 409 and "backwards" in r.json()["detail"]

    def test_already_running_is_refused(self, rollover_rig):
        rollover_rig.monkeypatch.setattr(ui, "get_fy_prefix", lambda: "FY27-28")
        rollover_rig.monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=True))
        r = rollover_rig.client.post("/api/rollover/run?wait=false", headers=H)
        assert r.status_code == 409 and rollover_rig.calls == []

    def test_last_attempt_is_shown_in_ist(self, rollover_rig):
        rollover_rig.monkeypatch.setattr(ui, "get_fy_prefix", lambda: "FY27-28")
        rollover_rig.db.insert_run({"run_id": "r1", "mode": "rollover",
                                    "started_at": "2027-04-01T00:30:00+00:00",
                                    "status": "ROLLOVER_BLOCKED", "error_message": "final LAN backup failed"})
        last = rollover_rig.client.get("/api/rollover", headers=H).json()["last_attempt"]
        assert last["when"] == "2027-04-01 06:00 IST" and "final LAN backup failed" in last["error"]
