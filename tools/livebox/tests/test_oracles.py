"""Oracle tests for live-box lanes (plan sections 1.6 and 2, L7b).

Assumed API of the module under test, `tools.livebox.oracles` (not written yet; L7a):

    run_history_rows(db_path, day: date) -> list[dict]
        New run_history rows whose started_at (ISO, IST offset) falls on sim `day`, ordered by started_at.
        Each dict: {"run_id", "leg" ("cloud"|"lan", from column `mode`), "fy" (from extended_metrics JSON
        key "fy"), "status"}.

    check_run_history(db_path, day, expected) -> list[str]
        `expected` = list of {"leg", "fy", "status": str | set[str]} in order. Returns problem strings;
        empty list = pass. Compares lists (order, count, leg, fy, status), never totals only.
        Also reports any row left with status "RUNNING" on that day.

    load_mail_index(csv_path) -> list[Mail-dict]
        mail_index.csv columns: sim_time (ISO with offset), real_time, rcpts (';'-joined), subject, key,
        kind, leg, fy, cls ("missed"|"overdue"|"failure"|""), severity ("FAILURE"|"ACTION_REQUIRED"|
        "WARNING"|"RECOVERED"|"INFO"|"REPORT"), audience ("alerts"|"reports"|"both").
        Returns dicts with the same names; rcpts is a list[str], sim_time a datetime.

    check_mail_rules(mails, *, recipients, report_recipients, repeat_cooldown_hours=12,
                     stale_repeat_hours=24, injection_dates=(), outages=(), expected_per_day=None)
                     -> list[Violation]
        mails = list of dicts as returned by load_mail_index (rcpts may be a list).
        injection_dates = set of dates where an injection is active. M2 treats a day as healthy when
        neither it nor the previous 24 h is in the injection window.
        outages = list of (start_datetime, end_datetime); M4 applies to mails after `end`.
        expected_per_day = {date: int} overrides the M3 limit of 4 problem mails per day.
        Violation has `.rule` ("M1".."M5") and `.detail` (str). Problem mail = severity in
        FAILURE / ACTION_REQUIRED / WARNING (RECOVERED counts for M2 only).

    tree_hash(root) -> dict[str, str]         relative posix path -> sha256 of content
    compare_trees(a, b) -> TreeDiff            a, b = directory paths or tree_hash dicts.
        TreeDiff: .ok (bool), .missing (paths in a not in b), .extra (paths in b not in a),
        .differing (paths in both with different content); each a sorted list.

    read_status(payload: dict) -> StatusView   payload = parsed /status JSON.
        StatusView: .cards -> sorted list of card names present among cloud, lan, cloud_next, lan_next;
        .card(name) -> the card dict; .overlap_active (bool); .activates_on (str | None);
        .next_fy (str | None); .configured_fy (str).

Imports are lazy so the red state is ImportError only.
"""
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

IST = timezone(timedelta(hours=5, minutes=30))


def _oracles():
    from tools.livebox import oracles
    return oracles


# --------------------------------------------------------------------------- fixtures

RUN_HISTORY_DDL = """
CREATE TABLE run_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE, mode TEXT NOT NULL,
    started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, exit_code INTEGER,
    files_copied INTEGER DEFAULT 0, bytes_copied INTEGER DEFAULT 0, files_failed INTEGER DEFAULT 0,
    duration_seconds REAL, error_message TEXT, extended_metrics TEXT);
CREATE TABLE integrity_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, audit_id TEXT NOT NULL UNIQUE, mode TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'full', started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
    files_checked INTEGER DEFAULT 0, bytes_checked INTEGER DEFAULT 0, mismatches INTEGER DEFAULT 0,
    detail TEXT, created_at TEXT NOT NULL);
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT,
    subject TEXT, status TEXT NOT NULL, error TEXT, attempts INTEGER DEFAULT 0, created_at TEXT NOT NULL,
    fault_code TEXT, message_id TEXT, thread_id TEXT, attachments_rung TEXT, audience TEXT, recipients TEXT);
"""


def make_db(path, rows):
    """rows: (run_id, leg, started_at_iso, status, fy)"""
    con = sqlite3.connect(path)
    con.executescript(RUN_HISTORY_DDL)
    for run_id, leg, started, status, fy in rows:
        con.execute(
            "INSERT INTO run_history (run_id, mode, started_at, ended_at, status, extended_metrics)"
            " VALUES (?,?,?,?,?,?)",
            (run_id, leg, started, started, status, json.dumps({"fy": fy})))
    con.commit()
    con.close()
    return path


def at(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


DAY = date(2091, 1, 2)          # overlap ordinary day: cloud C then N, LAN C then N
OTHER = date(2091, 1, 3)


def overlap_day_rows(day, prefix="r"):
    return [
        (f"{prefix}-c-c-{day}", "cloud", at(day, 2, 1).isoformat(), "CLOUD_COMPLETE", "FY90-91"),
        (f"{prefix}-c-n-{day}", "cloud", at(day, 2, 5).isoformat(), "CLOUD_COMPLETE", "FY91-92"),
        (f"{prefix}-l-c-{day}", "lan", at(day, 21, 1).isoformat(), "LAN_COMPLETE", "FY90-91"),
        (f"{prefix}-l-n-{day}", "lan", at(day, 21, 6).isoformat(), "LAN_COMPLETE", "FY91-92"),
    ]


EXPECTED_OVERLAP_DAY = [
    {"leg": "cloud", "fy": "FY90-91", "status": {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"}},
    {"leg": "cloud", "fy": "FY91-92", "status": {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"}},
    {"leg": "lan", "fy": "FY90-91", "status": "LAN_COMPLETE"},
    {"leg": "lan", "fy": "FY91-92", "status": "LAN_COMPLETE"},
]


# --------------------------------------------------------------------------- run_history oracle

def test_run_history_rows_only_that_calendar_day(tmp_path):
    db = make_db(tmp_path / "m.db", overlap_day_rows(DAY) + overlap_day_rows(OTHER, "x"))
    rows = _oracles().run_history_rows(db, DAY)
    assert [(r["leg"], r["fy"], r["status"]) for r in rows] == [
        ("cloud", "FY90-91", "CLOUD_COMPLETE"), ("cloud", "FY91-92", "CLOUD_COMPLETE"),
        ("lan", "FY90-91", "LAN_COMPLETE"), ("lan", "FY91-92", "LAN_COMPLETE")]


def test_run_history_matches_expected(tmp_path):
    db = make_db(tmp_path / "m.db", overlap_day_rows(DAY) + overlap_day_rows(OTHER, "x"))
    assert _oracles().check_run_history(db, DAY, EXPECTED_OVERLAP_DAY) == []


def test_run_history_missing_row_flagged(tmp_path):
    db = make_db(tmp_path / "m.db", overlap_day_rows(DAY)[:3])
    assert _oracles().check_run_history(db, DAY, EXPECTED_OVERLAP_DAY)


def test_run_history_extra_row_flagged(tmp_path):
    rows = overlap_day_rows(DAY) + [("dup", "cloud", at(DAY, 3).isoformat(), "CLOUD_COMPLETE", "FY90-91")]
    db = make_db(tmp_path / "m.db", rows)
    assert _oracles().check_run_history(db, DAY, EXPECTED_OVERLAP_DAY)


def test_run_history_wrong_status_flagged(tmp_path):
    rows = overlap_day_rows(DAY)
    rows[2] = (rows[2][0], "lan", rows[2][2], "LAN_PARTIAL", "FY90-91")
    db = make_db(tmp_path / "m.db", rows)
    assert _oracles().check_run_history(db, DAY, EXPECTED_OVERLAP_DAY)


def test_run_history_wrong_fy_flagged(tmp_path):
    rows = overlap_day_rows(DAY)
    rows[1] = (rows[1][0], "cloud", rows[1][2], "CLOUD_COMPLETE", "FY90-91")
    db = make_db(tmp_path / "m.db", rows)
    assert _oracles().check_run_history(db, DAY, EXPECTED_OVERLAP_DAY)


def test_run_history_running_row_flagged(tmp_path):
    rows = overlap_day_rows(DAY)
    rows[3] = (rows[3][0], "lan", rows[3][2], "RUNNING", "FY91-92")
    db = make_db(tmp_path / "m.db", rows)
    problems = _oracles().check_run_history(db, DAY, EXPECTED_OVERLAP_DAY)
    assert any("RUNNING" in p for p in problems)


def test_run_history_empty_day_expected_empty(tmp_path):
    db = make_db(tmp_path / "m.db", overlap_day_rows(OTHER))
    assert _oracles().check_run_history(db, DAY, []) == []


# --------------------------------------------------------------------------- mail rules

REC = ["partners@example.test"]
REP = ["reports@example.test"]


def mail(when, key="k:cloud:FY90-91", severity="FAILURE", rcpts=None, leg="cloud", fy="FY90-91", cls="failure",
         audience="alerts", kind="failure"):
    return {"sim_time": when, "real_time": when, "rcpts": rcpts if rcpts is not None else list(REC),
            "subject": f"{kind} {key}", "key": key, "kind": kind, "leg": leg, "fy": fy, "cls": cls,
            "severity": severity, "audience": audience}


def rules(mails, **kw):
    kw.setdefault("recipients", REC)
    kw.setdefault("report_recipients", REP)
    return {v.rule for v in _oracles().check_mail_rules(mails, **kw)}


def test_clean_mail_set_passes():
    mails = [mail(at(DAY, 8, 0), key="weekly", severity="REPORT", rcpts=REP, audience="reports",
                  kind="weekly", cls=""),
             mail(at(DAY, 9), key="rollover:activated:FY91-92", severity="INFO", kind="activated", cls="")]
    assert rules(mails) == set()


def test_m1_same_key_inside_cooldown_flagged_and_after_cooldown_ok():
    base = at(DAY, 2)
    assert "M1" in rules([mail(base), mail(base + timedelta(hours=5))], injection_dates={DAY})
    assert "M1" not in rules([mail(base), mail(base + timedelta(hours=13))], injection_dates={DAY, OTHER})


def test_m1_stale_key_uses_24h_and_activation_key_24h():
    base = at(DAY, 2)
    stale = "stale:cloud:FY90-91"
    assert "M1" in rules([mail(base, key=stale), mail(base + timedelta(hours=13), key=stale)],
                         injection_dates={DAY, OTHER})
    assert "M1" not in rules([mail(base, key=stale), mail(base + timedelta(hours=25), key=stale)],
                             injection_dates={DAY, OTHER, OTHER + timedelta(days=1)})
    act = "rollover:activation:FY91-92"
    assert "M1" in rules([mail(base, key=act, severity="WARNING"),
                          mail(base + timedelta(hours=13), key=act, severity="WARNING")],
                         injection_dates={DAY, OTHER})


def test_m2_problem_mail_on_healthy_day_flagged_but_ok_with_injection():
    m = [mail(at(DAY, 2), severity="WARNING")]
    assert "M2" in rules(m)
    assert "M2" not in rules(m, injection_dates={DAY})
    # injection yesterday still covers the next 24 h
    assert "M2" not in rules(m, injection_dates={DAY - timedelta(days=1)})
    # recovered mails are also not allowed on a healthy day
    assert "M2" in rules([mail(at(DAY, 3), severity="RECOVERED", key="r")])
    # INFO is allowed
    assert "M2" not in rules([mail(at(DAY, 3), severity="INFO", key="i")])


def test_m3_more_than_four_problem_mails_per_day_flagged():
    five = [mail(at(DAY, 1 + i), key=f"k{i}") for i in range(5)]
    four = five[:4]
    assert "M3" in rules(five, injection_dates={DAY})
    assert "M3" not in rules(four, injection_dates={DAY})
    assert "M3" not in rules(five, injection_dates={DAY}, expected_per_day={DAY: 5})


def test_m4_catchup_storm_after_outage_flagged():
    outage = (at(date(2091, 1, 10), 0), at(date(2091, 1, 13), 9))
    boot = at(date(2091, 1, 13), 9, 5)
    storm = [mail(boot + timedelta(minutes=i), key=f"missed:{i}", severity="WARNING", cls="missed")
             for i in range(3)]
    clean = storm[:1]
    kw = dict(outages=[outage], injection_dates={date(2091, 1, d) for d in (10, 11, 12, 13)})
    assert "M4" in rules(storm, **kw)
    assert "M4" not in rules(clean, **kw)


def test_m4_one_per_class_per_leg_fy_ok():
    outage = (at(date(2091, 1, 10), 0), at(date(2091, 1, 13), 9))
    boot = at(date(2091, 1, 13), 9, 5)
    mails = [mail(boot, key="a", severity="WARNING", cls="missed"),
             mail(boot, key="b", severity="WARNING", cls="overdue"),
             mail(boot, key="c", severity="FAILURE", cls="failure"),
             mail(boot, key="d", severity="WARNING", cls="missed", leg="lan")]
    assert "M4" not in rules(mails, outages=[outage],
                             injection_dates={date(2091, 1, d) for d in (10, 11, 12, 13)})


def test_m5_report_to_alert_list_and_problem_to_report_list_flagged():
    assert "M5" in rules([mail(at(DAY, 8), key="weekly", severity="REPORT", rcpts=REC,
                               audience="reports", kind="weekly", cls="")])
    assert "M5" in rules([mail(at(DAY, 2), rcpts=REP)], injection_dates={DAY})
    assert "M5" not in rules([mail(at(DAY, 2))], injection_dates={DAY})


def test_load_mail_index_roundtrip(tmp_path):
    csv = tmp_path / "mail_index.csv"
    csv.write_text(
        "sim_time,real_time,rcpts,subject,key,kind,leg,fy,cls,severity,audience\n"
        f"{at(DAY, 2).isoformat()},2026-11-24T10:00:00+05:30,a@x.test;b@x.test,Backup failed,"
        "k1,failure,cloud,FY90-91,failure,FAILURE,alerts\n", encoding="utf-8")
    mails = _oracles().load_mail_index(csv)
    assert len(mails) == 1
    assert mails[0]["rcpts"] == ["a@x.test", "b@x.test"]
    assert mails[0]["sim_time"] == at(DAY, 2)
    assert mails[0]["severity"] == "FAILURE"


# --------------------------------------------------------------------------- tree hash

def make_tree(root, files):
    for rel, data in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return Path(root)


BASE = {"a.txt": b"hello", "sub/b.bin": b"\x00\x01\x02\x03", "sub/deep/c.txt": b"c"}


def test_tree_hash_equal_trees(tmp_path):
    a = make_tree(tmp_path / "a", BASE)
    b = make_tree(tmp_path / "b", BASE)
    diff = _oracles().compare_trees(a, b)
    assert diff.ok and not diff.missing and not diff.extra and not diff.differing
    h = _oracles().tree_hash(a)
    assert set(h) == set(BASE)


def test_tree_hash_single_byte_difference(tmp_path):
    a = make_tree(tmp_path / "a", BASE)
    b = make_tree(tmp_path / "b", {**BASE, "sub/b.bin": b"\x00\x01\x02\x04"})
    diff = _oracles().compare_trees(a, b)
    assert not diff.ok
    assert diff.differing == ["sub/b.bin"] and not diff.missing and not diff.extra


def test_tree_hash_missing_and_extra_files(tmp_path):
    a = make_tree(tmp_path / "a", BASE)
    b = make_tree(tmp_path / "b", {k: v for k, v in BASE.items() if k != "a.txt"} | {"new/z.txt": b"z"})
    diff = _oracles().compare_trees(a, b)
    assert not diff.ok
    assert diff.missing == ["a.txt"]
    assert diff.extra == ["new/z.txt"]
    assert diff.differing == []


def test_tree_hash_accepts_precomputed_hashes(tmp_path):
    a = make_tree(tmp_path / "a", BASE)
    snap = _oracles().tree_hash(a)
    (a / "a.txt").write_bytes(b"hellp")
    assert _oracles().compare_trees(snap, a).differing == ["a.txt"]


# --------------------------------------------------------------------------- /status reader

def card(fy, status="CLOUD_COMPLETE"):
    return {"enabled": True, "fy": fy, "last_run": {"status": status, "fy": fy}}


def status_payload(overlap_active, *, nxt=True):
    p = {"fy_prefix": "FY90-91", "cloud": card("FY90-91"), "lan": card("FY90-91", "LAN_COMPLETE"),
         "overlap": {"enabled": True, "active": overlap_active, "configured_fy": "FY90-91",
                     "next_fy": "FY91-92", "activates_on": "2090-12-31", "activated_at": None,
                     "ended_at": None, "activation_pending": False, "note": None}}
    if overlap_active and nxt:
        p["cloud_next"] = card("FY91-92")
        p["lan_next"] = card("FY91-92", "LAN_COMPLETE")
    return p


def test_status_two_cards_before_activation():
    v = _oracles().read_status(status_payload(False))
    assert v.cards == ["cloud", "lan"]
    assert v.overlap_active is False
    assert v.activates_on == "2090-12-31"
    assert v.configured_fy == "FY90-91"


def test_status_four_cards_during_overlap():
    v = _oracles().read_status(status_payload(True))
    assert v.cards == ["cloud", "cloud_next", "lan", "lan_next"]
    assert v.overlap_active is True
    assert v.next_fy == "FY91-92"
    assert v.card("cloud_next")["fy"] == "FY91-92"
    assert v.card("lan")["last_run"]["status"] == "LAN_COMPLETE"


def test_status_active_without_next_cards_is_reported_as_given():
    v = _oracles().read_status(status_payload(True, nxt=False))
    assert v.cards == ["cloud", "lan"]
    assert v.overlap_active is True


def test_status_card_unknown_name_raises():
    v = _oracles().read_status(status_payload(False))
    with pytest.raises(KeyError):
        v.card("cloud_next")
