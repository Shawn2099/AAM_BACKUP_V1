"""The Google Cloud service-account key, stored encrypted at rest (plan S3, D-14).

`paths.gcs_key_path` points either at the plain key file (`.json`, dev / tests, amber on a
server) or at the DPAPI-protected copy `gcs_key.json.dpapi` (`dpapi:<base64>` of the JSON text,
machine scope, see core.secrets). A protected key is never written to disk in plain text: rclone
gets it in memory through the environment variable of its remote
(`RCLONE_CONFIG_AAM_GCS_SERVICE_ACCOUNT_CREDENTIALS`, verified with rclone v1.74.3), set only while
the temporary rclone config exists (core.rclone_config).
"""
from __future__ import annotations

import json
from pathlib import Path

from core import secrets as secrets_store

PROTECTED_SUFFIX = ".dpapi"
RCLONE_ENV = "RCLONE_CONFIG_AAM_GCS_SERVICE_ACCOUNT_CREDENTIALS"


def is_protected(path: str | Path) -> bool:
    return str(path).lower().endswith(PROTECTED_SUFFIX)


def read_json(path: str | Path) -> str:
    """The key's JSON text from a protected key file. Raises SecretsError / OSError / ValueError
    when the file is missing, not decryptable on this server, or not a service-account key."""
    stored = Path(path).read_text(encoding="utf-8").strip()
    if not secrets_store.is_protected(stored):
        raise secrets_store.SecretsError("the key file is not DPAPI-protected")
    text = secrets_store.unprotect(stored)
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise ValueError("the decrypted key is not a Google service-account key")
    return text


def protect_file(plain_path: str | Path, protected_path: str | Path) -> Path:
    """Write the protected copy of a plain key file (installer). The plain file is NOT deleted
    here: the caller removes it only after the protected copy decrypted correctly."""
    text = Path(plain_path).read_text(encoding="utf-8")
    json.loads(text)                                   # refuse a file that is not JSON
    out = Path(protected_path)
    out.write_text(secrets_store.protect(text), encoding="utf-8")
    if read_json(out) != text:
        out.unlink(missing_ok=True)
        raise secrets_store.SecretsError("the protected key did not decrypt to the same text")
    return out
