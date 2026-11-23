r"""Set the dashboard and/or Advanced password from the server console (plan E4, E7).

  python deploy/set_dashboard_passwords.py --config C:\AAMBackup\config.yaml --only-missing
  python deploy/set_dashboard_passwords.py --config ... --which login|admin|both

Passwords are typed twice (hidden), never passed as arguments and never printed. Exit 0 = done
(or nothing to do), 1 = refused / failed.
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import password_setup as ps  # noqa: E402
from core.manifest import ManifestDB  # noqa: E402
from models.config import load_config  # noqa: E402


def main(argv=None, ask=getpass.getpass) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--which", choices=("login", "admin", "both"), default="both")
    p.add_argument("--only-missing", action="store_true",
                   help="ask only for the passwords that have no hash yet (installer)")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    which = ["login", "admin"] if a.which == "both" else [a.which]
    if a.only_missing:
        which = [w for w in which if w in ps.missing(cfg.dashboard)]
    if not which:
        print("Both passwords are already set.")
        return 0
    try:
        db = ManifestDB.from_config(cfg)
    except Exception:  # noqa: BLE001 - history is best effort when the DB cannot be opened
        db = None
    try:
        done = ps.set_passwords(a.config, which, ask, db=db)
    except ps.PasswordSetupError as e:
        print(f"Not changed: {e}")
        return 1
    finally:
        if db is not None:
            db.close()
    print("Password(s) set: " + ", ".join(done) + ". Everyone signed in with an old one is signed out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
