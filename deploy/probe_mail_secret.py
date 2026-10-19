r"""Readiness probe for the mail password, run as LocalSystem (plan S5).

The watchdog runs as LocalSystem and sends the alert emails, so it must be able to decrypt
notifications.smtp_password. 04_check_readiness.ps1 starts this as a SYSTEM scheduled task.

Usage:  .venv\Scripts\python.exe deploy\probe_mail_secret.py <config.yaml> <result file>
Writes one line ("OK|FAIL mail password: <detail>") and exits 0 only when it is fine.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    try:
        from core import secret_setup
        from models.config import load_config
        line = f"OK mail password: {secret_setup.check_smtp_password(load_config(argv[1]).notifications)}"
    except Exception as e:  # noqa: BLE001 - every failure is reported as a line
        line = f"FAIL mail password: {type(e).__name__}: {' '.join(str(e).split())}"
    Path(argv[2]).write_text(line + "\n", encoding="utf-8")
    print(line)
    return 0 if line.startswith("OK") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
