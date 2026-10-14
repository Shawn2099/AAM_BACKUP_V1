"""WP7 prep — transport-failure classifier, fixtures are REAL production lines."""

import pytest

from core.rclone_errors import find_transport_failure

# Verbatim (bucket/object query trimmed) from backup_2026-09..10 production logs.
PROD_X509 = ('2026/10/04 03:00:01 ERROR : error listing: Get "https://storage.googleapis.com/'
             'storage/v1/b/aam-backup-demo-innovizta/o?alt=json": oauth2: cannot fetch token: '
             'Post "https://oauth2.googleapis.com/token": tls: failed to verify certificate: '
             'x509: certificate signed by unknown authority')
PROD_DNS = ('2026/10/11 22:00:32 ERROR : error listing: Get "https://storage.googleapis.com/'
            'storage/v1/b/x/o": oauth2: cannot fetch token: Post "https://oauth2.googleapis.com/'
            'token": dial tcp: lookup oauth2.googleapis.com: no such host')
PROD_GETADDR = ('ERROR : oauth2: cannot fetch token: Post "https://oauth2.googleapis.com/token": '
                'dial tcp: lookup oauth2.googleapis.com: getaddrinfow: This is usually a temporary '
                'error during hostname resolution')


@pytest.mark.parametrize("line, category", [
    (PROD_X509, "TLS_INTERCEPTED"),       # TLS beats AUTH/LISTING in the same line
    (PROD_DNS, "DNS"),                    # DNS beats NETWORK ("dial tcp") and AUTH
    (PROD_GETADDR, "DNS"),
    ("ERROR : file.txt: Failed to copy: googleapi: Error 403: forbidden", "AUTH"),
    ("ERROR : Local file system at \\\\10.10.151.53\\aam_test: The network path was not found.",
     "NETWORK"),
    ("CRITICAL: Failed to create file system: directory not found", "LISTING"),
])
def test_real_signatures_classified(line, category):
    hit = find_transport_failure("INFO : starting\n" + line + "\nNOTICE : done")
    assert hit is not None and hit.category == category
    assert hit.hint and hit.evidence.startswith(line.strip()[:40])


def test_content_mismatch_lines_are_not_transport_failures():
    text = ("2026/10/12 03:05:00 ERROR : a.txt: md5 differ\n"
            "2026/10/12 03:05:00 ERROR : b.txt: sizes differ\n"
            "2026/10/12 03:05:00 NOTICE: gcs: 2 differences found\n"
            "2026/10/12 03:05:00 NOTICE: Failed to check: 2 differences found")
    assert find_transport_failure(text) is None


def test_empty():
    assert find_transport_failure("") is None
