r"""Ask for the mail password on this server (hidden) and store it encrypted (fresh-install plan R-10).

  .venv\Scripts\python.exe deploy\set_mail_password.py --config C:\AAMBackup\config.yaml [--only-missing]

The password is typed twice, never echoed, and written to config.yaml as a Windows DPAPI value (machine scope: the
agent, the watchdog and this installer can read it; a copy of config.yaml on another server cannot). It must be
typed on the server itself. With --only-missing nothing is asked when a password is already stored (plain or
encrypted) or when notifications.smtp_auth is false. Exit 0 = done / nothing to do, 1 = failed.
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config_file, secret_setup  # noqa: E402
from core import secrets as secrets_store  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--only-missing", action="store_true")
    a = p.parse_args(argv)
    try:
        with config_file.config_transaction(a.config, tmp_prefix=".s6_") as txn:
            n = txn.doc.get("notifications") or {}
            if a.only_missing and (n.get("smtp_auth") is False or str(n.get("smtp_password") or "")):
                txn.unchanged = True
                print("Mail password: nothing to ask.")
                return 0
            if not sys.stdin.isatty():
                raise RuntimeError("no console to ask on - run this in the server's own PowerShell window")
            first = getpass.getpass("Mail password (typed twice, hidden): ")
            if not first:
                raise RuntimeError("an empty mail password was entered")
            if getpass.getpass("Mail password again: ") != first:
                raise RuntimeError("the two entries differ")
            config_file.set_dotted(txn.doc, "notifications.smtp_password", secrets_store.protect(first))
    except Exception as e:  # noqa: BLE001 - reported, installer stops
        print(f"Mail password NOT stored: {e}")
        return 1
    # encrypt_smtp_password is the same code path 06 uses; calling it proves the value now reads back
    print(f"Mail password: stored encrypted ({secret_setup.encrypt_smtp_password(a.config)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
