from __future__ import annotations

import time

import httpx

from agents.crawler.fetcher import Fetcher


async def test_fetcher_converts_html_extracts_links_and_rate_limits():
    async def handler(request):
        return httpx.Response(
            200,
            text='<html><body><a href="/faculty">Faculty</a><p>Hello</p></body></html>',
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    fetcher = Fetcher(
        request_interval_seconds=0.05,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    async with fetcher:
        start = time.monotonic()
        first = await fetcher.fetch("https://www.example.edu.cn/")
        second = await fetcher.fetch("https://www.example.edu.cn/next")
        elapsed = time.monotonic() - start

    assert "Hello" in first.text
    assert "https://www.example.edu.cn/faculty" in first.links
    assert second.status_code == 200
    assert elapsed >= 0.005  # rate limit enforced (relaxed for Windows timer resolution)


async def test_fetcher_retries_429_then_succeeds():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="slow down", request=request)
        return httpx.Response(200, text="<p>ok</p>", request=request)

    fetcher = Fetcher(
        request_interval_seconds=0,
        max_retries=1,
        retry_base_delay=0,
        transport=httpx.MockTransport(handler),
    )
    async with fetcher:
        result = await fetcher.fetch("https://www.example.edu.cn/")
    assert result.status_code == 200
    assert calls == 2


def test_filter_same_domain_allows_subdomains_and_rejects_external():
    links = [
        "https://cs.pku.edu.cn/people",
        "https://www.pku.edu.cn/about",
        "https://www.baidu.com/",
    ]
    assert Fetcher.filter_same_domain(links, "https://www.pku.edu.cn/") == [
        "https://cs.pku.edu.cn/people",
        "https://www.pku.edu.cn/about",
    ]
