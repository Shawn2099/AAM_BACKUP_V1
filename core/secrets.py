"""Secrets encrypted at rest with Windows DPAPI (plan S1, D-14).

`protect(text)` -> "dpapi:<base64>"; `unprotect(value)` -> text. Machine scope
(CRYPTPROTECT_LOCAL_MACHINE): the agent account, the watchdog (LocalSystem) and the installer
(administrator) must all read the same value, and a copy of config.yaml on another machine is
useless. An application-specific entropy is mixed in, so another program on this server cannot
decrypt it by just calling DPAPI. stdlib `ctypes` only, no dependency. On a non-Windows machine
(CI, dev) `protect` refuses and `unprotect` of a "dpapi:" value fails: a secret is never silently
stored plain by this module.
"""
from __future__ import annotations

import base64
import sys

PREFIX = "dpapi:"
_ENTROPY = b"AAM-Backup/secrets/v1"
_LOCAL_MACHINE = 0x4       # CRYPTPROTECT_LOCAL_MACHINE
_UI_FORBIDDEN = 0x1        # CRYPTPROTECT_UI_FORBIDDEN


class SecretsError(Exception):
    pass


def is_protected(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)


def available() -> bool:
    return sys.platform == "win32"


def _blob(data: bytes):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _crypt(data: bytes, protect_it: bool) -> bytes:
    if not available():
        raise SecretsError("Windows DPAPI is only available on Windows")
    import ctypes

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, keep1 = _blob(data)
    ent, keep2 = _blob(_ENTROPY)
    out = type(src)()
    fn = crypt32.CryptProtectData if protect_it else crypt32.CryptUnprotectData
    if protect_it:
        ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                _LOCAL_MACHINE | _UI_FORBIDDEN, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                _UI_FORBIDDEN, ctypes.byref(out))
    del keep1, keep2
    if not ok:
        raise SecretsError(f"DPAPI {'encrypt' if protect_it else 'decrypt'} failed "
                           f"(error {ctypes.GetLastError()}): the value was not written on this "
                           "server, or not for this application")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(text: str) -> str:
    return PREFIX + base64.b64encode(_crypt(text.encode("utf-8"), True)).decode("ascii")


def unprotect(value: str) -> str:
    """The plain text of a "dpapi:..." value. A value without the prefix is returned unchanged
    (a plain value still loads in dev / tests; the callers show amber)."""
    if not is_protected(value):
        return value
    try:
        raw = base64.b64decode(value[len(PREFIX):], validate=True)
    except ValueError as e:
        raise SecretsError("the protected value is not valid base64") from e
    return _crypt(raw, False).decode("utf-8")


def unprotect_bytes(value: str) -> bytes:
    return unprotect(value).encode("utf-8")
