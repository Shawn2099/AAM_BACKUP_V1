"""Plan S3: the Google key stored encrypted; rclone gets it in memory only."""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from core import gcs_key
from core import secrets as sec
from core.health import check_gcs_key
from core.rclone_config import temp_rclone_config

windows = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")
KEY = {"type": "service_account", "client_email": "x@y.iam.gserviceaccount.com",
       "private_key": "-----BEGIN PRIVATE KEY-----\nSECRETBODY\n-----END PRIVATE KEY-----\n"}


def _plain(tmp_path):
    p = tmp_path / "key.json"
    p.write_text(json.dumps(KEY), encoding="utf-8")
    return p


def test_a_plain_key_is_used_as_before(tmp_path):
    with temp_rclone_config(str(_plain(tmp_path)), "asia-south1", "1", "STANDARD") as cfg:
        text = Path(cfg).read_text(encoding="utf-8")
        assert "service_account_file = " in text and gcs_key.RCLONE_ENV not in os.environ


@windows
def test_a_protected_key_reaches_rclone_through_the_environment_only(tmp_path):
    protected = gcs_key.protect_file(_plain(tmp_path), tmp_path / "gcs_key.json.dpapi")
    with temp_rclone_config(str(protected), "asia-south1", "1", "STANDARD") as cfg:
        text = Path(cfg).read_text(encoding="utf-8")
        assert "service_account_file" not in text and "SECRETBODY" not in text
        assert json.loads(os.environ[gcs_key.RCLONE_ENV])["client_email"] == KEY["client_email"]
    assert gcs_key.RCLONE_ENV not in os.environ              # gone with the temporary config


@windows
def test_the_environment_is_restored_even_when_the_body_raises(tmp_path):
    protected = gcs_key.protect_file(_plain(tmp_path), tmp_path / "k.json.dpapi")
    with pytest.raises(RuntimeError):
        with temp_rclone_config(str(protected), "asia-south1", "1", "STANDARD"):
            raise RuntimeError("tool failed")
    assert gcs_key.RCLONE_ENV not in os.environ


@windows
def test_no_plain_key_text_is_written_anywhere_in_the_temp_folder(tmp_path):
    protected = gcs_key.protect_file(_plain(tmp_path), tmp_path / "k.json.dpapi")
    with temp_rclone_config(str(protected), "asia-south1", "1", "STANDARD"):
        for f in Path(tempfile.gettempdir()).glob("rclone_*.conf"):
            assert "SECRETBODY" not in f.read_text(encoding="utf-8", errors="ignore")
    assert "SECRETBODY" not in (tmp_path / "k.json.dpapi").read_text(encoding="utf-8")


@windows
def test_health_check_accepts_a_good_protected_key_and_refuses_bad_ones(tmp_path):
    good = gcs_key.protect_file(_plain(tmp_path), tmp_path / "k.json.dpapi")
    assert check_gcs_key(str(good)) == (True, "")
    bad = tmp_path / "bad.json.dpapi"
    bad.write_text("dpapi:AAAA", encoding="utf-8")
    ok, why = check_gcs_key(str(bad))
    assert ok is False and "cannot be used on this server" in why
    notjson = tmp_path / "notjson.json.dpapi"
    notjson.write_text(sec.protect("hello"), encoding="utf-8")
    assert check_gcs_key(str(notjson))[0] is False
    plain_in_dpapi = tmp_path / "plain.json.dpapi"
    plain_in_dpapi.write_text(json.dumps(KEY), encoding="utf-8")
    assert check_gcs_key(str(plain_in_dpapi))[0] is False       # a .dpapi file must really be protected


def test_protect_file_refuses_a_file_that_is_not_json(tmp_path):
    bad = tmp_path / "k.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        gcs_key.protect_file(bad, tmp_path / "k.json.dpapi")
