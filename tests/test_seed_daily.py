"""tools/seed_daily.py: one unattended day of add/replace/delete churn in _seed_test."""
import csv
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import seed_daily as sd  # noqa: E402
import seed_test_files as sf  # noqa: E402


def _files(dest):
    return [p for p in (dest / sf.SEED_DIR).rglob("*") if p.is_file() and p.name != sf.MANIFEST]


def _manifest(dest):
    with (dest / sf.SEED_DIR / sf.MANIFEST).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_plan_never_breaks_the_cap_or_the_floor():
    for total in (0, 1, 5, 10, 199, 200, 250):
        for seed in range(50):
            p = sd.plan_day(random.Random(seed), total, add_max=5, replace_max=3,
                            delete_max=2, keep_min=10, cap=200)
            assert p["add"] >= 0 and p["replace"] >= 0 and p["delete"] >= 0
            after = total - p["delete"] + p["add"]
            assert after <= max(200, total)          # cap holds
            assert total - p["delete"] >= min(total, 10)   # floor holds
            assert p["replace"] <= total - p["delete"]     # cannot rewrite what was just deleted


def test_a_day_actually_adds_replaces_and_deletes(tmp_path):
    real = tmp_path / "client.docx"
    real.write_bytes(b"real client file")
    sf.seed(tmp_path, count=30, max_kb=40, seed_value=11)
    before = {p: p.read_bytes() for p in _files(tmp_path)}
    plan = sd.plan_day(random.Random(42), 30, add_max=5, replace_max=3, delete_max=2,
                       keep_min=10, cap=200)

    r = sd.daily(tmp_path, add_max=5, replace_max=3, delete_max=2, seed_value=42)

    assert (r["added"], r["replaced"], r["deleted"]) == (plan["add"], plan["replace"], plan["delete"])
    assert r["added"] >= 1 and r["deleted"] >= 1        # the day is not a no-op
    after = _files(tmp_path)
    assert len(after) == 30 - r["deleted"] + r["added"]
    assert sum(1 for p in after if p in before and p.read_bytes() != before[p]) == r["replaced"]
    assert real.read_bytes() == b"real client file"
    assert len(_manifest(tmp_path)) == len(after)
    assert r["total_files"] == len(after)


def test_first_run_on_an_empty_drive_creates_the_folder(tmp_path):
    r = sd.daily(tmp_path, add_max=4, replace_max=3, delete_max=2, seed_value=1)
    assert r["deleted"] == 0 and r["replaced"] == 0
    assert 1 <= r["added"] <= 4
    assert (tmp_path / sf.SEED_DIR / sf.MANIFEST).exists()


def test_dry_run_changes_nothing(tmp_path):
    sf.seed(tmp_path, count=20, max_kb=20, seed_value=5)
    before = {p: p.read_bytes() for p in _files(tmp_path)}
    r = sd.daily(tmp_path, seed_value=3, dry_run=True)
    assert set(before) == set(_files(tmp_path))
    assert all(p.read_bytes() == b for p, b in before.items())
    assert r["dry_run"] is True


def test_a_full_folder_adds_nothing_but_still_churns(tmp_path):
    sf.seed(tmp_path, count=25, max_kb=20, seed_value=9)
    r = sd.daily(tmp_path, add_max=5, cap=25, keep_min=5, seed_value=2)
    assert r["added"] == 0
    assert len(_files(tmp_path)) == 25 - r["deleted"]


def test_quiet_settings_can_leave_files_untouched(tmp_path):
    sf.seed(tmp_path, count=15, max_kb=20, seed_value=8)
    r = sd.daily(tmp_path, add_max=0, replace_max=0, delete_max=0, seed_value=4)
    assert (r["added"], r["replaced"], r["deleted"]) == (0, 0, 0)
    assert len(_files(tmp_path)) == 15


def test_log_line_is_appended_and_dest_is_required(tmp_path, capsys):
    log = tmp_path / "logs" / "seed.log"
    sf.seed(tmp_path, count=12, max_kb=20, seed_value=6)
    plan = sd.plan_day(random.Random(77), 12, add_max=2, replace_max=1, delete_max=1,
                       keep_min=10, cap=200)
    assert sd.main(["--dest", str(tmp_path), "--add", "2", "--replace", "1",
                    "--delete", "1", "--seed", "77", "--log", str(log)]) == 0
    line = log.read_text(encoding="utf-8")
    assert line.count("\n") == 1
    assert (f"added {plan['add']}, replaced {plan['replace']}, deleted {plan['delete']}") in line
    assert "seeded files" in line
    assert f"added {plan['add']}, replaced {plan['replace']}, deleted {plan['delete']}" in capsys.readouterr().out

    try:
        sd.main(["--dest", str(tmp_path / "nope")])
        raise AssertionError("should have exited")
    except SystemExit as e:
        assert e.code == 2
