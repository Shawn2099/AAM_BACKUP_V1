"""OV-4.1 / OV-4.2 - per-leg alerts carry their FY (key, subject, incident) and incidents are per (mode, FY)
(plan 5.4 Alerts; R-2: an FY27-28 success never closes or recovers an FY26-27 incident; N-9: sibling alarm
incidents are per key)."""

import email as em
from unittest.mock import MagicMock, patch

import pytest

import flow
import watchdog as wd
from core import alert_class as ac
from core import email_compose
from core import notify as nt
from core.backup_repository import sweep_abandoned_runs
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support.smtp_server import smtp_server

A, B = "FY26-27", "FY27-28"


def _cfg(tmp_path, port=1, fy=A, cooldown=12) -> AppConfig:
    return AppConfig(
        paths={"source_drive": str(tmp_path / fy), "lan_destination": "\\\\nas\\backup\\" + fy,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080"},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@firm.example",
                       "recipients": ["o@firm.example"], "smtp_security": "none",
                       "smtp_auth": False, "retry_attempts": 1, "retry_base_seconds": 0.01},
        alerts={"repeat_cooldown_hours": cooldown},
    )


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _fail(fy, leg="cloud", code="AAM-CLD-01"):
    status = f"{leg.upper()}_FAILED"
    return nt.AlertEvent(key=f"backup:{fy}:{leg}:{status}", kind="FAILURE", mode=leg, title="t",
                         message="m", status=status, fault_code=code, run_id="r", fy=fy)


def _resolved(fy, leg="cloud", code="AAM-CLD-01"):
    return nt.AlertEvent(key=f"recovered:{fy}:{leg}", kind="RECOVERED", mode=leg, title="t",
                         message="m", status="RECOVERED", resolved_code=code, fy=fy)


def _run(db, run_id, mode, fy, status, started):
    db.insert_run({"run_id": run_id, "mode": mode, "fy": fy, "started_at": started, "status": status})


def _msgs(srv):
    return [em.message_from_string(m["data"]) for m in srv.messages]


# ── OV-4.1 keys and fy on every per-leg alert ────────────────────────────────

def _sent(mock):
    return [c.args[1] for c in mock.call_args_list]


def test_run_alert_key_and_fy(tmp_path):
    cfg = _cfg(tmp_path, fy=B)
    with patch("flow.notify") as n:
        flow._send_run_alert(cfg, "cloud", "CLOUD_FAILED", "boom", 1, "2027-04-02T02:00:00+05:30",
                             run_id="r1", fault_code="AAM-CLD-01")
    [ev] = _sent(n)
    assert ev.key == f"backup:{B}:cloud:CLOUD_FAILED" and ev.fy == B and ev.mode == "cloud"


def test_recovered_key_and_fy(tmp_path):
    cfg = _cfg(tmp_path, fy=B)
    d = ManifestDB.from_config(cfg)
    _run(d, "old", "lan", B, "LAN_FAILED", "2027-04-02T02:00:00+05:30")
    d.close()
    with patch("flow.notify") as n:
        flow._send_recovery_if_needed(cfg, "lan", "new")
    [ev] = _sent(n)
    assert ev.key == f"recovered:{B}:lan" and ev.fy == B and ev.kind == "RECOVERED"


def test_missed_key_and_fy(tmp_path):
    cfg = _cfg(tmp_path, fy=B)
    budget = MagicMock()
    budget.remaining.return_value = 0
    budget.describe.return_value = "4 h"
    with patch("flow.notify") as n, patch("flow._record_run"):
        with pytest.raises(flow.PartialRun):
            flow._missed_if_no_budget(cfg, "lan", "r", "2027-04-02T02:00:00+05:30", budget, 600, 3600, None)
    [ev] = _sent(n)
    assert ev.key == f"missed:{B}:lan" and ev.fy == B


def test_blocked_key_and_fy_leg_vs_rollover(tmp_path):
    cfg = _cfg(tmp_path, fy=A)
    record = {"results": [], "fy": B}
    with patch("flow.notify") as n:
        flow._open_block_request(cfg, "lan", "r", record, "2027-04-02T02:00:00+05:30")
        flow._open_block_request(cfg, "rollover", "r", record, "2027-04-02T02:00:00+05:30")
    leg_ev, roll_ev = _sent(n)
    assert leg_ev.key == f"blocked:{B}:lan" and leg_ev.fy == B       # the FY the approval belongs to
    assert roll_ev.key == "blocked:rollover" and roll_ev.fy is None   # the rollover is not per leg


def test_abandoned_sweep_uses_the_fy_of_the_swept_row(tmp_path):
    cfg = _cfg(tmp_path, fy=A)                    # configured FY differs from the swept row's
    d = MagicMock()
    d.abandon_dead_runs.return_value = [{
        "run_id": "r9", "mode": "cloud", "fy": B, "status": "CLOUD_ABANDONED",
        "started_at": "2027-04-02T02:00:00+05:30", "error_message": "gone"}]
    with patch("core.backup_repository.notify") as n:
        sweep_abandoned_runs(cfg, d)
    [ev] = _sent(n)
    assert ev.key == f"backup:{B}:cloud:CLOUD_ABANDONED" and ev.fy == B


def test_stale_code_parts_new_and_old_codes(tmp_path):
    cfg = _cfg(tmp_path, fy=A)
    assert wd._stale_code_parts("stale:FY27-28:lan", cfg) == ("FY27-28", "lan")
    assert wd._stale_code_parts("stale:lan", cfg) == (A, "lan")      # old code: the configured FY


def test_stale_alert_key_and_fy(tmp_path):
    cfg = _cfg(tmp_path, fy=A)
    ManifestDB.from_config(cfg).close()
    item = {"code": "stale:lan", "message": "overdue", "due_at": None}
    sent = []
    with patch("core.attention.compute_attention", return_value=[item]), \
            patch("core.notify.notify", side_effect=lambda c, e, db=None: sent.append(e)):
        assert wd._check_stale_legs(cfg) == ["lan"]
    [ev] = sent
    assert ev.key == f"stale:{A}:lan" and ev.fy == A and ev.mode == "lan"


def test_subject_carries_the_fy_label_only_when_the_event_has_an_fy(tmp_path):
    cfg = _cfg(tmp_path)
    with_fy, _, _ = email_compose.compose(cfg, _fail(B))
    plain_event = nt.AlertEvent(key="backup:cloud:CLOUD_FAILED", kind="FAILURE", mode="cloud", title="t",
                                message="m", status="CLOUD_FAILED", fault_code="AAM-CLD-01")
    without, _, _ = email_compose.compose(cfg, plain_event)
    assert with_fy == without.replace("Backup]", f"Backup] [{B}]", 1)
    assert f"[{B}]" in with_fy and "[FY" not in without
    rec, _, _ = email_compose.compose(cfg, _resolved(B))
    assert f"Backup] [{B}] RESOLVED:" in rec
    info, _, _ = email_compose.compose(cfg, nt.AlertEvent(key="x", kind="INFO", mode="test", title="hi",
                                                          message="m", fy=B))
    assert info.endswith(f"Backup] [{B}] INFO: hi")


def test_fallback_subject_carries_the_fy_label(tmp_path):
    cfg = _cfg(tmp_path)
    sent = {}

    def fake_send(notifications, subject, body, **kw):
        sent["subject"] = subject
        return nt.DeliveryResult(nt.SENT, None, 1, "<m@x>")
    with patch("core.notify.send_email", side_effect=fake_send):
        nt._fallback_alert(cfg, _fail(B), RuntimeError("x"))
    assert f"Backup] [{B}] FAILURE:" in sent["subject"]
    nt._FALLBACK_SENT.clear()


@pytest.mark.parametrize("key,kind,expected", [
    (f"backup:{B}:cloud:CLOUD_FAILED", "FAILURE", ac.FAILURE),
    (f"missed:{B}:lan", "WARNING", ac.FAILURE),
    (f"blocked:{B}:lan", "ACTION_REQUIRED", ac.DECISION),
    (f"stale:{B}:lan", "ACTION_REQUIRED", ac.OVERDUE),
    (f"recovered:{B}:lan", "RECOVERED", ac.RESOLVED),
])
def test_alert_class_of_the_new_keys(key, kind, expected):
    assert ac.classify(kind, key) == expected


def test_told_about_leg_since_ignores_the_new_stale_key(db):
    db.record_notification({"event_key": f"stale:{B}:lan", "kind": "ACTION_REQUIRED", "mode": "lan",
                            "subject": "s", "status": "SENT", "created_at": "2027-04-03T03:00:00+05:30",
                            "fy": B})
    assert db.told_about_leg_since("lan", "2027-04-03T00:00:00+05:30", fy=B) is False


# ── OV-4.2 incidents per (mode, fy) ──────────────────────────────────────────

def test_fy27_success_does_not_close_or_recover_an_fy26_incident(tmp_path):
    """R-2."""
    with smtp_server() as srv:
        cfg_a = _cfg(tmp_path, srv.server_address[1], fy=A)
        cfg_b = _cfg(tmp_path, srv.server_address[1], fy=B)
        d = ManifestDB.from_config(cfg_a)
        _run(d, "a1", "cloud", A, "CLOUD_FAILED", "2027-04-01T02:00:00+05:30")
        failed = _fail(A)
        assert nt.notify(cfg_a, failed, db=d).status == nt.SENT
        prior = d.last_notification(failed.key, status=nt.SENT)
        # FY27-28's first run is a success (it has no earlier run of its own): no RECOVERED about FY26-27
        _run(d, "b1", "cloud", B, "CLOUD_COMPLETE", "2027-04-02T02:00:00+05:30")
        d.close()
        with patch("flow.notify") as n:
            flow._send_recovery_if_needed(cfg_b, "cloud", "b1")
        assert n.call_count == 0
        d = ManifestDB.from_config(cfg_a)
        assert nt._incident_closed(failed, d, cfg_a, prior) is False
        # ... and the same happens with the RECOVERED mail of FY27-28 sent: still open for FY26-27
        _run(d, "b0", "cloud", B, "CLOUD_FAILED", "2027-04-01T01:00:00+05:30")
        assert nt.notify(cfg_b, _fail(B), db=d).status == nt.SENT
        assert nt.notify(cfg_b, _resolved(B), db=d).status == nt.SENT
        same, root = nt._incident(failed, d, cfg_a)
        assert same is not None and root == prior["message_id"]          # FY26-27 incident still unchanged
        # a later FY26-27 success closes it
        _run(d, "a2", "cloud", A, "CLOUD_COMPLETE", "2027-04-04T02:00:00+05:30")
        assert nt._incident_closed(failed, d, cfg_a, prior) is True
        d.close()


def test_cooldown_is_per_fy_two_fys_failing_the_same_night_send_two_mails(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        assert nt.notify(cfg, _fail(A), db=db).status == nt.SENT
        assert nt.notify(cfg, _fail(B), db=db).status == nt.SENT        # other FY: not suppressed
        assert nt.notify(cfg, _fail(A), db=db).status == nt.SUPPRESSED  # same FY: cooldown holds
        assert nt.notify(cfg, _fail(B), db=db).status == nt.SUPPRESSED
    assert len(srv.messages) == 2
    subjects = [m["Subject"] for m in _msgs(srv)]
    assert f"[{A}]" in subjects[0] and f"[{B}]" in subjects[1]


def test_recovered_threads_into_its_own_fy_incident(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], cooldown=0)
        nt.notify(cfg, _fail(A), db=db)
        nt.notify(cfg, _fail(B), db=db)
        nt.notify(cfg, _resolved(B), db=db)
        nt.notify(cfg, _resolved(A), db=db)
    fail_a, fail_b, rec_b, rec_a = _msgs(srv)
    assert rec_b["In-Reply-To"] == fail_b["Message-ID"]
    assert rec_a["In-Reply-To"] == fail_a["Message-ID"]              # FY B's RECOVERED did not take A's slot


def test_a_recovered_of_one_fy_does_not_restart_the_other_fys_incident(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], cooldown=0)
        nt.notify(cfg, _fail(A), db=db)
        nt.notify(cfg, _fail(B), db=db)
        nt.notify(cfg, _resolved(B), db=db)
        nt.notify(cfg, _fail(A), db=db)                                  # FY A repeat, B recovered meanwhile
    first, _b, _rb, repeat = _msgs(srv)
    assert repeat["In-Reply-To"] == first["Message-ID"]               # still the same incident (a reminder)


# ── N-9: sibling alarms are one incident per KEY ─────────────────────────────

K1, K2 = "overlap:unprotected:FY27-28", "overlap:sibling:FY 27-28"


def _sib(key, kind="ACTION_REQUIRED", fy=None):
    return nt.AlertEvent(key=key if kind != "RECOVERED" else f"recovered:{key}", kind=kind,
                         mode="rollover_files", title="t", message="m", status=None,
                         fault_code="AAM-ROL-06" if kind != "RECOVERED" else None,
                         resolved_code="AAM-ROL-06" if kind == "RECOVERED" else None, fy=fy)


def test_recovered_of_one_overlap_key_does_not_restart_or_thread_into_a_sibling_key(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], cooldown=0)
        # the filesystem-based closure check is not what is tested here: keep both alarms open
        patcher = patch("core.notify._incident_closed", return_value=False)
        patcher.start()
        d1 = nt.notify(cfg, _sib(K1), db=db)
        d2 = nt.notify(cfg, _sib(K2), db=db)
        assert d1.status == nt.SENT and d2.status == nt.SENT
        assert nt.notify(cfg, _sib(K1, "RECOVERED"), db=db).status == nt.SENT
        # K2 is still open: its repeat is the SAME incident (threaded under its own first mail)
        assert nt.notify(cfg, _sib(K2), db=db).status == nt.SENT
        # K1's RECOVERED threads into K1's conversation, not K2's
        # (a RECOVERED of K2 afterwards threads into K2's)
        assert nt.notify(cfg, _sib(K2, "RECOVERED"), db=db).status == nt.SENT
        patcher.stop()
    first1, first2, rec1, rep2, rec2 = _msgs(srv)
    assert rec1["In-Reply-To"] == first1["Message-ID"]
    assert rep2["In-Reply-To"] == first2["Message-ID"]
    assert rec2["In-Reply-To"] == first2["Message-ID"]


def test_other_modes_stay_incident_per_mode(db):
    event = _fail(A)
    assert nt._incident_keys(event) is None
    assert nt._incident_keys(_sib(K1, "RECOVERED")) == (K1, f"recovered:{K1}")
    assert nt._incident_keys(_sib(K1)) == (K1, f"recovered:{K1}")
