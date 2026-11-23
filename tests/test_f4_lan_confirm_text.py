"""F4 (manual-button review 2026-11-23): the LAN confirm text says what a LAN backup really does, in the classic
button AND in the per-FY button (the only manual route during the FY overlap): it wakes the NAS (only when WoL
is on), copies the changes, deletes on the NAS copy what was deleted at the source (robocopy /MIR mirror) and
switches the NAS off afterwards (only when that is on). Node drives the real script; skipped without Node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js" / "dashboard.js"
TEMPLATE = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
HARNESS = ROOT / "tests" / "js" / "confirm_text_harness.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")


def _confirm(kind: str, wol: str, shutdown: str) -> str:
    out = subprocess.run([shutil.which("node"), str(HARNESS), str(JS), kind, wol, shutdown],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])["confirm"]


@needs_node
@pytest.mark.parametrize("kind", ["classic", "fy"])
def test_lan_confirm_names_the_mirror_delete(kind):
    text = _confirm(kind, "yes", "yes")
    assert "deleted" in text and "NAS" in text and "source" in text.lower()


@needs_node
@pytest.mark.parametrize("kind", ["classic", "fy"])
def test_lan_confirm_follows_wol_and_shutdown_settings(kind):
    both = _confirm(kind, "yes", "yes")
    assert "wakes the NAS" in both and "switches the NAS off afterwards" in both
    neither = _confirm(kind, "no", "no")
    assert "wakes the NAS" not in neither and "switches the NAS off" not in neither
    assert "deleted" in neither            # the mirror text is not optional


@needs_node
def test_fy_confirm_still_names_the_year():
    assert "FY27-28" in _confirm("fy", "no", "no")


def test_template_gives_the_fy_cards_the_same_settings():
    section = TEMPLATE[TEMPLATE.index('id="fy-cards"'):][:300]
    assert "data-wol" in section and "data-shutdown" in section
