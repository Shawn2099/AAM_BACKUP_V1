"""D2 - targeted audit lab for the FY overlap (TESTMATRIX_D section 3, scenarios S0-S7).

Real robocopy + real rclone (local backend standing in for GCS) + real Prefect (clean PREFECT_HOME) + real SQLite,
driven through the production entry points `flow.backup`, `flow.rollover_check_flow`, `flow.integrity_audit_flow`.
Owner layout under the sandbox: source\\FY26-27, nas\\backup\\FY26-27 (the share parent is nas\\backup).

Safety: this PC is the loopback "NAS". NAS shutdown and Wake-on-LAN are forced off and asserted; a shutdown can never
run. Never the real config, services, AAMTest or the trial box.

    $env:PREFECT_HOME = "$env:TEMP\\aam_prefect_home_d2_..." ; $env:PYTHONPATH = "."
    <python> -m pytest audit_lab/test_fy_overlap_lab.py -q -s --timeout=1800 -p no:cacheprovider -o addopts=""
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# a clean Prefect home is mandatory (the PC's ~/.prefect belongs to another Prefect version); the runner sets it
os.environ.setdefault("PREFECT_HOME", tempfile.mkdtemp(prefix="aam_prefect_home_d2_"))

from audit_lab import labclock  # noqa: E402
from audit_lab import labconf as L  # noqa: E402

pytestmark = pytest.mark.timeout(1800)

WORKTREE = Path(__file__).resolve().parents[1]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MARKER = ".AAM_TARGET_MOUNTED"
FY1, FY2 = "FY26-27", "FY27-28"


# ── fixture ────────────────────────────────────────────────────────────────────────────────────────────────────
def _write_config(base: Path, cfg) -> Path:
    import yaml

    p = base / "config.yaml"
    with p.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(json.loads(cfg.model_dump_json()), fh, sort_keys=False, allow_unicode=True)
    return p


class Rig:
    """Handles for one scenario. `cfg` always re-reads config.yaml (the flows do the same)."""

    def __init__(self, sandbox: Path, flow, cfg_path: Path, events: list, wol: list, shutdown):
        self.sandbox, self.flow, self.cfg_path = sandbox, flow, cfg_path
        self.events, self.wol_calls, self.shutdown = events, wol, shutdown
        self.src = sandbox / "source" / FY1
        self.nas_parent = sandbox / "nas" / "backup"
        self.buckets = sandbox / "buckets" / "lab-bucket"

    # config
    def cfg(self):
        from models.config import load_config

        return load_config(str(self.cfg_path))

    def rewrite_config(self, **overrides) -> None:
        """Rebuild config.yaml with nested overrides, e.g. lan={"scheduled": False}."""
        cfg = L.build_config(self.sandbox, source=self.src, lan_dest=str(self.nas_parent / FY1),
                             runtime=self.sandbox / "runtime", rollover={"overlap_enabled": True}, **overrides)
        _write_config(self.sandbox, cfg)

    # paths
    def source(self, fy: str) -> Path:
        return self.sandbox / "source" / fy

    def nas(self, fy: str) -> Path:
        return self.nas_parent / fy

    def bucket(self, fy: str) -> Path:
        return self.buckets / fy

    def db(self):
        from core.manifest import ManifestDB

        return ManifestDB.from_config(self.cfg())

    # database reads
    def rows(self) -> list[dict]:
        """All run_history rows, oldest first, with a 'fy' key (suffix of the run id, else the configured FY)."""
        from core import fy_overlap

        db = self.db()
        try:
            out = [dict(r) for r in reversed(db.get_recent_runs(500))]
        finally:
            db.close()
        for r in out:
            r["pass_fy"] = fy_overlap.fy_of_id(r["run_id"]) or FY1
            r["metrics"] = json.loads(r["extended_metrics"]) if r.get("extended_metrics") else {}
        return out

    def new_rows(self, seen: set) -> list[dict]:
        fresh = [r for r in self.rows() if r["run_id"] not in seen]
        seen.update(r["run_id"] for r in fresh)
        return fresh

    def approvals(self) -> list[dict]:
        db = self.db()
        try:
            return db.open_approvals()
        finally:
            db.close()

    def all_approvals(self) -> list[dict]:
        conn = sqlite3.connect(self.cfg().paths.database_path)
        conn.row_factory = sqlite3.Row
        try:
            out = [dict(r) for r in conn.execute("SELECT * FROM approvals ORDER BY id")]
        finally:
            conn.close()
        return out

    def sql(self, query: str, args: tuple = ()) -> list[dict]:
        conn = sqlite3.connect(self.cfg().paths.database_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(query, args)]
        finally:
            conn.close()

    def run(self, **kw):
        """flow.backup / flow.integrity_audit_flow style call returning the exception (or None)."""
        fn = kw.pop("fn", None) or self.flow.backup
        try:
            fn(config_path=str(self.cfg_path), **kw)
        except BaseException as e:  # noqa: BLE001 - a BLOCKED / FAILED run raises by design
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            return e
        return None

    def activate(self) -> str:
        """The daily check's activation step with a proven clock, dated 5 Jan 2027."""
        from core import fy_overlap

        db = self.db()
        try:
            res = fy_overlap.activate_if_due(self.cfg(), db, now=dt.datetime(2027, 1, 5, 10, 0, tzinfo=IST))
        finally:
            db.close()
        assert res == fy_overlap.ACTIVATED, res
        return res

    def overlap_record(self, fy: str):
        db = self.db()
        try:
            return db.overlap_record(fy)
        finally:
            db.close()

    def approve(self, req: dict) -> str:
        from core import approvals as ap
        from core.time_utils import now_iso

        db = self.db()
        try:
            at = now_iso()
            assert db.approve_request(req["approval_id"], approved_by="lab (api-key)", approved_at=at,
                                      expires_at=ap.expires_at(at, 12))
        finally:
            db.close()
        return req["approval_id"]


def _populate(folder: Path, n: int, tag: str, subs: bool = False) -> None:
    for i in range(n):
        L.write_file(folder / "Clients" / f"f_{i:04d}.txt", f"{tag} {i}\n".encode() * 40)
    if subs:
        L.write_file(folder / "Clients" / "Zero.txt", b"")
        L.write_file(folder / "Clients" / "Sub A" / "a.txt", f"{tag} a\n".encode() * 20)
        L.write_file(folder / "Clients" / "Sub B" / "b.txt", f"{tag} b\n".encode() * 20)
        L.write_file(folder / "Clients" / "Sub B" / "café മല.txt", f"{tag} u\n".encode() * 20)


def _files(path: Path, skip_placeholder: str = "README_FY.txt") -> dict[str, tuple]:
    """relative path -> (md5, size, mtime_ns); the mount marker and the FY placeholder are excluded."""
    out = {}
    if not path.exists():
        return out
    for p in sorted(path.rglob("*")):
        if p.is_file() and p.name not in (MARKER, skip_placeholder):
            st = p.stat()
            out[str(p.relative_to(path))] = (hashlib.md5(p.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns)
    return out


def _md5s(path: Path) -> dict[str, str]:
    return {k: v[0] for k, v in _files(path).items()}


def _mirror_ok(rig: Rig, fy: str, nas: bool = True, cloud: bool = True) -> tuple[bool, str]:
    src = _md5s(rig.source(fy))
    bad = []
    if cloud and _md5s(rig.bucket(fy)) != src:
        bad.append("cloud differs")
    if nas and _md5s(rig.nas(fy)) != src:
        bad.append("nas differs")
    return not bad, ", ".join(bad)


def _ok_row(row: dict, leg: str) -> bool:
    return row["status"] in ({"cloud": ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"),
                              "lan": ("LAN_COMPLETE",)}[leg])


def _by_pass(rows: list[dict]) -> dict[str, dict]:
    """{'cloud': row, 'cloud-FY27-28': row, ...} for a list of fresh rows."""
    return {r["mode"] + ("" if r["pass_fy"] == FY1 else "-" + r["pass_fy"]): r for r in rows}


@pytest.fixture()
def overlap_rig(sandbox, monkeypatch):
    import flow
    from core import fy_overlap
    from core.health import CLOCK_OK, ClockResult

    # F-D11: the lab must test the worktree's code
    flow_file = Path(flow.__file__).resolve()
    assert str(flow_file).lower().startswith(str(WORKTREE).lower()), f"lab imports {flow_file}, not the worktree"
    assert "aam_backup" not in flow_file.parts, f"lab imports the main checkout: {flow_file}"
    home = Path(os.environ["PREFECT_HOME"]).resolve()
    assert home != (Path.home() / ".prefect").resolve(), "PREFECT_HOME must be a clean temp folder"

    # owner layout (the sandbox pre-creates nas\FY26-27 and gcs: unused)
    src = sandbox / "source" / FY1
    _populate(src, 60, "LEDGER", subs=True)
    nas_parent = sandbox / "nas" / "backup"
    L.make_canary(nas_parent / FY1)
    L.write_file(sandbox / "lab-key.json", b'{"type": "service_account"}')
    buckets = sandbox / "buckets"
    # lab artefact (F-D6): GCS answers an empty prefix with [] exit 0, the local backend fails on a missing folder
    (buckets / "lab-bucket" / FY1).mkdir(parents=True, exist_ok=True)
    (buckets / "lab-bucket" / FY2).mkdir(parents=True, exist_ok=True)

    cfg = L.build_config(sandbox, source=src, lan_dest=str(nas_parent / FY1), runtime=sandbox / "runtime",
                         rollover={"overlap_enabled": True})
    cfg_path = _write_config(sandbox, cfg)
    unc_ok = os.path.isdir(cfg.paths.lan_destination)
    if not unc_ok:
        pytest.skip(f"loopback admin share not reachable ({cfg.paths.lan_destination}): the LAN legs cannot run "
                    "here (needs an elevated shell / admin share)")

    # NEVER power off this PC
    assert cfg.lan.shutdown_after_backup is False and cfg.wol.enabled is False
    import core.shutdown as sd
    assert sd.shutdown_server.__name__ == "<lambda>", "the lab shutdown guard is not in place"

    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)

    events: list = []
    wol_calls: list = []
    shutdown = MagicMock(name="lan_shutdown_task")
    shutdown.with_options.return_value = shutdown
    clock_ok = lambda **k: ClockResult(CLOCK_OK, 0.1, "lab", "")  # noqa: E731
    import contextlib

    monkeypatch.setattr(flow, "notify", lambda cfg_, ev, *a, **k: events.append(ev) or True)
    monkeypatch.setattr(fy_overlap, "notify", lambda cfg_, ev, *a, **k: events.append(ev) or True)
    monkeypatch.setattr(fy_overlap, "check_clock", clock_ok)
    monkeypatch.setattr(flow, "check_clock", clock_ok)
    monkeypatch.setattr(flow, "wol_check_task", lambda *a, **k: wol_calls.append(a))
    monkeypatch.setattr(flow, "lan_shutdown_task", shutdown)
    monkeypatch.setattr(flow, "temp_rclone_config", lambda *a, **k: contextlib.nullcontext(rc))
    monkeypatch.chdir(buckets)
    monkeypatch.delenv("PREFECT_TEST_MODE", raising=False)   # the REAL concurrency slot

    from prefect.testing.utilities import prefect_test_harness

    try:
        with prefect_test_harness():
            from launch import _ensure_concurrency_limit

            _ensure_concurrency_limit()
            rig = Rig(sandbox, flow, cfg_path, events, wol_calls, shutdown)
            yield rig
            # the flow calls the shutdown task after every LAN_COMPLETE pass; with both switches off the real task
            # is a no-op. What must hold: every call saw shutdown off (the lab guard also raises if one got through)
            for call in shutdown.call_args_list:
                c = call.args[0]
                assert c.lan.shutdown_after_backup is False and c.wol.enabled is False, "NAS shutdown not forced off"
    finally:
        for (mod, attr), o in saved.items():
            setattr(mod, attr, o)


# ── S0 + S1 ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_fyov_before_activation_is_baseline(overlap_rig):
    """S0: no activation record -> exactly the baseline (two rows, no FY27-28 anywhere).
    S1: the daily check activates (source folder + placeholder only), once, NAS-independent, config untouched."""
    rig = overlap_rig
    seen: set = set()
    exc = rig.run(mode="all")
    rows = rig.new_rows(seen)
    by = _by_pass(rows)
    L.record("FYOV.S0", "overlap not active = baseline", f"exc={exc!r} rows={[(r['run_id'][-24:], r['status']) for r in rows]}",
             "OK" if exc is None and sorted(by) == ["cloud", "lan"] else "ISSUE")
    assert exc is None, exc
    assert sorted(by) == ["cloud", "lan"], sorted(by)
    for leg, r in by.items():
        assert _ok_row(r, leg), r["status"]
        assert not r["run_id"].endswith(FY2)
        assert "overlap" not in r["metrics"] and r["metrics"].get("fy") in (None, FY1) or "fy" not in r["metrics"]
    assert not rig.source(FY2).exists() and not rig.nas(FY2).exists()
    assert list(rig.bucket(FY2).iterdir()) == []
    assert rig.overlap_record(FY2) is None

    # S1 - the daily check at 06:00 on 5 Jan 2027
    import hashlib as _h
    cfg_bytes = _h.sha256(rig.cfg_path.read_bytes()).hexdigest()
    mods = labclock.CLOCK_MODULES + ("core.fy_overlap",)
    with labclock.fake_clock("2027-01-05T06:00:00+05:30", modules=mods, tick=0.001):
        r1 = rig.flow.rollover_check_flow(config_path=str(rig.cfg_path))
    rec = rig.overlap_record(FY2)
    mails = [e for e in rig.events if getattr(e, "key", "") == f"rollover:activated:{FY2}"]
    ph = rig.source(FY2) / "README_FY.txt"
    L.record("FYOV.S1", "daily check activates FY27-28", f"result={r1!r} record={rec} mails={len(mails)} placeholder={ph.is_file()}",
             "OK" if rec and ph.is_file() and len(mails) == 1 else "ISSUE")
    assert ph.is_file(), list(rig.source(FY2).iterdir()) if rig.source(FY2).exists() else "no source folder"
    assert rec and rec.get("activated_at") and rec.get("from_fy") == FY1, rec
    assert len(mails) == 1 and mails[0].kind == "INFO"
    assert not rig.nas(FY2).exists(), "the activation must not create the NAS folder"
    assert _h.sha256(rig.cfg_path.read_bytes()).hexdigest() == cfg_bytes, "config.yaml must not change"
    # second call: idempotent
    with labclock.fake_clock("2027-01-05T06:00:00+05:30", modules=mods, tick=0.001):
        rig.flow.rollover_check_flow(config_path=str(rig.cfg_path))
    assert len([e for e in rig.events if getattr(e, "key", "") == f"rollover:activated:{FY2}"]) == 1
    assert rig.overlap_record(FY2) == rec


# ── S2 + S3 ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_fyov_two_mirrors_two_prefixes(overlap_rig):
    """S2: one nightly run = four rows, two mirrors per leg, nothing crosses. S3: a delete in FY27-28 never
    touches the FY26-27 targets."""
    rig = overlap_rig
    seen: set = set()
    rig.activate()
    _populate(rig.source(FY2), 20, "NEXTFY")          # f_0001.txt exists in both years with different content
    exc = rig.run(mode="all")
    rows = rig.new_rows(seen)
    by = _by_pass(rows)
    names = [r["mode"] + ("" if r["pass_fy"] == FY1 else "-" + r["pass_fy"]) for r in rows]
    L.record("FYOV.S2.rows", "four passes", f"exc={exc!r} order={names} statuses={[r['status'] for r in rows]}",
             "OK" if names == ["cloud", f"cloud-{FY2}", "lan", f"lan-{FY2}"] else "ISSUE")
    assert exc is None, exc
    assert names == ["cloud", f"cloud-{FY2}", "lan", f"lan-{FY2}"], names
    for key, r in by.items():
        assert _ok_row(r, r["mode"]), (key, r["status"])
        if key.endswith(FY2):
            assert r["metrics"].get("fy") == FY2 and r["metrics"].get("overlap") is True, r["metrics"]
    for fy in (FY1, FY2):
        ok, why = _mirror_ok(rig, fy)
        assert ok, f"{fy}: {why}"
    assert (rig.nas(FY2) / MARKER).is_file(), "the FY27-28 pass makes its own NAS folder + marker"
    shared = "Clients/f_0001.txt".replace("/", os.sep)
    for target in (rig.bucket(FY1), rig.nas(FY1)):
        assert b"LEDGER 1" in (target / shared).read_bytes()
    for target in (rig.bucket(FY2), rig.nas(FY2)):
        assert b"NEXTFY 1" in (target / shared).read_bytes()
    only1 = set(_md5s(rig.source(FY1))) - set(_md5s(rig.source(FY2)))
    only2 = set(_md5s(rig.source(FY2))) - set(_md5s(rig.source(FY1)))
    for t1, t2 in ((rig.bucket(FY1), rig.bucket(FY2)), (rig.nas(FY1), rig.nas(FY2))):
        assert not (only2 & set(_md5s(t1))) and not (only1 & set(_md5s(t2)))
    # D-9: file_entries stay the configured FY's (no FY27-28 pass fills them)
    entries = rig.sql("SELECT COUNT(*) AS n FROM file_entries")[0]["n"]
    n_fy1 = len(_md5s(rig.source(FY1)))
    L.record("FYOV.S2.entries", "file_entries are configured-FY only", f"entries={entries} fy1_files={n_fy1}",
             "OK" if entries <= n_fy1 + 1 else "ISSUE")
    assert entries <= n_fy1 + 1, f"file_entries={entries} > FY26-27 files {n_fy1}: next-FY files leaked in"
    # run-log artefacts
    runtime = rig.cfg().paths.runtime_dir if hasattr(rig.cfg().paths, "runtime_dir") else str(rig.sandbox / "runtime")
    folders = [p for p in Path(runtime).rglob("*") if p.is_dir() and p.name.endswith(f"_{FY2}")]
    logs = [p for p in Path(runtime).rglob("*.log") if f"_cloud_{FY2}_" in p.name or f"_lan_{FY2}_" in p.name]
    lan_logs = [p for p in logs if f"_lan_{FY2}_" in p.name]
    cloud_logs = [p for p in logs if f"_cloud_{FY2}_" in p.name]
    utf8_hit = False
    for p in lan_logs:
        try:
            if FY2 in p.read_bytes().decode("utf-8", errors="ignore"):
                utf8_hit = True
        except OSError:
            pass
    L.record("FYOV.S2.logs", "FY27-28 run-log folders and files", f"folders={len(folders)} lan_logs={len(lan_logs)} "
             f"cloud_logs={len(cloud_logs)} utf8_has_fy={utf8_hit}", "OK" if folders and lan_logs and cloud_logs else "ISSUE")
    assert folders, "no run-log folder *_FY27-28"
    assert lan_logs and cloud_logs, "no *_lan_FY27-28_*.log / *_cloud_FY27-28_*.log"
    assert utf8_hit, "no rclone LAN log (UTF-8 JSON) holding FY27-28 paths"
    # shutdown task calls (if any) all saw shutdown_after_backup False (asserted in the fixture teardown)

    # S3 - delete in the next FY only
    snap = {k: _files(k_path) for k, k_path in (("bucket", rig.bucket(FY1)), ("nas", rig.nas(FY1)))}
    (rig.source(FY2) / "Clients" / "f_0001.txt").unlink()
    exc = rig.run(mode="all")
    rows = rig.new_rows(seen)
    assert exc is None, exc
    assert len(rows) == 4 and all(_ok_row(r, r["mode"]) for r in rows), [(r["run_id"], r["status"]) for r in rows]
    for target in (rig.bucket(FY2), rig.nas(FY2)):
        assert not (target / shared).exists(), f"{target} still holds the deleted file"
    assert _files(rig.bucket(FY1)) == snap["bucket"], "bucket FY26-27 changed (md5/size/mtime)"
    assert _files(rig.nas(FY1)) == snap["nas"], "NAS FY26-27 changed (md5/size/mtime)"
    assert b"LEDGER 1" in (rig.bucket(FY1) / shared).read_bytes() and b"LEDGER 1" in (rig.nas(FY1) / shared).read_bytes()
    L.record("FYOV.S3", "delete in FY27-28 never touches FY26-27", "FY26-27 targets identical (md5, size, mtime)", "OK")


# ── S4 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_fyov_g3_blocks_per_fy(overlap_rig):
    """S4: the delete cap (G3) judges each FY on its own; approvals are per (leg, FY)."""
    rig = overlap_rig
    seen: set = set()
    rig.activate()
    _populate(rig.source(FY2), 20, "NEXTFY")
    exc = rig.run(mode="all")
    assert exc is None, exc
    assert len(rig.new_rows(seen)) == 4

    for i in range(10):                                   # 50 % of FY27-28 > 10 %
        (rig.source(FY2) / "Clients" / f"f_{i:04d}.txt").unlink()
    before = {"bucket": _md5s(rig.bucket(FY2)), "nas": _md5s(rig.nas(FY2))}
    exc = rig.run(mode="all")
    by = _by_pass(rig.new_rows(seen))
    L.record("FYOV.S4.a", "G3 blocks only FY27-28", f"exc={type(exc).__name__} statuses={ {k: v['status'] for k, v in by.items()} }",
             "OK" if by[f'cloud-{FY2}']['status'].endswith('_BLOCKED') else "ISSUE")
    assert exc is not None, "the flow must fail while a pass is blocked"
    assert _ok_row(by["cloud"], "cloud") and _ok_row(by["lan"], "lan")
    for key in (f"cloud-{FY2}", f"lan-{FY2}"):
        assert by[key]["status"].endswith("_BLOCKED"), (key, by[key]["status"])
        assert "G3" in json.dumps(by[key]["metrics"].get("guards", by[key]["metrics"])), by[key]["metrics"]
    assert _md5s(rig.bucket(FY2)) == before["bucket"] and _md5s(rig.nas(FY2)) == before["nas"]
    open_ = rig.approvals()
    assert len(open_) == 2 and {a["fy"] for a in open_} == {FY2} and {a["leg"] for a in open_} == {"cloud", "lan"}, open_

    # FY26-27 shrinks too (10 of 60+ files > 10 %)
    for i in range(10, 20):
        (rig.source(FY1) / "Clients" / f"f_{i:04d}.txt").unlink()
    for i in range(20, 30):
        (rig.source(FY1) / "Clients" / f"f_{i:04d}.txt").unlink()
    exc = rig.run(mode="all")
    by = _by_pass(rig.new_rows(seen))
    assert all(v["status"].endswith("_BLOCKED") for v in by.values()), {k: v["status"] for k, v in by.items()}
    open_ = rig.approvals()
    keys = sorted((a["leg"], a["fy"]) for a in open_)
    L.record("FYOV.S4.b", "four blocked passes, one approval per (leg, FY)", f"open approvals={keys}",
             "OK" if len(keys) == 4 else "ISSUE")
    assert keys == [("cloud", FY1), ("cloud", FY2), ("lan", FY1), ("lan", FY2)], keys

    # (a) approve the cloud FY27-28 request, run cloud without fy
    req = next(a for a in open_ if a["leg"] == "cloud" and a["fy"] == FY2)
    before1 = _md5s(rig.bucket(FY1))
    aid = rig.approve(req)
    exc = rig.run(mode="cloud", approval_id=aid)
    by = _by_pass(rig.new_rows(seen))
    used = next(a for a in rig.all_approvals() if a["approval_id"] == aid)
    L.record("FYOV.S4.c", "an FY27-28 approval never unblocks FY26-27", f"statuses={ {k: v['status'] for k, v in by.items()} } used_by={used['used_by_run_id']}",
             "OK" if by["cloud"]["status"].endswith("_BLOCKED") and _ok_row(by[f"cloud-{FY2}"], "cloud") else "ISSUE")
    assert by["cloud"]["status"].endswith("_BLOCKED"), by["cloud"]["status"]
    assert _ok_row(by[f"cloud-{FY2}"], "cloud"), by[f"cloud-{FY2}"]["status"]
    assert _md5s(rig.bucket(FY1)) == before1, "bucket FY26-27 changed under an FY27-28 approval"
    assert _md5s(rig.bucket(FY2)) == _md5s(rig.source(FY2))
    assert used["status"] == "USED" and used["used_by_run_id"] == by[f"cloud-{FY2}"]["run_id"], used

    # (b) approve the LAN FY26-27 request and run only that FY
    req = next(a for a in rig.approvals() if a["leg"] == "lan" and a["fy"] == FY1)
    aid = rig.approve(req)
    exc = rig.run(mode="lan", approval_id=aid, fy=FY1)
    fresh = rig.new_rows(seen)
    L.record("FYOV.S4.d", "fy= runs exactly one pass", f"exc={exc!r} rows={[(r['run_id'][-20:], r['status']) for r in fresh]}",
             "OK" if len(fresh) == 1 else "ISSUE")
    assert exc is None, exc
    assert len(fresh) == 1 and fresh[0]["pass_fy"] == FY1 and _ok_row(fresh[0], "lan"), fresh
    assert _md5s(rig.nas(FY1)) == _md5s(rig.source(FY1))
    still = [a for a in rig.approvals() if a["leg"] == "lan" and a["fy"] == FY2]
    assert still and still[0]["status"] == "PENDING", still


# ── S5 / S5b ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_fyov_g5_per_fy(overlap_rig):
    """S5: a NAS FY27-28 folder with files but no marker is blocked by G5 (approval per FY); one pass's block never
    stops the other."""
    rig = overlap_rig
    seen: set = set()
    rig.activate()
    _populate(rig.source(FY2), 5, "NEXTFY")
    office = rig.nas(FY2) / "office.xlsx"
    L.write_file(office, b"someone elses file")
    exc = rig.run(mode="lan")
    by = _by_pass(rig.new_rows(seen))
    L.record("FYOV.S5.a", "G5 blocks FY27-28 only", f"statuses={ {k: v['status'] for k, v in by.items()} }",
             "OK" if by[f"lan-{FY2}"]["status"] == "LAN_BLOCKED" else "ISSUE")
    assert exc is not None
    assert _ok_row(by["lan"], "lan") and by[f"lan-{FY2}"]["status"] == "LAN_BLOCKED", {k: v["status"] for k, v in by.items()}
    assert "G5_CANARY" in json.dumps(by[f"lan-{FY2}"]["metrics"]), by[f"lan-{FY2}"]["metrics"]
    assert office.read_bytes() == b"someone elses file"
    assert sorted(p.name for p in rig.nas(FY2).iterdir()) == ["office.xlsx"], "nothing else may be written"
    open_ = rig.approvals()
    assert len(open_) == 1 and open_[0]["fy"] == FY2 and open_[0]["leg"] == "lan", open_

    aid = rig.approve(open_[0])
    exc = rig.run(mode="lan", approval_id=aid, fy=FY2)
    fresh = rig.new_rows(seen)
    used = next(a for a in rig.all_approvals() if a["approval_id"] == aid)
    L.record("FYOV.S5.b", "approved FY27-28 run", f"exc={exc!r} rows={[r['status'] for r in fresh]} approval={used['status']}",
             "OK" if exc is None and len(fresh) == 1 and _ok_row(fresh[0], "lan") else "ISSUE")
    assert exc is None, exc
    assert len(fresh) == 1 and _ok_row(fresh[0], "lan"), fresh
    assert (rig.nas(FY2) / MARKER).is_file() and not office.exists()
    assert _md5s(rig.nas(FY2)) == _md5s(rig.source(FY2))
    assert used["status"] == "USED"

    (rig.nas(FY1) / MARKER).unlink()
    exc = rig.run(mode="lan")
    by = _by_pass(rig.new_rows(seen))
    L.record("FYOV.S5.c", "a block in pass 1 never stops pass 2", f"statuses={ {k: v['status'] for k, v in by.items()} }",
             "OK" if by["lan"]["status"] == "LAN_BLOCKED" and _ok_row(by[f"lan-{FY2}"], "lan") else "ISSUE")
    assert exc is not None
    assert by["lan"]["status"] == "LAN_BLOCKED" and _ok_row(by[f"lan-{FY2}"], "lan"), {k: v["status"] for k, v in by.items()}


def test_fyov_empty_nas_folder_is_marked(overlap_rig):
    """S5b: an empty NAS FY27-28 folder is ours: marker written, no approval."""
    rig = overlap_rig
    seen: set = set()
    rig.activate()
    _populate(rig.source(FY2), 5, "NEXTFY")
    rig.nas(FY2).mkdir(parents=True)
    exc = rig.run(mode="lan")
    by = _by_pass(rig.new_rows(seen))
    L.record("FYOV.S5b", "empty folder is marked", f"exc={exc!r} statuses={ {k: v['status'] for k, v in by.items()} }",
             "OK" if exc is None and _ok_row(by[f"lan-{FY2}"], "lan") else "ISSUE")
    assert exc is None, exc
    assert _ok_row(by[f"lan-{FY2}"], "lan") and (rig.nas(FY2) / MARKER).is_file()
    assert rig.approvals() == []


# ── S6 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_fyov_weekly_audit_both_fys(overlap_rig):
    """S6: the weekly audit checks both FYs per leg, next FY first; damage in FY27-28 shows on FY27-28 only."""
    rig = overlap_rig
    seen: set = set()
    rig.activate()
    _populate(rig.source(FY2), 20, "NEXTFY")
    exc = rig.run(mode="all")
    assert exc is None, exc
    assert len(rig.new_rows(seen)) == 4

    rig.wol_calls.clear()
    exc = rig.run(fn=rig.flow.integrity_audit_flow, mode="all")
    audits = rig.sql("SELECT * FROM integrity_audits ORDER BY started_at, id")
    ids = [a["audit_id"] for a in audits]
    L.record("FYOV.S6.a", "first audit", f"exc={exc!r} audits={[(a['audit_id'][-26:], a['status']) for a in audits]} wol_calls={len(rig.wol_calls)}",
             "OK" if len(audits) == 4 and all(a["status"] == "VERIFIED" for a in audits) else "ISSUE")
    assert exc is None, exc
    assert len(audits) == 4 and all(a["status"] == "VERIFIED" for a in audits), [(a["audit_id"], a["status"]) for a in audits]
    for leg in ("cloud", "lan"):
        mine = [a["audit_id"] for a in audits if a["mode"] == leg]
        assert len(mine) == 2 and mine[0].endswith(f"-{leg}-full-{FY2}") and not mine[1].endswith(FY2), mine
    assert len(rig.wol_calls) == 1, f"one NAS wake per audit run, got {len(rig.wol_calls)}"
    n_before = len(audits)

    target = rig.nas(FY2) / "Clients" / "f_0002.txt"
    original = target.read_bytes()
    target.write_bytes(bytes(b ^ 0x01 for b in original))          # same size, different bytes
    rig.events.clear()
    exc = rig.run(fn=rig.flow.integrity_audit_flow, mode="all")
    audits = rig.sql("SELECT * FROM integrity_audits ORDER BY started_at, id")[n_before:]
    st = {("lan" if a["mode"] == "lan" else "cloud") + ("-" + FY2 if a["audit_id"].endswith(FY2) else ""): a["status"] for a in audits}
    blob = " ".join(repr(e) for e in rig.events)
    L.record("FYOV.S6.b", "damage in FY27-28 NAS", f"exc={type(exc).__name__} statuses={st} alert_has_key={'lan@' + FY2 + '=VERIFICATION_FAILED' in blob}",
             "OK" if st.get(f"lan-{FY2}") == "VERIFICATION_FAILED" and st.get("lan") == "VERIFIED" else "ISSUE")
    assert exc is not None, "a failed audit must fail the flow"
    assert st.get(f"lan-{FY2}") == "VERIFICATION_FAILED", st
    assert st.get("lan") == "VERIFIED", st
    assert f"lan@{FY2}=VERIFICATION_FAILED" in blob, blob[:800]


# ── S7 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_fyov_manual_only_lan_and_overlap_off(overlap_rig):
    """S7: a manual-only NAS leg still takes part in a hand-started run; switching the overlap off is the baseline."""
    rig = overlap_rig
    seen: set = set()
    rig.rewrite_config(lan={"scheduled": False})
    assert rig.cfg().lan.scheduled is False and rig.cfg().cloud.scheduled is True
    rig.activate()
    _populate(rig.source(FY2), 20, "NEXTFY")
    exc = rig.run(mode="lan")
    rows = rig.new_rows(seen)
    names = [r["mode"] + ("" if r["pass_fy"] == FY1 else "-" + r["pass_fy"]) for r in rows]
    L.record("FYOV.S7.a", "manual-only LAN joins the overlap", f"exc={exc!r} rows={names} statuses={[r['status'] for r in rows]}",
             "OK" if names == ["lan", f"lan-{FY2}"] and all(_ok_row(r, "lan") for r in rows) else "ISSUE")
    assert exc is None, exc
    assert names == ["lan", f"lan-{FY2}"] and all(_ok_row(r, "lan") for r in rows), names
    assert _md5s(rig.nas(FY2)) == _md5s(rig.source(FY2))

    # overlap off: same config, rollover.overlap_enabled false
    import yaml
    data = yaml.safe_load(rig.cfg_path.read_text(encoding="utf-8"))
    data["rollover"]["overlap_enabled"] = False
    rig.cfg_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    assert rig.cfg().rollover.overlap_enabled is False
    (rig.source(FY2) / "Clients" / "extra_new.txt").write_bytes(b"new in FY27-28")
    before = {"bucket": _files(rig.bucket(FY2)), "nas": _files(rig.nas(FY2))}
    exc = rig.run(mode="all")
    rows = rig.new_rows(seen)
    names = [r["mode"] + ("" if r["pass_fy"] == FY1 else "-" + r["pass_fy"]) for r in rows]
    L.record("FYOV.S7.b", "overlap off = baseline", f"exc={exc!r} rows={names}", "OK" if sorted(names) == ["cloud", "lan"] else "ISSUE")
    assert exc is None, exc
    assert sorted(names) == ["cloud", "lan"], names
    assert _files(rig.bucket(FY2)) == before["bucket"] and _files(rig.nas(FY2)) == before["nas"], \
        "FY27-28 targets must not change when the overlap is off"
