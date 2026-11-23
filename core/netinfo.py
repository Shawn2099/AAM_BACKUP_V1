"""The server's own office-network address (plan D1), for the dashboard link in emails.

`local_ip_for(target)` asks the OS which local address it would use to reach `target`: a UDP
socket is connected (no packet is sent) and its own address read back. The default-route address
is the office network; the route to the NAS is only a fallback (a server with a separate NAS NIC
would give the NAS-side address). Stdlib only. Loopback, unspecified and link-local addresses are
never returned: a link built from them would be dead for everyone else.
"""
from __future__ import annotations

import ipaddress
import socket

# RFC 5737 TEST-NET-1: never routed on a real network, so connecting to it only consults the
# routing table, i.e. the default route. No packet is sent by a UDP connect().
DEFAULT_ROUTE_PROBE = "192.0.2.1"


def usable(address: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return False
    return ip.version == 4 and not (ip.is_loopback or ip.is_unspecified or ip.is_link_local
                                    or ip.is_multicast)


def local_ip_for(target: str, port: int = 9) -> str | None:
    """The local IPv4 address the OS would use to reach `target` (None if there is no route)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target, port))
            address = s.getsockname()[0]
    except OSError:
        return None
    return address if usable(address) else None


def all_ipv4() -> list[str]:
    """Every usable IPv4 address of this server (for the readiness report)."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos if usable(i[4][0])})


def office_ip(nas_host: str | None = None) -> str | None:
    """The address office PCs reach this server on: the default-route interface, else the one that
    routes to the NAS, else None."""
    return local_ip_for(DEFAULT_ROUTE_PROBE) or (local_ip_for(nas_host) if nas_host else None)
