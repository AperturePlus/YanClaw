from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.crawler.cookies import (
    clear_cookies,
    cookie_path,
    cookies_to_httpx,
    list_cookie_files,
    load_cookies,
    save_cookies,
)

_SAMPLE_COOKIES = [
    {"name": "session", "value": "abc123", "domain": ".scu.edu.cn", "path": "/"},
    {"name": "_ts", "value": "xyz", "domain": ".scu.edu.cn", "path": "/"},
]


def test_save_and_load_cookies(tmp_path: Path):
    url = "https://www.scu.edu.cn/"
    path = save_cookies(url, _SAMPLE_COOKIES, cookies_dir=tmp_path)
    assert path.exists()
    loaded = load_cookies(url, cookies_dir=tmp_path)
    assert len(loaded) == 2
    assert loaded[0]["name"] == "session"


def test_load_cookies_returns_empty_when_no_file(tmp_path: Path):
    assert load_cookies("https://www.example.com/", cookies_dir=tmp_path) == []


def test_clear_cookies(tmp_path: Path):
    url = "https://www.scu.edu.cn/"
    save_cookies(url, _SAMPLE_COOKIES, cookies_dir=tmp_path)
    assert clear_cookies(url, cookies_dir=tmp_path) is True
    assert clear_cookies(url, cookies_dir=tmp_path) is False
    assert load_cookies(url, cookies_dir=tmp_path) == []


def test_list_cookie_files(tmp_path: Path):
    save_cookies("https://www.scu.edu.cn/", _SAMPLE_COOKIES, cookies_dir=tmp_path)
    save_cookies("https://www.pku.edu.cn/", [_SAMPLE_COOKIES[0]], cookies_dir=tmp_path)
    entries = list_cookie_files(cookies_dir=tmp_path)
    assert len(entries) == 2
    domains = {e[0] for e in entries}
    assert "scu.edu.cn" in domains
    assert "pku.edu.cn" in domains


def test_cookies_to_httpx():
    result = cookies_to_httpx(_SAMPLE_COOKIES)
    assert result == {"session": "abc123", "_ts": "xyz"}


def test_cookies_to_httpx_skips_incomplete():
    cookies = [{"name": "ok", "value": "v"}, {"name": "bad"}, {"other": "x"}]
    assert cookies_to_httpx(cookies) == {"ok": "v"}


def test_cookie_path_uses_site_root():
    path = cookie_path("https://cs.scu.edu.cn/faculty")
    assert path.name == "scu.edu.cn.json"


def test_save_cookies_creates_directory(tmp_path: Path):
    nested = tmp_path / "a" / "b"
    save_cookies("https://www.scu.edu.cn/", _SAMPLE_COOKIES, cookies_dir=nested)
    assert (nested / "scu.edu.cn.json").exists()
