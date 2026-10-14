"""Classify rclone/transport failures from log text (plan WP7, WP12).

Production evidence (2026-07..10): the integrity audit reported
"content mismatch, missing-from-dest=20, elapsed_s=0" on nights when the
destination could not even be listed — TLS interception by the office
firewall (x509: certificate signed by unknown authority, 72x), DNS outages
(no such host, 14x), unreachable LAN share. Those are "could not check",
never "data is missing".

`find_transport_failure(text)` returns the primary cause (or None) with an
operator hint. Patterns are data (overridable from config), matched
case-insensitively, most specific category first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (category, regex, operator hint) — ORDER MATTERS: most specific first.
DEFAULT_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("TLS_INTERCEPTED", r"x509:|certificate signed by unknown authority|failed to verify certificate"
                        r"|self[- ]signed certificate",
     "HTTPS to Google is being intercepted (firewall/proxy TLS inspection, e.g. Sophos). "
     "Exclude *.googleapis.com from TLS inspection."),
    ("DNS", r"no such host|getaddrinfo|name resolution|temporary failure in name resolution"
            r"|dial tcp: lookup",
     "The server could not resolve Google/NAS host names (DNS). Check DNS / internet access."),
    ("AUTH", r"oauth2: cannot fetch token|invalid_grant|\b401\b|unauthori[sz]ed|\b403\b|forbidden"
             r"|accessdenied|permission denied for bucket",
     "Google refused the service-account credentials or permissions."),
    ("NETWORK", r"dial tcp|connection refused|connection reset|i/o timeout|timed out"
                r"|network is unreachable|no route to host|broken pipe"
                r"|the network path was not found|the specified network name is no longer available"
                r"|winerror 53|winerror 64|winerror 67|winerror 1231",
     "Network path to the destination is down or unreachable."),
    ("LISTING", r"error listing|directory not found|couldn't list|failed to list|object not found",
     "The destination could not be listed, so nothing could be compared."),
)


@dataclass(frozen=True)
class TransportFailure:
    category: str
    hint: str
    evidence: str  # the matching log line (trimmed)


def find_transport_failure(
    text: str,
    patterns: tuple[tuple[str, str, str], ...] = DEFAULT_PATTERNS,
) -> TransportFailure | None:
    """Return the highest-priority transport failure found in `text`."""
    if not text:
        return None
    lines = text.splitlines()
    for category, regex, hint in patterns:
        rx = re.compile(regex, re.IGNORECASE)
        for line in lines:
            if rx.search(line):
                return TransportFailure(category, hint, line.strip()[:500])
    return None
