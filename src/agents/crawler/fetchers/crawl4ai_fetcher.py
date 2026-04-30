"""Crawl4ai-based fetcher that delegates to a self-hosted crawl4ai Docker service.

Requires a running crawl4ai container (see ``docker/docker-compose.yml``).
Configure via environment variables::

    YANCLAW_CRAWL4AI_BASE_URL  (default http://localhost:11235)
    YANCLAW_CRAWL4AI_API_TOKEN (optional, for Bearer auth)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from agents.crawler.fetchers.httpx_fetcher import (
    FetchResult,
    Fetcher,
    _detect_block_reason,
)
from runtime.logger import get_logger

_logger = get_logger("crawler.crawl4ai")


class Crawl4aiFetcher:
    """Async fetcher backed by a remote crawl4ai REST API.

    Shares the same ``FetchResult`` return type and ``filter_same_domain``
    static method as the httpx :class:`Fetcher` so callers can swap backends
    without changing downstream code.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:10086",
        api_token: str | None = None,
        request_interval_seconds: float = 2.0,
        max_retries: int = 3,
        timeout_seconds: float = 60.0,
        retry_base_delay: float = 1.0,
        cookies: list[dict] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.request_interval_seconds = request_interval_seconds
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._timeout_seconds = timeout_seconds
        self._cookies = cookies or []
        self._client: httpx.AsyncClient | None = None
        self._last_request_at: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> "Crawl4aiFetcher":
        headers: dict[str, str] = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        self._client = httpx.AsyncClient(
            timeout=self._timeout_seconds,
            headers=headers,
        )
        _logger.info("crawl4ai fetcher ready base_url=%s timeout=%.0fs", self.base_url, self._timeout_seconds)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        if self._client is None:
            async with self:
                return await self.fetch(url)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._wait_for_domain(url)
            try:
                result = await self._do_fetch(url)
                _logger.debug("crawl4ai ok url=%s status=%s block=%s", url, result.status_code, result.block_reason)
                return result
            except Exception as error:
                last_error = error
                if attempt < self.max_retries:
                    delay = self.retry_base_delay * (2 ** attempt)
                    _logger.warning("crawl4ai attempt %d/%d failed url=%s error=%s retrying in %.1fs", attempt + 1, self.max_retries + 1, url, error, delay)
                    await asyncio.sleep(delay)
                else:
                    _logger.error("crawl4ai exhausted retries url=%s error=%s", url, error)

        raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error

    async def _do_fetch(self, url: str) -> FetchResult:
        assert self._client is not None
        payload: dict[str, Any] = {
            "urls": [url],
            "browser_config": {
                "type": "BrowserConfig",
                "params": {"headless": True},
            },
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {
                    "cache_mode": "bypass",
                    "magic": True,
                },
            },
        }
        if self._cookies:
            import json as _json

            cookies_json = _json.dumps(self._cookies)
            payload["hooks"] = {
                "code": {
                    "on_page_context_created": (
                        "async def hook(page, context, **kwargs):\n"
                        "    import json\n"
                        f"    await context.add_cookies(json.loads('{cookies_json}'))\n"
                        "    return page"
                    ),
                },
                "timeout": 15,
            }
        response = await self._client.post(
            f"{self.base_url}/crawl",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        result = self._parse_result(data, url)
        return result

    @staticmethod
    def _parse_result(data: dict[str, Any], original_url: str) -> FetchResult:
        """Extract a FetchResult from the crawl4ai JSON response."""
        # Response shape: {"success": bool, "results": [{ ... }]}
        results = data.get("results") or data.get("result") or []
        if isinstance(results, dict):
            results = [results]
        if not results:
            return FetchResult(
                url=original_url, text="", links=[], status_code=0,
                block_reason="crawl4ai_empty_response",
            )

        item = results[0]
        success = item.get("success", data.get("success", False))
        status_code = item.get("status_code", 200 if success else 0)
        final_url = item.get("url", original_url)
        html = item.get("html", "")
        markdown = item.get("markdown", "")

        # crawl4ai v0.8.x may return markdown as a dict:
        #   {"raw_markdown": "...", "markdown_with_citations": "...", ...}
        if isinstance(markdown, dict):
            markdown = (
                markdown.get("raw_markdown")
                or markdown.get("fit_markdown")
                or next((v for v in markdown.values() if isinstance(v, str) and v), "")
            )
        if not isinstance(markdown, str):
            markdown = ""

        if isinstance(html, dict):
            html = html.get("cleaned_html") or html.get("raw_html") or ""
        if not isinstance(html, str):
            html = ""

        # Prefer markdown from crawl4ai (already cleaned); fall back to html→text.
        helper = Fetcher.__new__(Fetcher)
        text = markdown if markdown else helper._html_to_text(html) if html else ""

        # Extract links from raw HTML when available.
        links = helper._extract_links(html, final_url) if html else []

        block_reason = _detect_block_reason(
            status_code=status_code,
            body_text=html or text,
            headers=None,
        )
        if not success and not block_reason:
            error_msg = item.get("error_message") or item.get("error", "")
            if error_msg:
                block_reason = f"crawl4ai_error: {error_msg}"

        return FetchResult(
            url=final_url,
            text=text,
            links=links,
            status_code=status_code,
            block_reason=block_reason,
        )

    filter_same_domain = staticmethod(Fetcher.filter_same_domain)

    async def _wait_for_domain(self, url: str) -> None:
        domain = (urlparse(url).hostname or "").lower()
        lock = self._domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            previous = self._last_request_at.get(domain)
            if previous is not None:
                wait = self.request_interval_seconds - (time.monotonic() - previous)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at[domain] = time.monotonic()
