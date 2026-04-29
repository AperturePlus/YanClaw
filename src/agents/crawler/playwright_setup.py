from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def chromium_executable_path() -> Path | None:
    """Return Chromium executable path resolved by Playwright, if available."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    with sync_playwright() as p:
        path = p.chromium.executable_path
    if not path:
        return None
    return Path(path)


def chromium_installed() -> bool:
    path = chromium_executable_path()
    return bool(path and path.exists())


def ensure_chromium_installed() -> bool:
    """Ensure Playwright Chromium browser binary exists.

    Returns True if installation was performed in this call.
    """
    if chromium_installed():
        return False
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
    )
    return True


def main() -> None:
    """CLI entrypoint to install Chromium binary required by PlaywrightFetcher."""
    ensure_chromium_installed()
