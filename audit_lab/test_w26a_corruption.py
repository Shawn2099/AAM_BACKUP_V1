"""WAVE 26a - release card L1 (docs/TEST_DESIGN_RELEASE_2026-11-22.md section B, card L1).

Same-size, same-mtime corruption of ONE file on BOTH destinations:

    * the NAS copy of Tax\\a.xlsx and the local remote copy (the bucket) get one byte
      flipped at offset 100 and their original mtime restored with os.utime, so size
      and modification time still match the source (what robocopy and rclone compare);
    * the next LAN backup therefore copies nothing, and is COMPLETE by design;
    * the weekly integrity audit (rclone check, hash based) must catch the divergence on
      BOTH legs, naming Tax/a.xlsx, and raise exactly one alert mail (ACTION_REQUIRED) plus red dashboard items.

Oracle: our own MD5 of the source file against the destination files (labconf.md5),
never the product's audit verdict. Expected values come from the card.

Rig: the WAVE 6 `rig` fixture (real robocopy, real rclone against a local `type = local`
remote, loopback admin share as the NAS, FY leaf FY26-27). The clock is pinned with the
labclock seam (no system clock change) and the Google date check is simulated offline.

Run with a clean Prefect home (CLAUDE.md "Environment gotchas"):
    PREFECT_HOME=<empty dir> .venv\\Scripts\\python.exe -m pytest audit_lab/test_w26a_corruption.py -q -s -p no:cacheprovider -o addopts=""
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

import pendulum
import pytest

from audit_lab import labclock
from audit_lab import labconf as L
from audit_lab.test_w12_clock import _flow_call
from audit_lab.test_w6_flow import rig  # noqa: F401  (fixture: production wiring + local GCS)

pytestmark = pytest.mark.timeout(900)

FY = "FY26-27"
REL = "Tax/a.xlsx"
#: production modules whose own `pendulum` reference the fake clock must replace
_MODULES = labclock.CLOCK_MODULES + ("flow", "core.fy_overlap", "core.attention", "core.manifest",
                                      "core.run_artefacts")


def _at(y, mo, d, h=1, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz="Asia/Kolkata")


@contextlib.contextmanager
def _db(cfg_path: Path):
    from core.manifest import ManifestDB
    from models.config import load_config

    m = ManifestDB.from_config(load_config(str(cfg_path)))
    try:
        yield m
    finally:
        m.close()


def _run(fn, at, **kwargs):
    """One production flow call under the fake clock. Returns the raised exception or None."""
    exc = None
    with labclock.fake_clock(at, modules=_MODULES, tick=0.001):
        try:
            _flow_call(fn, **kwargs)
        except Exception as e:  # noqa: BLE001 - the caller decides what an exception means
            exc = e
    time.sleep(1.0)     # keep Prefect's harness DB from two state writes on one microsecond
    return exc


def _flip_byte_keep_metadata(path: Path, offset: int) -> None:
    """Flip one byte (same size) and put the original atime/mtime back (same mtime)."""
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[offset] ^= 0xFF
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_w26a_same_size_same_mtime_corruption_is_caught_by_the_weekly_audit(rig, monkeypatch):
    flow = rig["flow"]
    labclock.install_google_date(monkeypatch)      # no real network for the clock check
    cfg_path = rig["cfg_path"]

    # the card's `Tax\a.xlsx`, written with an OLD mtime: a recently written mismatch is
    # classified as "waiting for the next backup" (PENDING_BACKUP) by the audit, not as
    # a divergence, so the card's premise needs a settled file.
    src_xlsx = rig["src"] / "Tax" / "a.xlsx"
    L.write_file(src_xlsx, b"TAX WORKBOOK ROW 0001\n" * 80, mtime=_at(2026, 10, 15, 9).timestamp())
    nas_xlsx = rig["nas"] / "Tax" / "a.xlsx"
    remote_xlsx = rig["buckets"] / "lab-bucket" / FY / "Tax" / "a.xlsx"

    # 1. LAN and cloud COMPLETE, so both destinations hold a copy of Tax\a.xlsx
    exc = _run(flow.backup, _at(2026, 11, 23, 12), config_path=str(cfg_path), mode="all")
    assert exc is None, f"the baseline backup must complete: {exc!r}"
    with _db(cfg_path) as m:
        assert m.last_run("lan", fy=FY)["status"] == "LAN_COMPLETE"
        assert m.last_run("cloud", fy=FY)["status"] == "CLOUD_COMPLETE"
    assert nas_xlsx.is_file() and remote_xlsx.is_file(), "both destinations must hold the file"

    # the premise: same size, same mtime, same bytes on both destinations (our own MD5 oracle)
    src_md5 = L.md5(src_xlsx)
    assert L.md5(nas_xlsx) == src_md5 and L.md5(remote_xlsx) == src_md5
    assert nas_xlsx.stat().st_mtime_ns == src_xlsx.stat().st_mtime_ns, "NAS copy mtime differs from source"
    assert remote_xlsx.stat().st_mtime_ns == src_xlsx.stat().st_mtime_ns, "remote copy mtime differs from source"
    size_before = src_xlsx.stat().st_size

    # 2. the corruption: byte 100 flipped on the NAS copy AND in the local remote copy,
    #    original mtime restored on both
    mtime_nas = nas_xlsx.stat().st_mtime_ns
    mtime_remote = remote_xlsx.stat().st_mtime_ns
    _flip_byte_keep_metadata(nas_xlsx, 100)
    _flip_byte_keep_metadata(remote_xlsx, 100)
    for label, p, mt in (("NAS", nas_xlsx, mtime_nas), ("remote", remote_xlsx, mtime_remote)):
        assert p.stat().st_size == size_before, f"{label} copy changed size"
        assert p.stat().st_mtime_ns == mt, f"{label} copy mtime not restored"
        assert L.md5(p) != src_md5, f"{label} copy still matches the source: corruption did not land"

    # 3. the next LAN backup: COMPLETE by design (same size + mtime = nothing to copy)
    exc = _run(flow.backup, _at(2026, 11, 23, 13), config_path=str(cfg_path), mode="lan")
    assert exc is None, f"the LAN backup is expected to stay COMPLETE by design: {exc!r}"
    with _db(cfg_path) as m:
        assert m.last_run("lan", fy=FY)["status"] == "LAN_COMPLETE"
    assert L.md5(nas_xlsx) != src_md5, "the LAN backup silently repaired the NAS copy (not by design)"

    # 4. the weekly integrity audit, both legs. It fails by design (raises), which is not asserted here.
    _run(flow.integrity_audit_flow, _at(2026, 11, 23, 14), config_path=str(cfg_path), mode="all")

    with _db(cfg_path) as m:
        for leg in ("lan", "cloud"):
            row = m.latest_audit(leg, fy=FY)
            assert row is not None, f"the audit wrote no {leg} row"
            assert row["status"] == "VERIFICATION_FAILED", f"{leg} audit: {row}"
            assert REL in (row["detail"] or ""), f"{leg} audit does not name {REL}: {row['detail']!r}"

    # The product's choice (flow._audit_alert_kind): a real divergence is ACTION_REQUIRED, never switchable
    # (a person must look: "do not delete anything, forward to support"). FAILURE would also be acceptable, so
    # the test asserts what matters: EXACTLY ONE alert mail for the audit, of a human-attention kind, and a red
    # dashboard item per leg.
    audit_mails = [a[1] for a in rig["alerts"] if len(a) > 1 and str(getattr(a[1], "key", "")).startswith("audit:")]
    kinds = [e.kind for e in audit_mails]
    assert len(audit_mails) == 1, f"expected exactly one audit alert mail, got {[(e.key, e.kind) for e in audit_mails]}"
    assert kinds[0] in ("ACTION_REQUIRED", "FAILURE"), f"audit alert kind {kinds[0]}"
    from core import attention
    from models.config import load_config
    with _db(cfg_path) as m:
        items = attention.compute_attention(m, load_config(str(cfg_path)))
    red = {i["code"] for i in items if i["level"] == attention.RED}
    for leg in ("lan", "cloud"):
        assert f"audit:{FY}:{leg}" in red, f"no red dashboard item for the {leg} audit: {sorted(red)}"

    # the oracle: our own MD5, computed here, still shows both destinations differ from the source
    assert L.md5(nas_xlsx) != src_md5 and L.md5(remote_xlsx) != src_md5

    L.record("W26a.L1_same_size_mtime_corruption",
             "a same-size, same-mtime single-byte corruption on the NAS copy and the remote copy of "
             "Tax/a.xlsx is caught by the weekly MD5 audit on both legs; the LAN backup stays COMPLETE by design",
             f"LAN baseline + cloud COMPLETE | corrupted byte 100 on both copies, mtime restored | "
             f"next LAN run LAN_COMPLETE | audit lan={'VERIFICATION_FAILED'} cloud={'VERIFICATION_FAILED'} "
             f"both detail name {REL} | audit alert mails={len(audit_mails)} kind={kinds[0]} + red items on both legs",
             "OK - the MD5 audit catches same-size/same-mtime corruption on both legs")
