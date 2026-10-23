"""Wake-on-LAN target rules shared by the install checks (deploy/install_checks.py) and the Settings save
(core/config_edit.py): wol.server_ip must be the machine the NAS path points at, and no documentation
placeholders. Deliberately NOT part of AppConfig validation: a validator that raises would stop the services
of an existing site on upgrade (fresh-install plan R-4/I-4)."""

from __future__ import annotations

import ipaddress
import re

Line = tuple[str, str, str]

PLACEHOLDER_MAC = "AA:BB:CC:DD:EE:FF"
PLACEHOLDER_IP = "192.0.2.10"  # TEST-NET-1: reserved for documentation, never a real NAS


def _unc_host(path: str) -> str | None:
    m = re.match(r"^\\\\([^\\/]+)[\\/]", path.replace("/", "\\") + "\\") if path.startswith(("\\\\", "//")) else None
    return m.group(1) if m else None


def check_wol_target(lan_enabled: bool, wol_enabled: bool, lan_destination: str, server_ip: str,
                     mac: str) -> list[Line]:
    if lan_enabled and _unc_host(lan_destination) == PLACEHOLDER_IP:
        return [("FAIL", "NAS folder", f"lan_destination still has the example address {PLACEHOLDER_IP}: enter the NAS address")]
    if not (lan_enabled and wol_enabled):
        return [("OK", "Wake-on-LAN", "off (nothing to wake)" if not wol_enabled else "NAS backup is off")]
    out: list[Line] = []
    if mac.strip().upper() == PLACEHOLDER_MAC:
        out.append(("FAIL", "Wake-on-LAN", f"wol.mac_address is the example value {PLACEHOLDER_MAC}: enter the NAS MAC"))
    if server_ip.strip() == PLACEHOLDER_IP:
        out.append(("FAIL", "Wake-on-LAN", f"wol.server_ip is the example value {PLACEHOLDER_IP}: enter the NAS address"))
    host = _unc_host(lan_destination)
    if host is None:
        out.append(("WARN", "Wake-on-LAN", f"lan_destination {lan_destination} is not a \\\\host\\share path: "
                    "cannot compare it with wol.server_ip"))
    else:
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        if not literal:
            out.append(("WARN", "Wake-on-LAN", f"the NAS path uses the name {host}; make sure wol.server_ip "
                        f"({server_ip}) is that machine"))
        elif host != server_ip.strip():
            out.append(("FAIL", "Wake-on-LAN", f"the NAS path points at {host} but wol.server_ip is {server_ip}: the "
                        "wake-up and the shutdown would go to a different machine than the backup"))
    if not out:
        out.append(("OK", "Wake-on-LAN", f"wol.server_ip {server_ip} is the NAS host of lan_destination"))
    return out
