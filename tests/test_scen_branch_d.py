"""Branch D scenarios (WoL, core/wol.py + models/config.py validators).

Program functions/validators only - no logic re-implemented.
"""
import pytest

from models.config import WolConfig

from tests.scenario_support import real_gate, record_op

pytestmark = [real_gate()]


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


class TestWOL06InvalidMACAtConfigLoad:
    """WOL-06: malformed MAC must die at CONFIG LOAD time (field validator),
    never at 1 a.m. inside the wake path."""

    def test_WOL_06_invalid_mac(self):
        sid = "WOL-06"
        ops = {}
        try:
            raised = None
            try:
                WolConfig(enabled=True, mac_address="AA:BB:CC")
            except ValueError as ve:
                raised = str(ve)

            # control: the same validator accepts a well-formed MAC
            ok_cfg = WolConfig(enabled=False, mac_address="02-00-00-00-00-01")

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:140],
                "catalog_anchor": "config.py field_validator valid_mac",
                "control_accepted": ok_cfg.mac_address,
                "inference": ("three-pair regex enforced at model construction; "
                              "matches the pipeline-entry abort observed live in "
                              "Batch-4 LAN-19 discovery (a)"),
            })
            assert raised and "Invalid MAC address format" in raised, f"op={ops}"
            assert raised.endswith("AA:BB:CC") or "AA:BB:CC" in raised, f"op={ops}"
            assert ok_cfg.mac_address == "02-00-00-00-00-01", f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 10: WOL-01 .. WOL-05
# ======================================================================

import time
from pathlib import Path

import pytest

from core.wol import (
    WolTimeout,
    _smb_port_open,
    _send_magic_packet,
    ensure_server_online,
    wait_for_server,
)

from tests.scenario_support import cfg, real_gate, record_op

pytestmark = [real_gate()]


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


class TestWOL01DisabledNoOp:
    """WOL-01: wol.enabled=false -> immediate True, zero probes, zero packets."""

    def test_WOL_01_disabled(self):
        sid = "WOL-01"
        ops = {}
        try:
            config = cfg()
            config.wol.enabled = False
            config.wol.server_ip = "192.0.2.55"

            from loguru import logger
            captured = []
            hid = logger.add(captured.append, level="DEBUG")
            try:
                result = ensure_server_online(config)
            finally:
                logger.remove(hid)

            blob = "\n".join(captured)
            ops.update({
                "returned": result,
                "disabled_line": any("WoL disabled" in m for m in captured),
                "no_probe": not any("already online" in m or "sending WoL" in m
                                    for m in captured),
                "inference": "catalog contract: no packet, return True",
            })
            assert result is True, f"op={ops}"
            assert ops["disabled_line"] and ops["no_probe"], f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestWOL02AlreadyOnlineShortCircuit:
    """WOL-02: SMB 445 reachable -> 'already online', no magic packet."""

    def test_WOL_02_already_online(self):
        sid = "WOL-02"
        ops = {}
        try:
            config = cfg()
            config.wol.enabled = True
            config.wol.mac_address = "02-00-00-00-00-01"
            config.wol.server_ip = "127.0.0.1"

            smb_open = _smb_port_open(config.wol.server_ip)

            from loguru import logger
            captured = []
            hid = logger.add(captured.append, level="DEBUG")
            try:
                result = ensure_server_online(config)
            finally:
                logger.remove(hid)

            blob = "\n".join(captured)
            ops.update({
                "smb_445_open": smb_open,
                "returned": result,
                "already_online_line": any("already online" in m for m in captured),
                "no_wake_attempt": not any("sending WoL" in m for m in captured),
                "inference": ("live NAS share host answers 445 -> wake machinery "
                              "skipped entirely"),
            })
            assert result is True, f"op={ops}"
            assert ops["already_online_line"] and ops["no_wake_attempt"], f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestWOL03DualBroadcastAndPollSuccess:
    """WOL-03: dual-broadcast emission observed verbatim (global + subnet,
    per-round counters), then the SMB poll-success path incl stability wait -
    all through the program's own sender/poller with real parameters."""

    def test_WOL_03_dual_broadcast(self):
        sid = "WOL-03"
        ops = {}
        try:
            config = cfg()
            config.wol.enabled = True
            config.wol.mac_address = "02-00-00-00-00-01"
            config.wol.server_ip = "127.0.0.1"
            subnet_bc = config.wol.get_broadcast_address()

            from loguru import logger
            captured = []
            hid = logger.add(captured.append, level="DEBUG")
            t0 = time.monotonic()
            try:
                _send_magic_packet(
                    config.wol.mac_address,
                    subnet_bc,
                    repeat=2,
                    interval=2,
                )
                wait_for_server(
                    config.wol.server_ip,
                    wake_timeout=30,
                    ping_interval=2,
                    stability_wait=3,
                )
            finally:
                logger.remove(hid)
            wall = round(time.monotonic() - t0, 1)

            blob = "\n".join(captured)
            global_rounds = blob.count("via 255.255.255.255")
            subnet_rounds = blob.count(f"via subnet broadcast {subnet_bc}")
            poll_ok = "SMB accessible after WoL" in blob
            ops.update({
                "subnet_broadcast": subnet_bc,
                "global_round_lines": global_rounds,
                "subnet_round_lines": subnet_rounds,
                "poll_success_line": poll_ok,
                "wall_s_incl_stability": wall,
                "inference": ("both broadcast targets transmitted per round "
                              "(G3 retransmit loop); SMB poll succeeded against "
                              "the live NAS host and stability wait was honored"),
            })
            assert global_rounds == 2 and subnet_rounds == 2, f"op={ops}"
            assert poll_ok, f"op={ops}"
            assert wall >= 3, f"stability wait not honored op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestWOL04TimeoutNeverWakes:
    """WOL-04: unreachable target -> WolTimeout with the exact contract text.
    Short wake_timeout via real parameter (production 300s witnessed in the
    Batch-4 LAN-19 discovery)."""

    def test_WOL_04_timeout(self):
        sid = "WOL-04"
        ops = {}
        try:
            t0 = time.monotonic()
            raised = None
            try:
                wait_for_server(
                    "192.0.2.55",
                    wake_timeout=6,
                    ping_interval=2,
                    stability_wait=0,
                )
            except WolTimeout as wt:
                raised = str(wt)
            wall = round(time.monotonic() - t0, 1)

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:150],
                "wall_s": wall,
                "catalog_fragment": "not accessible within",
            })
            assert raised and "SMB not accessible within 6s after WoL" in raised, f"op={ops}"
            assert 5 <= wall <= 20, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestWOL05GlobalBlockedFallbackWiring:
    """WOL-05: managed-switch drop of global broadcast cannot be produced
    without network gear - the fallback's code path (per-target OSError ->
    warn-and-continue to subnet broadcast) is read-verified instead, and its
    dual-transmission behavior was observed live in WOL-03."""

    def test_WOL_05_fallback_wiring(self):
        sid = "WOL-05"
        ops = {}
        try:
            import inspect

            from core import wol as wol_mod

            src = inspect.getsource(wol_mod._send_magic_packet)
            checks = {
                "global_first": 'wol_send(mac_address, ip_address="255.255.255.255"' in src,
                "subnet_second": "ip_address=subnet_broadcast" in src,
                "per_target_oserror_warn": src.count("except OSError") >= 2
                                           and src.count("logger.warning") >= 2,
                "continues_after_failure": "if subnet_broadcast != \"255.255.255.255\"" in src,
            }
            ops.update({
                "wiring": checks,
                "live_dual_send_evidence": "see WOL-03 ledger row (same session)",
                "inference": ("fallback semantics present and dual delivery "
                              "proven live; switch-level drop itself out of "
                              "scope for this rig"),
            })
            assert all(checks.values()), f"op={ops}"
            record_op(sid, "WIRING-EVIDENCED", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 11: WOL-07 .. WOL-09 (closes Branch D)
# ======================================================================

class TestWOL07InvalidBroadcastAddress:
    """WOL-07: broadcast override must be a valid IPv4 or empty."""

    def test_WOL_07_invalid_broadcast(self):
        sid = "WOL-07"
        ops = {}
        try:
            raised = None
            try:
                WolConfig(enabled=True,
                          mac_address="02-00-00-00-00-01",
                          broadcast_address="999.999.0.1")
            except ValueError as ve:
                raised = str(ve)

            control = WolConfig(enabled=False, broadcast_address="")

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:160],
                "control_empty_accepted": control.broadcast_address == "",
                "catalog_fragment": "Invalid broadcast_address",
            })
            assert raised and "Invalid broadcast_address IPv4 '999.999.0.1'" in raised, f"op={ops}"
            assert "auto-derive" in raised, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestWOL08SmbProbeOSErrorSafety:
    """WOL-08: DNS/socket failure inside the probe must yield False safely.
    Trigger: unresolvable hostname -> gaierror (an OSError) from connect."""

    def test_WOL_08_oserror_safe(self):
        sid = "WOL-08"
        ops = {}
        try:
            dns_fail = _smb_port_open("scen-hl09-no-such-host.invalid", timeout=3)

            # adjacent edge: out-of-range port - does the narrow except hold?
            overflow_behavior = None
            try:
                res = _smb_port_open("127.0.0.1", port=99999, timeout=2)
                overflow_behavior = f"returned {res}"
            except Exception as oe:
                overflow_behavior = f"raised {type(oe).__name__}"

            ops.update({
                "dns_failure_result": dns_fail,
                "port_overflow_behavior": overflow_behavior,
                "inference": ("gaierror (OSError subclass) handled -> False; "
                              "port-overflow behavior recorded verbatim"),
            })
            assert dns_fail is False, f"op={ops}"
            if overflow_behavior != "returned False":
                record_op(sid, "ANOMALY-RECORDED", ops)
                return
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestWOL09PerRoundFailureContinuation:
    """WOL-09: per-round/per-target failure continuation. A REAL OSError is
    not producible without network gear, so the wiring is read-verified -
    plus an adjacent robustness probe: what happens with an unparseable MAC
    (ValueError is NOT in the caught set)?"""

    def test_WOL_09_continuation(self):
        sid = "WOL-09"
        ops = {}
        try:
            import inspect

            from core import wol as wol_mod

            src = inspect.getsource(wol_mod._send_magic_packet)
            checks = {
                "round_loop": "for attempt in range(1, rounds + 1)" in src,
                "global_guarded": src.count("except OSError") >= 2,
                "subnet_skips_when_same": '!= "255.255.255.255"' in src,
            }

            escape = None
            try:
                _send_magic_packet("ZZ:ZZ:ZZ:ZZ:ZZ:ZZ", "127.0.0.255",
                                   repeat=1, interval=1)
            except Exception as ue:
                escape = f"{type(ue).__name__}: {ue}"[:120]

            ops.update({
                "wiring": checks,
                "unparseable_mac_outcome": escape or "swallowed silently",
                "inference": ("round loop + dual guards verified; robustness "
                              "probe recorded verbatim - non-OSError escapes "
                              "the warn-and-continue net"),
            })
            assert all(checks.values()), f"op={ops}"
            assert escape and escape.startswith("ValueError"), f"op={ops}"
            record_op(sid, "ANOMALY-RECORDED", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
