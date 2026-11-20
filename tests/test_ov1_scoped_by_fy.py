"""OV-1.2 .. OV-1.4 - every per-FY read / write is scoped by `fy` (plan 5.4; R-1, R-2, R-5, R-6, R-8, G-1)."""

import pytest

from core.manifest import ALL_FYS, ManifestDB
from core.backup_repository import record_sync_results

A, B = "FY26-27", "FY27-28"


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _run(db, run_id, mode, fy, status, started="2027-01-05T02:00:00+05:30", **extra):
    db.insert_run({"run_id": run_id, "mode": mode, "fy": fy, "started_at": started,
                   "status": status, **extra})


# ── OV-1.2 file catalog ──────────────────────────────────────────────────────

def test_same_relative_path_in_two_fys_stays_independent(db):
    db.upsert_file_entry("Clients/a.xlsx", 10, 1.0, fy=A, cloud_status="synced")
    db.upsert_file_entry("Clients/a.xlsx", 99, 2.0, fy=B, cloud_status="synced")
    assert db.get_entry("Clients/a.xlsx", fy=A)["file_size"] == 10
    assert db.get_entry("Clients/a.xlsx", fy=B)["file_size"] == 99
    assert db.file_count("cloud_status", fy=A) == 1
    assert db.file_count("cloud_status", fy=B) == 1
    assert db.file_count("cloud_status", fy=ALL_FYS) == 2


def test_pruning_one_fy_leaves_the_other(db):
    for fy in (A, B):
        db.bulk_upsert_synced([{"path": "x.txt", "size": 1, "mtime": 1},
                               {"path": "y.txt", "size": 1, "mtime": 1}], "cloud", fy=fy)
    assert db.prune_stale_synced("cloud", {"x.txt"}, fy=A) == 1      # y.txt gone from FY A only
    assert sorted(db.get_synced_paths("cloud", fy=A)) == ["x.txt"]
    assert sorted(db.get_synced_paths("cloud", fy=B)) == ["x.txt", "y.txt"]


def test_clearing_and_deleting_one_fy_leaves_the_other(db):
    for fy in (A, B):
        db.bulk_upsert_synced([{"path": "x.txt", "size": 1, "mtime": 1}], "lan", fy=fy)
    assert db.clear_mode_status("lan", ["x.txt"], fy=A) == 1
    assert db.get_synced_paths("lan", fy=A) == []
    assert db.get_synced_paths("lan", fy=B) == ["x.txt"]
    db.delete_entries(["x.txt"], fy=B)
    assert db.get_entry("x.txt", fy=B) is None


def test_cloud_synced_entries_per_fy(db):
    db.bulk_upsert_synced([{"path": "x.txt", "size": 5, "mtime": 7}], "cloud", fy=A)
    assert db.get_cloud_synced_entries(fy=A) == {"x.txt": (5, 7)}
    assert db.get_cloud_synced_entries(fy=B) == {}


def test_record_sync_results_is_per_fy(db):
    record_sync_results(db, "cloud", [{"path": "a.txt", "size": 1, "mtime": 1}], fy=A)
    record_sync_results(db, "cloud", [{"path": "b.txt", "size": 1, "mtime": 1}], fy=B)
    # a sync of FY B must not prune FY A's rows (the old single-catalog behaviour did)
    assert db.get_synced_paths("cloud", fy=A) == ["a.txt"]
    assert db.get_synced_paths("cloud", fy=B) == ["b.txt"]
    record_sync_results(db, "cloud", [], removed=["a.txt"], fy=B)
    assert db.get_synced_paths("cloud", fy=A) == ["a.txt"]


def test_writers_refuse_all_fys_and_blank(db):
    with pytest.raises(ValueError):
        db.upsert_file_entry("a", 1, 1.0, fy=ALL_FYS)
    with pytest.raises(ValueError):
        db.bulk_upsert_synced([{"path": "a"}], "cloud", fy="")


def test_fy_has_no_default():
    with pytest.raises(TypeError):
        ManifestDB(":memory:").file_count("lan_status")          # noqa - missing fy
    with pytest.raises(TypeError):
        ManifestDB(":memory:").last_successful_run("cloud")       # noqa - missing fy


# ── OV-1.3 run history ───────────────────────────────────────────────────────

def test_insert_run_requires_fy(db):
    with pytest.raises(KeyError):
        db.insert_run({"run_id": "r", "mode": "cloud", "started_at": "t", "status": "CLOUD_RUNNING"})


def test_success_of_one_fy_never_answers_for_the_other(db):
    _run(db, "a1", "cloud", A, "CLOUD_FAILED", "2027-01-05T02:00:00+05:30")
    _run(db, "b1", "cloud", B, "CLOUD_COMPLETE", "2027-01-05T02:30:00+05:30")
    assert db.last_successful_run("cloud", fy=A) is None
    assert db.last_successful_run("cloud", fy=B)["run_id"] == "b1"
    assert db.last_run("cloud", fy=A)["run_id"] == "a1"
    assert db.last_run("cloud", fy=ALL_FYS)["run_id"] == "b1"
    assert db.leg_success_after("cloud", "2027-01-01T00:00:00+05:30", fy=A) is False
    assert db.leg_success_after("cloud", "2027-01-01T00:00:00+05:30", fy=B) is True


def test_previous_run_status_and_fault_code_are_per_fy(db):
    _run(db, "a1", "cloud", A, "CLOUD_FAILED", "2027-01-05T02:00:00+05:30",
         extended_metrics='{"fault_code": "AAM-X"}')
    _run(db, "b1", "cloud", B, "CLOUD_COMPLETE", "2027-01-05T02:30:00+05:30")
    _run(db, "b2", "cloud", B, "CLOUD_COMPLETE", "2027-01-06T02:30:00+05:30")
    assert db.previous_run_status("cloud", "b2", fy=B) == "CLOUD_COMPLETE"
    assert db.previous_run_status("cloud", "a2", fy=A) == "CLOUD_FAILED"
    assert db.previous_run_fault_code("cloud", "b2", fy=B) is None
    assert db.previous_run_fault_code("cloud", "a2", fy=A) == "AAM-X"


def test_recent_runs_and_first_partial_per_fy(db):
    _run(db, "a1", "cloud", A, "CLOUD_PARTIAL", "2027-01-05T02:00:00+05:30")
    _run(db, "b1", "cloud", B, "CLOUD_PARTIAL", "2027-01-07T02:00:00+05:30")
    assert [r["run_id"] for r in db.get_recent_runs(10, fy=A)] == ["a1"]
    assert len(db.get_recent_runs(10, fy=ALL_FYS)) == 2
    assert db.first_partial_after("cloud", None, fy=B) == "2027-01-07T02:00:00+05:30"


def test_guard_baseline_is_per_fy(db):
    m = '{"guards": {"fy": "%s", "source_files": 5}}'
    _run(db, "a1", "cloud", A, "CLOUD_COMPLETE", extended_metrics=m % A)
    assert db.guard_baseline("cloud", A) == {"fy": A, "source_files": 5}
    assert db.guard_baseline("cloud", B) is None


def test_sweep_reads_the_fy_from_the_running_row(db):
    _run(db, "b1", "cloud", B, "CLOUD_RUNNING", "2027-01-05T02:00:00+05:30",
         extended_metrics='{"owner": "1:1"}')
    rows = db.abandon_dead_runs(lambda token: False, 1)
    assert rows[0]["fy"] == B and rows[0]["status"] == "CLOUD_ABANDONED"


# ── OV-1.4 audits + notifications ────────────────────────────────────────────

def _audit(db, audit_id, fy, status, started):
    db.record_audit({"audit_id": audit_id, "mode": "cloud", "fy": fy, "started_at": started,
                     "status": status, "created_at": started})


def test_audits_are_per_fy(db):
    _audit(db, "x1", A, "VERIFICATION_FAILED", "2027-01-03T08:00:00+05:30")
    _audit(db, "x2", B, "VERIFIED", "2027-01-03T08:30:00+05:30")
    assert db.latest_audit("cloud", fy=A)["status"] == "VERIFICATION_FAILED"
    assert db.latest_audit("cloud", fy=B)["status"] == "VERIFIED"
    assert db.audit_verified_after("2027-01-01T00:00:00+05:30", fy=A) is False
    assert db.audit_verified_after("2027-01-01T00:00:00+05:30", fy=B) is True


def test_record_audit_requires_fy(db):
    with pytest.raises(KeyError):
        db.record_audit({"audit_id": "x", "mode": "cloud", "started_at": "t", "status": "VERIFIED"})


def _note(db, key, kind, fy, mode="cloud", status="SENT", created="2027-01-05T03:00:00+05:30"):
    db.record_notification({"event_key": key, "kind": kind, "mode": mode, "status": status,
                            "created_at": created, "fy": fy, "audience": "alerts"})


def test_a_recovered_mail_of_one_fy_does_not_close_the_other_incident(db):
    _note(db, f"backup:{A}:cloud:CLOUD_FAILED", "FAILURE", A)
    _note(db, f"recovered:{B}:cloud", "RECOVERED", B)
    assert db.last_open_incident("cloud", fy=A)["event_key"].startswith("backup:FY26-27")
    assert db.last_open_incident("cloud", fy=B) is None
    prior = db.last_notification(f"backup:{A}:cloud:CLOUD_FAILED")
    assert db.sent_in_mode_after("cloud", prior["id"], recovered=True, fy=A) is False
    assert db.sent_in_mode_after("cloud", prior["id"], recovered=True, fy=B) is True
    assert db.sent_in_mode_after("cloud", prior["id"], recovered=True, fy=ALL_FYS) is True


def test_told_about_leg_since_is_per_fy(db):
    _note(db, f"backup:{B}:cloud:CLOUD_FAILED", "FAILURE", B, created="2027-01-05T03:00:00+05:30")
    since = "2027-01-04T00:00:00+05:30"
    assert db.told_about_leg_since("cloud", since, fy=B) is True
    assert db.told_about_leg_since("cloud", since, fy=A) is False


def test_notification_without_fy_is_stored_null(db):
    db.record_notification({"event_key": "watchdog:x", "kind": "FAILURE", "mode": "watchdog",
                            "status": "SENT", "created_at": "2027-01-05T03:00:00+05:30"})
    row = db.last_notification("watchdog:x")
    assert row["fy"] is None


# ── review follow-ups (Opus pre-commit review of WP1) ─────────────────────────

def test_last_success_falls_back_to_an_earlier_fy_after_the_switch(db):
    """B/I1: right after the April switch the new FY has no history; that is not 'never backed up'."""
    _run(db, "a1", "cloud", A, "CLOUD_COMPLETE", "2027-03-31T02:00:00+05:30")
    assert db.last_successful_run("cloud", fy=B) is None
    assert db.last_successful_run_or_earlier("cloud", fy=B)["run_id"] == "a1"
    _run(db, "b1", "cloud", B, "CLOUD_COMPLETE", "2027-04-02T02:00:00+05:30")
    assert db.last_successful_run_or_earlier("cloud", fy=B)["run_id"] == "b1"      # own FY wins
    assert db.last_successful_run_or_earlier("cloud", fy=A)["run_id"] == "a1"


def test_a_notification_row_without_fy_is_found_by_all_fys_only(db):
    """B1: until alerts carry an FY (OV-4.1) every mail row has fy NULL; only ALL_FYS finds it."""
    db.record_notification({"event_key": "backup:lan:LAN_FAILED", "kind": "FAILURE", "mode": "lan",
                            "status": "SENT", "created_at": "2027-01-05T03:00:00+05:30",
                            "audience": "alerts"})
    since = "2027-01-04T00:00:00+05:30"
    assert db.told_about_leg_since("lan", since, fy=ALL_FYS) is True
    assert db.told_about_leg_since("lan", since, fy=A) is False
