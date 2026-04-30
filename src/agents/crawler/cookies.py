"""Cookie storage for per-university WAF bypass.

Cookies are stored as JSON files under ``data/cookies/{domain}.json``.
Each file contains a list of Playwright-compatible cookie dicts::

    [{"name": "foo", "value": "bar", "domain": ".example.edu.cn", "path": "/"}]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agents.crawler.fetchers import _site_root

CookieList = list[dict[str, Any]]

_DEFAULT_DIR = Path("data/cookies")


def _domain_key(start_url: str) -> str:
    host = (urlparse(start_url).hostname or "").lower()
    return _site_root(host) or host


def cookie_path(start_url: str, cookies_dir: Path = _DEFAULT_DIR) -> Path:
    return cookies_dir / f"{_domain_key(start_url)}.json"


def load_cookies(start_url: str, cookies_dir: Path = _DEFAULT_DIR) -> CookieList:
    """Return cookies for *start_url*'s domain, or ``[]`` if none stored."""
    path = cookie_path(start_url, cookies_dir)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    return data


def save_cookies(
    start_url: str,
    cookies: CookieList,
    cookies_dir: Path = _DEFAULT_DIR,
) -> Path:
    """Persist *cookies* for *start_url*'s domain. Returns the file path."""
    cookies_dir.mkdir(parents=True, exist_ok=True)
    path = cookie_path(start_url, cookies_dir)
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_cookies(start_url: str, cookies_dir: Path = _DEFAULT_DIR) -> bool:
    """Delete stored cookies for *start_url*'s domain. Returns True if file existed."""
    path = cookie_path(start_url, cookies_dir)
    if path.exists():
        path.unlink()
        return True
    return False


def list_cookie_files(cookies_dir: Path = _DEFAULT_DIR) -> list[tuple[str, int]]:
    """Return ``[(domain, cookie_count), ...]`` for all stored cookie files."""
    if not cookies_dir.exists():
        return []
    result: list[tuple[str, int]] = []
    for path in sorted(cookies_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            count = len(data) if isinstance(data, list) else 0
        except (json.JSONDecodeError, OSError):
            count = 0
        result.append((path.stem, count))
    return result


def cookies_to_httpx(cookies: CookieList) -> dict[str, str]:
    """Convert Playwright-format cookies to a simple ``{name: value}`` dict for httpx/curl_cffi."""
    return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
