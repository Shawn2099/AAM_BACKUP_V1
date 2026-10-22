"""Install-time checks (fresh-install plan R-1, I-3, I-4, I-14): deploy/install_checks.py."""

import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "install_checks", Path(__file__).resolve().parent.parent / "deploy" / "install_checks.py")
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)

NOW = datetime(2026, 10, 23, 5, 0, tzinfo=timezone.utc)
IST = timedelta(hours=5, minutes=30)


def test_timezone_matches():
    assert ic.check_timezone("Asia/Kolkata", NOW, local_offset=IST)[0] == "OK"


def test_pacific_clock_on_an_india_schedule_fails_with_the_fix():
    status, _, detail = ic.check_timezone("Asia/Kolkata", NOW, local_offset=timedelta(hours=-7))
    assert status == "FAIL" and "tzutil" in detail and "-07:00" in detail and "+05:30" in detail


def test_gcs_key_missing_empty_bad_and_good(tmp_path):
    assert ic.check_gcs_key("")[0] == "FAIL"
    assert ic.check_gcs_key(str(tmp_path / "nope.json"))[0] == "FAIL"
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert ic.check_gcs_key(str(bad))[0] == "FAIL"
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"type": "authorized_user"}))
    assert ic.check_gcs_key(str(wrong))[0] == "FAIL"
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"type": "service_account", "project_id": "p"}))
    assert ic.check_gcs_key(str(good))[0] == "OK"


@pytest.mark.parametrize("source, today, history, want", [
    ("D:\\FY26-27", date(2026, 10, 23), False, "OK"),
    ("D:\\FY25-26", date(2026, 10, 23), False, "FAIL"),   # fresh install one year behind
    ("D:\\FY25-26", date(2026, 10, 23), True, "WARN"),    # pending April switch on a running site
    ("D:\\FY24-25", date(2026, 10, 23), True, "FAIL"),    # more than one year behind
    ("D:\\FY27-28", date(2026, 10, 23), False, "FAIL"),   # clock behind
    ("D:\\Data", date(2026, 10, 23), False, "FAIL"),      # no year folder
])
def test_financial_year(source, today, history, want):
    assert ic.check_financial_year(source, today, history)[0] == want


def test_trial_box_paths_pass():
    assert ic.check_financial_year("E:\\FY26-27", date(2026, 10, 23), True)[0] == "OK"


def _wol(lan=True, wol=True, dest="\\\\10.10.151.53\\aam_nas\\FY26-27", ip="10.10.151.53", mac="00:11:22:33:44:55"):
    return ic.check_wol_target(lan, wol, dest, ip, mac)


def test_wol_same_host_ok():
    assert [s for s, _, _ in _wol()] == ["OK"]


def test_wol_different_host_fails():
    lines = _wol(ip="10.10.186.231")
    assert any(s == "FAIL" and "10.10.151.53" in d and "10.10.186.231" in d for s, _, d in lines)


def test_wol_off_or_nas_off_is_not_checked():
    # the trial box: WoL off with a stale server_ip must still pass
    assert [s for s, _, _ in _wol(wol=False, ip="10.10.186.231")] == ["OK"]
    assert [s for s, _, _ in _wol(lan=False, ip="10.10.186.231")] == ["OK"]


def test_wol_hostname_is_a_warning_not_a_failure():
    lines = _wol(dest="\\\\nas\\backup\\FY26-27")
    assert [s for s, _, _ in lines] == ["WARN"]


def test_wol_placeholders_fail():
    lines = _wol(mac="aa:bb:cc:dd:ee:ff", ip="192.0.2.10", dest="\\\\192.0.2.10\\x\\FY26-27")
    assert sum(1 for s, _, _ in lines if s == "FAIL") == 2


def test_forward_slash_unc_is_parsed():
    assert ic._unc_host("//10.1.1.1/share/FY26-27") == "10.1.1.1"
    assert ic._unc_host("D:\\FY26-27") is None
