r"""Encrypt the mail password in config.yaml with Windows DPAPI (plan S5).

  python deploy/encrypt_secrets.py --config C:\AAMBackup\config.yaml

Run by 06_install_services.ps1. A password that is empty or already encrypted is left alone.
The Google key file is not touched (it stays plain; deploy/lock_gcs_key.ps1 restricts who can
read it). Exit 0 = done / nothing to do, 1 = failed. Never prints the password.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import secret_setup  # noqa: E402
from core.config_file import ConfigWriteError  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    a = p.parse_args(argv)
    try:
        print(f"Mail password: {secret_setup.encrypt_smtp_password(a.config)}.")
    except (ConfigWriteError, Exception) as e:  # noqa: BLE001 - reported, installer stops
        print(f"Mail password NOT encrypted: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
