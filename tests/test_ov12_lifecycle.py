"""OV-12.1 - GCS lifecycle: an ACTIVE FY prefix (the configured year and the early next year) is never moved to
ARCHIVE (plan D-13, R-19). The proposed file lists only CLOSED years in its ARCHIVE rule and has no unscoped
age-based ARCHIVE rule (the old `age: 365` rule would archive live objects of the active year from January of the
following year, while that year is still being backed up)."""
import json
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
PROPOSED = DEPLOY / "gcs_lifecycle.fy_overlap_proposed.json"
CURRENT = DEPLOY / "gcs_lifecycle.json"
ACTIVE_PREFIXES = ("FY26-27/", "FY27-28/")        # configured year + the early next year (Jan 2027 on)


def _archive_rules(path):
    rules = json.loads(path.read_text(encoding="utf-8"))["rule"]
    return [r for r in rules if r["action"].get("storageClass") == "ARCHIVE"
            and r["action"]["type"] == "SetStorageClass"]


def _could_archive(rule, prefix):
    cond = rule["condition"]
    prefixes = cond.get("matchesPrefix")
    if prefixes is None:
        return True                                  # no prefix filter: matches every object in the bucket
    return any(prefix.startswith(p) for p in prefixes)


def test_the_proposed_file_never_archives_an_active_fy_prefix():
    for rule in _archive_rules(PROPOSED):
        for active in ACTIVE_PREFIXES:
            assert not _could_archive(rule, active), (rule, active)


def test_every_archive_rule_is_scoped_to_a_prefix():
    assert all("matchesPrefix" in r["condition"] for r in _archive_rules(PROPOSED))


def test_the_proposed_file_still_archives_closed_years():
    prefixes = [p for r in _archive_rules(PROPOSED) for p in r["condition"]["matchesPrefix"]]
    assert "FY24-25/" in prefixes and "FY25-26/" in prefixes


def test_the_noncurrent_version_rules_are_unchanged():
    def keep(path):
        return [r for r in json.loads(path.read_text(encoding="utf-8"))["rule"]
                if r["condition"].get("isLive") is False or r["action"]["type"] == "AbortIncompleteMultipartUpload"]
    assert keep(PROPOSED) == keep(CURRENT)


def test_the_current_file_has_the_unscoped_age_rule_that_this_proposal_removes():
    """Documents the finding: the git version can archive an active year's objects."""
    assert any(not r["condition"].get("matchesPrefix") for r in _archive_rules(CURRENT))
