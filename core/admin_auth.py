"""Dashboard sign-in and Advanced-settings login (owner 2026-10-15; plan E, 2026-10-18).

Two separate passwords: the dashboard password (`dashboard.login_password_hash`) and the
Advanced password (`dashboard.admin_password_hash`). Only salted PBKDF2-SHA256 hashes are stored
in config.yaml - never a password. A missing hash means nobody signs in (fail closed). Stdlib
only (hashlib, hmac, secrets).

The iteration count is a fixed security parameter, not an operational
tunable: it is stored inside every hash, so raising it later never breaks a
stored password.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 600_000   # OWASP 2023 recommendation for PBKDF2-HMAC-SHA256


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"),
                                 _ITERATIONS).hex()
    return f"{_ALGO}${_ITERATIONS}${salt}${digest}"


def _check_hash(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != _ALGO:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"),
                                        int(iterations)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, digest)


def verify_admin_password(password: str, dashboard) -> bool:
    """True when `password` opens Advanced: only the stored Advanced hash counts (no hash set =
    nobody gets in); never the dashboard password."""
    if not password or not dashboard.admin_password_hash:
        return False
    return _check_hash(password, dashboard.admin_password_hash)


def verify_login_password(password: str, dashboard) -> bool:
    """True when `password` is the dashboard sign-in password (plan E1). Only the stored hash
    counts: no hash set = nobody signs in (fail closed); never the API key."""
    if not password or not dashboard.login_password_hash:
        return False
    return _check_hash(password, dashboard.login_password_hash)


def credentials_set(dashboard) -> bool:
    """Both passwords exist. Until they do, the dashboard shows only the 'not set up' page."""
    return bool(dashboard.login_password_hash and dashboard.admin_password_hash)


def fingerprint(stored_hash: str) -> str:
    """Short, non-reversible tag of a stored hash. A session remembers the tag of the password it
    was opened with; when the hash in config.yaml changes (a password change here, or the reset
    script run as another process) the tags differ and the session is no longer valid."""
    if not stored_hash:
        return ""
    return hashlib.sha256(stored_hash.encode("utf-8")).hexdigest()[:16]


def new_password_problem(new: str, confirm: str, min_length: int) -> str | None:
    """Why a new admin password is not acceptable, or None."""
    if new != confirm:
        return "The two new passwords are not the same."
    if len(new) < min_length:
        return f"The new password must have at least {min_length} characters."
    if new.strip() != new:
        return "The new password must not start or end with a space."
    return None
