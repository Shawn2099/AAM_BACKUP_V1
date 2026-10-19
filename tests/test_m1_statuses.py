"""M1 / plan §5.3 — single status vocabulary."""

import re
from pathlib import Path

import pytest

from core import statuses as st
from core.statuses import AuditStatus, RunStatus

ROOT = Path(__file__).resolve().parent.parent


def test_every_run_status_has_exactly_one_known_category():
    groups = [st.SUCCESS_STATUSES - st._LEGACY_SUCCESS, st.PARTIAL_STATUSES,
              st.BLOCKED_STATUSES, st.RUNNING_STATUSES, st.SKIPPED_STATUSES,
              st.FAILED_STATUSES]
    for s in RunStatus:
        hits = [g for g in groups if s in g]
        assert len(hits) == 1, f"{s} in {len(hits)} groups"
        assert st.category(s) != st.CATEGORY_UNKNOWN
        assert st.label(s) and st.label(s) != s.value or s.value in ("",)


@pytest.mark.parametrize("status, cat", [
    ("CLOUD_COMPLETE", "success"), ("LAN_COMPLETE", "success"),
    ("CLOUD_NO_CHANGES_COMPLETE", "no-changes"), ("LAN_NO_CHANGES_COMPLETE", "no-changes"),
    ("CLOUD_PARTIAL", "partial"), ("LAN_SUSPECT", "failed"), ("CLOUD_SUSPECT", "failed"),
    ("CLOUD_VERIFY_FAILED", "failed"), ("LAN_BLOCKED", "blocked"),
    ("CLOUD_SLOT_TIMEOUT", "failed"), ("LAN_ABANDONED", "failed"),
    ("LAN_RUNNING", "running"), ("CLOUD_SKIPPED", "skipped"),
    ("SOMETHING_NEW", "unknown"), (None, "unknown"), ("", "unknown"),
])
def test_category(status, cat):
    assert st.category(status) == cat


def test_success_is_exact_not_suffix_based():
    # "_INCOMPLETE" / "NOT_COMPLETE" style values must never count as success.
    for bad in ("CLOUD_INCOMPLETE", "LAN_NOT_COMPLETE", "X_COMPLETE"):
        assert not st.is_success(bad)
    assert st.is_success("CLOUD_NO_CHANGES_COMPLETE")


def test_unknown_status_label_is_never_truncated():
    assert st.label("CLOUD_SOMETHING_VERY_LONG") == "CLOUD_SOMETHING_VERY_LONG"


def test_describe_never_calls_failed_files_a_clean_complete():
    text = st.describe("LAN_COMPLETE", 10, 2)
    assert "failed" in text and "attention" in text


def test_describe_partial_and_suspect():
    assert "3 could not be copied" in st.describe("LAN_PARTIAL", 7, 3)
    assert "unproven" in st.describe("CLOUD_SUSPECT")


def test_leg_success_sets():
    assert st.CLOUD_SUCCESS_STATUSES == {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"}
    assert st.LAN_SUCCESS_STATUSES == {"LAN_COMPLETE", "LAN_NO_CHANGES_COMPLETE"}


def test_audit_statuses():
    assert {a.value for a in AuditStatus} == {
        "VERIFIED", "VERIFICATION_FAILED", "AUDIT_INCOMPLETE", "NOT_VERIFIED", "PENDING_BACKUP"}


def test_no_suffix_based_status_classification_left_in_consumers():
    """Consumers must use core.statuses, not endswith/includes on status."""
    offenders = []
    pattern = re.compile(
        r"status[^\n]{0,40}(endswith|includes)\(\s*['\"]_(COMPLETE|PARTIAL|FAILED|SKIPPED)"
        r"|GLOB '\*_COMPLETE'"
        r"|\"NO_CHANGES\" (not )?in"
    )
    for rel in ("core/report.py", "core/manifest.py", "ui.py", "flow.py",
                "static/js/dashboard.js"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            offenders.append(f"{rel}: {m.group(0)}")
    assert not offenders, offenders
