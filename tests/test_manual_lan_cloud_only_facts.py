"""Cloud-only / manual-only config and mail facts (owner 2026-11-23, step F; cards T1, T2, T12 partly)."""
from types import SimpleNamespace

import pendulum
import pytest
from pydantic import ValidationError

from core import email_compose as ec
from core import notify as nt
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import AppConfig

PLACEHOLDER = "\\\\placeholder-nas\\share\\FY26-27"


def _cfg(tmp_path, lan=None, cloud=None, wol=None) -> AppConfig:
    key = tmp_path / "k.json"
    key.write_text("{}")
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": PLACEHOLDER,
               "runtime_dir": str(tmp_path / "rt"), "gcs_key_path": str(key)},
        wol=wol if wol is not None else {"enabled": False},
        lan={"enabled": False, **(lan or {})},
        cloud={"enabled": True, "bucket": "aam-test-bucket", "project_number": "1", **(cloud or {})},
        dashboard={"public_url": "http://backup-server:8080"},
        schedule={"lan_cron": "0 21 * * *", "cloud_cron": "0 2 * * *"})


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


# ── config validation: reported, NOT relaxed (owner rule: only when clearly safe) ───────────────────

def test_cloud_only_config_loads_when_wol_is_off_and_a_unc_placeholder_is_given(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.lan.enabled is False and cfg.wol.enabled is False


def test_cloud_only_config_with_default_wol_still_fails_with_a_clear_message(tmp_path):
    """T2: wol.enabled defaults to true and then needs a MAC and an IP; the message names the fix."""
    with pytest.raises(ValidationError) as e:
        _cfg(tmp_path, wol={})
    assert "Wake-on-LAN off" in str(e.value)


def test_a_drive_letter_destination_is_still_rejected_even_when_lan_is_off(tmp_path):
    with pytest.raises(ValidationError):
        AppConfig(paths={"source_drive": str(tmp_path), "lan_destination": "E:\\backup",
                         "runtime_dir": str(tmp_path / "rt")},
                  wol={"enabled": False}, lan={"enabled": False}, cloud={"enabled": False})


# ── mail facts ────────────────────────────────────────────────────────────────────────────────────

def test_facts_never_show_the_placeholder_nas_for_a_cloud_only_config(tmp_path):
    facts = ec._facts(_cfg(tmp_path))
    assert facts["nas"] == "the NAS" and "placeholder" not in facts["dest"]


def test_facts_show_the_real_nas_when_lan_is_on(tmp_path):
    facts = ec._facts(_cfg(tmp_path, lan={"enabled": True}))
    assert facts["nas"] == "placeholder-nas" and facts["dest"] == PLACEHOLDER


def test_a_cloud_only_mail_has_no_placeholder_host_anywhere(tmp_path, db):
    ev = nt.AlertEvent(key="backup:cloud:CLOUD_FAILED", kind="FAILURE", mode="cloud",
                       title="Cloud backup failed", message="Cloud backup Failed: x",
                       status="CLOUD_FAILED", exit_code=1, run_id="r-cloud",
                       occurred_at="2026-10-18T21:08:07+05:30")
    _, html, text = ec.compose(_cfg(tmp_path), ev, db, now=pendulum.datetime(2026, 10, 19, 8, tz=IST))
    assert "placeholder-nas" not in html and "placeholder-nas" not in text


def test_the_dashboard_link_ignores_the_placeholder_nas_when_lan_is_off(tmp_path):
    assert nt._nas_host(_cfg(tmp_path)) is None
    assert nt._nas_host(_cfg(tmp_path, lan={"enabled": True})) == "placeholder-nas"


# ── "what happens next" for a manual-only leg ────────────────────────────────────────────────────

def _failure(mode):
    leg = mode.upper()
    return nt.AlertEvent(key=f"backup:{mode}:{leg}_FAILED", kind="FAILURE", mode=mode, title="failed",
                         message="x", status=f"{leg}_FAILED", exit_code=1, run_id="r1",
                         occurred_at="2026-10-18T21:08:07+05:30")


@pytest.mark.parametrize("mode", ["lan", "cloud"])
def test_next_text_does_not_promise_an_automatic_run_for_a_manual_only_leg(tmp_path, db, mode):
    cfg = _cfg(tmp_path, lan={"enabled": True, "scheduled": mode != "lan"},
               cloud={"scheduled": mode != "cloud"})
    text = ec._next_text(cfg, _failure(mode), None, pendulum.datetime(2026, 10, 19, 8, tz=IST))
    assert "started by hand" in text and "runs automatically" not in text


@pytest.mark.parametrize("mode", ["lan", "cloud"])
def test_next_text_keeps_the_automatic_promise_for_a_nightly_leg(tmp_path, db, mode):
    cfg = _cfg(tmp_path, lan={"enabled": True})
    text = ec._next_text(cfg, _failure(mode), None, pendulum.datetime(2026, 10, 19, 8, tz=IST))
    assert "runs automatically" in text


# ── the FY activation mail ──────────────────────────────────────────────────────────────────────

def test_activation_mail_wording_follows_the_leg_modes(tmp_path):
    from core.fy_overlap import _nightly_words

    nightly = _cfg(tmp_path, lan={"enabled": True})
    assert _nightly_words(nightly) == "the nightly backups copy both folders"
    mixed = _cfg(tmp_path, lan={"enabled": True, "scheduled": False})
    assert "nightly cloud backup copies both folders" in _nightly_words(mixed)
    assert "NAS backup does so whenever it is started by hand" in _nightly_words(mixed)
    both = _cfg(tmp_path, lan={"enabled": True, "scheduled": False}, cloud={"scheduled": False})
    assert _nightly_words(both).startswith("every backup copies both folders whenever it is started by hand")


def test_support_bundle_does_not_show_the_placeholder_target_when_lan_is_off(tmp_path):
    from core import support_bundle as sb

    line = sb._settings(_cfg(tmp_path))[0]
    assert "(LAN off)" in line and "placeholder-nas" not in line
