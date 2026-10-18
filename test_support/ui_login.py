"""Signed-in dashboard sessions for tests (plan E9/E10).

The dashboard always needs a sign-in, so every test config carries the two known password
hashes below and a fixed session / Advanced-session token is registered in the ui module. Tests
send `HEADERS` (a Cookie) instead of the removed X-API-Key. `register(ui)` is called by the
autouse fixture in tests/conftest.py and audit_lab/conftest.py.
"""
from __future__ import annotations

import time

LOGIN_PASSWORD = "office-login-pass"
ADMIN_PASSWORD = "owner-admin-pass-1"
LOGIN_HASH = ("pbkdf2_sha256$600000$79ba5888d02133b126d116d1a820e2ac$"
              "90164f71a72abccf41ca8cfa4bf22ba51cdee40f4060046aa2633392efe2b1f3")
ADMIN_HASH = ("pbkdf2_sha256$600000$a1f2c118660e9b0dde89e451350b51e7$"
              "2806350ac5faa9e6bdad34fab9e0811737a76b6cd16874bc332aedea4e605ec3")
DASHBOARD = {"login_password_hash": LOGIN_HASH, "admin_password_hash": ADMIN_HASH}
SESSION_TOKEN = "t" * 64
ADMIN_TOKEN = "a" * 64
HEADERS = {"Cookie": f"session={SESSION_TOKEN}"}
ADMIN_HEADERS = {"Cookie": f"session={SESSION_TOKEN}; admin_session={ADMIN_TOKEN}"}


def register(ui) -> None:
    """Make the fixed tokens valid sessions of `ui` (for the stored test hashes)."""
    from core import admin_auth      # not at import: sandbox_config imports this before AAM_CONFIG is set
    ui._sessions[SESSION_TOKEN] = {"created_at": time.time(),
                                   "fp": admin_auth.fingerprint(LOGIN_HASH)}
    ui._admin_sessions[ADMIN_TOKEN] = {"created_at": time.time(),
                                       "fp": admin_auth.fingerprint(ADMIN_HASH)}


def unregister(ui) -> None:
    ui._sessions.pop(SESSION_TOKEN, None)
    ui._admin_sessions.pop(ADMIN_TOKEN, None)

DASHBOARD_YAML = (f"dashboard:\n  login_password_hash: {LOGIN_HASH}\n"
                  f"  admin_password_hash: {ADMIN_HASH}\n")
