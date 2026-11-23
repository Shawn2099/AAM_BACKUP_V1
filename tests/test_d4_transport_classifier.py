"""D4 (final audit 2026-10-20): a file NAME or a COUNT must not turn a real divergence into "could not run".

`find_transport_failure` ran its regexes over the whole audit log. `\\b403\\b`, `forbidden`, `unauthorized`, `timed out`
... match inside per-file result lines ("ERROR : Invoices/403 Smith.pdf: sizes differ") and inside the summary
("403 differences found"), so a real VERIFICATION_FAILED (red, "files do not match") was downgraded to
AUDIT_INCOMPLETE with category AUTH (amber, "could not run"): reproduced. rclone's own per-file verdict lines and the
numbers of its summary lines are comparison RESULTS, not transport evidence; real transport errors stay classified."""

import pytest

from core import integrity
from core.rclone_errors import find_transport_failure
from test_m3_audit_truth import _check

STAMP = "2026/10/20 03:05:12"


@pytest.mark.parametrize("line", [
    f"{STAMP} ERROR : Invoices/403 Smith.pdf: sizes differ",
    f"{STAMP} ERROR : Reports/Timed out invoices.pdf: sizes differ",
    f"{STAMP} ERROR : Legal/Forbidden items.docx: md5 differ",
    f"{STAMP} ERROR : Tax/unauthorized.pdf: File not in Google cloud storage bucket aam-cloudbackup",
    f"{STAMP} ERROR : Clients/401 list.xlsx: File not in Local file system at E:/DATA/FY26-27",
    f"{STAMP} NOTICE: 403 differences found",
    f"{STAMP} NOTICE: 401 errors while checking",
    f"{STAMP} ERROR : Failed to check: 403 differences found",
    "12 differences found",
])
def test_comparison_results_are_not_transport_evidence(line):
    assert find_transport_failure(line + "\n") is None


@pytest.mark.parametrize("line,category", [
    (f"{STAMP} ERROR : : error listing: dial tcp: lookup storage.googleapis.com: no such host", "DNS"),
    (f"{STAMP} ERROR : : googleapi: Error 403: Forbidden, forbidden", "AUTH"),
    (f"{STAMP} ERROR : : oauth2: cannot fetch token: 400 Bad Request", "AUTH"),
    (f"{STAMP} ERROR : : tls: failed to verify certificate: x509: certificate signed by unknown authority",
     "TLS_INTERCEPTED"),
    (f"{STAMP} ERROR : The network path was not found.", "NETWORK"),
    (f"{STAMP} ERROR : Failed to check with 403 errors: last error was: error listing: dial tcp: i/o timeout",
     "NETWORK"),     # the count is masked, the real cause after it still classifies
])
def test_real_transport_errors_still_classify(line, category):
    found = find_transport_failure(line + "\n")
    assert found is not None and found.category == category


def test_a_real_error_between_comparison_lines_is_still_found():
    text = "\n".join([f"{STAMP} ERROR : a/403 b.pdf: sizes differ",
                      f"{STAMP} ERROR : : error listing: dial tcp: lookup storage.googleapis.com: no such host",
                      f"{STAMP} NOTICE: 403 differences found"])
    assert find_transport_failure(text).category == "DNS"


def test_audit_with_a_403_in_a_file_name_stays_verification_failed(tmp_path, monkeypatch):
    stderr = (f"{STAMP} ERROR : Invoices/403 Smith.pdf: sizes differ\n"
              f"{STAMP} NOTICE: 1 differences found\n")
    res = _check(monkeypatch, rc=1, stderr=stderr, rows=[("*", "Invoices/403 Smith.pdf"), ("=", "a.txt")])
    assert res["status"] == "VERIFICATION_FAILED" and "content-changed=1" in res["detail"]


def test_audit_with_exactly_403_differences_stays_verification_failed(monkeypatch):
    rows = [("*", f"f{i}.txt") for i in range(403)]
    stderr = f"{STAMP} NOTICE: 403 differences found\n"
    res = _check(monkeypatch, rc=1, stderr=stderr, rows=rows)
    assert res["status"] == "VERIFICATION_FAILED" and integrity is not None
