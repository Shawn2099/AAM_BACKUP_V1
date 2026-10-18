"""Plan I3 (G-7): one sweep covers cloud, LAN and rollover rows, each with its own incident key and
wording; a *_MISSED row is never swept (it is a finished row)."""

import json
from unittest.mock import MagicMock, patch

from core import backup_repository as repo
from core.manifest import ManifestDB
from core.time_utils import now_iso
from models.config import MaintenanceConfig
from tests.test_m5_abandoned_runs import _dead_token


def test_sweep_covers_every_run_mode_with_its_own_key_and_wording(tmp_path):
    db_path = str(tmp_path / "m.db")
    db = ManifestDB(db_path)
    cfg = MagicMock()
    cfg.paths.database_path = db_path
    cfg.maintenance = MaintenanceConfig()
    try:
        for mode in ("cloud", "lan", "rollover"):
            db.insert_run({"run_id": f"r-{mode}", "mode": mode, "started_at": now_iso(),
                           "status": f"{mode.upper()}_RUNNING",
                           "extended_metrics": json.dumps({"owner": _dead_token()})})
        db.insert_run({"run_id": "r-missed", "mode": "lan", "started_at": now_iso(),
                       "ended_at": now_iso(), "status": "LAN_MISSED"})
        with patch.object(repo, "notify") as notify:
            rows = repo.sweep_abandoned_runs(cfg, db)
    finally:
        db.close()
    assert sorted(r["status"] for r in rows) == ["CLOUD_ABANDONED", "LAN_ABANDONED",
                                                 "ROLLOVER_ABANDONED"]
    events = {c.args[1].key: c.args[1] for c in notify.call_args_list}
    assert set(events) == {"backup:cloud:CLOUD_ABANDONED", "backup:lan:LAN_ABANDONED",
                           "rollover:abandoned"}
    assert events["rollover:abandoned"].fault_code == "AAM-ROL-04"
    assert "rollover" not in events["backup:cloud:CLOUD_ABANDONED"].title.lower()
    assert "copies what is missing" in events["backup:lan:LAN_ABANDONED"].message
