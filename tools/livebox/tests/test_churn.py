"""Tests for tools.livebox.churn (plan LIVEBOX_TEST_PLAN_2026-11-24 section 3, rows L6a/L6b).

Red until L6a writes the module. Imports are lazy so collection works.

ASSUMED API (plan names; adjust when churn.py lands):

    seed_tree(root, seed, scale=300, now=None) -> list[str]
        Builds Client_###/<Type>/file_####.<ext> with random bytes under root,
        returns sorted relative POSIX paths. Deterministic per seed.
    apply_day(root, day: datetime.date, seed, role="C", adverse=None) -> list[Op]
        Runs the plan's daily events for simulated `day` (role "C" configured FY,
        "N" next FY). Op has .kind ("add","modify","delete","rename","idle",
        "large_add","large_delete","late_edit",...) and .path (relative str).
        Every write sets os.utime to noon of `day`. Sundays and the Aug idle
        week (Aug 1-7 here) return/perform no change. Normal days never emit
        guard events.
    apply_guard_event(root, day, seed, kind) -> list[Op]
        kind in {"mass_delete","shrink","wipe"}; only called explicitly by
        adverse lanes. mass_delete removes ~15 % of files, shrink ~25 %,
        wipe removes everything.
    ChurnPathError(ValueError)
        Raised by safe_path(root, rel) / any op when a path escapes root.
    safe_path(root, rel) -> pathlib.Path
"""
import datetime as dt
import hashlib
import os
from pathlib import Path

import pytest

DAY_WORK = dt.date(2026, 6, 10)  # Wednesday
DAY_SUNDAY = dt.date(2026, 6, 14)
DAY_AUG_IDLE = dt.date(2026, 8, 4)  # Tuesday inside the 7-day August idle week


def _churn():
    import importlib

    return importlib.import_module("tools.livebox.churn")


def _snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = (
                hashlib.sha256(p.read_bytes()).hexdigest(),
                int(p.stat().st_mtime),
            )
    return out


def _ops_repr(ops):
    return [(o.kind, o.path) for o in ops]


def _files(root: Path):
    return [p for p in root.rglob("*") if p.is_file()]


def test_seed_tree_shape_matches_plan(tmp_path):
    c = _churn()
    rels = c.seed_tree(tmp_path / "src", seed=1, scale=300)
    assert len(rels) == 300
    assert len(_files(tmp_path / "src")) == 300
    for r in rels:
        parts = r.split("/")
        assert parts[0].startswith("Client_") and len(parts[0]) == len("Client_000")
        assert parts[0][7:].isdigit()
        assert len(parts) == 3  # Client_###/<Type>/file_####.<ext>
        assert parts[2].startswith("file_") and "." in parts[2]
    exts = {r.rsplit(".", 1)[1].lower() for r in rels}
    assert len(exts) >= 3  # extension mix, not a single type
    assert len({r.split("/")[0] for r in rels}) > 1  # several client folders
    assert any(p.stat().st_size > 0 for p in _files(tmp_path / "src"))


def test_seed_tree_deterministic(tmp_path):
    c = _churn()
    c.seed_tree(tmp_path / "a", seed=7, scale=60)
    c.seed_tree(tmp_path / "b", seed=7, scale=60)
    a, b = _snapshot(tmp_path / "a"), _snapshot(tmp_path / "b")
    assert {k: v[0] for k, v in a.items()} == {k: v[0] for k, v in b.items()}
    c.seed_tree(tmp_path / "c", seed=8, scale=60)
    assert {k: v[0] for k, v in _snapshot(tmp_path / "c").items()} != {
        k: v[0] for k, v in a.items()
    }


def test_same_seed_and_day_gives_identical_ops_and_bytes(tmp_path):
    c = _churn()
    for n in "ab":
        c.seed_tree(tmp_path / n, seed=3, scale=100)
    ops_a = c.apply_day(tmp_path / "a", DAY_WORK, seed=3)
    ops_b = c.apply_day(tmp_path / "b", DAY_WORK, seed=3)
    assert _ops_repr(ops_a) == _ops_repr(ops_b)
    assert _snapshot(tmp_path / "a") == _snapshot(tmp_path / "b")


def test_different_day_gives_different_ops(tmp_path):
    c = _churn()
    for n in "ab":
        c.seed_tree(tmp_path / n, seed=3, scale=100)
    a = c.apply_day(tmp_path / "a", DAY_WORK, seed=3)
    b = c.apply_day(tmp_path / "b", DAY_WORK + dt.timedelta(days=1), seed=3)
    assert _ops_repr(a) != _ops_repr(b)


def test_mtimes_set_to_simulated_day(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=5, scale=100)
    ops = c.apply_day(root, DAY_WORK, seed=5)
    written = [o for o in ops if o.kind in ("add", "modify")]
    assert written
    for o in written:
        p = root / o.path
        assert p.exists()
        got = dt.datetime.fromtimestamp(p.stat().st_mtime)
        assert got.date() == DAY_WORK, (o.path, got)


def test_add_counts_for_configured_fy(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=11, scale=200)
    day = dt.date(2026, 6, 1)
    for i in range(20):
        d = day + dt.timedelta(days=i)
        if d.weekday() == 6:
            continue
        ops = c.apply_day(root, d, seed=11, role="C")
        adds = [o for o in ops if o.kind == "add"]
        mods = [o for o in ops if o.kind == "modify"]
        assert 3 <= len(adds) <= 8, (d, len(adds))
        assert 2 <= len(mods) <= 5, (d, len(mods))


def test_next_fy_role_counts_smaller(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=11, scale=200)
    for i in range(14):
        d = dt.date(2027, 1, 4) + dt.timedelta(days=i)
        if d.weekday() == 6:
            continue
        ops = c.apply_day(root, d, seed=11, role="N")
        assert 2 <= len([o for o in ops if o.kind == "add"]) <= 6
        assert 1 <= len([o for o in ops if o.kind == "modify"]) <= 3
        assert len([o for o in ops if o.kind == "delete"]) <= 1


def test_normal_days_delete_under_ten_percent_and_max_two(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=21, scale=300)
    d = dt.date(2026, 5, 4)
    for _ in range(90):
        d += dt.timedelta(days=1)
        before = len(_files(root))
        ops = c.apply_day(root, d, seed=21, role="C")
        dels = [o for o in ops if o.kind in ("delete",)]
        assert len(dels) <= 2, (d, len(dels))
        # no guard-style events on normal days
        assert len(dels) / max(before, 1) < 0.10
        assert not any(o.kind in ("mass_delete", "shrink", "wipe") for o in ops)
        assert len(_files(root)) > 0.7 * 300


def test_folder_rename_only_first_wednesday(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=2, scale=150)
    first_wed = dt.date(2026, 7, 1)  # 2026-07-01 is a Wednesday
    assert first_wed.weekday() == 2
    renamed_days = []
    d = dt.date(2026, 6, 28)
    while d <= dt.date(2026, 7, 31):
        ops = c.apply_day(root, d, seed=2, role="C")
        if any(o.kind == "rename" for o in ops):
            renamed_days.append(d)
        d += dt.timedelta(days=1)
    assert renamed_days == [first_wed]


def test_rename_stays_below_g3(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=2, scale=300)
    before = len(_files(root))
    ops = c.apply_day(root, dt.date(2026, 7, 1), seed=2, role="C")
    removed = sum(1 for o in ops if o.kind in ("delete", "rename"))
    assert any(o.kind == "rename" for o in ops)
    # a renamed folder is moved wholesale, never more than 10 % of the tree
    moved = before - len([p for p in _files(root)])
    assert abs(moved) < 0.10 * before + 12  # adds offset; deletions bounded


def test_sunday_is_idle(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=4, scale=80)
    before = _snapshot(root)
    ops = c.apply_day(root, DAY_SUNDAY, seed=4)
    assert all(o.kind == "idle" for o in ops) or ops == []
    assert _snapshot(root) == before


def test_august_idle_week_is_idle(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=4, scale=80)
    before = _snapshot(root)
    for i in range(7):
        c.apply_day(root, dt.date(2026, 8, 1) + dt.timedelta(days=i), seed=4)
    assert _snapshot(root) == before


def test_large_file_on_15th_deleted_next_15th(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=6, scale=50)
    ops = c.apply_day(root, dt.date(2026, 6, 15), seed=6)  # Monday
    big = [o for o in ops if o.kind == "large_add"]
    assert len(big) == 1
    p = root / big[0].path
    assert p.stat().st_size >= 50 * 1024 * 1024 - 1024
    for d in (dt.date(2026, 6, 16), dt.date(2026, 7, 14)):
        ops = c.apply_day(root, d, seed=6)
        assert not any(o.kind == "large_delete" for o in ops)
    ops = c.apply_day(root, dt.date(2026, 7, 15), seed=6)
    assert any(o.kind == "large_delete" and o.path == big[0].path for o in ops)
    assert not p.exists()


def test_late_edit_of_closed_fy_on_apr_2(tmp_path):
    c = _churn()
    root = tmp_path / "closed"
    c.seed_tree(root, seed=9, scale=50)
    ops = c.apply_day(root, dt.date(2027, 4, 2), seed=9, role="closed")
    assert sum(1 for o in ops if o.kind == "late_edit") == 1
    ops = c.apply_day(root, dt.date(2027, 4, 3), seed=9, role="closed")
    assert not any(o.kind == "late_edit" for o in ops)


def test_shared_txt_events(tmp_path):
    c = _churn()
    root_c, root_n = tmp_path / "c", tmp_path / "n"
    c.seed_tree(root_c, seed=1, scale=40)
    c.seed_tree(root_n, seed=2, scale=40)
    c.apply_day(root_c, dt.date(2027, 1, 3), seed=1, role="C")
    c.apply_day(root_n, dt.date(2027, 1, 3), seed=2, role="N")
    assert (root_c / "Clients" / "shared.txt").exists()
    assert (root_n / "Clients" / "shared.txt").exists()
    c.apply_day(root_c, dt.date(2027, 2, 15), seed=1, role="C")
    assert not (root_c / "Clients" / "shared.txt").exists()
    c.apply_day(root_n, dt.date(2027, 2, 15), seed=2, role="N")
    assert (root_n / "Clients" / "shared.txt").exists()


@pytest.mark.parametrize(
    "kind,low,high",
    [("mass_delete", 0.13, 0.17), ("shrink", 0.23, 0.27)],
)
def test_guard_events_only_on_explicit_call(tmp_path, kind, low, high):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=12, scale=200)
    before = len(_files(root))
    # normal apply_day never does it
    c.apply_day(root, DAY_WORK, seed=12)
    assert len(_files(root)) >= before - 3
    base = len(_files(root))
    ops = c.apply_guard_event(root, DAY_WORK, seed=12, kind=kind)
    gone = base - len(_files(root))
    assert low * base <= gone <= high * base
    assert ops


def test_guard_wipe_empties_source(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=12, scale=60)
    c.apply_guard_event(root, DAY_WORK, seed=12, kind="wipe")
    assert _files(root) == []
    assert root.exists()


def test_guard_unknown_kind_rejected(tmp_path):
    c = _churn()
    root = tmp_path / "src"
    c.seed_tree(root, seed=12, scale=10)
    with pytest.raises(ValueError):
        c.apply_guard_event(root, DAY_WORK, seed=12, kind="bogus")


@pytest.mark.parametrize(
    "rel", ["../outside.txt", "Client_001/../../outside.txt", "..", "/abs.txt"]
)
def test_path_escape_refused(tmp_path, rel):
    c = _churn()
    root = tmp_path / "src"
    root.mkdir()
    with pytest.raises(c.ChurnPathError):
        c.safe_path(root, rel)


def test_ops_never_touch_outside_root(tmp_path):
    c = _churn()
    root = tmp_path / "lane" / "src"
    c.seed_tree(root, seed=13, scale=100)
    sentinel = tmp_path / "lane" / "sentinel.txt"
    sentinel.write_text("keep")
    before_outside = {p for p in tmp_path.rglob("*") if root not in p.parents and p != root}
    d = dt.date(2026, 9, 1)
    for i in range(30):
        c.apply_day(root, d + dt.timedelta(days=i), seed=13)
    after_outside = {p for p in tmp_path.rglob("*") if root not in p.parents and p != root}
    assert before_outside == after_outside
    assert sentinel.read_text() == "keep"
    for p in root.rglob("*"):
        assert os.path.commonpath([root, p]) == str(root)
