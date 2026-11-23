"""Manual-only legs: the dashboard page and the launch reconcile (owner 2026-11-23, LAN and cloud)."""
from unittest.mock import MagicMock, patch

import pytest

import manual_leg_cases as c
from test_m3_dashboard_truth import client, db  # noqa: F401
from test_support import ui_login


def _page(client, monkeypatch, cfg):
    import ui
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    return client.get("/", headers={**ui_login.HEADERS, "Accept": "text/html"}).text


@pytest.mark.parametrize("leg,label", [("lan", "LAN"), ("cloud", "Cloud")])
def test_manual_only_card_has_no_next_scheduled_line(client, monkeypatch, leg, label):  # noqa: F811
    html = _page(client, monkeypatch, c.cfg_for(leg))
    assert f"{label}: manual only" in html
    assert html.count("Next scheduled backup") == 1       # only the other (nightly) leg


@pytest.mark.parametrize("leg,label", [("lan", "LAN"), ("cloud", "Cloud")])
def test_off_card_says_off_and_has_no_next_scheduled_line(client, monkeypatch, leg, label):  # noqa: F811
    html = _page(client, monkeypatch, c.cfg_for(leg, enabled=False))
    assert f"{label}: off" in html and html.count("Next scheduled backup") == 1


@pytest.mark.parametrize("leg", ["lan", "cloud"])
def test_nightly_card_keeps_the_schedule_line(client, monkeypatch, leg):  # noqa: F811
    html = _page(client, monkeypatch, c.cfg_for(leg, scheduled=True))
    assert html.count("Next scheduled backup") == 2


@pytest.mark.parametrize("leg,dep", [("lan", "backup-lan"), ("cloud", "backup-cloud")])
def test_reconcile_removes_leftover_schedules_of_a_manual_leg(leg, dep):
    from launch import _reconcile_disabled_legs
    removed = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def read_deployment_by_name(self, name):
            m = MagicMock()
            m.paused = False
            m.id = name
            return m

        async def read_deployment_schedules(self, dep_id):
            s = MagicMock()
            s.id = "sch-1"
            return [s]

        async def delete_deployment_schedule(self, dep_id, sch_id):
            removed.append((dep_id, sch_id))

        async def pause_deployment(self, dep_id):
            pass

        async def resume_deployment(self, dep_id):
            pass

    with patch("prefect.client.orchestration.get_client", return_value=Client()):
        res = _reconcile_disabled_legs(c.cfg_for(leg))
    assert res[dep].startswith("manual only (1 schedule(s) removed)")
    assert [r for r in removed if r[0].endswith(dep)] != []
    other = "backup-cloud" if leg == "lan" else "backup-lan"
    assert not res[other].startswith("manual only")
