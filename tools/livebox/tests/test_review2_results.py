r"""Red-first tests (review round 2): scenarios.results mail parsing must match the PRODUCT's mail format.

PINNED BEHAVIOUR of tools.livebox.scenarios.results.build_results(evidence_dir).mails (one dict per mail_index row):
  severity  one of PROBLEM | WARNING | RESOLVED | ACTION_REQUIRED | INFO, read from the subject word after the
            "[<firm> Backup] " prefix ("ACTION NEEDED:" -> ACTION_REQUIRED); "" when the subject has none (reports)
  code      the X-AAM-Fault-Code header (x_headers JSON column, or - when the column is blank - the header of the
            matching mail/*.eml, matched by Subject)
  key       joined from the notifications table (event_key of the SENT row with the same subject; manifest.db in the
            evidence dir, else the newest db_dump/*), NOT from X-AAM-Reference (the product sends the run id there
            for run mails)
  kind      the notifications.kind of that row (else "")
"""
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

REAL = Path(__file__).resolve().parents[3] / "agent_runs" / "livebox_dev32" / "A30"
needs_real = pytest.mark.skipif(not (REAL / "mail_index.csv").is_file(), reason="sample evidence A30 absent")


def res():
    from tools.livebox.scenarios import results
    return results


def write_index(ev: Path, rows: list[dict]) -> None:
    with open(ev / "mail_index.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["sim_time", "real_time", "rcpts", "subject", "from", "x_headers"])
        w.writeheader()
        for r in rows:
            w.writerow({"real_time": "", "rcpts": "ops@x.invalid", "from": "f@x.invalid", "x_headers": "", **r})


def write_notifications(ev: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(ev / "manifest.db")
    con.execute("create table notifications (id integer primary key, event_key text, kind text, mode text, "
                "subject text, status text, created_at text, fault_code text, audience text)")
    for i, (key, kind, subject, status) in enumerate(rows, 1):
        con.execute("insert into notifications values (?,?,?,?,?,?,?,?,?)",
                    (i, key, kind, "cloud", subject, status, "2091-01-01T02:00:00+05:30", None, "ops"))
    con.commit()
    con.close()


# --------------------------------------------------------------------------------------------- severity
@pytest.mark.parametrize("word,expected", [
    ("PROBLEM: Online backup (Google Cloud) - Upload failed (AAM-CLD-02)", "PROBLEM"),
    ("WARNING: FY91-92 is not backed up yet", "WARNING"),
    ("RESOLVED: Online backup (Google Cloud) is working again (was AAM-CLD-02)", "RESOLVED"),
    ("ACTION NEEDED: Weekly safety check - Safety check found differences (AAM-AUD-01)", "ACTION_REQUIRED"),
    ("INFO: FY91-92 is added to the backups", "INFO"),
])
def test_severity_from_subject(tmp_path, word, expected):
    write_index(tmp_path, [{"sim_time": "2091-01-01T08:00:00+05:30", "subject": f"[ACME Backup] {word}"}])
    (m,) = res().build_results(tmp_path).mails
    assert m["severity"] == expected


def test_report_subject_has_no_severity(tmp_path):
    write_index(tmp_path, [{"sim_time": "2091-01-01T08:00:00+05:30",
                            "subject": "[ACME Backup] Weekly backup report 25-31 Dec 2090 - all backups OK"}])
    (m,) = res().build_results(tmp_path).mails
    assert m["severity"] == ""


# --------------------------------------------------------------------------------------------- code
def test_code_from_x_aam_fault_code_header(tmp_path):
    hdr = json.dumps({"X-AAM-Reference": "run-abc", "X-AAM-Fault-Code": "AAM-CLD-02"})
    write_index(tmp_path, [{"sim_time": "2091-01-01T02:30:00+05:30",
                            "subject": "[ACME Backup] PROBLEM: Online backup - Upload failed (AAM-CLD-02)",
                            "x_headers": hdr}])
    (m,) = res().build_results(tmp_path).mails
    assert m["code"] == "AAM-CLD-02"


def test_no_fault_header_means_empty_code(tmp_path):
    write_index(tmp_path, [{"sim_time": "2091-01-01T02:30:00+05:30", "subject": "[ACME Backup] INFO: hello",
                            "x_headers": json.dumps({"X-AAM-Reference": "rollover:activated:FY91-92"})}])
    (m,) = res().build_results(tmp_path).mails
    assert m["code"] == ""


# --------------------------------------------------------------------------------------------- key from notifications
def test_key_joined_from_notifications_not_reference(tmp_path):
    subj = "[ACME Backup] WARNING: FY91-92 is not backed up yet"
    write_index(tmp_path, [{"sim_time": "2091-01-01T02:30:00+05:30", "subject": subj,
                            "x_headers": json.dumps({"X-AAM-Reference": "2091-01-01_0201_cloud_9f3ef765"})}])
    write_notifications(tmp_path, [("rollover:activation:FY91-92", "WARNING", subj, "SENT")])
    (m,) = res().build_results(tmp_path).mails
    assert m["key"] == "rollover:activation:FY91-92"
    assert m["kind"] == "WARNING"


def test_join_uses_each_notification_once_and_only_sent(tmp_path):
    subj = "[ACME Backup] WARNING: FY91-92 is not backed up yet"
    write_index(tmp_path, [{"sim_time": "2091-01-01T02:30:00+05:30", "subject": subj},
                           {"sim_time": "2091-01-02T02:30:00+05:30", "subject": subj}])
    write_notifications(tmp_path, [("k:failed", "WARNING", subj, "FAILED"), ("k:first", "WARNING", subj, "SENT"),
                                   ("k:second", "WARNING", subj, "SENT")])
    a, b = res().build_results(tmp_path).mails
    assert (a["key"], b["key"]) == ("k:first", "k:second")


def test_key_from_db_dump_when_no_manifest_db(tmp_path):
    subj = "[ACME Backup] WARNING: FY91-92 is not backed up yet"
    write_index(tmp_path, [{"sim_time": "2091-01-01T02:30:00+05:30", "subject": subj}])
    (tmp_path / "db_dump").mkdir()
    row = {"id": 1, "event_key": "rollover:activation:FY91-92", "kind": "WARNING", "mode": "cloud",
           "subject": subj, "status": "SENT", "created_at": "2091-01-01T02:30:00+05:30", "fault_code": None,
           "audience": "ops"}
    (tmp_path / "db_dump" / "2091-01-01.jsonl").write_text(
        json.dumps({"day": "2091-01-01", "table": "notifications", "row": row}) + "\n", encoding="utf-8")
    (m,) = res().build_results(tmp_path).mails
    assert m["key"] == "rollover:activation:FY91-92"


# --------------------------------------------------------------------------------------------- real captured mail
@needs_real
def test_real_eml_supplies_fault_code_when_index_column_blank(tmp_path):
    (tmp_path / "mail").mkdir()
    eml = next(REAL.glob("mail/*.eml"))
    for p in REAL.glob("mail/*.eml"):
        if "ACTION NEEDED" in p.read_text(encoding="utf-8", errors="replace"):
            eml = p
            break
    shutil.copy(eml, tmp_path / "mail" / eml.name)
    subject = next(line for line in eml.read_text(encoding="utf-8", errors="replace").splitlines()
                   if line.startswith("Subject:"))[len("Subject: "):]
    write_index(tmp_path, [{"sim_time": "2090-12-31T08:00:00+05:30", "subject": subject, "x_headers": ""}])
    (m,) = res().build_results(tmp_path).mails
    assert m["severity"] == "ACTION_REQUIRED"
    assert m["code"] == "AAM-AUD-01"


@needs_real
def test_real_evidence_mails_parse_like_the_product(tmp_path):
    shutil.copy(REAL / "mail_index.csv", tmp_path / "mail_index.csv")
    shutil.copy(REAL / "manifest.db", tmp_path / "manifest.db")
    shutil.copytree(REAL / "mail", tmp_path / "mail")
    r = res().build_results(tmp_path)
    by_sev = {}
    for m in r.mails:
        by_sev.setdefault(m["severity"], []).append(m)
    assert len(by_sev["ACTION_REQUIRED"]) >= 1 and all(m["code"] == "AAM-AUD-01" for m in by_sev["ACTION_REQUIRED"])
    info = by_sev["INFO"]
    assert len(info) == 1 and info[0]["key"] == "rollover:activated:FY91-92" and info[0]["kind"] == "INFO"
    assert all(m["severity"] == "" for m in r.mails if "report" in m["subject"].lower())


@needs_real
def test_real_mail_through_build_results_into_w1_expect_no_false_fail(tmp_path):
    """X01-style check: the one real INFO 'rollover:activated' mail must count as exactly one INFO activated mail,
    and the real ACTION NEEDED mail must be countable by severity + code (no false 'observed 0')."""
    shutil.copy(REAL / "mail_index.csv", tmp_path / "mail_index.csv")
    shutil.copy(REAL / "manifest.db", tmp_path / "manifest.db")
    shutil.copytree(REAL / "mail", tmp_path / "mail")
    r = res().build_results(tmp_path)
    from tools.livebox.scenarios import w1
    out = w1._x01_expect(r)
    assert not any("INFO activated mail" in s for s in out), out
    action = w1._mails(r, "ACTION_REQUIRED", code="AAM-AUD-01")
    assert len(action) == 1
    pack_expect = lambda rr: [] if len(w1._mails(rr, "ACTION_REQUIRED", code="AAM-AUD-01")) == 1 else ["bad"]
    assert pack_expect(r) == []
