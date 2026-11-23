"""Plan S1: DPAPI round trip (Windows only), entropy binding, non-Windows guard."""
import base64
import sys

import pytest

from core import secrets as sec

windows = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")


@windows
def test_round_trip_and_the_stored_form_hides_the_secret():
    stored = sec.protect("S3cret-Pass 123 ₹")
    assert stored.startswith("dpapi:") and "S3cret" not in stored and sec.is_protected(stored)
    assert sec.unprotect(stored) == "S3cret-Pass 123 ₹"


@windows
def test_two_encryptions_of_the_same_text_differ():
    assert sec.protect("same") != sec.protect("same")


@windows
def test_a_value_made_with_other_entropy_does_not_decrypt(monkeypatch):
    stored = sec.protect("secret")
    monkeypatch.setattr(sec, "_ENTROPY", b"another-application")
    with pytest.raises(sec.SecretsError):
        sec.unprotect(stored)


@windows
def test_tampered_or_garbage_values_fail_loudly():
    stored = sec.protect("secret")
    raw = bytearray(base64.b64decode(stored[len("dpapi:"):]))
    raw[-1] ^= 0xFF
    with pytest.raises(sec.SecretsError):
        sec.unprotect("dpapi:" + base64.b64encode(bytes(raw)).decode())
    with pytest.raises(sec.SecretsError):
        sec.unprotect("dpapi:%%%not-base64%%%")


def test_a_plain_value_passes_through_and_is_not_protected():
    assert sec.unprotect("plain-password") == "plain-password"
    assert not sec.is_protected("plain-password") and not sec.is_protected("") and not sec.is_protected(None)


def test_without_windows_protect_refuses_instead_of_storing_plain(monkeypatch):
    monkeypatch.setattr(sec, "available", lambda: False)
    with pytest.raises(sec.SecretsError):
        sec.protect("secret")
    with pytest.raises(sec.SecretsError):
        sec.unprotect("dpapi:AAAA")
