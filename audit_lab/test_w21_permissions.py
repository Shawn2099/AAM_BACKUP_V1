"""WAVE 21 — preflight permission checks against the REAL tools (M2 2.4, plan WP4).

whoami.exe, SMB over the loopback admin share (our fake NAS) and the real
LAN pipeline. The lab account is a normal, non-elevated user, so it does
NOT hold SeBackupPrivilege (experiment C1) - that is asserted, not hidden.
"""
from __future__ import annotations

import time

import pytest

from audit_lab import labconf as L
from audit_lab.labconf import build_config, write_file

pytestmark = pytest.mark.timeout(300)


def test_w21_real_whoami_is_parsed_and_the_missing_privilege_is_named(sandbox):
    from core import permissions as perm
    from core.run_artefacts import RunArtefacts
    from core.tool_runner import run_tool

    art = RunArtefacts(sandbox / "runtime" / "rl", "w21")
    raw = run_tool("whoami", [perm.whoami_path(), "/priv", "/fo", "csv", "/nh"],
                   timeout_s=60, artefacts=art)
    held = perm.parse_privileges(raw.stdout_text())
    assert "SeChangeNotifyPrivilege" in held, held      # every token holds it
    try:
        perm.check_backup_privilege(art, timeout_s=60)
        verdict = "held"
    except perm.PermissionCheckError as e:
        verdict = str(e)
    L.record("W21.privilege", "whoami /priv sees SeBackupPrivilege exactly when it is held",
             f"privileges={sorted(held)} | check={verdict[:120]}",
             "OK - parsed by name; message names the right and the fix")
    if "SeBackupPrivilege" in held:
        assert verdict == "held"
    else:
        assert "SeBackupPrivilege" in verdict and "secpol.msc" in verdict


def test_w21_real_smb_write_probe_and_free_space(sandbox):
    from core import permissions as perm

    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    unc = L._to_unc(dest)
    perm.probe_destination_write(unc, "w21-run")
    assert sorted(p.name for p in dest.iterdir()) == [".AAM_TARGET_MOUNTED"]
    perm.check_destination_free_space(unc, bytes_needed=1, min_free_gb=0)
    with pytest.raises(perm.PermissionCheckError, match="Not enough free space"):
        perm.check_destination_free_space(unc, bytes_needed=1, min_free_gb=100_000)


def test_w21_lan_pipeline_fails_on_free_space_and_copies_nothing(sandbox, monkeypatch):
    """Real robocopy /L /BYTES feeds the free-space check; a NAS that is too
    small FAILS the run before robocopy /MIR starts."""
    import flow
    from core import permissions as perm
    from core.manifest import ManifestDB
    from core.time_utils import now_iso

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    src = sandbox / "source" / "FY26-26"
    for i in range(5):
        write_file(src / "Clients" / f"ledger_{i}.txt", f"L{i}\n".encode() * 200)
    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime",
                       lan_enabled=True, cloud_enabled=False,
                       lan={"min_free_dest_gb": 100_000})

    with pytest.raises(perm.PermissionCheckError, match="Not enough free space") as ei:
        flow._run_lan_pipeline(cfg, "w21-space", now_iso(), time.monotonic())
    db = ManifestDB.from_config(cfg)
    try:
        row = db.last_run("lan")
    finally:
        db.close()
    copied = sorted(p.name for p in dest.rglob("*") if p.is_file())
    L.record("W21.free_space", "NAS too small -> LAN_FAILED before any copy",
             f"row={row['status']} | dest files={copied} | {str(ei.value)[:140]}",
             "OK - fails closed with the numbers" if copied == [".AAM_TARGET_MOUNTED"]
             else "DATA WRITTEN")
    assert row["status"] == "LAN_FAILED"
    assert copied == [".AAM_TARGET_MOUNTED"], "nothing may be copied"
