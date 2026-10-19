"""Mail password at rest, for the installer and the readiness probe (plan S5).

Only `notifications.smtp_password` is encrypted here. The Google key file stays plain by owner
decision (2026-10-19); `deploy/lock_gcs_key.ps1` restricts who can read it.
"""
from __future__ import annotations

from core import config_file, secrets as secrets_store


class SecretProblem(Exception):
    pass


def encrypt_smtp_password(config_path: str) -> str:
    """Encrypt a plain mail password in config.yaml (through the locked config store).
    Returns what was done; a password that is empty or already encrypted is not touched."""
    with config_file.config_transaction(config_path, tmp_prefix=".s5_") as txn:
        notifications = txn.doc.get("notifications") or {}
        value = str(notifications.get("smtp_password") or "")
        if not value:
            txn.unchanged = True
            return "no password set"
        if secrets_store.is_protected(value):
            txn.unchanged = True
            return "already encrypted"
        config_file.set_dotted(txn.doc, "notifications.smtp_password", secrets_store.protect(value))
    return "encrypted"


def check_smtp_password(notifications) -> str:
    """Readiness: the mail password in the loaded config. Raises SecretProblem when it is stored
    plain or cannot be decrypted by the account this runs as."""
    password = notifications.smtp_password
    if getattr(password, "undecryptable", False):
        raise SecretProblem("notifications.smtp_password cannot be decrypted by this account "
                            "(config.yaml from another server?) - enter the mail password again "
                            "in Settings")
    if not password:
        return "no mail password set (unauthenticated mail)"
    if not getattr(password, "protected", True):
        raise SecretProblem("notifications.smtp_password is not encrypted - run the installer "
                            "(06_install_services.ps1) or save it again in Settings")
    return "encrypted and readable by this account"
