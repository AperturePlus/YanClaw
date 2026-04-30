from __future__ import annotations

import pytest

from agents.crawler.fetchers import FetchResult

curl_cffi = pytest.importorskip("curl_cffi", reason="curl_cffi not installed")

from agents.crawler.fetchers.curl_cffi_fetcher import CurlCffiFetcher


def test_curl_cffi_fetcher_has_same_interface_as_fetcher():
    """CurlCffiFetcher exposes the same public API as Fetcher."""
    assert hasattr(CurlCffiFetcher, "fetch")
    assert hasattr(CurlCffiFetcher, "filter_same_domain")
    assert hasattr(CurlCffiFetcher, "__aenter__")
    assert hasattr(CurlCffiFetcher, "__aexit__")


def test_curl_cffi_fetcher_filter_same_domain():
    links = [
        "https://cs.pku.edu.cn/people",
        "https://www.pku.edu.cn/about",
        "https://www.baidu.com/",
    ]
    assert CurlCffiFetcher.filter_same_domain(links, "https://www.pku.edu.cn/") == [
        "https://cs.pku.edu.cn/people",
        "https://www.pku.edu.cn/about",
    ]


def test_curl_cffi_fetcher_constructor_defaults():
    fetcher = CurlCffiFetcher()
    assert fetcher.request_interval_seconds == 2.0
    assert fetcher.max_retries == 3
    assert fetcher._impersonate == "chrome131"


def test_curl_cffi_fetcher_accepts_custom_impersonate():
    fetcher = CurlCffiFetcher(impersonate="safari18_0")
    assert fetcher._impersonate == "safari18_0"
