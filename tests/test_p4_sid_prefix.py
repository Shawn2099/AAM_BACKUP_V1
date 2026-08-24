"""P4-SID - ledger sid collision fix: scenario rows carry their branch tag.

Evidence: RT-01 and Branch-A both wrote rows tagged LAN-03, making the
ledger ambiguous. Contract: a test module declaring module-level BRANCH_TAG
gets its sids recorded as "<TAG>/<sid>"; modules without one are unchanged.
"""
import io
import sys
from contextlib import redirect_stdout

import pytest

from tests import scenario_support


def _capture_record(scenario_id: str):
    buf = io.StringIO()
    with redirect_stdout(buf):
        scenario_support.record_op(scenario_id, "PASS", {"probe": 1})
    return buf.getvalue()


@pytest.fixture
def this_module_tag():
    """Declare BRANCH_TAG on THIS calling module - exactly what scenario
    files do - and clean up afterwards."""
    monkey_target = sys.modules[__name__]
    yield monkey_target
    if hasattr(monkey_target, "BRANCH_TAG"):
        delattr(monkey_target, "BRANCH_TAG")


def test_branch_tag_prefixes_sid(this_module_tag):
    this_module_tag.BRANCH_TAG = "X"
    out = _capture_record("ZZ-99")
    assert "OP[X/ZZ-99]" in out, out


def test_no_tag_keeps_plain_sid():
    out = _capture_record("QQ-01")
    assert "OP[QQ-01]" in out, out


def test_report_row_writes_prefixed_id(tmp_path, monkeypatch, this_module_tag):
    this_module_tag.BRANCH_TAG = "Y"
    monkeypatch.setattr(scenario_support, "REPORT", tmp_path / "rep.md")
    _capture_record("AB-02")
    content = (tmp_path / "rep.md").read_text(encoding="utf-8")
    assert "| Y/AB-02 |" in content
