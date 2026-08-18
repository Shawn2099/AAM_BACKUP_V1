"""Wake-on-LAN — magic packet sending and SMB readiness waiting.

Reference: AAM_BACKUP_V2/core/wol.py — proven logic, rewritten clean.
"""

import socket
import time

from loguru import logger
from wakeonlan import send_magic_packet as wol_send

from models.config import AppConfig


class WolTimeout(RuntimeError):
    """Server did not respond within wake timeout."""


def _smb_port_open(server_ip: str, port: int = 445, timeout: float = 5.0) -> bool:
    """TCP connect to SMB port. More reliable than ping."""
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
    repeat: int = 3,
    interval: int = 5,
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
    """
    rounds = max(1, int(repeat))
    for attempt in range(1, rounds + 1):
        try:
            wol_send(mac_address, ip_address="255.255.255.255", port=9)
            logger.debug(f"WoL magic packet sent to {mac_address} via 255.255.255.255 (round {attempt}/{rounds})")
        except OSError as e:
            logger.warning(f"WoL global broadcast failed: {e}")

        if subnet_broadcast != "255.255.255.255":
            try:
                wol_send(mac_address, ip_address=subnet_broadcast, port=9)
                logger.info(
                    f"WoL magic packet sent to {mac_address} via subnet broadcast "
                    f"{subnet_broadcast} (round {attempt}/{rounds})"
                )
            except OSError as e:
                logger.warning(f"WoL subnet broadcast ({subnet_broadcast}) failed: {e}")

        if attempt < rounds:
            time.sleep(interval)




def wait_for_server(
    server_ip: str,
    wake_timeout: int,
    ping_interval: int,
    stability_wait: int,
) -> None:
    """Poll SMB port until server responds, then wait for stability.

    Raises:
        WolTimeout: If server does not respond within wake_timeout seconds.
    """
    start_time = time.time()
    while time.time() - start_time < wake_timeout:
        if _smb_port_open(server_ip):
            logger.info(f"Backup server {server_ip} SMB accessible after WoL")
            if stability_wait > 0:
                logger.debug(f"Waiting {stability_wait}s for server stability")
                time.sleep(stability_wait)
            return
        time.sleep(ping_interval)

    raise WolTimeout(
        f"Backup server {server_ip} SMB not accessible within {wake_timeout}s after WoL"
    )


def ensure_server_online(config: AppConfig) -> bool:
    """Wake backup server and wait for SMB readiness.

    Returns True if server is online (or WoL disabled).
    Raises WolTimeout on timeout.
    """
    if not config.wol.enabled:
        logger.debug("WoL disabled, assuming server is online")
        return True

    server_ip = config.wol.server_ip

    if _smb_port_open(server_ip):
        logger.info(f"Backup server {server_ip} already online")
        return True

    logger.info(f"Backup server {server_ip} offline, sending WoL")
    _send_magic_packet(
        config.wol.mac_address,
        config.wol.get_broadcast_address(),
        repeat=getattr(config.wol, "wake_retry_count", 3),
        interval=getattr(config.wol, "wake_retry_interval_seconds", 5),
    )
    wait_for_server(
        server_ip,
        config.wol.wake_timeout_seconds,
        config.wol.ping_interval_seconds,
        config.wol.stability_wait_seconds,
    )
    return True
