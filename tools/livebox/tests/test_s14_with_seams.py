"""S14 (production slot read) must work while the rig seams (clock-proof fake replacing http.client connections)
are installed. Bug seen on the box: AttributeError: type object '_GoogleDateConn' has no attribute 'debuglevel'."""
from __future__ import annotations

import http.server
import importlib
import threading

import pytest

from tools.livebox.tests.test_review_fixes_safety import (FakeRig, _hooks, _prep_preflight, _set_url, _stub_checks,
                                                          safety)


def rl():
    return importlib.import_module("tools.livebox.run_lane")


def rigmod():
    return importlib.import_module("tools.livebox.rig")


def _serve(body: bytes, ctype: str):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/api"


@pytest.fixture
def json_api():
    srv, url = _serve(b'{"limit":1,"active_slots":0}', "application/json")
    yield url
    srv.shutdown()


@pytest.fixture
def html_api():
    srv, url = _serve(b"<!doctype html><html>dashboard</html>", "text/html")
    yield url
    srv.shutdown()


@pytest.fixture
def seams():
    h = rigmod().install_google_date()
    yield h
    h.uninstall()


def test_fetch_prod_slot_reads_json_with_clock_proof_seam_installed(seams, json_api):
    assert rl().fetch_prod_slot(json_api) == {"active": 0, "limit": 1}


def test_fetch_prod_slot_html_gives_none_and_not_json_reason_with_seam(seams, html_api):
    r = rl()
    assert r.fetch_prod_slot(html_api) is None
    assert "not JSON" in r.last_error


def test_google_date_fake_has_the_http_client_class_attributes(seams):
    import http.client

    assert http.client.HTTPSConnection.debuglevel == 0


def test_post_phase_preflight_s1_to_s14_completes_with_seams_and_prod_api(tmp_path, monkeypatch, seams, json_api):
    h = _hooks(tmp_path, monkeypatch)
    _stub_checks(monkeypatch, real=("check_production_slot",))
    rig = FakeRig(h.lane_root, api_url="http://127.0.0.1:5000/api")
    rig.seams._installed = False
    _prep_preflight(h, monkeypatch, rig)
    _set_url(monkeypatch, "http://127.0.0.1:5000/api")
    h.prod_api = json_api
    h._safety_preflight("pre")
    h._safety_preflight("post")
    rules = {r.rule: r.status for r in h.safety_results}
    assert rules.get("S14") == "ok", rules
    assert not [r for r in h.safety_results if r.status == "refuse"]
