"""The FY archive step runs gcloud without leaving a copy of the Google key behind.

Proven on the trial box (2026-10-19, SDK 589.0.0): `gcloud auth activate-service-account` stores the
key in `<CLOUDSDK_CONFIG>\credentials.db`. `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=<key file>` runs the
same command with no activation and no stored credentials. The SDK ships without Python, so
`CLOUDSDK_PYTHON` is set from the interpreter that runs the agent, and its settings live in the
runtime folder (`CLOUDSDK_CONFIG`), not in a user profile that the service account may not have.
"""
import sys
from unittest.mock import patch

from core.fy_rollover import run_archive_transition
from test_support.fake_tools import fake_run_tool


def _run(tmp_path, **kw):
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}')
    with patch("core.fy_rollover._resolve_gcloud", return_value="gcloud"), \
         fake_run_tool("core.fy_rollover", exit_code=0) as calls:
        ok = run_archive_transition("b", "FY25-26", str(key), **kw)
    return ok, calls


def test_the_key_is_passed_as_a_file_never_activated(tmp_path):
    ok, calls = _run(tmp_path, config_dir=str(tmp_path / "gcloud"))
    assert ok is True
    for c in calls:
        assert "activate-service-account" not in " ".join(c["argv"])
        assert c["env"]["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] == str(tmp_path / "key.json")


def test_gcloud_gets_a_python_and_a_config_folder_in_the_runtime(tmp_path):
    _, calls = _run(tmp_path, config_dir=str(tmp_path / "gcloud"))
    env = calls[-1]["env"]
    assert env["CLOUDSDK_PYTHON"] == sys.executable
    assert env["CLOUDSDK_CONFIG"] == str(tmp_path / "gcloud")
    assert (tmp_path / "gcloud").is_dir()
    assert env["CLOUDSDK_CORE_DISABLE_PROMPTS"] == "1"


def test_a_python_already_set_for_the_machine_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDSDK_PYTHON", r"C:\somewhere\python.exe")
    _, calls = _run(tmp_path, config_dir=str(tmp_path / "gcloud"))
    assert calls[-1]["env"]["CLOUDSDK_PYTHON"] == r"C:\somewhere\python.exe"


def test_the_first_step_only_proves_gcloud_starts(tmp_path):
    _, calls = _run(tmp_path, config_dir=str(tmp_path / "gcloud"))
    assert calls[0]["name"] == "gcloud-check" and calls[0]["argv"][1:] == ["--version"]


def test_without_a_config_folder_gcloud_keeps_its_own_default(tmp_path):
    _, calls = _run(tmp_path)
    assert "CLOUDSDK_CONFIG" not in calls[-1]["env"] or \
        calls[-1]["env"]["CLOUDSDK_CONFIG"] == __import__("os").environ.get("CLOUDSDK_CONFIG")
