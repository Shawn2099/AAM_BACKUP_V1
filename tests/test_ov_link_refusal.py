"""LC2a: an early FY folder that is a junction / symlink / file is a refusal the owner must SEE at once
(attention item + one ACTION_REQUIRED mail), not one INFO log line. The configured FY keeps running."""

import os
import subprocess

import pendulum
import pytest

from core import attention
from core import fy_overlap as ov
from core.faults import CATALOG as FAULTS
from tests.test_ov2_activation import (  # noqa: F401 - fixtures
    CFG_FY, NEXT, RUN_DATE, T0, Clock, Mail, _run, env, frozen)


def _junction(env):
    target = env.tmp / "elsewhere"
    target.mkdir()
    link = env.src / NEXT
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("the OS refused to create a junction")
    return link, target


def _cleanup(link):
    if os.path.lexists(link):
        os.rmdir(link)        # a junction is removed without touching its target


def _deferred_item(env, now):
    items = attention.compute_attention(env.db, env.cfg, now)
    return [i for i in items if i["code"] == f"overlap:deferred:{NEXT}"]


def test_junction_refusal_mails_once_at_once_and_shows_an_item(env, frozen):
    if os.name != "nt":
        pytest.skip("junctions are Windows-only")
    frozen(T0)
    link, target = _junction(env)
    try:
        res, mail = _run(env)
        assert res["state"] == "deferred"
        # one ACTION_REQUIRED mail on the very first refusal, naming the folder, with a registered fault code
        assert mail.keys() == [f"overlap:activation:{NEXT}"]
        ev = mail.events[0]
        assert ev.kind == "ACTION_REQUIRED" and ev.fault_code in FAULTS
        assert str(link) in ev.message and "not being backed up" in ev.message
        # a second run in the same hour: no second mail (repeat spacing is alerts.stale_repeat_hours)
        again, _ = _run(env, mail=mail)
        assert again["state"] == "deferred" and len(mail.events) == 1
        # the dashboard shows it (red once it has been mailed)
        items = _deferred_item(env, T0)
        assert len(items) == 1 and items[0]["level"] == attention.RED
        assert NEXT in items[0]["message"]
        # nothing written through the link, the configured FY still the only active folder
        assert os.listdir(target) == []
        assert ov.active_folders(env.cfg, env.db, RUN_DATE) == [CFG_FY]
    finally:
        _cleanup(link)


def test_junction_gone_activates_and_sends_one_resolved(env, frozen):
    if os.name != "nt":
        pytest.skip("junctions are Windows-only")
    frozen(T0)
    link, _target = _junction(env)
    mail = Mail()
    try:
        _run(env, mail=mail)
    finally:
        _cleanup(link)
    frozen(T0.add(hours=1))
    res, _ = _run(env, mail=mail)
    assert res["state"] == "activated"
    assert [e.kind for e in mail.events] == ["ACTION_REQUIRED", "INFO", "RECOVERED"]
    assert _deferred_item(env, T0.add(hours=1)) == []


def test_a_normal_folder_gives_no_alert_and_no_item(env, frozen):
    frozen(T0)
    (env.src / NEXT).mkdir()
    res, mail = _run(env)
    assert res["state"] == "activated"
    assert [e.kind for e in mail.events] == ["INFO"]
    assert _deferred_item(env, T0) == []


def test_a_file_with_the_name_is_also_mailed_at_once(env, frozen):
    frozen(T0)
    (env.src / NEXT).write_text("not a folder")
    res, mail = _run(env)
    assert res["state"] == "deferred"
    assert [e.kind for e in mail.events] == ["ACTION_REQUIRED"]


def test_a_clock_deferral_still_waits_for_the_alert_hours(env, frozen):
    """The immediate mail is only for causes a human must fix; a clock that may fix itself keeps the wait."""
    from core.health import CLOCK_UNVERIFIED
    frozen(T0)
    res, mail = _run(env, Clock(CLOCK_UNVERIFIED, "no answer"))
    assert res["state"] == "deferred" and mail.events == []


_ = pendulum  # (kept for the frozen fixture's module)
