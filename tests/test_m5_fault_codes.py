"""Fault codes (docs/EMAIL_CONTENT_SPEC.md §4, audit 2026-10-18): every
non-success run carries exactly one AAM-<AREA>-<NN> code, attached where the
cause is known (raise site, guard, tool evidence), never parsed from message
text; anything without a code is AAM-SYS-06."""

import re
from pathlib import Path

import pytest

from core import faults, guards, health, permissions
from core.health import HealthError
from core.permissions import PermissionCheckError

SPEC = (Path(__file__).resolve().parent.parent / "docs" / "EMAIL_CONTENT_SPEC.md").read_text(
    encoding="utf-8")
PLACEHOLDERS = {"source", "dest", "nas", "leg_name"}


# ── the catalog ──────────────────────────────────────────────────────────

def test_codes_are_well_formed_and_documented_in_the_spec():
    for code, fault in faults.CATALOG.items():
        assert re.fullmatch(r"AAM-[A-Z]{3}-\d{2}", code) and fault.code == code
        assert f"| {code} |" in SPEC, f"{code} missing from EMAIL_CONTENT_SPEC.md §4"


def test_every_text_is_plain_and_uses_only_known_placeholders():
    jargon = re.compile(r"robocopy|rclone|exit code|\bSMB\b|prefect|\bguard\b|\bG[1-6]\b|NSSM|UNC",
                        re.IGNORECASE)
    for fault in faults.CATALOG.values():
        assert fault.severity in (faults.ACTION, faults.PROBLEM, faults.WARNING)
        assert fault.who and fault.steps and fault.title and fault.what
        for text in (fault.title, fault.what, *fault.steps):
            assert not jargon.search(text), f"{fault.code}: {text!r}"
            assert set(re.findall(r"{(\w+)}", text)) <= PLACEHOLDERS, fault.code
            text.format(source="E:\\FY26-27", dest="\\\\nas\\FY26-27", nas="10.0.0.9",
                        leg_name="office backup (NAS)")


def test_unknown_code_is_the_unexpected_error():
    assert faults.get("AAM-XXX-99").code == faults.UNKNOWN == "AAM-SYS-06"
    assert faults.get(None).code == faults.UNKNOWN
    with pytest.raises(ValueError):
        faults.coded(RuntimeError("x"), "AAM-XXX-99")


def test_code_survives_raise_from():
    try:
        try:
            raise faults.coded(OSError("inner"), "AAM-NAS-03")
        except OSError as e:
            raise RuntimeError("outer") from e
    except RuntimeError as outer:
        assert faults.code_of(outer) == "AAM-NAS-03"


# ── classify_run: from evidence, never from text ─────────────────────────

@pytest.mark.parametrize("leg,status,sync,code", [
    ("lan", "LAN_COMPLETE", {}, None),
    ("cloud", "CLOUD_NO_CHANGES_COMPLETE", {}, None),
    ("lan", "LAN_SLOT_TIMEOUT", {}, "AAM-SYS-01"),
    ("cloud", "CLOUD_ABANDONED", {}, "AAM-SYS-02"),
    ("lan", "LAN_SUSPECT", {"exit_code": 1}, "AAM-LAN-04"),
    ("cloud", "CLOUD_SUSPECT", {"exit_code": 0}, "AAM-CLD-08"),
    ("cloud", "CLOUD_VERIFY_FAILED", {}, "AAM-CLD-09"),
    ("lan", "LAN_PARTIAL", {"files_failed": 3}, "AAM-LAN-02"),
    ("lan", "LAN_PARTIAL", {"files_failed": 0}, "AAM-LAN-03"),
    ("cloud", "CLOUD_PARTIAL", {"exit_code": 10}, "AAM-CLD-07"),
    ("lan", "LAN_FAILED", {"exit_code": 16}, "AAM-LAN-01"),
    ("cloud", "CLOUD_FAILED", {"exit_code": 7}, "AAM-CLD-06"),
    ("cloud", "CLOUD_FAILED", {"exit_code": 1, "transport_category": "TLS_INTERCEPTED"}, "AAM-CLD-02"),
    ("cloud", "CLOUD_FAILED", {"exit_code": 1, "transport_category": "AUTH"}, "AAM-CLD-03"),
    ("cloud", "CLOUD_FAILED", {"exit_code": 1, "transport_category": "DNS"}, "AAM-CLD-01"),
    ("cloud", "CLOUD_FAILED", {"exit_code": -1, "fault_code": "AAM-SYS-04"}, "AAM-SYS-04"),
    ("lan", "LAN_FAILED", {"exit_code": -1, "timed_out": True}, "AAM-LAN-01"),
    ("cloud", "CLOUD_FAILED", {"exit_code": -1, "timed_out": True}, "AAM-CLD-06"),
    ("lan", "LAN_FAILED", {"exit_code": -1}, "AAM-SYS-06"),        # no evidence: unknown
])
def test_classify_run_from_status_and_tool_evidence(leg, status, sync, code):
    assert faults.classify_run(leg, status, sync_result=sync) == code


def test_an_attached_code_wins_and_the_message_is_never_parsed():
    exc = faults.coded(HealthError("anything at all"), "AAM-NAS-01")
    assert faults.classify_run("lan", "LAN_FAILED", exc=exc, sync_result={"exit_code": -1}) \
        == "AAM-NAS-01"
    plain = HealthError("Cannot reach LAN host on SMB port 445")   # words alone: not a code
    assert faults.classify_run("lan", "LAN_FAILED", exc=plain, sync_result={"exit_code": -1}) \
        == "AAM-SYS-06"


def test_blocked_run_takes_the_first_blocking_guards_code():
    record = {"results": [
        {"guard": "G1_SOURCE_HAS_FILES", "verdict": "PASS"},
        {"guard": "G3_MASS_DELETE", "verdict": "BLOCK", "fault_code": "AAM-DEL-02"},
        {"guard": "G5_CANARY", "verdict": "BLOCK"},
    ]}
    assert faults.classify_run("lan", "LAN_BLOCKED", guard_record=record) == "AAM-DEL-02"
    old = {"results": [{"guard": "G2_SOURCE_SHRINK", "verdict": "BLOCK"}]}   # pre-code rows
    assert faults.classify_run("cloud", "CLOUD_BLOCKED", guard_record=old) == "AAM-SRC-04"


# ── guards carry their own code ──────────────────────────────────────────

def test_guard_blocks_carry_specific_codes(tmp_path):
    assert guards.g1_source_has_files(None, "E:\\FY").fault_code == "AAM-SRC-03"
    assert guards.g2_source_shrink(None, 100, 10).fault_code == "AAM-SRC-05"
    assert guards.g2_source_shrink(10, 100, 10).fault_code == "AAM-SRC-04"
    assert guards.g3_mass_delete(None, 100, 10, 5).fault_code == "AAM-DEL-02"
    assert guards.g3_mass_delete(90, 100, 10, 5).fault_code == "AAM-DEL-01"
    assert guards.g1_source_has_files("E:\\FY\\a.xlsx", "E:\\FY").fault_code is None


# ── raise sites ──────────────────────────────────────────────────────────

def _raised(fn, *a, **kw) -> str | None:
    with pytest.raises(Exception) as info:
        fn(*a, **kw)
    return faults.code_of(info.value)


def test_source_missing_and_source_disk_full(tmp_path, monkeypatch):
    assert health.source_drive_fault(str(tmp_path / "gone"))[0] == "AAM-SRC-01"
    assert health.source_drive_fault(str(tmp_path), min_free_gb=10**9)[0] == "AAM-SYS-05"
    assert health.source_drive_fault(str(tmp_path), min_free_gb=0) == (None, "")
    code = _raised(health.pre_backup_health, str(tmp_path / "gone"), "lan", "")
    assert code == "AAM-SRC-01"


def test_tools_missing_and_cloud_key_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "check_binary_exists", lambda name: False)
    assert _raised(health.pre_backup_health, str(tmp_path), "lan", "") == "AAM-SYS-04"
    monkeypatch.setattr(health, "check_binary_exists", lambda name: True)
    assert _raised(health.pre_backup_health, str(tmp_path), "cloud", "") == "AAM-CLD-04"


def test_permission_checks_carry_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions.shutil, "disk_usage",
                        lambda p: type("U", (), {"free": 1})())
    assert _raised(permissions.check_destination_free_space, str(tmp_path), 10**12, 0) \
        == "AAM-NAS-04"
    assert _raised(permissions.check_destination_free_space, str(tmp_path), None, 0) \
        == "AAM-NAS-05"
    assert _raised(permissions.probe_source_read, str(tmp_path / "gone")) == "AAM-SRC-02"
    assert _raised(permissions.probe_destination_write, str(tmp_path / "gone"), "r1") \
        == "AAM-NAS-03"


def test_nas_unreachable_is_nas_01(monkeypatch):
    from core import lan_preflight
    monkeypatch.setattr(lan_preflight, "_is_smb_reachable", lambda *a, **k: False)
    code = _raised(lan_preflight.run_lan_dry_run, "E:\\FY", "\\\\10.0.0.9\\nas\\FY",
                   smb_attempts=1, smb_wait_seconds=0, smb_connect_timeout_seconds=0.1)
    assert code == "AAM-NAS-01"


def test_nas_not_waking_is_nas_02():
    from core import wol
    assert isinstance(faults.coded(wol.WolTimeout("x"), "AAM-NAS-02"), wol.WolTimeout)
    src = Path(wol.__file__).read_text(encoding="utf-8")
    assert 'coded(WolTimeout(' in src and '"AAM-NAS-02"' in src


def test_codes_reach_the_run_row():
    src = (Path(__file__).resolve().parent.parent / "flow.py").read_text(encoding="utf-8")
    # both pipelines classify in their except block and store it in the run row
    assert src.count("faults.classify_run(") >= 2
    assert src.count("fault_code=fault_code") >= 2


# ── the code reaches the run row through the real pipeline ───────────────

def _recorded_metrics(m) -> dict:
    import json
    return json.loads(m["_record_run"].call_args.kwargs["extended_metrics"])


def test_pipeline_records_the_raise_site_code():
    from tests.test_m3_alerts import _cfg, _cloud_rig

    import flow
    from core.time_utils import now_iso
    with _cloud_rig() as m:
        m["health_check_task"].side_effect = faults.coded(HealthError("no key"), "AAM-CLD-04")
        with pytest.raises(HealthError):
            flow._run_cloud_pipeline(_cfg(), "run-f1", now_iso())
        assert _recorded_metrics(m)["fault_code"] == "AAM-CLD-04"


def test_pipeline_records_the_status_code_and_the_guard_code():
    from tests.test_m3_alerts import _cfg, _cloud_rig

    import flow
    from core.time_utils import now_iso
    with _cloud_rig() as m:
        m["cloud_sync_task"].with_options.return_value.return_value = {
            "status": "CLOUD_COMPLETE", "exit_code": 0, "stats": None}
        m["cloud_verify_and_report_task"].with_options.return_value.return_value = {
            "verified": False, "size": {}, "manifest": [], "diff": {"added": ["a"]}}
        with pytest.raises(RuntimeError):
            flow._run_cloud_pipeline(_cfg(), "run-f2", now_iso())
        assert _recorded_metrics(m)["fault_code"] == "AAM-CLD-09"
    with _cloud_rig() as m, __import__("unittest.mock").mock.patch("flow._open_block_request"):
        record = {"results": [{"guard": "G3_MASS_DELETE", "verdict": "BLOCK",
                               "fault_code": "AAM-DEL-01", "reason": "r", "metrics": {}}]}
        m["cloud_guards_task"].side_effect = guards.GuardBlocked("cloud", record)
        with pytest.raises(guards.GuardBlocked):
            flow._run_cloud_pipeline(_cfg(), "run-f3", now_iso())
        assert _recorded_metrics(m)["fault_code"] == "AAM-DEL-01"


def test_cloud_preflight_fault_from_the_probe_result():
    import flow
    assert flow._cloud_preflight_fault({"error": "rclone not found"}) == "AAM-SYS-04"
    assert flow._cloud_preflight_fault(
        {"error": "x509: certificate signed by unknown authority"}) == "AAM-CLD-02"
    assert flow._cloud_preflight_fault({"error": "exit 3: something new"}) == "AAM-CLD-10"
