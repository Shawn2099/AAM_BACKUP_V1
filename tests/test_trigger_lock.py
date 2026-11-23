"""Manual starts are serialised: a double click or two tabs create exactly ONE flow run.

Bug (live, REVIEW_MANUAL_BUTTON_FLOWS finding 5): the trigger endpoints did check-then-create with no lock,
so two concurrent POSTs both passed the busy check and both created a run. Now one process-wide asyncio lock
is held from the busy check until the run is CREATED (arun_deployment(timeout=0) returned); a freshly created
run is SCHEDULED and due now, which the busy check counts, so the second request gets the normal 400
already_running. The wait for the run to finish happens outside the lock.

Endpoints are called as coroutines inside ONE event loop (the dashboard is one process / one loop).
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from prefect.client.schemas.objects import StateType
from starlette.requests import Request

import ui


def _req(query: str = "wait=false") -> Request:
    return Request({"type": "http", "query_string": query.encode(), "client": ("127.0.0.1", 1), "headers": []})


def _body(resp):
    return json.loads(resp.body)


class _World:
    """A fake Prefect: creating a run takes a moment, and from then on the busy check sees it."""

    def __init__(self, delay: float = 0.05, fail_first: bool = False):
        self.created: list[dict] = []
        self.delay = delay
        self.fail_first = fail_first

    async def arun(self, name, parameters=None, timeout=None):
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("API down")
        await asyncio.sleep(self.delay)
        self.created.append({"name": name, "timeout": timeout})
        state = SimpleNamespace(type=StateType.SCHEDULED, name="Scheduled", is_final=lambda: False)
        return SimpleNamespace(id=f"run-{len(self.created)}", state=state)

    async def is_running(self, leg):
        await asyncio.sleep(0.01)                      # a real API call yields to the loop
        return bool(self.created)


@pytest.fixture
def world(monkeypatch):
    w = _World()
    monkeypatch.setattr(ui, "_require_auth", lambda request: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_running", w.is_running)
    monkeypatch.setattr(ui, "_agent_serving", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "arun_deployment", w.arun)
    return w


def test_two_concurrent_cloud_clicks_create_exactly_one_run(world):
    async def go():
        return await asyncio.gather(ui.trigger_cloud(_req()), ui.trigger_cloud(_req()))

    a, b = asyncio.run(go())
    assert len(world.created) == 1
    codes = sorted([a.status_code, b.status_code])
    assert codes == [202, 400]
    refused = a if a.status_code == 400 else b
    assert _body(refused)["status"] == "already_running"


def test_a_cloud_and_a_lan_click_at_the_same_instant_also_serialise(world):
    async def go():
        return await asyncio.gather(ui.trigger_cloud(_req()), ui.trigger_lan(_req()))

    a, b = asyncio.run(go())
    assert len(world.created) == 1
    assert sorted([a.status_code, b.status_code]) == [202, 400]


def test_the_wait_for_the_run_is_outside_the_lock(world, monkeypatch):
    """The first request is still waiting for its run to finish; the second is refused at once (not queued
    behind the 180 s wait)."""
    release = None

    async def slow_wait(flow_run, timeout):
        await release.wait()
        return flow_run

    monkeypatch.setattr(ui, "_wait_for_run", slow_wait)

    async def go():
        nonlocal release
        release = asyncio.Event()
        first = asyncio.create_task(ui.trigger_cloud(_req("")))
        await asyncio.sleep(0.2)                       # run created, first is now in its (outside-lock) wait
        second = await asyncio.wait_for(ui.trigger_cloud(_req("")), timeout=2)
        release.set()
        return await first, second

    first, second = asyncio.run(go())
    assert second.status_code == 400 and _body(second)["status"] == "already_running"
    assert first.status_code == 202
    assert len(world.created) == 1


def test_a_request_after_the_first_finished_works(world):
    async def go():
        one = await ui.trigger_cloud(_req())
        world.created.clear()                          # the first run finished: Prefect shows nothing active
        two = await ui.trigger_cloud(_req())
        return one, two

    one, two = asyncio.run(go())
    assert one.status_code == 202 and two.status_code == 202
    assert len(world.created) == 1


def test_an_error_while_creating_releases_the_lock(world):
    world.fail_first = True

    async def go():
        one = await ui.trigger_cloud(_req())
        two = await asyncio.wait_for(ui.trigger_cloud(_req()), timeout=5)
        return one, two

    one, two = asyncio.run(go())
    assert one.status_code == 500 and _body(one)["status"] == "trigger_failed"
    assert two.status_code == 202
    assert len(world.created) == 1


def test_the_run_is_created_with_timeout_zero_and_the_bound_goes_to_the_wait(world, monkeypatch):
    waited = []

    async def fake_wait(flow_run, timeout):
        waited.append(timeout)
        return flow_run

    monkeypatch.setattr(ui, "_wait_for_run", fake_wait)
    asyncio.run(ui.trigger_cloud(_req("")))
    assert world.created[0]["timeout"] == 0
    assert waited == [ui._dash("trigger_wait_seconds")]


def test_two_concurrent_verify_clicks_create_exactly_one_run(world, monkeypatch):
    monkeypatch.setattr(ui, "_backup_first_question", lambda *a, **k: None)
    monkeypatch.setattr(ui, "_requested_fy", lambda request: None)
    # verify counts ITS audit run as active: any created run is "verifying"
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))

    async def verifying():
        return bool(world.created)

    monkeypatch.setattr(ui, "_is_verify_running", verifying)

    async def go():
        return await asyncio.gather(ui.trigger_verify("all", _req()), ui.trigger_verify("all", _req()))

    a, b = asyncio.run(go())
    assert len(world.created) == 1
    assert sorted([a.status_code, b.status_code]) == [202, 400]
