"""A second click while the first start request is in flight sends no second request, and the button is
disabled at once (before the request goes out). Same Node harness style as the F3 test; skipped without Node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js" / "dashboard.js"
HARNESS = ROOT / "tests" / "js" / "inflight_click_harness.js"


def _drive(scenario: str) -> dict:
    out = subprocess.run([shutil.which("node"), str(HARNESS), str(JS), scenario],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
@pytest.mark.parametrize("scenario", ["classic", "fy", "verify"])
def test_second_click_while_the_first_request_is_in_flight_sends_nothing(scenario):
    state = _drive(scenario)
    assert state["fetchesDuringFlight"] == 1 and state["fetches"] == 1
    assert state["disabledDuringFlight"], "the button is disabled while the request is in flight"
    assert state["textDuringFlight"] == "Starting..."


def test_classic_cards_disable_on_any_running_backup():
    js = JS.read_text(encoding="utf-8")
    assert "const anyBusy = " in js and js.count("anyBusy && btn") == 2
