"""Harness: SimClock pins loguru's tz lookup so a broken post-2038 local offset (Windows Server 2016 CRT,
tm_gmtoff = days=-49711) cannot crash aware_now() under the fake clock. Works for loguru 0.7.x and 0.8.x."""
import datetime as dt
import time
import types

import loguru._datetime as ld
import pytest

from tools.livebox import rig


class _FakeTraveller:
    def start(self):
        return types.SimpleNamespace(move_to=lambda *a, **k: None)

    def stop(self):
        pass


def _clock():
    c = rig.SimClock.__new__(rig.SimClock)
    c._tm = types.SimpleNamespace(travel=lambda *a, **k: _FakeTraveller())
    c._traveller = c._coords = None
    c.now = None
    return c


def _bad_localtime():
    real = time.localtime

    def bad(ts=None):
        r = real(ts)
        return types.SimpleNamespace(tm_gmtoff=-49711 * 86400 + 82904, tm_zone="IST", **{
            k: getattr(r, k) for k in ("tm_year", "tm_mon", "tm_mday", "tm_hour", "tm_min", "tm_sec")})

    return bad


def _snapshot():
    return {n: getattr(ld, n, None) for n in ("_get_tzinfo", "_fallback_tzinfo", "localtime")}


def _run(monkeypatch):
    bad = _bad_localtime()
    monkeypatch.setattr(ld, "localtime", bad)
    if hasattr(ld, "_fallback_tzinfo"):
        monkeypatch.setattr(ld, "_fallback_tzinfo",
                            lambda ts: dt.timezone(dt.timedelta(days=-49711, seconds=82904)))
    with pytest.raises(ValueError):
        ld.aware_now()
    before = _snapshot()
    c = _clock()
    c.start(dt.datetime(2090, 12, 25, 9, 0))
    try:
        assert abs(ld.aware_now().utcoffset()) < dt.timedelta(hours=24)
    finally:
        c.stop()
    assert _snapshot() == before


def test_aware_now_survives_bad_gmtoff_and_is_restored(monkeypatch):
    _run(monkeypatch)


def test_aware_now_survives_on_loguru_07_shape(monkeypatch):
    """0.7.x has no _get_tzinfo/_fallback_tzinfo: only localtime is patched, nothing raises AttributeError."""
    for n in ("_get_tzinfo", "_fallback_tzinfo"):
        if hasattr(ld, n):
            monkeypatch.delattr(ld, n)

    def aware_now_07():  # verbatim shape of loguru 0.7.3
        now = ld.datetime_.now()
        local = ld.localtime(now.timestamp())
        tzinfo = ld.timezone(ld.timedelta(seconds=local.tm_gmtoff), local.tm_zone)
        return ld.datetime.combine(now.date(), now.time().replace(tzinfo=tzinfo))

    monkeypatch.setattr(ld, "aware_now", aware_now_07)
    _run(monkeypatch)
    assert not hasattr(ld, "_get_tzinfo")
