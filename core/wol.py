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
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((server_ip, port))
        sock.close()
        return result == 0
    except OSError:
        return False


def send_magic_packet(mac_address: str) -> None:
    """Send WoL magic packet to global broadcast 255.255.255.255:9."""
    try:
        wol_send(mac_address, ip_address="255.255.255.255", port=9)
        logger.info(f"WoL magic packet sent to {mac_address}")
    except Exception as e:
        raise OSError(f"Failed to send WoL packet: {e}")


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
        time.sleep(ping_interval)
        if _smb_port_open(server_ip):
            logger.info(f"Backup server {server_ip} SMB accessible after WoL")
            if stability_wait > 0:
                logger.debug(f"Waiting {stability_wait}s for server stability")
                time.sleep(stability_wait)
            return

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
    send_magic_packet(config.wol.mac_address)
    wait_for_server(
        server_ip,
        config.wol.wake_timeout_seconds,
        config.wol.ping_interval_seconds,
        config.wol.stability_wait_seconds,
    )
    return True
