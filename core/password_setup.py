"""Set or reset the dashboard / Advanced passwords from the server console (plan E4, E7).

Used by `deploy/set_dashboard_passwords.py` (called by the installer when a hash is missing and
by `deploy/11_reset_dashboard_passwords.ps1`). Writes through the locked, atomic config store, so
it is safe while the agent runs; the agent ends every older session on its next config refresh
(sessions carry the fingerprint of the hash they were opened with). Never prints or logs a password.
"""
from __future__ import annotations

from collections.abc import Callable

from core import admin_auth, config_edit
from core.time_utils import now_iso

KEYS = {"login": "dashboard.login_password_hash", "admin": "dashboard.admin_password_hash"}
NAMES = {"login": "Dashboard", "admin": "Advanced"}


class PasswordSetupError(Exception):
    pass


def missing(dashboard) -> list[str]:
    """Which of the two passwords have no hash yet."""
    return [w for w, key in KEYS.items() if not getattr(dashboard, key.split(".")[1])]


def set_passwords(config_path: str, which: list[str], ask: Callable[[str], str], *,
                  actor: str = "server console", db=None) -> list[str]:
    """Ask for each password in `which` (typed twice), check the rules, write the hashes.
    The two passwords must differ. Returns the names written. Nothing is written unless every
    password passes the rules."""
    from models.config import load_config

    cfg = load_config(config_path)
    dash = cfg.dashboard
    chosen: dict[str, str] = {}
    for w in which:
        new = ask(f"New {NAMES[w]} password: ")
        problem = admin_auth.new_password_problem(new, ask(f"{NAMES[w]} password again: "),
                                                  dash.admin_password_min_length)
        if problem:
            raise PasswordSetupError(problem)
        chosen[w] = new
    # the two passwords must differ: against each other and against the one not being changed
    login = chosen.get("login")
    admin = chosen.get("admin")
    if login and admin and login == admin:
        raise PasswordSetupError("The dashboard password and the Advanced password must be different.")
    if login and "admin" not in chosen and dash.admin_password_hash \
            and admin_auth._check_hash(login, dash.admin_password_hash):
        raise PasswordSetupError("The dashboard password and the Advanced password must be different.")
    if admin and "login" not in chosen and dash.login_password_hash \
            and admin_auth._check_hash(admin, dash.login_password_hash):
        raise PasswordSetupError("The dashboard password and the Advanced password must be different.")
    for w, pw in chosen.items():
        config_edit.set_password_hash(config_path, KEYS[w], admin_auth.hash_password(pw),
                                      actor=actor, db=db, now_iso=now_iso)
    return [NAMES[w] for w in chosen]
