"""The dashboard's last-N-nights strip: worst run of the IST day, one entry per night."""

import pendulum

from core import night_strip as ns
from core.time_utils import IST

TODAY = pendulum.datetime(2026, 10, 19, 12, 0, tz=IST)


def _run(day, hhmm, status):
    return {"started_at": f"2026-10-{day:02d}T{hhmm}:00+05:30", "status": status}


def test_one_entry_per_night_oldest_first_ending_today():
    out = ns.build([], days=30, today=TODAY)
    assert len(out) == 30
    assert out[-1]["date"] == "2026-10-19" and out[0]["date"] == "2026-09-20"
    assert {n["state"] for n in out} == {ns.NONE}


def test_success_partial_and_failure_map_through_the_status_table():
    runs = [_run(19, "02:00", "CLOUD_NO_CHANGES_COMPLETE"), _run(18, "21:00", "LAN_PARTIAL"),
            _run(17, "21:00", "LAN_FAILED"), _run(16, "21:00", "LAN_BLOCKED")]
    by = {n["date"]: n["state"] for n in ns.build(runs, days=5, today=TODAY)}
    assert by == {"2026-10-15": ns.NONE, "2026-10-16": ns.BAD, "2026-10-17": ns.BAD,
                  "2026-10-18": ns.PARTIAL, "2026-10-19": ns.OK}


def test_a_failure_is_not_hidden_by_a_later_success_the_same_day():
    runs = [_run(18, "02:00", "LAN_FAILED"), _run(18, "21:00", "LAN_COMPLETE")]
    assert ns.build(runs, days=1, today=TODAY.subtract(days=1))[0]["state"] == ns.BAD


def test_a_run_still_in_progress_does_not_count_and_bad_rows_are_skipped():
    runs = [_run(19, "09:00", "LAN_RUNNING"), {"started_at": "garbage", "status": "LAN_COMPLETE"}]
    assert ns.build(runs, days=1, today=TODAY)[0]["state"] == ns.NONE


def test_day_boundaries_are_ist_not_utc():
    # 00:30 IST on the 19th is still the 18th in UTC; it belongs to the 19th
    runs = [{"started_at": "2026-10-19T00:30:00+05:30", "status": "LAN_COMPLETE"}]
    assert ns.build(runs, days=1, today=TODAY)[0] == {"date": "2026-10-19", "state": ns.OK}


def test_status_endpoint_carries_the_strip(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import ui
    from core.manifest import ManifestDB

    db = ManifestDB(str(tmp_path / "m.db"))
    cfg = SimpleNamespace(dashboard=SimpleNamespace(history_nights=7))
    monkeypatch.setattr(ui, "get_db", lambda: db)
    out = ui._nights(db, "lan", cfg)
    assert len(out) == 7 and out[-1]["date"] == pendulum.now(IST).format("YYYY-MM-DD")
    monkeypatch.setattr(db, "get_runs_between", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert ui._nights(db, "lan", cfg) == []
    db.close()
