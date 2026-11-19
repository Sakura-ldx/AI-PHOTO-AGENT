from __future__ import annotations

from app import launcher
from app.launcher import frontend_url


def test_frontend_url_uses_loopback_for_wildcard_host():
    assert frontend_url("0.0.0.0", 8000) == "http://127.0.0.1:8000/"
    assert frontend_url("::", 8000) == "http://127.0.0.1:8000/"


def test_frontend_url_formats_ipv6_loopback():
    assert frontend_url("::1", 8000) == "http://[::1]:8000/"


def test_open_when_ready_opens_browser(monkeypatch):
    opened: list[str] = []

    monkeypatch.setattr(launcher, "wait_for_http", lambda url, timeout_seconds: True)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url))

    launcher.open_when_ready("http://127.0.0.1:8000/", 1)

    assert opened == ["http://127.0.0.1:8000/"]
