r"""Send one real test mail to the alerts AND reports lists (fresh-install plan R-12).

  .venv\Scripts\python.exe deploy\send_test_mail.py C:\AAMBackup\config.yaml

One mail proves the host, port, TLS, login and both recipient lists in one step. It goes through the real alert
path (core.notify, recorded like every email). Exit 0 = delivered, 1 = not delivered (the reason is printed).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    try:
        from core.manifest import ManifestDB
        from core.notify import AUDIENCE_BOTH, SENT, AlertEvent, notify, resolve_recipients
        from core.time_utils import now_iso
        from models.config import load_config

        cfg = load_config(argv[1])
        if not cfg.notifications.smtp_host:
            print("FAIL Test mail: notifications.smtp_host is empty")
            return 1
        who = ", ".join(resolve_recipients(cfg.notifications, AUDIENCE_BOTH).addresses)
        if not who:
            print("FAIL Test mail: no recipients - fill both lists under notifications in config.yaml")
            return 1
        result = notify(cfg, AlertEvent(
            key=f"test:{now_iso()}", kind="INFO", mode="test",
            title="Test email from your backup system",
            message="This was sent while installing the backup system. If you can read it, backup emails reach "
                    "you. Nothing to do.",
            audience=AUDIENCE_BOTH), db=ManifestDB.from_config(cfg))
    except Exception as e:  # noqa: BLE001 - reported as a line
        print(f"FAIL Test mail: {type(e).__name__}: {' '.join(str(e).split())}")
        return 1
    if result.status == SENT:
        print(f"OK Test mail: sent to {who}. Check that it arrived in the inbox")
        return 0
    print(f"FAIL Test mail: not delivered: {' '.join(str(result.error).split())}. "
          "Check smtp_host, smtp_port, smtp_security, smtp_username and the password")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
