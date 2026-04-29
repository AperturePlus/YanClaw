from __future__ import annotations

from agents.crawler import playwright_setup


def test_ensure_chromium_installed_skips_when_present(monkeypatch):
    called = {"run": 0}

    monkeypatch.setattr(playwright_setup, "chromium_installed", lambda: True)

    def _run(*args, **kwargs):
        called["run"] += 1
        return None

    monkeypatch.setattr(playwright_setup.subprocess, "run", _run)

    installed_now = playwright_setup.ensure_chromium_installed()
    assert installed_now is False
    assert called["run"] == 0


def test_ensure_chromium_installed_runs_installer_when_missing(monkeypatch):
    calls: list[tuple[list[str], bool]] = []

    monkeypatch.setattr(playwright_setup, "chromium_installed", lambda: False)

    def _run(args, check):
        calls.append((list(args), bool(check)))
        return None

    monkeypatch.setattr(playwright_setup.subprocess, "run", _run)

    installed_now = playwright_setup.ensure_chromium_installed()
    assert installed_now is True
    assert calls == [([playwright_setup.sys.executable, "-m", "playwright", "install", "chromium"], True)]
