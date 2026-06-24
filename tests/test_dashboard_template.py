"""Tests for templates/dashboard.py — pure render function."""

from templates.dashboard import render_dashboard, CSS, JS


class TestRenderDashboard:
    def test_returns_html_string(self):
        result = render_dashboard()
        assert isinstance(result, str)
        assert "<!DOCTYPE html>" in result

    def test_contains_css(self):
        result = render_dashboard()
        assert "<style>" in result

    def test_contains_js(self):
        result = render_dashboard()
        assert "<script>" in result

    def test_contains_title(self):
        result = render_dashboard()
        assert "AAM Backup Dashboard" in result

    def test_displays_fy_prefix(self):
        result = render_dashboard(fy_prefix="FY26-27")
        assert "FY26-27" in result

    def test_displays_flash_message(self):
        result = render_dashboard(flash_html='<div class="flash success">OK</div>')
        assert "flash success" in result

    def test_shows_logout_link_when_auth_enabled(self):
        result = render_dashboard(auth_enabled=True)
        assert "/logout" in result

    def test_hides_logout_link_when_auth_disabled(self):
        result = render_dashboard(auth_enabled=False)
        assert "/logout" not in result

    def test_css_and_js_constants_exist(self):
        assert len(CSS) > 100
        assert len(JS) > 100
        assert "<style>" in CSS
        assert "<script>" in JS

