"""A4 (final audit 2026-10-20): the NAS is "ready" only when the SHARE answers.

`smb_path_usable` treated ANY FileNotFoundError as "the share answers". On Windows a missing share (winerror 67)
and an unreachable host (winerror 53) raise FileNotFoundError too (reproduced), so after Wake-on-LAN, once TCP 445
accepted but before the share was published, the wait returned "ready" at once and the stability re-check (same
function) agreed. Fix: the share root must answer; only a missing canary/FY folder BELOW an answering share
is the allowed "absent" (G5 decides what an absent canary means)."""

import os
import sys

import pytest

from core import smb_ready as sr

UNC = r"\\nas01\aam_nas\FY26-27\.aam_mount_marker"
ROOT = r"\\nas01\aam_nas"


def _oserror(cls, winerror):
    exc = cls(2, "simulated")
    exc.winerror = winerror
    return exc


def _stat_with(monkeypatch, behaviour):
    """behaviour: {path: exception-to-raise | None}; an unlisted path answers."""
    calls = []

    def fake(path, *a, **k):
        calls.append(str(path))
        exc = behaviour.get(str(path))
        if exc is not None:
            raise exc
        return os.stat_result((0,) * 10)

    monkeypatch.setattr(sr.os, "stat", fake)
    return calls


@pytest.mark.parametrize("winerror", [53, 67])
def test_missing_share_or_unreachable_host_is_not_ready(monkeypatch, winerror):
    _stat_with(monkeypatch, {ROOT: _oserror(FileNotFoundError, winerror)})
    ok, why = sr.smb_path_usable(UNC)
    assert ok is False and "share" in why.lower()


@pytest.mark.parametrize("winerror", [2, 3])
def test_missing_canary_below_an_answering_share_is_allowed(monkeypatch, winerror):
    _stat_with(monkeypatch, {UNC: _oserror(FileNotFoundError, winerror)})
    ok, why = sr.smb_path_usable(UNC)
    assert ok is True and "absent" in why


def test_canary_present_is_ready(monkeypatch):
    calls = _stat_with(monkeypatch, {})
    assert sr.smb_path_usable(UNC) == (True, "smb stat ok")
    assert calls[0] == ROOT, "the share root must be asked first"


def test_other_errors_on_the_canary_are_not_ready(monkeypatch):
    _stat_with(monkeypatch, {UNC: PermissionError(5, "denied")})
    assert sr.smb_path_usable(UNC)[0] is False


def test_a_vanished_mapped_drive_is_not_ready(monkeypatch):
    _stat_with(monkeypatch, {"Z:\\": _oserror(FileNotFoundError, 3)})
    assert sr.smb_path_usable(r"Z:\FY26-27\.aam_mount_marker")[0] is False


def test_no_probe_path_stays_ready():
    assert sr.smb_path_usable(None)[0] is True


@pytest.mark.skipif(sys.platform != "win32", reason="UNC paths are Windows")
def test_real_missing_share_is_not_ready():
    ok, _why = sr.smb_path_usable(r"\\localhost\no_such_share_zz_a4\FY26-27\.aam_mount_marker")
    assert ok is False


def test_wait_keeps_waiting_until_the_share_is_published(monkeypatch):
    state = {"n": 0}

    def fake(path, *a, **k):
        if str(path) == ROOT:
            state["n"] += 1
            if state["n"] <= 2:                       # port open, share not yet published
                raise _oserror(FileNotFoundError, 67)
        return os.stat_result((0,) * 10)

    monkeypatch.setattr(sr.os, "stat", fake)
    result = sr.wait_for_smb_ready(
        "nas01", probe_path=UNC, connect_timeout_s=1, max_wait_s=60, poll_interval_s=0,
        stability_wait_s=0, tcp_probe=lambda *a: (True, "tcp open"), sleep=lambda s: None)
    assert result.ready and result.attempts == 3
