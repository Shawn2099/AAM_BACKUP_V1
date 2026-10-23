r"""Install-time checks that belong to a first install, run by 04_check_readiness.ps1 (plan INSTALL I-3/I-4/I-14, R-1).

  .venv\Scripts\python.exe deploy\install_checks.py C:\AAMBackup\config.yaml

Prints one line per check, "OK|WARN|FAIL <check>: <detail>", and exits 1 when any line is FAIL. It changes
nothing. These checks are deliberately NOT part of AppConfig validation: a validator that raises would stop the
services of an existing site on upgrade (and every 1 April), whereas these are install-time questions.

  * Windows clock zone equals schedule.timezone (a clean Server 2016 defaults to Pacific time: the Windows Update
    Active Hours and every robocopy log time would then be wrong).
  * The Google key named in the config exists and is a service-account key (cloud leg on).
  * The source year folder is the current financial year (an install in the wrong year would start a rollover
    straight away); a pending April rollover on a site that already has run history is only a warning.
  * WoL target: same host as the NAS path, no documentation placeholders (NAS leg on and WoL on).
"""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

Line = tuple[str, str, str]



def check_timezone(tz_name: str, now_utc: datetime | None = None, local_offset=None) -> Line:
    """Windows' own clock zone must give the same UTC offset as schedule.timezone, now AND six months from now
    (two zones with the same offset today can switch summer time on different dates)."""
    from zoneinfo import ZoneInfo

    now_utc = now_utc or datetime.now(timezone.utc)
    for when in (now_utc, now_utc + timedelta(days=182)):
        want = when.astimezone(ZoneInfo(tz_name)).utcoffset()
        have = local_offset if local_offset is not None else when.astimezone().utcoffset()
        if want != have:
            return ("FAIL", "Time zone",
                    f"Windows is set to UTC{_fmt(have)} but schedule.timezone {tz_name} is UTC{_fmt(want)}"
                    f"{' (summer-time rules differ)' if when is not now_utc else ''}. Fix: set the Windows time zone "
                    f'of the office (India: tzutil /s "India Standard Time"), then w32tm /resync')
    return "OK", "Time zone", f"Windows clock zone matches schedule.timezone {tz_name}"


def _fmt(delta) -> str:
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def check_gcs_key(key_path: str) -> Line:
    if not key_path:
        return "FAIL", "Google key", "paths.gcs_key_path is empty but the cloud backup is on"
    p = Path(key_path)
    if not p.is_file():
        return "FAIL", "Google key", f"{key_path} does not exist (copy the downloaded .json key there)"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return "FAIL", "Google key", f"{key_path} is not readable JSON ({type(e).__name__})"
    if not isinstance(data, dict) or data.get("type") != "service_account":
        return "FAIL", "Google key", f"{key_path} is JSON but not a service-account key (no type: service_account)"
    return "OK", "Google key", f"{key_path} is a service-account key"


def check_financial_year(source: str, today: date | None = None, has_history: bool = False) -> Line:
    from core.fy_rollover import CLOCK_BEHIND, FORWARD, JUMP, NONE, _fy_name, classify_rollover
    from core.time_utils import get_fy_prefix

    configured = _fy_name(source)
    if configured is None:
        return "FAIL", "Financial year", f"{source} does not end in a year folder like FY26-27"
    current = get_fy_prefix(today)
    kind = classify_rollover(configured, current)
    if kind == NONE:
        return "OK", "Financial year", f"{configured} is the current financial year"
    if kind == CLOCK_BEHIND:
        return ("FAIL", "Financial year",
                f"the server date says {current}, EARLIER than the source folder {configured}: fix the server clock")
    if kind == JUMP:
        return ("FAIL", "Financial year",
                f"the source folder is {configured} but today is in {current} (more than one year ahead): use the "
                f"current year's folder")
    assert kind == FORWARD
    if has_history:
        return ("WARN", "Financial year",
                f"the source folder is {configured}, today is in {current}: the yearly switch is due and will run "
                "on its own at the next 06:00 check")
    return ("FAIL", "Financial year",
            f"the source folder is {configured} but today is in {current}. A new install must start on the "
            f"current year's folder (create it and point source_drive / lan_destination at it)")


from core.wol_target import PLACEHOLDER_IP, PLACEHOLDER_MAC, _unc_host, check_wol_target  # noqa: E402,F401


def _has_history(db_path: str) -> bool:
    p = Path(db_path)
    if not p.is_file():
        return False
    try:
        con = sqlite3.connect(f"file:{quote(p.as_posix())}?mode=ro", uri=True, timeout=5)
        try:
            return con.execute("SELECT COUNT(*) FROM run_history").fetchone()[0] > 0
        finally:
            con.close()
    except sqlite3.Error:
        return False


def run_checks(config_path: str) -> list[Line]:
    from models.config import load_config

    config = load_config(config_path)
    out: list[Line] = [check_timezone(config.schedule.timezone)]
    if config.cloud.enabled:
        out.append(check_gcs_key(config.paths.gcs_key_path))
    out.append(check_financial_year(config.paths.source_drive,
                                    has_history=_has_history(config.paths.database_path)))
    out.extend(check_wol_target(config.lan.enabled, config.wol.enabled, config.paths.lan_destination,
                                config.wol.server_ip, config.wol.mac_address))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    try:
        lines = run_checks(argv[1])
    except Exception as e:  # noqa: BLE001 - reported as one FAIL line
        lines = [("FAIL", "install checks", f"{type(e).__name__}: {' '.join(str(e).split())}")]
    for status, name, detail in lines:
        print(f"{status} {name}: {' '.join(detail.split())}")
    return 1 if any(s == "FAIL" for s, _, _ in lines) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
