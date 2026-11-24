"""Pure calendar generator for the livebox lanes (plan 2026-11-24, section 2, dc50265 semantics).

No I/O, no clock reads. Given a start date and a number of days it produces one `Day` per simulated
day: the scheduled events (box crons), the expected `run_history` rows per leg per FY pass, the
expected mails by class, and the activation / switch / overlap flags. Lane overrides (skip an event,
force a status) and outage windows (days where the whole server is down) are supported.

Box schedule (Indian time, all events naive local `datetime`):
    cloud 02:01 daily, rollover check 06:00 daily, Sunday audit 08:00, Monday weekly report 08:00,
    monthly report on the 1st 08:30, LAN 21:01 daily; status probes 09:00 and 23:30.

State machine (run in event order, so an outage that swallows the 06:00 check simply makes the
activation or the switch happen at the first check afterwards):
    - check on/after the activation day (MM-DD of `activate_on`, in the configured FY) and before the
      switch: next FY is activated (once). Needs `overlap_enabled`.
    - check on/after 1 April after the configured FY ended: switch (configured FY := next FY, overlap
      ended). Happens even when the overlap was never activated.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# box cron times (hour, minute)
CLOUD_AT = (2, 1)
ROLLOVER_AT = (6, 0)
WEEKLY_AT = (8, 0)
AUDIT_AT = (8, 0)
MONTHLY_AT = (8, 30)
STATUS_AM_AT = (9, 0)
LAN_AT = (21, 1)
STATUS_PM_AT = (23, 30)

# event kinds
CLOUD = "cloud"
LAN = "lan"
ROLLOVER_CHECK = "rollover_check"
AUDIT = "audit"
WEEKLY = "weekly_report"
MONTHLY = "monthly_report"
STATUS_AM = "status_0900"
STATUS_PM = "status_2330"
CATCHUP_PREFIX = "catchup_"

# mail classes
MAIL_ACTIVATED = "activated"
MAIL_ANNOUNCE = "announce"
MAIL_WEEKLY = "weekly"
MONTHLY_MAIL = "monthly"
MAIL_AUDIT = "audit"       # Sunday audit summary mail (INFO, never an action mail)
MAIL_PROBLEM = "problem"   # always 0 on a healthy lane
MAIL_ACTIVATION_WAITING = "activation_waiting"   # activation due but the clock proof is missing
MAIL_RESOLVED = "resolved"                       # cloud recovery mail
MAIL_ROLLOVER_BLOCKED = "rollover_blocked"       # the switch is blocked (clock unproven / final backup incomplete)

# rollover check outcomes
NO_ROLLOVER = "NO_ROLLOVER_NEEDED"
ROLLOVER_COMPLETED = "ROLLOVER_COMPLETED"
# activation outcomes
ACT_NOT_DUE = "NOT_DUE"
ACT_ACTIVATED = "ACTIVATED"
ACT_ALREADY = "ALREADY_ACTIVE"
ACT_ENDED = "ENDED"
ACT_OFF = "OVERLAP_DISABLED"
ACT_WAITING_CLOCK = "WAITING_FOR_CLOCK"   # activation due, but cloud down / clock unproven that day

CLOUD_OK = ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE")
LAN_OK = ("LAN_COMPLETE",)
AUDIT_OK = ("VERIFIED", "PENDING_BACKUP")
AUDIT_NEXT_NO_BACKUP = ("AUDIT_INCOMPLETE", "PENDING_BACKUP")
ROLLOVER_ROW_STATUS = "ROLLOVER_COMPLETE"
ROLLOVER_BLOCKED_STATUS = "ROLLOVER_BLOCKED"
FINAL_CLOUD_SUFFIX = "-rollover-final-cloud"     # the two backups of the closing FY that the rollover runs at 06:00
FINAL_LAN_SUFFIX = "-rollover-final-lan"


# ---------------------------------------------------------------- FY helpers (same as core.fy_overlap)
def start_year(fy: str) -> int:
    return 2000 + int(fy[2:4])


def next_fy(fy: str) -> str:
    a = int(fy[2:4]) + 1
    return f"FY{a % 100:02d}-{(a + 1) % 100:02d}"


def fy_of_date(d: dt.date) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"FY{y % 100:02d}-{(y + 1) % 100:02d}"


def activation_date(configured_fy: str, activate_on: str = "12-31") -> dt.date:
    month, day = int(activate_on[:2]), int(activate_on[3:])
    y = start_year(configured_fy)
    return dt.date(y if month >= 4 else y + 1, month, day)


def switch_date(configured_fy: str) -> dt.date:
    return dt.date(start_year(configured_fy) + 1, 4, 1)


# ---------------------------------------------------------------- data classes
@dataclass(frozen=True)
class Event:
    at: dt.datetime            # tz-aware IST
    kind: str                  # CLOUD, LAN, ROLLOVER_CHECK, ...; "catchup_<kind>" for catch-up runs
    fy: str | None = None      # FY of the pass for per-pass events

    @property
    def base_kind(self) -> str:
        return self.kind.removeprefix(CATCHUP_PREFIX)

    @property
    def is_catchup(self) -> bool:
        return self.kind.startswith(CATCHUP_PREFIX)


@dataclass(frozen=True)
class ExpectedRow:
    """One expected run_history row (or audit row)."""
    leg: str                   # "cloud" | "lan" | "audit_cloud" | "audit_lan" | "rollover"
    at: dt.datetime
    fy: str                    # FY prefix of the pass
    pass_kind: str             # "configured" | "next" | "single"
    run_suffix: str            # "" for the configured pass, "-FY91-92" for the next pass
    statuses: tuple[str, ...]  # allowed statuses
    order: int = 0             # 0 = first in the flow (C), 1 = second (N)

    @property
    def role(self) -> str:
        """'configured' or 'next' (a single/rollover pass counts as configured)."""
        return "next" if self.pass_kind == "next" else "configured"


@dataclass(frozen=True)
class ExpectedMail:
    mail_class: str
    count: int
    key: str | None = None     # e.g. "rollover:activated:FY91-92"
    at: dt.datetime | None = None
    recipients: str = "partners"
    fy: str | None = None      # FY the mail is about (activated: the next FY; announce: the new configured FY)
    severity: str | None = None   # notifications.kind the product must store ("WARNING", "ACTION_REQUIRED", ...)
    code: str | None = None       # fault code the mail must carry ("AAM-ROL-03", ...)

    @property
    def kind(self) -> str:
        return self.mail_class


@dataclass(frozen=True)
class Day:
    index: int
    date: dt.date
    weekday: int               # Mon=0
    configured_fy: str         # configured FY at start of day (before the 06:00 check)
    configured_fy_end: str     # configured FY at end of day
    next_fy_end: str | None    # active next FY at end of day (None when no overlap)
    events: tuple[Event, ...]
    rows: tuple[ExpectedRow, ...]
    mails: tuple[ExpectedMail, ...]
    rollover_result: str | None
    activation_result: str | None
    is_first_run: bool = False
    is_sunday: bool = False
    is_monday: bool = False
    is_first_of_month: bool = False
    is_leap_day: bool = False
    is_activation_day: bool = False
    is_switch_day: bool = False
    overlap_active_end: bool = False     # overlap active at end of day
    overlap_active_at_0900: bool = False
    cards_0900: int = 0
    cards_2330: int = 0
    outage: bool = False
    skipped: tuple[str, ...] = ()        # base kinds that did not run (override or outage)
    retention_boundary: bool = False     # set by `mark_retention`
    stale_ok: tuple[str, ...] = ()       # legs the watchdog may report overdue that day
    expect_raise: tuple[str, ...] = ()   # flows that must raise that day: "rollover" (blocked), "audit" (incomplete)

    @property
    def expected_rows(self) -> list[ExpectedRow]:
        """Backup rows only (cloud / lan legs), in flow order."""
        return [r for r in self.rows if r.leg in (CLOUD, LAN)]

    @property
    def expected_mails(self) -> list[ExpectedMail]:
        return list(self.mails)

    def rows_of(self, leg: str) -> list[ExpectedRow]:
        return [r for r in self.rows if r.leg == leg]

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def mail_count(self, mail_class: str) -> int:
        return sum(m.count for m in self.mails if m.mail_class == mail_class)

    @property
    def is_overlap_day(self) -> bool:
        """Counted in the plan's '183 overlap days': activation day through the last day before the switch."""
        return self.overlap_active_end


@dataclass(frozen=True)
class DayOverride:
    """Lane override for one date."""
    skip: frozenset[str] = frozenset()                   # base kinds that do not run
    statuses: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # leg -> allowed statuses
    extra_mails: tuple[ExpectedMail, ...] = ()
    note: str = ""
    cloud_down: bool = False                             # cloud unreachable all day
    clock_unproven: bool = False                         # clock proof missing: activation / switch must wait
    stale_ok: tuple[str, ...] = ()                       # legs the watchdog may report overdue (merged into Day.stale_ok)


def overrides_map(x) -> dict[dt.date, DayOverride]:
    """Normalise overrides: Mapping or iterable of (date | ISO str, DayOverride) pairs, or None."""
    if not x:
        return {}
    pairs = x.items() if isinstance(x, Mapping) else x
    out: dict[dt.date, DayOverride] = {}
    for k, v in pairs:
        out[k if isinstance(k, dt.date) else dt.date.fromisoformat(str(k))] = v
    return out


@dataclass(frozen=True)
class Outage:
    """Server down: every event whose instant falls in [start, end] does not run.

    start/end are dates (inclusive, whole days) or tz-aware/naive datetimes (inclusive instants).
    catchup: 'none' = missed events are simply lost; 'latest' = the newest missed cloud run and the
    newest missed LAN run run once at `resume_at` (hour, minute on the first up day, default 00:05).
    """
    start: dt.date | dt.datetime
    end: dt.date | dt.datetime
    catchup: str = "none"
    resume_at: tuple[int, int] = (0, 5)

    def covers(self, at: dt.datetime) -> bool:
        a = _as_dt(self.start, False)
        b = _as_dt(self.end, True)
        return a <= at <= b


def _as_dt(v: dt.date | dt.datetime, end: bool) -> dt.datetime:
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=IST)
    t = dt.time(23, 59, 59, 999999) if end else dt.time(0, 0)
    return dt.datetime.combine(v, t, tzinfo=IST)


def at_ist(d: dt.date, hm: tuple[int, int]) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=IST)


def _tuple_hm(hm: tuple[int, int]) -> tuple[int, int]:
    return hm


# ---------------------------------------------------------------- generator
def build_calendar(
    start: dt.date,
    days: int,
    *,
    configured_fy: str | None = None,
    activate_on: str = "12-31",
    overlap_enabled: bool = True,
    overrides: Mapping[dt.date, DayOverride] | None = None,
    outages: Iterable[Outage] = (),
    cloud_enabled: bool = True,
    lan_enabled: bool = True,
    first_run_no_changes_ok: bool = False,
) -> list[Day]:
    """Return the simulated days. Pure and deterministic."""
    if days < 0:
        raise ValueError("days must be >= 0")
    overrides = overrides_map(overrides)
    outages = tuple(outages)
    cfg = configured_fy or fy_of_date(start)
    nxt: str | None = None           # active next FY (overlap record open)
    activated_for: set[str] = set()  # next FYs ever activated (db_meta overlap:<fy>)
    pending_catchup: dict[str, dt.datetime] = {}   # base kind -> newest missed instant
    waited_for: set[str] = set()     # next FYs whose activation has been waiting (first mail sent)
    cloud_was_down = False
    out: list[Day] = []

    for i in range(days):
        d = start + dt.timedelta(days=i)
        ov = overrides.get(d, DayOverride())
        cfg_start = cfg
        sched: list[tuple[dt.datetime, str]] = [
            (at_ist(d, CLOUD_AT), CLOUD),
            (at_ist(d, ROLLOVER_AT), ROLLOVER_CHECK),
            (at_ist(d, STATUS_AM_AT), STATUS_AM),
            (at_ist(d, LAN_AT), LAN),
            (at_ist(d, STATUS_PM_AT), STATUS_PM),
        ]
        if d.weekday() == 6:
            sched.append((at_ist(d, AUDIT_AT), AUDIT))
        if d.weekday() == 0:
            sched.append((at_ist(d, WEEKLY_AT), WEEKLY))
        if d.day == 1:
            sched.append((at_ist(d, MONTHLY_AT), MONTHLY))
        sched.sort(key=lambda p: p[0])

        # catch-up events due at the end of an outage that ended on a previous day
        extra: list[tuple[dt.datetime, str]] = []
        for o in outages:
            if o.catchup == "latest" and _as_dt(o.end, True).date() == d - dt.timedelta(days=1):
                at = at_ist(d, o.resume_at)
                for k in (CLOUD, LAN):
                    if k in pending_catchup:
                        extra.append((at, CATCHUP_PREFIX + k))
                pending_catchup.clear()
        sched = sorted(sched + extra, key=lambda p: p[0])

        events: list[Event] = []
        rows: list[ExpectedRow] = []
        mails: list[ExpectedMail] = []
        skipped: list[str] = []
        rollover_result: str | None = None
        activation_result: str | None = None
        activated_today = False
        switched_today = False
        overlap_at_0900 = False
        cards_am = 0
        in_outage_day = False
        cloud_down = ov.cloud_down and cloud_enabled
        expect_raise: list[str] = []

        def statuses_for(leg: str, default: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(ov.statuses.get(leg, default))

        for at, kind in sched:
            base = kind.removeprefix(CATCHUP_PREFIX)
            is_catchup = kind.startswith(CATCHUP_PREFIX)
            down = (not is_catchup) and any(o.covers(at) for o in outages)
            if down:
                in_outage_day = True
            if down or (not is_catchup and base in ov.skip):
                skipped.append(base)
                if down and base in (CLOUD, LAN):
                    pending_catchup[base] = at
                continue

            if base == CLOUD and cloud_enabled or base == LAN and lan_enabled:
                is_first = (i == 0 and base == CLOUD) or (i == 0 and base == LAN)
                default = (LAN_OK if base == LAN
                           else (("CLOUD_COMPLETE",) if is_first and not first_run_no_changes_ok else CLOUD_OK))
                missed = is_catchup and base == CLOUD and not cloud_down
                if base == CLOUD and cloud_down:
                    default = ("CLOUD_FAILED",)
                elif missed:
                    default = ("CLOUD_MISSED",)            # a catch-up run is late: the run budget is used up
                default = statuses_for(base, default)
                fys: list[tuple[str, str, str]] = [(cfg, "configured", "")]
                if nxt is not None:                        # both FY passes run (and fail) while the overlap is active
                    fys.append((nxt, "next", "-" + nxt))
                if len(fys) == 1:
                    fys = [(cfg, "single", "")]
                for order, (fy, pk, suf) in enumerate(fys):
                    rows.append(ExpectedRow(base, at, fy, pk, suf, default, order))
                    events.append(Event(at, kind, fy))
                if base == CLOUD:
                    if cloud_down:
                        for _ in fys:                      # one problem mail per FY pass
                            mails.append(ExpectedMail(MAIL_PROBLEM, 1, at=at))
                    elif missed:
                        mails.append(ExpectedMail(MAIL_PROBLEM, 1, "missed:cloud", at, severity="WARNING",
                                                  code="AAM-SYS-07"))
                        for _ in fys[1:]:
                            mails.append(ExpectedMail(MAIL_PROBLEM, 1, at=at))
                    elif cloud_was_down:
                        mails.append(ExpectedMail(MAIL_RESOLVED, 1, at=at))
                    cloud_was_down = cloud_down or missed
            elif base in (CLOUD, LAN):
                skipped.append(base)
            elif base == ROLLOVER_CHECK:
                events.append(Event(at, kind))
                # switch first (it ends the overlap), then activation
                sw = switch_date(cfg)
                if d >= sw and (ov.clock_unproven or cloud_down):
                    # the switch is blocked (AAM-ROL-03 clock unproven, or AAM-ROL-02 final cloud backup incomplete):
                    # no config change, no archive, the flow raises; the next day without override switches
                    rows.append(ExpectedRow("rollover", at, cfg, "single", "", (ROLLOVER_BLOCKED_STATUS,), 0))
                    expect_raise.append("rollover")
                    activation_result = ACT_ALREADY if nxt is not None else ACT_NOT_DUE
                    if ov.clock_unproven:
                        mails.append(ExpectedMail(MAIL_ROLLOVER_BLOCKED, 1, "rollover:clock_unverified", at, fy=cfg,
                                                  severity="ACTION_REQUIRED", code="AAM-ROL-03"))
                    else:
                        if cloud_enabled:
                            rows.append(ExpectedRow(CLOUD, at, cfg, "single", FINAL_CLOUD_SUFFIX, ("CLOUD_FAILED",), 0))
                        if lan_enabled:
                            rows.append(ExpectedRow(LAN, at, cfg, "single", FINAL_LAN_SUFFIX, LAN_OK, 0))
                        mails.append(ExpectedMail(MAIL_ROLLOVER_BLOCKED, 1, "blocked:rollover", at, fy=cfg,
                                                  severity="ACTION_REQUIRED", code="AAM-ROL-02"))
                elif d >= sw:
                    new_cfg = next_fy(cfg)
                    rollover_result = ROLLOVER_COMPLETED
                    activation_result = ACT_ENDED if nxt is not None else ACT_NOT_DUE
                    if cloud_enabled:                      # the two final backups of the closing FY (06:00)
                        rows.append(ExpectedRow(CLOUD, at, cfg, "single", FINAL_CLOUD_SUFFIX, CLOUD_OK, 0))
                    if lan_enabled:
                        rows.append(ExpectedRow(LAN, at, cfg, "single", FINAL_LAN_SUFFIX, LAN_OK, 0))
                    rows.append(ExpectedRow("rollover", at, cfg, "single", "", (ROLLOVER_ROW_STATUS,), 0))
                    mails.append(ExpectedMail(MAIL_ANNOUNCE, 1, f"rollover:done:{new_cfg}", at, fy=new_cfg))
                    cfg, nxt = new_cfg, None
                    switched_today = True
                else:
                    rollover_result = NO_ROLLOVER
                    if not overlap_enabled:
                        activation_result = ACT_OFF
                    elif nxt is not None:
                        activation_result = ACT_ALREADY
                    elif (d >= activation_date(cfg, activate_on) and next_fy(cfg) not in activated_for
                          and (ov.cloud_down or ov.clock_unproven)):
                        fy_n = next_fy(cfg)
                        activation_result = ACT_WAITING_CLOCK
                        mails.append(ExpectedMail(MAIL_ACTIVATION_WAITING, 1, f"rollover:activation:{fy_n}", at,
                                                  fy=fy_n,
                                                  severity="ACTION_REQUIRED" if fy_n in waited_for else "WARNING"))
                        waited_for.add(fy_n)
                    elif d >= activation_date(cfg, activate_on) and next_fy(cfg) not in activated_for:
                        nxt = next_fy(cfg)
                        activated_for.add(nxt)
                        activation_result = ACT_ACTIVATED
                        activated_today = True
                        mails.append(ExpectedMail(MAIL_ACTIVATED, 1, f"rollover:activated:{nxt}", at, fy=nxt))
                    else:
                        activation_result = ACT_NOT_DUE
            elif base == STATUS_AM:
                events.append(Event(at, kind))
                overlap_at_0900 = nxt is not None
                cards_am = _cards(cloud_enabled, lan_enabled, nxt is not None)
            elif base == STATUS_PM:
                events.append(Event(at, kind))
            elif base == AUDIT:
                events.append(Event(at, kind))
                for fy, pk, suf in ([(cfg, "configured", "")] + ([(nxt, "next", "-" + nxt)] if nxt else [])):
                    # E1: activated earlier today, no backup of the next FY yet -> never VERIFIED
                    ok = AUDIT_NEXT_NO_BACKUP if (pk == "next" and activated_today) else AUDIT_OK
                    if cloud_enabled:
                        cok = ("AUDIT_INCOMPLETE",) if cloud_down else ok    # the cloud cannot be reached
                        rows.append(ExpectedRow("audit_cloud", at, fy, pk, suf, statuses_for("audit", cok)))
                    if lan_enabled:
                        rows.append(ExpectedRow("audit_lan", at, fy, pk, suf, statuses_for("audit", ok)))
                if cloud_down:                             # AAM-AUD-02 "safety check could not run" + the flow raises
                    mails.append(ExpectedMail(MAIL_PROBLEM, 1, at=at, severity="WARNING", code="AAM-AUD-02"))
                    expect_raise.append("audit")
            elif base == WEEKLY:
                events.append(Event(at, kind))
                mails.append(ExpectedMail(MAIL_WEEKLY, 1, None, at))
            elif base == MONTHLY:
                events.append(Event(at, kind))
                mails.append(ExpectedMail(MONTHLY_MAIL, 1, None, at))

        mails.extend(ov.extra_mails)
        out.append(Day(
            index=i, date=d, weekday=d.weekday(),
            configured_fy=cfg_start, configured_fy_end=cfg, next_fy_end=nxt,
            events=tuple(events), rows=tuple(rows), mails=tuple(mails),
            rollover_result=rollover_result, activation_result=activation_result,
            is_first_run=(i == 0),
            is_sunday=d.weekday() == 6, is_monday=d.weekday() == 0,
            is_first_of_month=d.day == 1,
            is_leap_day=(d.month, d.day) == (2, 29),
            is_activation_day=activated_today, is_switch_day=switched_today,
            overlap_active_end=nxt is not None,
            overlap_active_at_0900=overlap_at_0900,
            cards_0900=cards_am,
            cards_2330=_cards(cloud_enabled, lan_enabled, nxt is not None),
            outage=in_outage_day, skipped=tuple(skipped),
            stale_ok=tuple(dict.fromkeys(
                ((CLOUD,) + ((f"cloud:{nxt}",) if nxt else ()) if cloud_down else ()) + tuple(ov.stale_ok))),
            expect_raise=tuple(dict.fromkeys(expect_raise)),
        ))
    return out


def _cards(cloud: bool, lan: bool, overlap: bool) -> int:
    return (int(cloud) + int(lan)) * (2 if overlap else 1)


# ---------------------------------------------------------------- presets and helpers
LANE_A_START = dt.date(2090, 12, 1)
LANE_A_DAYS = 728


def lane_a(**kw) -> list[Day]:
    """Lane A: 728 days from Fri 2090-12-01 (day 727 = Thu 2092-11-27), healthy."""
    return build_calendar(LANE_A_START, LANE_A_DAYS, **kw)


def mark_retention(days: list[Day], retention_days: int) -> list[Day]:
    """Flag every day with index > retention_days (day 91 for 90 days: first day a 90-day boundary bites)."""
    return [dataclasses.replace(d, retention_boundary=d.index > retention_days) for d in days]


def retention_boundary_date(start: dt.date, retention_days: int) -> dt.date:
    return start + dt.timedelta(days=retention_days + 1)


def totals(days: Iterable[Day]) -> dict[str, int]:
    """Whole-lane totals (plan section 2): mails by class, rows by leg, flag counts."""
    t: dict[str, int] = {
        "days": 0, "sundays": 0, "mondays": 0, "firsts": 0, "overlap_days": 0,
        "activations": 0, "switches": 0, "leap_days": 0, "problem_mails": 0,
        MAIL_ACTIVATED: 0, MAIL_ANNOUNCE: 0, MAIL_WEEKLY: 0, MONTHLY_MAIL: 0,
    }
    for d in days:
        t["days"] += 1
        t["sundays"] += d.is_sunday
        t["mondays"] += d.is_monday
        t["firsts"] += d.is_first_of_month
        t["overlap_days"] += d.overlap_active_end
        t["activations"] += d.is_activation_day
        t["switches"] += d.is_switch_day
        t["leap_days"] += d.is_leap_day
        for m in d.mails:
            t[m.mail_class] = t.get(m.mail_class, 0) + m.count
        for r in d.rows:
            t["rows_" + r.leg] = t.get("rows_" + r.leg, 0) + 1
    t["problem_mails"] = t.get(MAIL_PROBLEM, 0)
    return t


def day_of(days: list[Day], date: dt.date) -> Day:
    for d in days:
        if d.date == date:
            return d
    raise KeyError(date)
