"""Playwright-based fetcher for JS-rendered pages.

Use as a drop-in alternative to the httpx ``Fetcher`` when target sites
require JavaScript execution to render content.

Requires the ``playwright`` optional dependency::

    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import time
from typing import Iterable

from agents.crawler.fetcher import (
    FetchResult,
    Fetcher,
    _detect_block_reason,
    _is_html_content,
    _site_root,
)

try:
    from playwright.async_api import Browser, BrowserContext, async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment,misc]


class PlaywrightFetcher:
    """Async fetcher backed by a headless Chromium browser.

    Shares the same ``FetchResult`` return type and ``filter_same_domain``
    static method as the httpx :class:`Fetcher` so callers can swap backends
    without changing downstream code.
    """

    def __init__(
        self,
        *,
        request_interval_seconds: float = 2.0,
        max_retries: int = 3,
        timeout_seconds: float = 30.0,
        retry_base_delay: float = 1.0,
    ) -> None:
        if async_playwright is None:
            raise ImportError(
                "playwright is required for PlaywrightFetcher. "
                "Install dependencies, then run: uv run yanclaw-install-chromium"
            )
        self.request_interval_seconds = request_interval_seconds
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._timeout_ms = int(timeout_seconds * 1000)
        self._pw: object | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._last_request_at: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> "PlaywrightFetcher":
        pw = await async_playwright().start()
        self._pw = pw
        self._browser = await pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw and hasattr(self._pw, "stop"):
            await self._pw.stop()

    async def fetch(self, url: str) -> FetchResult:
        if self._context is None:
            async with self:
                return await self.fetch(url)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._wait_for_domain(url)
            try:
                page = await self._context.new_page()
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
                    status = resp.status if resp else 0

                    if status in {429, 503} and attempt < self.max_retries:
                        await asyncio.sleep(self.retry_base_delay * (2**attempt))
                        continue

                    content_type = (resp.headers.get("content-type", "") if resp else "")
                    final_url = page.url

                    if not _is_html_content(content_type):
                        return FetchResult(url=final_url, text="", links=[], status_code=status)

                    html = await page.content()
                    block_reason = _detect_block_reason(
                        status_code=status,
                        body_text=html,
                        headers=resp.headers if resp else None,
                    )
                    # Reuse Fetcher's HTML->text and link extraction logic.
                    fetcher_helper = Fetcher.__new__(Fetcher)
                    text = fetcher_helper._html_to_text(html)
                    links = fetcher_helper._extract_links(html, final_url)
                    return FetchResult(
                        url=final_url,
                        text=text,
                        links=links,
                        status_code=status,
                        block_reason=block_reason,
                    )
                finally:
                    await page.close()
            except Exception as error:
                last_error = error
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(self.retry_base_delay * (2**attempt))

        raise RuntimeError(f"Failed to fetch {url}") from last_error

    filter_same_domain = staticmethod(Fetcher.filter_same_domain)

    async def _wait_for_domain(self, url: str) -> None:
        from urllib.parse import urlparse

        domain = (urlparse(url).hostname or "").lower()
        lock = self._domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            previous = self._last_request_at.get(domain)
            if previous is not None:
                wait = self.request_interval_seconds - (time.monotonic() - previous)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at[domain] = time.monotonic()
