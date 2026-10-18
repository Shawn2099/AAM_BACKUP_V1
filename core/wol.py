"""Wake-on-LAN — magic packet sending and SMB readiness waiting.

Reference: AAM_BACKUP_V2/core/wol.py — proven logic, rewritten clean.
"""

import socket
import time

from loguru import logger
from wakeonlan import send_magic_packet as wol_send

from core.faults import coded
from core.smb_ready import wait_for_smb_ready
from models.config import AppConfig, WolConfig, model_default

CANARY_NAME = ".AAM_TARGET_MOUNTED"


class WolTimeout(RuntimeError):
    """Server did not respond within wake timeout."""


def _smb_port_open(server_ip: str, port: int = 445, timeout: float = model_default(WolConfig, "smb_connect_timeout_seconds")) -> bool:
    """TCP connect to SMB port. More reliable than ping.

    P1-WOL: ports outside the valid range are rejected by the clamp guard
    (connect_ex can raise OverflowError for them) - return False, never raise.
    """
    if not isinstance(port, int) or not (0 <= port <= 65535):
        logger.warning(f"SMB probe refused: invalid port {port!r}")
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            result = sock.connect_ex((server_ip, port))
            return result == 0
    except OSError:
        return False


def _send_magic_packet(
    mac_address: str,
    subnet_broadcast: str,
    repeat: int = model_default(WolConfig, "wake_retry_count"),
    interval: int = model_default(WolConfig, "wake_retry_interval_seconds"),
) -> None:
    """Send WoL magic packet to both global and subnet-directed broadcast.

    Global broadcast (255.255.255.255) works for most flat LAN setups.
    Subnet-directed broadcast (e.g. 192.168.10.255) reaches devices behind
    managed switches that drop 255.255.255.255 or across VLAN boundaries.
    Sending both maximises delivery without any router reconfiguration.

    G3: sends `repeat` rounds (default 3) spaced `interval` seconds apart
    (the wakeonlan 3.3.0 API has no built-in retransmit, so this is the
    standard external loop — same convention as `etherwake -s 3 -I 5`).
    Broadcast UDP is fire-and-forget: any single dropped round previously
    meant a failed wake for the whole night.

    P1-WOL: catches are broad-but-loud. Per-round failures (OSError from a
    dead interface, ValueError from an unparseable MAC, OverflowError from
    bad ports inside the library) are warned with type+args and the round
    continues — wake attempts must never abort mid-cycle on one bad target.
    KeyboardInterrupt/SystemExit still propagate (Exception excludes them).
    """
    rounds = max(1, int(repeat))
    for attempt in range(1, rounds + 1):
        try:
            wol_send(mac_address, ip_address="255.255.255.255", port=9)
            logger.debug(f"WoL magic packet sent to {mac_address} via 255.255.255.255 (round {attempt}/{rounds})")
        except Exception as e:
            logger.warning(f"WoL global broadcast failed ({type(e).__name__}: {e})")

        if subnet_broadcast != "255.255.255.255":
            try:
                wol_send(mac_address, ip_address=subnet_broadcast, port=9)
                logger.info(
                    f"WoL magic packet sent to {mac_address} via subnet broadcast "
                    f"{subnet_broadcast} (round {attempt}/{rounds})"
                )
            except Exception as e:
                logger.warning(
                    f"WoL subnet broadcast ({subnet_broadcast}) failed "
                    f"({type(e).__name__}: {e})"
                )

        if attempt < rounds:
            time.sleep(interval)




def _tcp_probe(host: str, port: int, timeout_s: float) -> tuple[bool, str]:
    ok = _smb_port_open(host, port, timeout_s)
    return ok, "tcp open" if ok else f"tcp {port}: no answer within {timeout_s:.0f}s"


def wait_for_server(
    server_ip: str,
    wake_timeout: int,
    ping_interval: int,
    stability_wait: int,
    *,
    probe_path: str | None = None,
    connect_timeout: float = model_default(WolConfig, "smb_connect_timeout_seconds"),
) -> None:
    """Wait until the NAS is genuinely usable over SMB (core.smb_ready).

    Ready = TCP 445 open AND (when `probe_path` is given) a real SMB stat
    succeeds AND the share is still usable after `stability_wait`.

    Raises:
        WolTimeout: if not usable within wake_timeout seconds (reason included).
    """
    result = wait_for_smb_ready(
        server_ip, probe_path=probe_path, connect_timeout_s=connect_timeout,
        max_wait_s=wake_timeout, poll_interval_s=ping_interval,
        stability_wait_s=stability_wait, sleep=time.sleep, tcp_probe=_tcp_probe,
    )
    if not result.ready:
        raise coded(WolTimeout(f"Backup server {result.reason} after WoL"), "AAM-NAS-02")
    logger.info(f"Backup server {server_ip} usable over SMB after WoL "
                f"({result.elapsed_s:.0f}s, {result.attempts} attempt(s))")


def _canary_path(config: AppConfig) -> str | None:
    dest = getattr(getattr(config, "paths", None), "lan_destination", None)
    if not isinstance(dest, str) or not dest:
        return None
    return dest.rstrip("\\/") + "\\" + CANARY_NAME


def ensure_server_online(config: AppConfig) -> bool:
    """Wake backup server and wait for SMB readiness.

    Returns True if server is online (or WoL disabled).
    Raises WolTimeout on timeout.
    """
    if not config.wol.enabled:
        logger.debug("WoL disabled, assuming server is online")
        return True

    server_ip = config.wol.server_ip
    connect_timeout = float(getattr(config.wol, "smb_connect_timeout_seconds", 15.0))
    probe_path = _canary_path(config)

    already = wait_for_smb_ready(
        server_ip, probe_path=probe_path, connect_timeout_s=connect_timeout,
        max_wait_s=0, poll_interval_s=0, stability_wait_s=0, min_attempts=1,
        sleep=time.sleep, tcp_probe=_tcp_probe,
    )
    if already.ready:
        logger.info(f"Backup server {server_ip} already online (SMB usable)")
        return True

    logger.info(f"Backup server {server_ip} offline, sending WoL")
    _send_magic_packet(
        config.wol.mac_address,
        config.wol.get_broadcast_address(),
        repeat=config.wol.wake_retry_count,
        interval=config.wol.wake_retry_interval_seconds,
    )
    wait_for_server(
        server_ip,
        config.wol.wake_timeout_seconds,
        config.wol.ping_interval_seconds,
        config.wol.stability_wait_seconds,
        probe_path=probe_path,
        connect_timeout=connect_timeout,
    )
    return True
