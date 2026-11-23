"""WAVE 25 - FY overlap, the 15-month fake-clock scenario (task OV-11.1, plan docs/PLAN_FY_OVERLAP_2026-11-20.md
section 7 test 8).

Everything is driven through the PRODUCTION Prefect flows (flow.backup, flow.rollover_check_flow,
flow.integrity_audit_flow) with REAL robocopy (loopback admin share = the NAS) and REAL rclone (a `type = local`
remote = the bucket), while the wall clock is moved through the seam in audit_lab/labclock.py.

The calendar (all IST, nights at 01:00, the 06:00 rollover check where it matters):

    Dec 2026         one FY folder (FY26-27), no activation, nothing else in the bucket / on the NAS
    1 Jan 2027       ACTIVATION: FY27-28 created next to FY26-27 (placeholder), NAS folder + marker created,
                     both folders backed up by both legs, one INFO mail
    Jan - Mar        two folders every night; the same relative path in both FYs; Sunday audit of both FYs
    31 Mar           last in-window night
    1 Apr 01:00      switch pending: both folders still backed up
    1 Apr 06:00      rollover: final backup of FY26-27, config switch to FY27-28, FY26-27 closed
    Apr - Dec 2027   one folder (FY27-28); FY26-27 is never touched again
    1 Jan 2028       next cycle: FY28-29 activated next to FY27-28

Seams (lab only, no production file is edited): labclock.fake_clock on the modules that read the clock
(the two defaults + flow, core.fy_overlap, core.attention, core.manifest, core.run_artefacts), the same
local-backend / no-/ZB wiring as WAVE 12, mail and the NAS power-off are recorders (the lab can never send a mail
or shut a machine down).

Run with a clean Prefect home (CLAUDE.md "Environment gotchas"):
    PREFECT_HOME=<empty dir> .venv\\Scripts\\python.exe -m pytest audit_lab/test_w25_fy_overlap.py -q -s -p no:cacheprovider -o addopts=""
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

import pendulum
import pytest

from audit_lab import labclock
from audit_lab import labconf as L
from audit_lab.test_w12_clock import _flow_call, _tree_map, _write_cfg

pytestmark = pytest.mark.timeout(3600)

IST = "Asia/Kolkata"
FY_A, FY_B, FY_C = "FY26-27", "FY27-28", "FY28-29"   # A = configured at the start
_OK = ("LAN_COMPLETE", "CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE")
_BAD_KINDS = ("FAILURE", "ACTION_REQUIRED", "WARNING", "RECOVERED")
#: production modules whose own `pendulum` reference the fake clock must replace for the overlap code
_MODULES = labclock.CLOCK_MODULES + ("flow", "core.fy_overlap", "core.attention", "core.manifest",
                                      "core.run_artefacts")


def _at(y, mo, d, h=1, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


class Lab:
    """The rig plus the helpers the scenario needs."""

    def __init__(self, sandbox, flow, cfg_path, alerts, shutdowns):
        self.sandbox, self.flow, self.cfg_path = sandbox, flow, cfg_path
        self.alerts, self.shutdowns = alerts, shutdowns
        self.source_parent = sandbox / "source"
        self.nas_parent = sandbox / "nas"
        self.bucket = sandbox / "buckets" / "lab-bucket"

    # ---- paths ----
    def src(self, fy): return self.source_parent / fy
    def nas(self, fy): return self.nas_parent / fy
    def cloud(self, fy): return self.bucket / fy

    # ---- db ----
    @contextlib.contextmanager
    def db(self):
        from core.manifest import ManifestDB

        from models.config import load_config
        cfg = load_config(str(self.cfg_path))
        m = ManifestDB.from_config(cfg)
        try:
            yield cfg, m
        finally:
            m.close()

    def config(self):
        from models.config import load_config
        return load_config(str(self.cfg_path))

    def legs(self, fy):
        with self.db() as (_c, m):
            return [r for r in m.get_recent_runs(5000, fy=fy) if r["mode"] in ("cloud", "lan")]

    def counts(self, fys):
        out = {}
        for fy in fys:
            rows = self.legs(fy)
            out[fy] = {leg: sum(1 for r in rows if r["mode"] == leg) for leg in ("cloud", "lan")}
        return out

    # ---- the flows, under the fake clock ----
    def clock(self, at):
        return labclock.fake_clock(at, modules=_MODULES, tick=0.001)

    def backup(self, at, mode="all"):
        before = len(self.shutdowns)
        with self.clock(at):
            _flow_call(self.flow.backup, config_path=str(self.cfg_path), mode=mode)
        time.sleep(1.0)       # keep Prefect's harness DB from two state writes on one microsecond
        return len(self.shutdowns) - before

    def rollover(self, at):
        with self.clock(at):
            res = _flow_call(self.flow.rollover_check_flow, config_path=str(self.cfg_path))
        time.sleep(1.0)
        return res

    def audit(self, at, mode="all"):
        with self.clock(at):
            _flow_call(self.flow.integrity_audit_flow, config_path=str(self.cfg_path), mode=mode)
        time.sleep(1.0)

    # ---- the accountants' work ----
    def work(self, fy, tag, *, amend=True, delete=None):
        src = self.src(fy)
        L.write_file(src / "Clients" / f"n_{tag}.txt", f"{fy} new work {tag}\n".encode() * 20)
        if amend and (src / "Clients" / "a_000.txt").exists():
            (src / "Clients" / "a_000.txt").write_bytes(f"{fy} amended {tag}\n".encode() * 20)
        if delete and (src / "Clients" / delete).exists():
            (src / "Clients" / delete).unlink()

    # ---- checks ----
    def converged(self, fy, label):
        """Source == NAS == cloud prefix, byte for byte (sha256), for ONE FY folder."""
        s, n, c = _tree_map(self.src(fy)), _tree_map(self.nas(fy)), _tree_map(self.cloud(fy))
        assert s, f"{label}: {fy} source is empty"
        assert n == s, f"{label}: NAS {fy} differs from the source: " \
                       f"missing={sorted(set(s) - set(n))[:4]} different={sorted(p for p in set(s) & set(n) if s[p] != n[p])[:4]} " \
                       f"extra={sorted(set(n) - set(s))[:4]}"
        assert c == s, f"{label}: cloud {fy} differs from the source: " \
                       f"missing={sorted(set(s) - set(c))[:4]} different={sorted(p for p in set(s) & set(c) if s[p] != c[p])[:4]} " \
                       f"extra={sorted(set(c) - set(s))[:4]}"

    def expect_night(self, label, before, fys, shutdown_calls):
        """After one nightly flow: exactly one new cloud + one new LAN success row per FY in `fys`,
        nothing for any other FY, and exactly one NAS shutdown decision."""
        after = self.counts(before.keys())
        for fy, legs in after.items():
            for leg, n in legs.items():
                want = before[fy][leg] + (1 if fy in fys else 0)
                assert n == want, f"{label}: {fy} {leg} rows {before[fy][leg]} -> {n}, expected {want}"
        for fy in fys:
            for leg in ("cloud", "lan"):
                last = [r for r in self.legs(fy) if r["mode"] == leg][0]
                assert last["status"] in _OK, f"{label}: {fy} {leg} ended {last['status']}"
        assert shutdown_calls == 1, f"{label}: {shutdown_calls} NAS shutdown decisions in one LAN flow (expected exactly 1)"
        with self.db() as (_c, m):
            assert not m.open_approvals(), f"{label}: an approval is open: {m.open_approvals()}"

    def shutdown_rows(self, fys):
        """LAN run rows (all FYs given) that carry the nas_shutdown marker of the loop-level decision."""
        n = 0
        for fy in fys:
            for r in self.legs(fy):
                if r["mode"] != "lan":
                    continue
                try:
                    metrics = json.loads(r.get("extended_metrics") or "{}")
                except ValueError:
                    metrics = {}
                if isinstance(metrics, dict) and "nas_shutdown" in metrics:
                    n += 1
        return n


@pytest.fixture()
def lab(sandbox, monkeypatch):
    """The production wiring of WAVE 12, on a rig that has ONE FY folder (FY26-27) in December."""
    import core.lan_sync as ls
    import core.notify as notify_mod
    import flow

    src = sandbox / "source" / FY_A
    L.make_canary(sandbox / "nas" / FY_A)
    L.write_file(sandbox / "lab-key.json", b'{"type": "service_account"}')
    for i in range(12):
        L.write_file(src / "Clients" / f"a_{i:03d}.txt", f"{FY_A} ledger {i}\n".encode() * 30)
    (sandbox / "buckets" / "lab-bucket").mkdir(parents=True, exist_ok=True)   # the bucket; NO FY prefix in it

    cfg = L.build_config(sandbox, source=src, lan_dest=str(sandbox / "nas" / FY_A), runtime=sandbox / "runtime",
                         maintenance={"db_retention_days": 3650})          # 15 simulated months stay readable
    cfg_path = _write_cfg(sandbox, cfg)
    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)

    orig_build = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig_build(s, d, c))

    alerts: list = []

    def _record_alert(config, event, *a, **k):
        alerts.append(event)
        return True

    shutdowns: list = []

    def _spy_shutdown(config, artefacts_dir=None):
        shutdowns.append(artefacts_dir)
        return {"simulated": True, "note": "audit lab: the NAS is never powered off"}

    # Lab-only GCS semantics: `rclone lsjson` of an EMPTY prefix returns [] on GCS, but a `local` remote answers
    # "directory not found" for a prefix that was never written. The preflight probe is therefore given an empty
    # prefix directory (the prefix is only listable; nothing is copied into it by this seam).
    real_dry_run = flow.run_cloud_dry_run

    def _dry_run_with_listable_prefix(source, bucket, fy_prefix, *a, **k):
        (sandbox / "buckets" / bucket / fy_prefix).mkdir(parents=True, exist_ok=True)
        return real_dry_run(source, bucket, fy_prefix, *a, **k)

    monkeypatch.setattr(flow, "run_cloud_dry_run", _dry_run_with_listable_prefix)
    # The archive step is `gcloud storage objects update --storage-class=ARCHIVE`: Google-only, no local equivalent
    # (and no gcloud on the lab PC). A recorder stands in; the scenario asserts it targets the OLD FY prefix only.
    import core.fy_rollover as fr
    archived: list = []

    def _fake_archive(bucket, old_fy, *a, **k):
        archived.append((bucket, old_fy))
        return True

    monkeypatch.setattr(fr, "run_archive_transition", _fake_archive)
    monkeypatch.setattr(flow, "notify", _record_alert)
    monkeypatch.setattr(notify_mod, "notify", _record_alert)         # core.fy_overlap imports it at call time
    monkeypatch.setattr(flow, "lan_shutdown_task", _spy_shutdown)
    monkeypatch.setattr(flow, "temp_rclone_config", lambda *a, **k: contextlib.nullcontext(rc))
    monkeypatch.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda *a, **k: 0)
    labclock.install_google_date(monkeypatch)
    monkeypatch.chdir(sandbox / "buckets")
    monkeypatch.delenv("PREFECT_TEST_MODE", raising=False)

    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        from launch import _ensure_concurrency_limit

        _ensure_concurrency_limit()
        lab_obj = Lab(sandbox, flow, cfg_path, alerts, shutdowns)
        lab_obj.archived = archived
        yield lab_obj

    ls.build_robocopy_command = orig_build
    for (mod, attr), old in saved.items():
        setattr(mod, attr, old)


def test_w25_fifteen_month_overlap_cycle(lab):
    from core import fy_overlap
    from core import rollover_journal as rj
    from core.fy_rollover import configured_fy

    log: list[str] = []

    def note(s):
        log.append(s)
        print(f"[W25] {s}")

    # ================================================================== December 2026: one FY folder
    before = lab.counts([FY_A, FY_B])
    d = lab.backup(_at(2026, 12, 10))
    lab.expect_night("10 Dec", before, [FY_A], d)
    lab.converged(FY_A, "10 Dec")
    assert lab.rollover(_at(2026, 12, 10, 6)) == "NO_ROLLOVER_NEEDED"
    lab.work(FY_A, "dec20", delete="a_011.txt")
    before = lab.counts([FY_A, FY_B])
    d = lab.backup(_at(2026, 12, 20))
    lab.expect_night("20 Dec", before, [FY_A], d)
    lab.converged(FY_A, "20 Dec")
    with lab.db() as (_c, m):
        assert m.get_activation(FY_B) is None, "FY27-28 was activated in December"
    assert sorted(p.name for p in lab.source_parent.iterdir()) == [FY_A]
    assert sorted(p.name for p in lab.nas_parent.iterdir()) == [FY_A]
    assert sorted(p.name for p in lab.bucket.iterdir() if not p.name.startswith("_aam_")) == [FY_A], "something else is in the bucket in December"
    assert lab.legs(FY_B) == [], "run rows for FY27-28 exist in December"
    note("Dec 2026: one folder, no activation, bucket / NAS / source hold FY26-27 only")

    # ================================================================== 1 January 2027: activation
    assert _at(2027, 1, 1).day_of_week == pendulum.FRIDAY
    lab.work(FY_A, "dec31", amend=False)
    before = lab.counts([FY_A, FY_B])
    d = lab.backup(_at(2027, 1, 1))
    lab.expect_night("1 Jan", before, [FY_B, FY_A], d)
    with lab.db() as (cfg, m):
        act = m.get_activation(FY_B)
        assert act is not None, "no activation row for FY27-28 after the first January run"
        assert act["created_by_app"], f"the app made the folder but the row says otherwise: {act}"
        from core.health import CLOCK_OK
        assert act["clock_state"] == CLOCK_OK, act
        assert act["activated_at"].startswith("2027-01-01"), act
        assert Path(act["source_path"]) == lab.src(FY_B), act
        assert cfg.paths.source_drive.endswith(FY_A), "activation must not touch config.yaml"
        d1 = pendulum.date(2027, 1, 1)
        assert fy_overlap.active_folders(cfg, m, d1) == [FY_B, FY_A]
    assert (lab.src(FY_B) / "README_FY.txt").is_file(), "the placeholder was not written into the new folder"
    assert (lab.nas(FY_B) / ".AAM_TARGET_MOUNTED").is_file(), "no mount marker in the new NAS folder"
    assert (lab.cloud(FY_B) / "README_FY.txt").is_file(), "the placeholder is not in the cloud prefix"
    lab.converged(FY_A, "1 Jan")
    lab.converged(FY_B, "1 Jan")
    starts = [a for a in lab.alerts if getattr(a, "key", "") == f"overlap:start:{FY_B}"]
    assert len(starts) == 1 and starts[0].kind == "INFO", [(a.key, a.kind) for a in lab.alerts]
    assert starts[0].fy == FY_B
    assert lab.rollover(_at(2027, 1, 1, 6)) == "NO_ROLLOVER_NEEDED"       # re-check: no second activation, no alert
    assert len([a for a in lab.alerts if a.key == f"overlap:start:{FY_B}"]) == 1
    note("1 Jan 2027: activation row + INFO mail once, folder created with placeholder, NAS marker, both FYs "
         "byte-identical on cloud and NAS, one shutdown decision")

    # ================================================================== January: two folders every night
    # staff start using the next FY; the SAME relative path exists in both FYs with different content
    for fy in (FY_A, FY_B):
        L.write_file(lab.src(fy) / "Clients" / "shared.txt", f"shared in {fy}\n".encode() * 15)
    lab.work(FY_B, "jan02", amend=False)
    lab.work(FY_A, "jan02", delete="a_010.txt")
    before = lab.counts([FY_A, FY_B])
    d = lab.backup(_at(2027, 1, 2))
    lab.expect_night("2 Jan", before, [FY_B, FY_A], d)
    assert lab.rollover(_at(2027, 1, 2, 6)) == "NO_ROLLOVER_NEEDED"
    lab.converged(FY_A, "2 Jan")
    lab.converged(FY_B, "2 Jan")
    assert (lab.cloud(FY_A) / "Clients" / "shared.txt").read_bytes() != (lab.cloud(FY_B) / "Clients" / "shared.txt").read_bytes()

    # Sunday 3 Jan: the night, then the weekly audit of BOTH FYs in one run
    before = lab.counts([FY_A, FY_B])
    lab.work(FY_B, "jan03", amend=False)
    d = lab.backup(_at(2027, 1, 3))
    lab.expect_night("3 Jan", before, [FY_B, FY_A], d)
    assert _at(2027, 1, 3).day_of_week == pendulum.SUNDAY
    lab.audit(_at(2027, 1, 3, 10))
    with lab.db() as (_c, m):
        for fy in (FY_B, FY_A):
            for leg in ("cloud", "lan"):
                row = m.latest_audit(leg, fy=fy)
                assert row is not None, f"the audit wrote no {leg} row for {fy}"
                assert row["status"] == "VERIFIED", f"audit {leg} {fy}: {row['status']}"
        audit_a = {leg: m.latest_audit(leg, fy=FY_A) for leg in ("cloud", "lan")}
    note("Sun 3 Jan: weekly audit VERIFIED for both FYs on both legs")

    # February: the shared path is deleted in FY26-27 only; FY27-28's copy must survive (independent catalog)
    (lab.src(FY_A) / "Clients" / "shared.txt").unlink()
    lab.work(FY_A, "feb15", delete="a_009.txt")
    lab.work(FY_B, "feb15", amend=False)
    before = lab.counts([FY_A, FY_B])
    d = lab.backup(_at(2027, 2, 15))
    lab.expect_night("15 Feb", before, [FY_B, FY_A], d)
    lab.converged(FY_A, "15 Feb")
    lab.converged(FY_B, "15 Feb")
    assert (lab.cloud(FY_B) / "Clients" / "shared.txt").is_file(), "pruning FY26-27 removed FY27-28's object"
    assert (lab.nas(FY_B) / "Clients" / "shared.txt").is_file(), "pruning FY26-27 removed FY27-28's NAS file"
    assert not (lab.cloud(FY_A) / "Clients" / "shared.txt").exists()
    note("15 Feb: same relative path deleted in FY26-27 only: FY27-28 cloud + NAS copies intact")

    # 31 March: the last in-window night
    lab.work(FY_B, "mar31", amend=False)
    lab.work(FY_A, "mar31", delete="a_008.txt")
    before = lab.counts([FY_A, FY_B])
    d = lab.backup(_at(2027, 3, 31))
    lab.expect_night("31 Mar", before, [FY_B, FY_A], d)
    assert lab.rollover(_at(2027, 3, 31, 6)) == "NO_ROLLOVER_NEEDED"
    lab.converged(FY_A, "31 Mar")
    lab.converged(FY_B, "31 Mar")
    note("31 Mar: last in-window night, two folders, converged")

    # ================================================================== 1 April: switch pending, then the rollover
    lab.work(FY_B, "apr01", amend=False)
    lab.work(FY_A, "apr01", delete="a_007.txt")
    before = lab.counts([FY_A, FY_B])
    d = lab.backup(_at(2027, 4, 1))
    lab.expect_night("1 Apr 01:00 (switch pending)", before, [FY_B, FY_A], d)
    lab.converged(FY_A, "1 Apr 01:00")
    lab.converged(FY_B, "1 Apr 01:00")
    cfg = lab.config()
    assert configured_fy(cfg.paths.source_drive, cfg.paths.lan_destination) == FY_A, "config switched before the rollover"

    result = lab.rollover(_at(2027, 4, 1, 6))
    assert result == "ROLLOVER_COMPLETED", result
    with lab.db() as (cfg, m):
        assert cfg.paths.source_drive.endswith(FY_B), cfg.paths.source_drive
        assert cfg.paths.lan_destination.endswith(FY_B), cfg.paths.lan_destination
        journal = m.rollover_get(FY_A, FY_B)
        assert journal is not None and journal["steps"][rj.CONFIG_SWITCH]["state"] == rj.DONE, journal
        d2 = pendulum.date(2027, 4, 2)
        assert fy_overlap.active_folders(cfg, m, d2) == [FY_B], "the old FY is still in the active set after the switch"
        assert not m.open_approvals(), m.open_approvals()
        # the activation row of FY27-28 is durable; nothing else was activated
        assert m.get_activation(FY_B) is not None and m.get_activation(FY_C) is None
    lab.converged(FY_A, "after rollover (final backup)")
    lab.converged(FY_B, "after rollover")
    assert [fy for _b, fy in lab.archived] == [FY_A], f"archive transition targets: {lab.archived}"
    frozen_cloud, frozen_nas = _tree_map(lab.cloud(FY_A)), _tree_map(lab.nas(FY_A))
    audit_a_after_switch = None
    note("1 Apr: both folders backed up while the switch was pending; rollover completed, config now FY27-28, "
         "FY26-27 out of the active set")

    # ================================================================== April-December 2027: FY26-27 is closed
    L.write_file(lab.src(FY_A) / "Clients" / "late_edit.txt", b"edited after the switch\n" * 10)   # must NOT be backed up
    lab.work(FY_B, "apr02", amend=False)
    before = lab.counts([FY_A, FY_B])
    d = lab.backup(_at(2027, 4, 2))
    lab.expect_night("2 Apr (after the switch)", before, [FY_B], d)
    lab.converged(FY_B, "2 Apr")
    assert _tree_map(lab.cloud(FY_A)) == frozen_cloud, "the closed FY26-27 changed in the cloud"
    assert _tree_map(lab.nas(FY_A)) == frozen_nas, "the closed FY26-27 changed on the NAS"
    assert not (lab.cloud(FY_A) / "Clients" / "late_edit.txt").exists()
    assert not (lab.nas(FY_A) / "Clients" / "late_edit.txt").exists()

    # Sunday audit after the switch: only the configured FY is audited, the closed FY's last audit stays as it was
    lab.audit(_at(2027, 4, 4, 10))
    with lab.db() as (_c, m):
        audit_a_after_switch = {leg: m.latest_audit(leg, fy=FY_A) for leg in ("cloud", "lan")}
        for leg in ("cloud", "lan"):
            assert m.latest_audit(leg, fy=FY_B)["status"] == "VERIFIED"
    assert audit_a_after_switch == audit_a, "the audit touched the closed FY26-27"
    note("Apr 2027: FY26-27 frozen in cloud + NAS (a late edit was NOT backed up), audit only on FY27-28")

    for (m_, dd) in ((6, 15), (10, 12), (12, 15)):
        lab.work(FY_B, f"m{m_}", amend=False)
        before = lab.counts([FY_A, FY_B, FY_C])
        d = lab.backup(_at(2027, m_, dd))
        lab.expect_night(f"{dd}/{m_}/2027", before, [FY_B], d)
        lab.converged(FY_B, f"{dd}/{m_}/2027")
        assert lab.legs(FY_C) == [] and not lab.src(FY_C).exists() and not lab.cloud(FY_C).exists()
    with lab.db() as (_c, m):
        assert m.get_activation(FY_C) is None, "FY28-29 was activated before January"
    note("Jun / Oct / Dec 2027: one folder, no activation, FY26-27 untouched")

    # ================================================================== January 2028: the next cycle
    lab.work(FY_B, "dec31", amend=False)
    before = lab.counts([FY_A, FY_B, FY_C])
    d = lab.backup(_at(2028, 1, 1))
    lab.expect_night("1 Jan 2028", before, [FY_C, FY_B], d)
    with lab.db() as (cfg, m):
        act_c = m.get_activation(FY_C)
        assert act_c is not None and act_c["created_by_app"], act_c
        assert m.get_activation(FY_B) is not None, "the first cycle's activation row was lost"
        assert fy_overlap.active_folders(cfg, m, pendulum.date(2028, 1, 2)) == [FY_C, FY_B]
    assert (lab.src(FY_C) / "README_FY.txt").is_file()
    assert (lab.nas(FY_C) / ".AAM_TARGET_MOUNTED").is_file()
    assert (lab.cloud(FY_C) / "README_FY.txt").is_file()
    lab.converged(FY_B, "1 Jan 2028")
    lab.converged(FY_C, "1 Jan 2028")
    assert len([a for a in lab.alerts if a.key == f"overlap:start:{FY_C}"]) == 1

    lab.work(FY_C, "jan02", amend=False)
    lab.work(FY_B, "jan02")
    before = lab.counts([FY_A, FY_B, FY_C])
    d = lab.backup(_at(2028, 1, 2))
    lab.expect_night("2 Jan 2028", before, [FY_C, FY_B], d)
    assert _at(2028, 1, 2).day_of_week == pendulum.SUNDAY
    lab.audit(_at(2028, 1, 2, 10))
    with lab.db() as (_c, m):
        for fy in (FY_C, FY_B):
            for leg in ("cloud", "lan"):
                row = m.latest_audit(leg, fy=fy)
                assert row is not None and row["status"] == "VERIFIED", f"audit {leg} {fy}: {row}"
        assert {leg: m.latest_audit(leg, fy=FY_A) for leg in ("cloud", "lan")} == audit_a, "the audit touched FY26-27"
    lab.converged(FY_B, "2 Jan 2028")
    lab.converged(FY_C, "2 Jan 2028")
    assert _tree_map(lab.cloud(FY_A)) == frozen_cloud and _tree_map(lab.nas(FY_A)) == frozen_nas
    note("1-2 Jan 2028: FY28-29 activated next to FY27-28, both audited VERIFIED, FY26-27 still frozen")

    # ================================================================== the whole 15 months: alerts and rows
    kinds = [(a.kind, a.key, a.fy) for a in lab.alerts]
    bad = [k for k in kinds if k[0] in _BAD_KINDS]
    assert not bad, f"false alerts during a healthy run: {bad}"
    infos = sorted(a.key for a in lab.alerts if a.kind == "INFO")
    assert f"overlap:start:{FY_B}" in infos and f"overlap:start:{FY_C}" in infos, infos
    with lab.db() as (_c, m):
        for fy in (FY_A, FY_B, FY_C):
            for leg in ("cloud", "lan"):
                inc = m.last_open_incident(leg, fy=fy)
                assert not inc, f"an incident is still open for {fy} {leg}: {inc}"
        assert not m.open_approvals()
    # every per-FY row was recorded under its own FY: A: Dec(2)+Jan..Apr1(5, 4 + the rollover's final backup counted)
    per_fy = lab.counts([FY_A, FY_B, FY_C])
    assert per_fy[FY_C] == {"cloud": 2, "lan": 2}, per_fy
    assert per_fy[FY_A]["cloud"] == per_fy[FY_A]["lan"] and per_fy[FY_A]["cloud"] >= 9, per_fy
    assert per_fy[FY_B]["cloud"] == per_fy[FY_B]["lan"] and per_fy[FY_B]["cloud"] >= 12, per_fy
    # the NAS shutdown decision: the loop decides once per LAN flow, so the number of markers equals the LAN flows
    flows_lan = len(lab.shutdowns)
    assert lab.shutdown_rows([FY_A, FY_B, FY_C]) == flows_lan, (lab.shutdown_rows([FY_A, FY_B, FY_C]), flows_lan)

    L.record("W25.fy_overlap_15_months",
             "FY overlap over 15 simulated months (Dec 2026 - Jan 2028) through the production flows with real "
             "robocopy / rclone: activation, two-folder nights, per-FY audit, April switch, FY closed, next cycle",
             "\n".join(log) + f"\nrows per FY: {per_fy}\nINFO mails: {infos}\nNAS shutdown decisions: {flows_lan}",
             "OK - both folders on cloud + NAS, per-FY rows, one activation row per cycle, one NAS decision per "
             "LAN flow, old FY closed after the switch, no false alerts")
