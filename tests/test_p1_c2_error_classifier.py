"""P1 C2 (plan §4, rows E1-E7): rclone error classification after the P2 reorder.

NETWORK is checked before AUTH, and `oauth2: cannot fetch token` no longer counts as AUTH on its own:
the token endpoint is often unreachable, which is a network failure, not bad credentials.
A file name such as "403 Smith.pdf" is not a status code.
"""

import pytest

from core.rclone_errors import find_transport_failure

WRAP = 'ERROR : error listing: Get "https://storage.googleapis.com/storage/v1/b/bkt/o": '
TOKEN = 'oauth2: cannot fetch token: Post "https://oauth2.googleapis.com/token": '


@pytest.mark.parametrize("line", [
    # E1: the token endpoint times out (exact production shape)
    'oauth2: cannot fetch token: Post "https://oauth2.googleapis.com/token": '
    'dial tcp 142.250.1.95:443: i/o timeout',
    # E2: the same wrapper with the connection refused (Go wording)
    TOKEN + "dial tcp 142.250.1.95:443: connect: connection refused",
    # E2b: plain connection refused, already NETWORK today
    "ERROR : dial tcp 142.250.1.95:443: connect: connection refused",
])
def test_e1_e2_token_endpoint_network_failures_are_network(line):
    hit = find_transport_failure("INFO : start\n" + line + "\nNOTICE : done")
    assert hit is not None and hit.category == "NETWORK"


def test_e3_invalid_grant_is_auth():
    hit = find_transport_failure(WRAP + TOKEN + "400 Bad Request: invalid_grant: Invalid JWT Signature.")
    assert hit is not None and hit.category == "AUTH"


def test_e4_googleapi_403_is_auth():
    hit = find_transport_failure("ERROR : file.txt: Failed to copy: googleapi: Error 403: forbidden")
    assert hit is not None and hit.category == "AUTH"


def test_e5_host_not_found_is_dns():
    hit = find_transport_failure(WRAP + "lookup storage.googleapis.com: no such host")
    assert hit is not None and hit.category == "DNS"


def test_e6_certificate_error_is_tls():
    hit = find_transport_failure(WRAP + "x509: certificate signed by unknown authority")
    assert hit is not None and hit.category == "TLS_INTERCEPTED"


def test_e7_file_named_403_is_not_auth():
    text = ("NOTICE : 403 Smith.pdf: Failed to copy: The process cannot access the file because "
            "it is being used by another process.")
    assert find_transport_failure(text) is None
