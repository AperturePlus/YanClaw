from __future__ import annotations

import pytest

from agents.crawler.fetcher import FetchResult
from agents.crawler.hybrid_fetcher import HybridFetcher


class _FakeBackend:
    def __init__(self, result: FetchResult | None = None, *, raise_error: bool = False) -> None:
        self._result = result
        self._raise_error = raise_error
        self.calls: list[str] = []

    async def __aenter__(self) -> "_FakeBackend":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if self._raise_error:
            raise RuntimeError("backend failed")
        if self._result is None:
            raise RuntimeError("missing result")
        return self._result


class _AlwaysErrorBackend:
    def __init__(self, message: str) -> None:
        self.message = message
        self.calls: list[str] = []

    async def __aenter__(self) -> "_AlwaysErrorBackend":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        raise RuntimeError(self.message)


@pytest.mark.asyncio
async def test_hybrid_fetcher_fallbacks_to_next_backend_on_block():
    blocked = FetchResult(
        url="https://www.example.edu.cn/",
        text="",
        links=[],
        status_code=202,
        block_reason="waf_challenge",
    )
    usable = FetchResult(
        url="https://www.example.edu.cn/",
        text="faculty page",
        links=["https://www.example.edu.cn/faculty"],
        status_code=200,
    )
    httpx_backend = _FakeBackend(blocked)
    curl_backend = _FakeBackend(usable)

    fetcher = HybridFetcher(
        backend_order=("httpx", "curl_cffi"),
        backend_factories={
            "httpx": lambda: httpx_backend,
            "curl_cffi": lambda: curl_backend,
        },
    )
    async with fetcher:
        result = await fetcher.fetch("https://www.example.edu.cn/")

    assert result == usable
    assert httpx_backend.calls == ["https://www.example.edu.cn/"]
    assert curl_backend.calls == ["https://www.example.edu.cn/"]


@pytest.mark.asyncio
async def test_hybrid_fetcher_pins_preferred_backend_per_host():
    blocked = FetchResult(
        url="https://www.example.edu.cn/",
        text="",
        links=[],
        status_code=202,
        block_reason="waf_challenge",
    )
    usable = FetchResult(
        url="https://www.example.edu.cn/",
        text="faculty page",
        links=["https://www.example.edu.cn/faculty"],
        status_code=200,
    )
    httpx_backend = _FakeBackend(blocked)
    curl_backend = _FakeBackend(usable)

    fetcher = HybridFetcher(
        backend_order=("httpx", "curl_cffi"),
        backend_factories={
            "httpx": lambda: httpx_backend,
            "curl_cffi": lambda: curl_backend,
        },
    )
    async with fetcher:
        result1 = await fetcher.fetch("https://www.example.edu.cn/a")
        result2 = await fetcher.fetch("https://www.example.edu.cn/b")

    assert result1 == usable
    assert result2 == usable
    # First request tries httpx then curl; second request should go directly to pinned curl.
    assert httpx_backend.calls == ["https://www.example.edu.cn/a"]
    assert curl_backend.calls == ["https://www.example.edu.cn/a", "https://www.example.edu.cn/b"]


@pytest.mark.asyncio
async def test_hybrid_fetcher_raises_when_no_backends_available():
    def _missing() -> _FakeBackend:
        raise ImportError("missing dependency")

    fetcher = HybridFetcher(
        backend_order=("httpx",),
        backend_factories={"httpx": _missing},
    )
    with pytest.raises(RuntimeError, match="no available backend"):
        async with fetcher:
            pass


@pytest.mark.asyncio
async def test_hybrid_fetcher_disables_crawl4ai_after_service_error():
    crawl4ai_backend = _AlwaysErrorBackend("500 Internal Server Error for /crawl")
    usable = FetchResult(
        url="https://www.example.edu.cn/",
        text="faculty page",
        links=["https://www.example.edu.cn/faculty"],
        status_code=200,
    )
    httpx_backend = _FakeBackend(usable)

    fetcher = HybridFetcher(
        backend_order=("crawl4ai", "httpx"),
        backend_factories={
            "crawl4ai": lambda: crawl4ai_backend,
            "httpx": lambda: httpx_backend,
        },
    )

    async with fetcher:
        result1 = await fetcher.fetch("https://www.example.edu.cn/a")
        result2 = await fetcher.fetch("https://www.scu.edu.cn/zzjg/")

    assert result1 == usable
    assert result2 == usable
    # crawl4ai should be attempted once, then disabled for the remainder of this run.
    assert crawl4ai_backend.calls == ["https://www.example.edu.cn/a"]
    assert httpx_backend.calls == [
        "https://www.example.edu.cn/a",
        "https://www.scu.edu.cn/zzjg/",
    ]
