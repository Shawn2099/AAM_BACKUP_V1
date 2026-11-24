r"""Tests for tools.livebox.rig (plan 1.3-1.7, row L4a; work package L4b). Written before the module exists.

ASSUMED API (imports are lazy, so every test is red with ImportError until rig.py lands; dev PC, tmp_path only,
loopback only, nothing is ever powered off or woken):

    install_seams(lane_root, *, bucket_dir=None, remote_config_text=None) -> Seams
        Seams is a context manager (__exit__ == uninstall()) and also has .uninstall() (idempotent).
        Attributes (plain lists, one entry per call, args kept):
            .wake_calls      flow.ensure_server_online replaced by a spy (call count, returns True)
            .shutdown_calls  flow.lan_shutdown_task replaced by a spy returning {"simulated": True}
            .archive_calls   core.fy_rollover.run_archive_transition replaced by a recorder; entry is a dict with
                             at least keys "bucket" and "old_fy"; the replacement returns True
        Also replaced: core.wol._send_magic_packet and core.shutdown.shutdown_server become raisers (RuntimeError,
        attribute livebox_raiser = True); the lane's local-remote temp_rclone_config is patched into flow,
        core.cloud_sync and core.cloud_preflight (yields a config file path whose text is the local remote).
        Uninstall restores every module attribute to the object that was there before.

    rclone_config_text(variant, *, key_path="dummy-key.json") -> str     variant in {"local","blackhole","dns"}
        "local"     -> "[aam_gcs]\ntype = local\n"
        "blackhole" -> section aam_gcs, "type = google cloud storage", "endpoint = http://127.0.0.1:9",
                       "service_account_file = <key_path>"
        "dns"       -> same but "endpoint = https://aam-livebox.invalid"
        anything else -> ValueError

    install_google_date(skew_seconds=0.0, *, raise_oserror=False) -> handle (context manager, .uninstall())
        replaces core.health.http.client.HTTPSConnection; with raise_oserror the connection raises OSError.

    LaneSmtp(evidence_dir, *, fail_with=None) -> context manager (starts the loopback fake SMTP server)
        .host / .port           where to send
        .drain(sim_time: str) -> int   moves captured messages to <evidence_dir>/mail/<seq>_<simtime>.eml
                                       (seq zero padded, starting at 1) and appends rows to
                                       <evidence_dir>/mail_index.csv with header
                                       sim_time,real_time,rcpts,subject  ; returns the number drained
        .stop()                 closes the port (connection refused afterwards)

    assert_argv_in_lane(argv, lane_root) -> None     S15: raises tools.livebox.safety.SafetyRefusal(rule "S15") for
                                                     any absolute path argument outside the lane root.
"""
from __future__ import annotations

import csv
import socket
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest


def _rig():
    import importlib

    return importlib.import_module("tools.livebox.rig")


def _mods():
    import importlib

    return {n: importlib.import_module(n) for n in
            ("flow", "core.wol", "core.shutdown", "core.fy_rollover", "core.cloud_sync", "core.cloud_preflight",
             "core.health")}


SEAM_ATTRS = [
    ("flow", "ensure_server_online"), ("flow", "lan_shutdown_task"), ("flow", "temp_rclone_config"),
    ("core.wol", "_send_magic_packet"), ("core.shutdown", "shutdown_server"),
    ("core.fy_rollover", "run_archive_transition"),
    ("core.cloud_sync", "temp_rclone_config"), ("core.cloud_preflight", "temp_rclone_config"),
]


def _snapshot():
    m = _mods()
    return {(mod, attr): getattr(m[mod], attr) for mod, attr in SEAM_ATTRS}


@pytest.fixture()
def lane(tmp_path):
    root = tmp_path / "laneA"
    (root / "bucket").mkdir(parents=True)
    return root


# ---------------------------------------------------------------- seams: install / remove

def test_install_changes_and_uninstall_restores_every_attribute(lane):
    rig = _rig()
    before = _snapshot()
    seams = rig.install_seams(str(lane), bucket_dir=str(lane / "bucket"))
    during = _snapshot()
    assert all(during[k] is not before[k] for k in before), [k for k in before if during[k] is before[k]]
    seams.uninstall()
    after = _snapshot()
    assert all(after[k] is before[k] for k in before)


def test_context_manager_restores_even_on_error(lane):
    rig = _rig()
    before = _snapshot()
    with pytest.raises(ZeroDivisionError):
        with rig.install_seams(str(lane), bucket_dir=str(lane / "bucket")):
            1 / 0
    assert all(_snapshot()[k] is before[k] for k in before)


def test_uninstall_is_idempotent(lane):
    rig = _rig()
    before = _snapshot()
    seams = rig.install_seams(str(lane), bucket_dir=str(lane / "bucket"))
    seams.uninstall()
    seams.uninstall()
    assert all(_snapshot()[k] is before[k] for k in before)


# ---------------------------------------------------------------- raisers really raise

def test_real_wol_packet_sender_is_a_raiser(lane):
    rig = _rig()
    import core.wol as wol

    with rig.install_seams(str(lane), bucket_dir=str(lane / "bucket")):
        assert getattr(wol._send_magic_packet, "livebox_raiser", False) is True
        with pytest.raises(RuntimeError):
            wol._send_magic_packet("AA:BB:CC:DD:EE:FF", "255.255.255.255")


def test_real_nas_shutdown_is_a_raiser(lane):
    rig = _rig()
    import core.shutdown as sd

    with rig.install_seams(str(lane), bucket_dir=str(lane / "bucket")):
        assert getattr(sd.shutdown_server, "livebox_raiser", False) is True
        with pytest.raises(RuntimeError):
            sd.shutdown_server("127.0.0.1", delay_s=0, command_timeout_s=1, verify_timeout_s=1)


# ---------------------------------------------------------------- spies

def test_wake_spy_counts_and_does_not_wake(lane):
    rig = _rig()
    import flow

    with rig.install_seams(str(lane), bucket_dir=str(lane / "bucket")) as seams:
        assert seams.wake_calls == []
        flow.ensure_server_online(object())
        flow.ensure_server_online(object())
        assert len(seams.wake_calls) == 2


def test_shutdown_spy_counts_and_reports_simulated(lane):
    rig = _rig()
    import flow

    with rig.install_seams(str(lane), bucket_dir=str(lane / "bucket")) as seams:
        out = flow.lan_shutdown_task(object(), "artefacts")
        assert out == {"simulated": True}
        assert len(seams.shutdown_calls) == 1


def test_archive_step_is_replaced_and_recorded(lane):
    rig = _rig()
    import core.fy_rollover as fr

    with rig.install_seams(str(lane), bucket_dir=str(lane / "bucket")) as seams:
        ok = fr.run_archive_transition(bucket="lab-bucket", old_fy="FY90-91", gcs_key_path="k.json",
                                       auth_timeout=1, archive_timeout=1, config_dir=str(lane / "gcloud"))
        assert ok is True
        assert len(seams.archive_calls) == 1
        assert seams.archive_calls[0]["bucket"] == "lab-bucket"
        assert seams.archive_calls[0]["old_fy"] == "FY90-91"


def test_spies_start_empty_on_every_install(lane):
    rig = _rig()
    with rig.install_seams(str(lane), bucket_dir=str(lane / "bucket")) as a:
        import flow
        flow.ensure_server_online(object())
        assert len(a.wake_calls) == 1
    with rig.install_seams(str(lane), bucket_dir=str(lane / "bucket")) as b:
        assert b.wake_calls == [] and b.shutdown_calls == [] and b.archive_calls == []


# ---------------------------------------------------------------- cloud: local remote seam + variants

def test_patched_temp_rclone_config_yields_local_remote(lane):
    rig = _rig()
    import core.cloud_sync as cs

    with rig.install_seams(str(lane), bucket_dir=str(lane / "bucket")):
        with cs.temp_rclone_config("k.json", "us", "1", "STANDARD") as path:
            text = Path(path).read_text(encoding="utf-8")
    assert "[aam_gcs]" in text and "type = alias" in text and "type = local" not in text


def test_rclone_text_local():
    text = _rig().rclone_config_text("local")
    assert text.startswith("[aam_gcs]") and "type = local" in text
    assert "endpoint" not in text and "service_account_file" not in text


def test_rclone_text_blackhole_refuses_connections():
    text = _rig().rclone_config_text("blackhole", key_path="C:/x/dummy.json")
    assert "[aam_gcs]" in text
    assert "type = google cloud storage" in text
    assert "endpoint = http://127.0.0.1:9" in text
    assert "service_account_file = C:/x/dummy.json" in text
    # port 9 on loopback really is closed on the dev PC (discard service not running)
    with socket.socket() as s:
        s.settimeout(2)
        assert s.connect_ex(("127.0.0.1", 9)) != 0


def test_rclone_text_dns_uses_invalid_tld():
    text = _rig().rclone_config_text("dns")
    assert "endpoint = https://aam-livebox.invalid" in text
    assert "type = google cloud storage" in text
    assert "127.0.0.1" not in text


def test_rclone_text_unknown_variant_rejected():
    with pytest.raises(ValueError):
        _rig().rclone_config_text("real-gcs")


@pytest.mark.parametrize("variant", ["blackhole", "dns"])
def test_adverse_variants_never_carry_a_real_google_endpoint(variant):
    text = _rig().rclone_config_text(variant)
    assert "googleapis.com" not in text and "storage.google" not in text


# ---------------------------------------------------------------- Google clock proof

def test_google_date_agrees():
    rig = _rig()
    import core.health as health

    with rig.install_google_date():
        offset, why = health._google_offset(5)
    assert offset is not None and abs(offset) < 5


def test_google_date_skew_is_visible():
    rig = _rig()
    import core.health as health

    with rig.install_google_date(skew_seconds=900):
        offset, _ = health._google_offset(5)
    assert offset is not None and 890 < abs(offset) < 910


def test_google_date_raise_means_no_internet():
    rig = _rig()
    import core.health as health

    with rig.install_google_date(raise_oserror=True):
        offset, why = health._google_offset(5)
    assert offset is None and why


def test_google_date_removed_restores_connection_class():
    rig = _rig()
    import core.health as health

    original = health.http.client.HTTPSConnection
    h = rig.install_google_date()
    assert health.http.client.HTTPSConnection is not original
    h.uninstall()
    assert health.http.client.HTTPSConnection is original


# ---------------------------------------------------------------- fake SMTP + mail index

def _send(host, port, subject, to="owner@example.test"):
    msg = EmailMessage()
    msg["From"] = "aam@example.test"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("body for " + subject)
    with smtplib.SMTP(host, port, timeout=5) as s:
        s.send_message(msg)


def test_smtp_captures_and_drain_writes_eml_and_index(tmp_path):
    rig = _rig()
    ev = tmp_path / "evidence"
    with rig.LaneSmtp(ev) as smtp:
        _send(smtp.host, smtp.port, "FAILURE one")
        _send(smtp.host, smtp.port, "WARNING two", to="a@example.test")
        assert smtp.drain("2090-12-01T02:01:00+05:30") == 2
        assert smtp.drain("2090-12-01T03:00:00+05:30") == 0
    emls = sorted((ev / "mail").glob("*.eml"))
    assert len(emls) == 2
    assert emls[0].name.startswith("0001_") and emls[1].name.startswith("0002_")
    assert "FAILURE one" in emls[0].read_text(encoding="utf-8", errors="replace")
    with open(ev / "mail_index.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["subject"] for r in rows] == ["FAILURE one", "WARNING two"]
    assert rows[0]["sim_time"] == "2090-12-01T02:01:00+05:30"
    assert "owner@example.test" in rows[0]["rcpts"] and "a@example.test" in rows[1]["rcpts"]
    assert rows[0]["real_time"]


def test_smtp_sequence_continues_across_drains(tmp_path):
    rig = _rig()
    with rig.LaneSmtp(tmp_path) as smtp:
        _send(smtp.host, smtp.port, "one")
        smtp.drain("t1")
        _send(smtp.host, smtp.port, "two")
        smtp.drain("t2")
    names = sorted(p.name for p in (tmp_path / "mail").glob("*.eml"))
    assert [n[:4] for n in names] == ["0001", "0002"]
    with open(tmp_path / "mail_index.csv", newline="", encoding="utf-8") as fh:
        assert len(list(csv.DictReader(fh))) == 2     # header written once


def test_smtp_fail_with_rejects_and_captures_nothing(tmp_path):
    rig = _rig()
    with rig.LaneSmtp(tmp_path, fail_with="451 try later") as smtp:
        with pytest.raises(smtplib.SMTPException):
            _send(smtp.host, smtp.port, "x")
        assert smtp.drain("t") == 0


def test_smtp_stopped_port_is_closed(tmp_path):
    rig = _rig()
    with rig.LaneSmtp(tmp_path) as smtp:
        host, port = smtp.host, smtp.port
        smtp.stop()
        with pytest.raises(OSError):
            _send(host, port, "x")


def test_smtp_binds_loopback_only(tmp_path):
    rig = _rig()
    with rig.LaneSmtp(tmp_path) as smtp:
        assert smtp.host == "127.0.0.1"


# ---------------------------------------------------------------- S15 argv check

def test_argv_inside_lane_passes(tmp_path):
    rig = _rig()
    root = str(tmp_path / "laneA")
    rig.assert_argv_in_lane(
        ["robocopy.exe", root + r"\source\FY90-91", root + r"\nas\FY90-91", "/MIR", "/R:1", "/W:1",
         "/UNILOG:" + root + r"\runtime\run_logs\x.log"], root)
    rig.assert_argv_in_lane(["rclone.exe", "sync", root + r"\source", "aam_gcs:lab-bucket/FY90-91",
                             "--config", root + r"\rclone.conf"], root)


def test_argv_outside_lane_refused(tmp_path):
    rig = _rig()
    from tools.livebox.safety import SafetyRefusal

    root = str(tmp_path / "laneA")
    for bad in (r"C:\AAMBackup\data", r"C:\BackupAgent\runtime", str(tmp_path / "laneB" / "x"),
                r"\\nas\share\FY90-91"):
        with pytest.raises(SafetyRefusal) as exc:
            rig.assert_argv_in_lane(["robocopy.exe", root + r"\source", bad, "/MIR"], root)
        assert exc.value.rule == "S15"


def test_argv_flag_with_embedded_outside_path_refused(tmp_path):
    rig = _rig()
    from tools.livebox.safety import SafetyRefusal

    root = str(tmp_path / "laneA")
    with pytest.raises(SafetyRefusal):
        rig.assert_argv_in_lane(["robocopy.exe", root + r"\a", root + r"\b", r"/LOG:C:\BackupAgent\x.log"], root)


def test_argv_prefix_sibling_is_not_inside_lane(tmp_path):
    rig = _rig()
    from tools.livebox.safety import SafetyRefusal

    root = str(tmp_path / "laneA")
    with pytest.raises(SafetyRefusal):
        rig.assert_argv_in_lane(["robocopy.exe", root + "2" + r"\source", root + r"\nas"], root)
