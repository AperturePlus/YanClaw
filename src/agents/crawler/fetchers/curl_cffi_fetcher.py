"""curl_cffi-based fetcher with browser TLS fingerprint impersonation.

Use as a drop-in alternative to the httpx ``Fetcher`` when target sites
block requests based on TLS fingerprinting (JA3/JA4).

Requires the ``curl_cffi`` package::

    pip install curl_cffi
"""

from __future__ import annotations

import asyncio
import time
from typing import Iterable
from urllib.parse import urlparse

from agents.crawler.fetchers.httpx_fetcher import (
    FetchResult,
    Fetcher,
    _detect_block_reason,
    _is_html_content,
)

try:
    from curl_cffi.requests import AsyncSession
except ImportError:  # pragma: no cover
    AsyncSession = None  # type: ignore[assignment,misc]


class CurlCffiFetcher:
    """Async fetcher backed by curl_cffi with browser impersonation.

    Shares the same ``FetchResult`` return type and ``filter_same_domain``
    static method as the httpx :class:`Fetcher`.
    """

    def __init__(
        self,
        *,
        request_interval_seconds: float = 2.0,
        max_retries: int = 3,
        timeout_seconds: float = 30.0,
        retry_base_delay: float = 1.0,
        impersonate: str = "chrome131",
        cookies: dict[str, str] | None = None,
    ) -> None:
        if AsyncSession is None:
            raise ImportError(
                "curl_cffi is required for CurlCffiFetcher. "
                "Install it with: pip install curl_cffi"
            )
        self.request_interval_seconds = request_interval_seconds
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._timeout_seconds = timeout_seconds
        self._impersonate = impersonate
        self._cookies = cookies or {}
        self._session: AsyncSession | None = None
        self._last_request_at: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> "CurlCffiFetcher":
        self._session = AsyncSession(
            impersonate=self._impersonate,
            timeout=self._timeout_seconds,
            allow_redirects=True,
            cookies=self._cookies or None,
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._session is not None:
            await self._session.close()

    async def fetch(self, url: str) -> FetchResult:
        if self._session is None:
            async with self:
                return await self.fetch(url)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._wait_for_domain(url)
            try:
                response = await self._session.get(url)
                if response.status_code in {429, 503} and attempt < self.max_retries:
                    await asyncio.sleep(self.retry_base_delay * (2 ** attempt))
                    continue
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"HTTP {response.status_code} for {url}"
                    )
                content_type = response.headers.get("content-type", "")
                block_reason = _detect_block_reason(
                    status_code=response.status_code,
                    body_text=response.text,
                    headers=response.headers,
                )
                if not _is_html_content(content_type):
                    return FetchResult(
                        url=str(response.url),
                        text="",
                        links=[],
                        status_code=response.status_code,
                        block_reason=block_reason,
                    )
                helper = Fetcher.__new__(Fetcher)
                text = helper._html_to_text(response.text)
                links = helper._extract_links(response.text, str(response.url))
                return FetchResult(
                    url=str(response.url),
                    text=text,
                    links=links,
                    status_code=response.status_code,
                    block_reason=block_reason,
                )
            except Exception as error:
                last_error = error
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(self.retry_base_delay * (2 ** attempt))
        raise RuntimeError(f"Failed to fetch {url}") from last_error

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
