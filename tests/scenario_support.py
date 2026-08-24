"""Shared support for catalog-scenario tests (docs/SCENARIO_CATALOG_V2.md).

Real hardware only — zero mocks. Each scenario test records the EXACT
observable operation produced by the program (status, exit code, filesystem
state, error text) into docs/SCENARIO_TEST_REPORT.md so QA can audit the
actual behaviour against the catalog's Expected Operation contract.
"""

import inspect
import json
from datetime import datetime
from pathlib import Path

import pytest

from models.config import CONFIG_PATH, load_config
from tests.e2e_helpers import nas_test_dir, source_test_dir  # real prod-derived paths

REPORT = Path(__file__).resolve().parent.parent / "docs" / "SCENARIO_TEST_REPORT.md"

GATE_ENV = "AAM_RUN_REAL_HARDWARE"


def _caller_branch_tag() -> str | None:
    """P4-SID: the calling module's BRANCH_TAG, if it declares one.

    Scenario files set e.g. BRANCH_TAG = "A" so ledger rows read "A/LAN-03"
    and can never collide with RT-xx rows for the same sid.
    """
    for frame_info in inspect.stack():
        if frame_info.filename == __file__:
            continue
        tag = frame_info.frame.f_globals.get("BRANCH_TAG")
        if isinstance(tag, str) and tag:
            return tag
    return None


def real_gate():
    return pytest.mark.skipif(
        __import__("os").environ.get(GATE_ENV) != "1",
        reason=f"F4: real-hardware scenario - set {GATE_ENV}=1",
    )


def cfg():
    return load_config(str(CONFIG_PATH))


def ensure_canary() -> Path:
    d = nas_test_dir()
    d.mkdir(parents=True, exist_ok=True)
    canary = d / ".AAM_TARGET_MOUNTED"
    canary.touch(exist_ok=True)
    return canary


def record_op(scenario_id: str, result: str, ops: dict) -> None:
    """Append the exact observed operation for one scenario to the report."""
    if not REPORT.exists():
        REPORT.write_text(
            "# Scenario Test Report — exact observed operations\n\n"
            "| Batch | ID | Verdict | Exact observed operation |\n"
            "|---|---|---|---|\n",
            encoding="utf-8",
        )
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ops_json = json.dumps(ops, default=str, ensure_ascii=False)
    tag = _caller_branch_tag()
    full_sid = f"{tag}/{scenario_id}" if tag else scenario_id
    with REPORT.open("a", encoding="utf-8") as f:
        f.write(f"| {stamp} | {full_sid} | {result} | `{ops_json}` |\n")
    # Also surface in pytest output (-s) for immediate visibility
    print(f"\n=== OP[{full_sid}] {result}: {ops_json}")
