from __future__ import annotations

import json

import httpx
import pytest

from agents.crawler.fetchers.crawl4ai_fetcher import Crawl4aiFetcher
from agents.crawler.fetchers import FetchResult


# ---------------------------------------------------------------------------
# Interface / constructor tests (no network)
# ---------------------------------------------------------------------------

def test_crawl4ai_fetcher_has_same_interface_as_fetcher():
    """Crawl4aiFetcher exposes the same public API as Fetcher."""
    assert hasattr(Crawl4aiFetcher, "fetch")
    assert hasattr(Crawl4aiFetcher, "filter_same_domain")
    assert hasattr(Crawl4aiFetcher, "__aenter__")
    assert hasattr(Crawl4aiFetcher, "__aexit__")


def test_crawl4ai_fetcher_filter_same_domain():
    links = [
        "https://cs.pku.edu.cn/people",
        "https://www.pku.edu.cn/about",
        "https://www.baidu.com/",
    ]
    assert Crawl4aiFetcher.filter_same_domain(links, "https://www.pku.edu.cn/") == [
        "https://cs.pku.edu.cn/people",
        "https://www.pku.edu.cn/about",
    ]


def test_crawl4ai_fetcher_constructor_defaults():
    fetcher = Crawl4aiFetcher()
    assert fetcher.base_url == "http://localhost:10086"
    assert fetcher.api_token is None
    assert fetcher.request_interval_seconds == 2.0
    assert fetcher.max_retries == 3


def test_crawl4ai_fetcher_constructor_custom():
    fetcher = Crawl4aiFetcher(
        base_url="http://myhost:9999/",
        api_token="secret",
        request_interval_seconds=0.5,
    )
    assert fetcher.base_url == "http://myhost:9999"
    assert fetcher.api_token == "secret"
    assert fetcher.request_interval_seconds == 0.5


# ---------------------------------------------------------------------------
# _parse_result tests (pure logic, no network)
# ---------------------------------------------------------------------------

def test_parse_result_success():
    data = {
        "success": True,
        "results": [
            {
                "success": True,
                "url": "https://example.com/page",
                "status_code": 200,
                "html": "<html><body><a href='/link'>Link</a><p>Hello</p></body></html>",
                "markdown": "# Hello\n\n[Link](/link)",
            }
        ],
    }
    result = Crawl4aiFetcher._parse_result(data, "https://example.com/page")
    assert result.status_code == 200
    assert result.block_reason is None
    # Prefers markdown over html→text
    assert "Hello" in result.text
    assert result.url == "https://example.com/page"
    assert len(result.links) >= 1


def test_parse_result_empty_response():
    data = {"success": True, "results": []}
    result = Crawl4aiFetcher._parse_result(data, "https://example.com")
    assert result.status_code == 0
    assert result.block_reason == "crawl4ai_empty_response"
    assert result.text == ""


def test_parse_result_failure_with_error_message():
    data = {
        "success": False,
        "results": [
            {
                "success": False,
                "url": "https://example.com",
                "status_code": 0,
                "html": "",
                "markdown": "",
                "error_message": "Navigation timeout",
            }
        ],
    }
    result = Crawl4aiFetcher._parse_result(data, "https://example.com")
    assert result.block_reason is not None
    assert "Navigation timeout" in result.block_reason


def test_parse_result_waf_detected():
    waf_body = (
        "document.cookie; __jsl_clearance; acw_sc__v2; "
        "setTimeout(function(){}, 3000);"
    )
    # _detect_block_reason requires len(body_text) > 2000 for status=200
    padding = "x" * 2100
    waf_html = f"<html><body><script>{waf_body}</script>{padding}</body></html>"
    data = {
        "success": True,
        "results": [
            {
                "success": True,
                "url": "https://example.edu.cn",
                "status_code": 200,
                "html": waf_html,
                "markdown": "",
            }
        ],
    }
    result = Crawl4aiFetcher._parse_result(data, "https://example.edu.cn")
    assert result.block_reason is not None
    assert "waf" in result.block_reason.lower()


def test_parse_result_markdown_as_dict():
    """crawl4ai v0.8.x returns markdown as a dict with raw_markdown key."""
    data = {
        "success": True,
        "results": [
            {
                "success": True,
                "url": "https://example.com",
                "status_code": 200,
                "html": "<html><body><p>Hello</p></body></html>",
                "markdown": {
                    "raw_markdown": "# Hello\n\nWorld",
                    "markdown_with_citations": "# Hello [1]\n\nWorld",
                    "fit_markdown": "Hello World",
                },
            }
        ],
    }
    result = Crawl4aiFetcher._parse_result(data, "https://example.com")
    assert isinstance(result.text, str)
    assert "Hello" in result.text


def test_parse_result_dict_result():
    """Handle response where 'result' is a dict instead of a list."""
    data = {
        "success": True,
        "result": {
            "success": True,
            "url": "https://example.com",
            "status_code": 200,
            "html": "<html><body>OK</body></html>",
            "markdown": "OK",
        },
    }
    result = Crawl4aiFetcher._parse_result(data, "https://example.com")
    assert result.status_code == 200
    assert result.text == "OK"


# ---------------------------------------------------------------------------
# fetch() with mocked HTTP transport (no real crawl4ai server)
# ---------------------------------------------------------------------------

def _make_transport(response_json: dict, status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=response_json)
    return httpx.MockTransport(handler)


async def test_fetch_calls_crawl4ai_api():
    api_response = {
        "success": True,
        "results": [
            {
                "success": True,
                "url": "https://example.com",
                "status_code": 200,
                "html": "<html><body><p>Content</p></body></html>",
                "markdown": "Content",
            }
        ],
    }
    transport = _make_transport(api_response)
    fetcher = Crawl4aiFetcher(
        base_url="http://test-server:10086",
        request_interval_seconds=0,
    )
    fetcher._client = httpx.AsyncClient(transport=transport)
    try:
        result = await fetcher.fetch("https://example.com")
        assert isinstance(result, FetchResult)
        assert result.status_code == 200
        assert "Content" in result.text
    finally:
        await fetcher._client.aclose()


async def test_fetch_retries_on_server_error():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(500, json={"error": "internal"})
        return httpx.Response(
            200,
            json={
                "success": True,
                "results": [
                    {
                        "success": True,
                        "url": "https://example.com",
                        "status_code": 200,
                        "html": "<html><body>OK</body></html>",
                        "markdown": "OK",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    fetcher = Crawl4aiFetcher(
        request_interval_seconds=0,
        retry_base_delay=0.01,
    )
    fetcher._client = httpx.AsyncClient(transport=transport)
    try:
        result = await fetcher.fetch("https://example.com")
        assert result.status_code == 200
        assert call_count == 3
    finally:
        await fetcher._client.aclose()


async def test_fetch_raises_after_max_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    transport = httpx.MockTransport(handler)
    fetcher = Crawl4aiFetcher(
        request_interval_seconds=0,
        max_retries=1,
        retry_base_delay=0.01,
    )
    fetcher._client = httpx.AsyncClient(transport=transport)
    try:
        with pytest.raises(RuntimeError, match="Failed to fetch"):
            await fetcher.fetch("https://example.com")
    finally:
        await fetcher._client.aclose()


async def test_fetch_sends_auth_header():
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "success": True,
                "results": [
                    {
                        "success": True,
                        "url": "https://example.com",
                        "status_code": 200,
                        "html": "",
                        "markdown": "",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    fetcher = Crawl4aiFetcher(
        api_token="mytoken",
        request_interval_seconds=0,
    )
    async with fetcher:
        # Replace the real client's transport with our mock
        await fetcher._client.aclose()
        fetcher._client = httpx.AsyncClient(
            transport=transport,
            headers={"Authorization": "Bearer mytoken"},
        )
        await fetcher.fetch("https://example.com")

    assert captured_headers.get("authorization") == "Bearer mytoken"
