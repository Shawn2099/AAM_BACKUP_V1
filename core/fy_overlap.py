"""FY overlap building blocks (plan docs/PLAN_FY_OVERLAP_2026-11-20.md, section 3 and 5.1; task OV-2.2).

Pure decisions about which financial-year folders are backed up in one flow run. Nothing here
writes anything; the only reads are the manifest DB passed in. The clock is never read implicitly
except by `run_date()` (taken ONCE per flow run and handed to every function here).

Words (plan section 3): configured FY = leaf of paths.source_drive; next FY = the FY after it;
calendar FY = get_fy_prefix(date); in window = calendar FY is the configured one and the month is
between rollover.early_start_month and March; switch pending = calendar FY is the next FY and the
rollover journal has not finished its config_switch step; active set = [next FY, configured FY]
(new first = run order) when overlap is on, in window or switch pending, and the next FY has a
durable activation row, otherwise [configured FY].
"""

from __future__ import annotations

import re
from datetime import date as _date
from datetime import datetime

import pendulum

from core import rollover_journal as rj
from core.fy_rollover import (
    _child_path,
    _parent_path,
    configured_fy,
    effective_fy_prefix,
)
from core.time_utils import IST, get_fy_prefix
from models.config import AppConfig

CURRENT = "current"
EARLY = "early"

_CANONICAL_FY = re.compile(r"^FY(\d{2})-(\d{2})$")
_LAST_MONTH_OF_FY = 3        # March: the calendar month in which a financial year ends (April start)


def run_date(scheduled: datetime | None = None) -> pendulum.Date:
    """The IST date of a run: of `scheduled` when given (a late scheduled run keeps its own day),
    else of now. Naive datetimes are taken as IST."""
    if scheduled is None:
        return pendulum.now(IST).date()
    if scheduled.tzinfo is None:
        moment = pendulum.instance(scheduled, tz=IST)
    else:
        moment = pendulum.instance(scheduled).in_timezone(IST)
    return moment.date()


def next_fy(fy: str) -> str:
    """'FY26-27' -> 'FY27-28'; 'FY99-00' -> 'FY00-01'. ValueError for anything not canonical."""
    m = _CANONICAL_FY.match(fy.strip().upper()) if isinstance(fy, str) else None
    if not m:
        raise ValueError(f"not a financial-year name like 'FY26-27': {fy!r}")
    start, end = int(m.group(1)), int(m.group(2))
    if end != (start + 1) % 100:
        raise ValueError(f"not a consecutive financial-year name: {fy!r}")
    nxt = (start + 1) % 100
    return f"FY{nxt:02d}-{(nxt + 1) % 100:02d}"


def calendar_fy(date: _date) -> str:
    """The FY the calendar says for this date (April start)."""
    return get_fy_prefix(date)


def _configured(config: AppConfig) -> str | None:
    return configured_fy(config.paths.source_drive, config.paths.lan_destination)


def in_window(config: AppConfig, run_date: _date) -> bool:
    """Overlap on, the calendar year is still the configured one, and the month is early_start_month..March."""
    if not config.rollover.overlap_enabled:
        return False
    configured = _configured(config)
    if configured is None or calendar_fy(run_date) != configured:
        return False
    return config.rollover.early_start_month <= run_date.month <= _LAST_MONTH_OF_FY


def switch_pending(config: AppConfig, db, run_date: _date) -> bool:
    """The calendar has moved exactly one FY past the configured one and the config switch has not
    happened yet (no journal, or its config_switch step is not done). Open follow-up steps do not count.
    A jump (more than one FY) or a clock behind is never pending. Needs no clock proof."""
    if not config.rollover.overlap_enabled:
        return False
    configured = _configured(config)
    if configured is None:
        return False
    nxt = next_fy(configured)
    if calendar_fy(run_date) != nxt:
        return False
    journal = db.rollover_get(configured, nxt)
    switched = journal is not None and journal["steps"][rj.CONFIG_SWITCH]["state"] == rj.DONE
    return not switched


def active_folders(config: AppConfig, db, run_date: _date) -> list[str]:
    """FY folders backed up in this run, in run order (new FY first)."""
    configured = _configured(config)
    if configured is None:
        return [effective_fy_prefix(config.paths.source_drive)]
    if config.rollover.overlap_enabled and (in_window(config, run_date) or switch_pending(config, db, run_date)):
        nxt = next_fy(configured)
        if db.get_activation(nxt) is not None:
            return [nxt, configured]
    return [configured]


def folder_for(config: AppConfig, fy: str) -> str:
    """The source folder of that FY (the configured FY's is paths.source_drive itself)."""
    if fy == _configured(config):
        return config.paths.source_drive
    return _child_path(_parent_path(config.paths.source_drive), fy)


def folder_view(config: AppConfig, fy: str) -> AppConfig:
    """The config as seen by one FY's pipeline: source (and NAS folder when the LAN leg is on) point at
    that FY's folders. The configured FY gets the config itself; other FYs a deep copy (never mutates `config`)."""
    if fy == _configured(config):
        return config
    view = config.model_copy(deep=True)
    view.paths.source_drive = _child_path(_parent_path(config.paths.source_drive), fy)
    if config.lan.enabled:
        view.paths.lan_destination = _child_path(_parent_path(config.paths.lan_destination), fy)
    return view


def role_of(config: AppConfig, fy: str) -> str:
    """'current' for the configured FY, 'early' for the one backed up ahead of the switch."""
    return CURRENT if fy == _configured(config) else EARLY
