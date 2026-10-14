"""Fake-clock seam for the audit lab (WAVE 12).

Why this is safe and sufficient:

* Exactly two production modules read the wall clock themselves, and both do it
  through a module-level ``import pendulum``:
      core/time_utils.py  -> pendulum.now(IST)          (now_iso, cutoff_iso,
                                                        now_formatted, get_fy_prefix)
      core/health.py      -> pendulum.now("UTC")        (check_clock_skew)
* Everything else in the system reaches time through those helpers
  (``flow`` imports ``now_iso``/``get_fy_prefix``, ``core/manifest.py`` imports
  ``cutoff_iso``/``now_iso``, ``core/fy_rollover.py`` imports
  ``get_fy_prefix``). Because the helpers resolve ``pendulum`` from their own
  module globals at CALL time, replacing the module attribute moves the clock
  for every one of those callers at once.
* Prefect's internals imported pendulum into their own module objects, so
  scheduling/timing is unaffected - only our code's notion of "now" moves.

The shim returns a REAL ``pendulum.DateTime`` built at the fixed instant, so
``.subtract(days=...)``, ``.isoformat()``, ``.format()``, ``.date()`` and
``.in_timezone()`` all keep working normally. Every other pendulum attribute is
delegated to the real module.

No production file is edited; this is a lab-only seam, exactly like
``labconf.patch_cloud_modules_to_local`` and ``labconf.strip_zb``.
"""
from __future__ import annotations

import contextlib
import importlib

import pendulum

#: the production modules whose `pendulum` reference this seam replaces
CLOCK_MODULES = ("core.time_utils", "core.health")

#: the deployment timezone (matches core.time_utils.IST)
IST = pendulum.timezone("Asia/Kolkata")

# set while a fake_clock context is active, so tests can read the same instant
# the production code sees (a test module's own `import pendulum` is NOT patched)
_ACTIVE: "_FrozenPendulum | None" = None


class _FrozenPendulum:
    """A pendulum stand-in whose ``now()`` returns a controlled instant."""

    def __init__(self, at: "pendulum.DateTime", tick: float | None = None):
        object.__setattr__(self, "_at", at)
        object.__setattr__(self, "_tick", tick)

    def now(self, tz=None):
        cur = self._at
        tick = self._tick
        if tick:
            # advance on every read so distinct events get distinct timestamps
            # (Prefect's own task_run_state table has a UNIQUE(task_run_id,
            # timestamp) constraint that an exactly-frozen clock violates)
            object.__setattr__(self, "_at", cur.add(seconds=tick))
        if tz is None:
            return cur
        return cur.in_timezone(tz)

    def __getattr__(self, name):
        # anything other than `now` behaves exactly like the real pendulum
        return getattr(pendulum, name)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<lab frozen pendulum at {self._at!r}>"


def parse_at(at) -> "pendulum.DateTime":
    """Accept a DateTime, an ISO-8601 string (with or without offset) or a date."""
    if isinstance(at, pendulum.DateTime):
        return at
    s = str(at)
    if len(s) == 10 and s.count("-") == 2:            # bare date YYYY-MM-DD
        return pendulum.parse(s + "T00:00:00", tz="Asia/Kolkata")
    dt = pendulum.parse(s)
    if dt.timezone is None:                           # naive -> read as IST
        dt = pendulum.parse(s, tz="Asia/Kolkata")
    return dt


@contextlib.contextmanager
def fake_clock(at, *, modules: tuple[str, ...] = CLOCK_MODULES, tick: float | None = None):
    """Freeze the wall clock as seen by the production code.

    Usage:
        with fake_clock("2027-04-01T00:00:00+05:30") as frozen:
            get_fy_prefix()        # -> 'FY27-28'
            flow.backup(...)       # records run_history at the frozen instant

    ``tick`` (seconds) advances the returned instant on every read, so a long
    run still gets distinct timestamps while the DATE never moves. Use a tiny
    tick when driving code that persists its own state (Prefect's task_run_state
    table has UNIQUE(task_run_id, timestamp)).
    """
    global _ACTIVE
    frozen = parse_at(at)
    shim = _FrozenPendulum(frozen, tick)
    saved = {}
    for name in modules:
        mod = importlib.import_module(name)
        saved[name] = getattr(mod, "pendulum", None)
        mod.pendulum = shim
    previous = _ACTIVE
    _ACTIVE = shim
    try:
        yield frozen
    finally:
        _ACTIVE = previous
        for name, original in saved.items():
            mod = importlib.import_module(name)
            if original is None:
                try:
                    delattr(mod, "pendulum")
                except AttributeError:
                    pass
            else:
                mod.pendulum = original


# set while a fake_clock context is active, so tests can read the same instant
# the production code sees (a test module's own `import pendulum` is NOT patched)
_ACTIVE: "_FrozenPendulum | None" = None


# set while a fake_clock context is active, so tests can read the same instant
# the production code sees (a test module's own `import pendulum` is NOT patched)
_ACTIVE: "_FrozenPendulum | None" = None


def now(tz=IST) -> "pendulum.DateTime":
    """The current instant as the PRODUCTION code sees it.

    Outside a fake_clock() block this is the real clock; inside one it is the
    frozen instant. Test code must use this instead of pendulum.now() so that
    generated fixtures (DB rows, log names, ...) agree with the code under test.
    """
    if _ACTIVE is None:
        return pendulum.now(tz)
    return _ACTIVE.now(tz)


def iso_now() -> str:
    """now() as an IST ISO-8601 string (what now_iso() would return)."""
    return now().isoformat()


def install_google_date(monkeypatch, skew_seconds: float = 0.0) -> None:
    """Make core.health.check_clock_skew() see a Date header from the frozen clock.

    Without this, check_clock_skew() performs a REAL HTTPS call to
    www.googleapis.com and compares Google's time with our frozen clock - which
    legitimately fails with "System clock skew detected" and aborts the flow.
    Simulating "Google agrees with our clock" keeps the production check running
    for real (no monkeypatched check_clock_skew) while making it deterministic.

    Pass skew_seconds to simulate a genuinely skewed server (+/- seconds).
    """
    import email.utils

    import core.health as health

    class _Resp:
        def getheader(self, _name):
            return email.utils.formatdate(
                now("UTC").add(seconds=skew_seconds).int_timestamp, usegmt=True)

    class _GoogleDateConn:
        def __init__(self, _host, timeout=None):
            self.timeout = timeout

        def request(self, *_a, **_k):
            pass

        def getresponse(self):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(health.http.client, "HTTPSConnection", _GoogleDateConn)
