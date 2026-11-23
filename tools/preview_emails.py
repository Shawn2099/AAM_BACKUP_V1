r"""Send (or save) a sample of every email so the owner can review how they look (plan G2).

  python tools/preview_emails.py --config C:\AAMBackup\config.yaml --to owner@example.com
  python tools/preview_emails.py --config config.yaml --out previews\      # .eml files only

Renders through the real composer and sends through the real transport with "[PREVIEW]" in the
subject. Nothing is recorded in the notifications table and no cooldown starts.
"""
import argparse
import os
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.email_samples import samples  # noqa: E402
from core.notify import AUDIENCE_ALERTS, SENT, send_email  # noqa: E402
from models.config import load_config  # noqa: E402


def _eml(sample: dict, sender: str, to: str) -> bytes:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "[PREVIEW] " + sample["subject"], sender, to
    msg.set_content(sample["text"])
    msg.add_alternative(sample["html"], subtype="html")
    return bytes(msg)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--to", help="send every sample to this address (replaces the recipients)")
    p.add_argument("--out", help="write .eml files into this folder")
    a = p.parse_args(argv)
    if not a.to and not a.out:
        p.error("give --to and/or --out")
    config = load_config(a.config)
    items = samples(config)
    if a.out:
        folder = Path(a.out)
        folder.mkdir(parents=True, exist_ok=True)
        for i, s in enumerate(items, 1):
            (folder / f"{i:02d}.eml").write_bytes(
                _eml(s, config.notifications.sender or "backup@localhost", a.to or "owner@localhost"))
        print(f"{len(items)} .eml files written to {folder}")
    failed = 0
    if a.to:
        # previews go to --to ONLY: both real lists are replaced, never added to
        notifications = config.notifications.model_copy(
            update={"recipients": [a.to], "report_recipients": []})
        for s in items:
            r = send_email(notifications, "[PREVIEW] " + s["subject"], s["html"],
                           audience=AUDIENCE_ALERTS)
            if r.status != SENT:
                failed += 1
                print(f"NOT SENT: {s['name']}: {r.error}")
        print(f"{len(items) - failed} of {len(items)} previews sent to {a.to}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
