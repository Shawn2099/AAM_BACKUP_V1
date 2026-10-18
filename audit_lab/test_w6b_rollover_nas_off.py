"""WAVE 6b - plan A10 (R1) with the REAL robocopy: the NAS is unreachable when the rollover wants
to create its new FY folder. The config must still switch (the cloud protects the new FY from
day 1), the step stays pending in the journal, and the NEXT LAN night - NAS awake again -
creates the folder + mount marker itself and finishes COMPLETE without any approval.
"""
from __future__ import annotations

from pathlib import Path

from audit_lab import labconf as L
from audit_lab.test_w6_flow import _db_rows, rig  # noqa: F401 - the production-wired rig fixture

import pytest

pytestmark = pytest.mark.timeout(1500)


def test_w6b_nas_off_during_rollover_then_next_lan_night_completes(rig, monkeypatch):  # noqa: F811
    flow = rig["flow"]
    import core.fy_rollover as fr
    from core import rollover_journal as rj
    from core.manifest import ManifestDB
    from models.config import load_config

    monkeypatch.setattr(flow, "get_fy_prefix", lambda *a, **k: "FY27-28")
    real_create = fr.create_nas_folder
    calls = {"n": 0}

    def nas_off_the_first_time(lan_root, new_fy):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("NAS share is not reachable (simulated: the NAS is switched off)")
        return real_create(lan_root, new_fy)

    monkeypatch.setattr(fr, "create_nas_folder", nas_off_the_first_time)

    result = flow.rollover_check_flow(config_path=str(rig["cfg_path"]))
    cfg_path = rig["sandbox"] / "config.yaml"
    after = load_config(str(cfg_path))
    new_nas = Path(rig["nas"].parent / "FY27-28")

    db = ManifestDB(after.paths.database_path)
    try:
        journal = db.rollover_get("FY26-26", "FY27-28")
        states = {k: v["state"] for k, v in journal["steps"].items()}
    finally:
        db.close()
    rows_after_rollover = [r["status"] for r in _db_rows(cfg_path)[0]]
    L.record("W6b.rollover_nas_off",
             "NAS unreachable at rollover: config still switches, NAS folder step stays open",
             f"result={result!r} | lan_destination={after.paths.lan_destination!r} | "
             f"steps={states} | new NAS folder exists={new_nas.exists()} | rows={rows_after_rollover}",
             "OK - switched, nas_folder failed and pending for the LAN run"
             if result == "ROLLOVER_COMPLETED" and states[rj.NAS_FOLDER] == rj.FAILED
             and states[rj.CONFIG_SWITCH] == rj.DONE and not new_nas.exists()
             else f"ISSUE - {result!r} {states}")
    assert result == "ROLLOVER_COMPLETED"
    assert after.paths.lan_destination.endswith("FY27-28")
    assert states[rj.CONFIG_SWITCH] == rj.DONE and states[rj.NAS_FOLDER] == rj.FAILED
    assert not new_nas.exists()

    # the next LAN night: the NAS is awake again; the real pipeline + real robocopy
    flow.backup(config_path=str(cfg_path), mode="lan")

    rows, _lan, _cloud = _db_rows(cfg_path)
    lan_statuses = [r["status"] for r in rows if r["mode"] == "lan"]
    db = ManifestDB(after.paths.database_path)
    try:
        final = db.rollover_get("FY26-26", "FY27-28")["steps"]
        approvals = db.open_approvals()
    finally:
        db.close()
    marker = new_nas / ".AAM_TARGET_MOUNTED"
    L.record("W6b.next_lan_night",
             "the next LAN run creates the pending NAS folder + marker and completes without approval",
             f"lan rows={lan_statuses} | marker={marker.is_file()} | "
             f"nas_folder={final[rj.NAS_FOLDER]['state']} | nas_shutdown={final[rj.NAS_SHUTDOWN]['state']} "
             f"| open approvals={len(approvals)}",
             "OK - no G5 block for our own folder"
             if lan_statuses and lan_statuses[0] == "LAN_COMPLETE" and marker.is_file() and not approvals
             else f"ISSUE - {lan_statuses} marker={marker.is_file()} approvals={len(approvals)}")
    assert lan_statuses[0] == "LAN_COMPLETE", lan_statuses
    assert marker.is_file()
    assert final[rj.NAS_FOLDER]["state"] == rj.DONE
    assert final[rj.NAS_SHUTDOWN]["state"] == rj.SKIPPED
    assert not approvals, "our own new folder must never need an approval"
