"""F3 (manual-button review 2026-11-23): after the answer of POST /trigger/<leg> the classic button is always
usable again. A run that finishes inside the wait (HTTP 200) used to leave it greyed at 'Starting...' until a
page reload, because the status poll only resets the button of a run that is NOT 'Starting...'.

Behaviour test: dashboard.js runs in Node with stubbed browser objects (tests/js/trigger_button_harness.js);
skipped when Node.js is not installed. A string check of the script always runs."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js" / "dashboard.js"
HARNESS = ROOT / "tests" / "js" / "trigger_button_harness.js"


def _drive(scenario: str) -> dict:
    out = subprocess.run([shutil.which("node"), str(HARNESS), str(JS), scenario],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
@pytest.mark.parametrize("scenario", ["ok", "fail", "throw"])
def test_button_is_usable_again_after_every_answer(scenario):
    state = _drive(scenario)
    assert state["text"] == "Run Cloud Backup"
    assert state["pointer"] == "" and state["opacity"] == "" and not state["disabled"]
    assert state["toasts"], "the result is shown"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_a_finished_run_shows_its_result():
    state = _drive("ok")
    assert state["toasts"][0] == ["success", "The Cloud backup completed."]


def test_script_resets_the_button_before_showing_the_result():
    js = JS.read_text(encoding="utf-8")
    body = js[js.index("async function triggerBackup"):]
    assert body.index("resetTriggerButton(btn, mode);") < body.index("if (response.ok)")
