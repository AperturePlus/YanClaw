from __future__ import annotations

import pytest

from agents.crawler.fetcher import FetchResult

playwright = pytest.importorskip("playwright", reason="playwright not installed")


from agents.crawler.playwright_fetcher import PlaywrightFetcher


def test_playwright_fetcher_has_same_interface_as_fetcher():
    """PlaywrightFetcher exposes the same public API as Fetcher."""
    assert hasattr(PlaywrightFetcher, "fetch")
    assert hasattr(PlaywrightFetcher, "filter_same_domain")
    assert hasattr(PlaywrightFetcher, "__aenter__")
    assert hasattr(PlaywrightFetcher, "__aexit__")


def test_playwright_fetcher_filter_same_domain():
    links = [
        "https://cs.pku.edu.cn/people",
        "https://www.pku.edu.cn/about",
        "https://www.baidu.com/",
    ]
    assert PlaywrightFetcher.filter_same_domain(links, "https://www.pku.edu.cn/") == [
        "https://cs.pku.edu.cn/people",
        "https://www.pku.edu.cn/about",
    ]


def test_playwright_fetcher_constructor_defaults():
    fetcher = PlaywrightFetcher()
    assert fetcher.request_interval_seconds == 2.0
    assert fetcher.max_retries == 3
