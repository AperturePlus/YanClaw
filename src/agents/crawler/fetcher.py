from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urldefrag, urljoin, urlparse

import html2text
import httpx


@dataclass(frozen=True)
class FetchResult:
    url: str
    text: str
    links: list[str]
    status_code: int


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


class Fetcher:
    """Async HTTP fetcher with per-domain rate limiting and retries."""

    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(
        self,
        *,
        request_interval_seconds: float = 2.0,
        max_retries: int = 3,
        timeout_seconds: float = 30.0,
        retry_base_delay: float = 1.0,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.request_interval_seconds = request_interval_seconds
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._client = client
        self._own_client = client is None
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._insecure_client: httpx.AsyncClient | None = None
        self._last_request_at: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> "Fetcher":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=True,
                transport=self._transport,
                headers=self._HEADERS,
            )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._insecure_client is not None:
            await self._insecure_client.aclose()
        if self._client is not None and self._own_client:
            await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        if self._client is None:
            async with self:
                return await self.fetch(url)

        try:
            return await self._fetch_with_client(self._client, url)
        except (RuntimeError, httpx.ConnectError) as exc:
            if not _is_ssl_error(exc):
                raise
            # SSL failures are common on Chinese university subdomains;
            # retry with relaxed cipher suites and no certificate verification.
            client = await self._get_insecure_client()
            return await self._fetch_with_client(client, url)

    async def _fetch_with_client(
        self, client: httpx.AsyncClient, url: str
    ) -> FetchResult:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._wait_for_domain(url)
            try:
                response = await client.get(url)
                if response.status_code in {429, 503} and attempt < self.max_retries:
                    await asyncio.sleep(self.retry_base_delay * (2**attempt))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if not _is_html_content(content_type):
                    return FetchResult(
                        url=str(response.url),
                        text="",
                        links=[],
                        status_code=response.status_code,
                    )
                text = self._html_to_text(response.text)
                links = self._extract_links(response.text, str(response.url))
                return FetchResult(
                    url=str(response.url),
                    text=text,
                    links=links,
                    status_code=response.status_code,
                )
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as error:
                last_error = error
                if _is_ssl_error(error):
                    break  # SSL errors are deterministic; let fetch() fallback
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(self.retry_base_delay * (2**attempt))
        raise RuntimeError(f"Failed to fetch {url}") from last_error

    async def _get_insecure_client(self) -> httpx.AsyncClient:
        if self._insecure_client is None:
            import ssl as _ssl

            ctx = _ssl.create_default_context()
            ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            self._insecure_client = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=True,
                verify=ctx,
                headers=self._HEADERS,
            )
        return self._insecure_client

    @staticmethod
    def filter_same_domain(links: Iterable[Any], base_url: str) -> list[str]:
        base_host = (urlparse(base_url).hostname or "").lower()
        base_root = _site_root(base_host)
        filtered: list[str] = []
        seen: set[str] = set()

        for raw_link in links:
            link = _coerce_link(raw_link)
            if not link:
                continue
            parsed = urlparse(link)
            host = (parsed.hostname or "").lower()
            if not host:
                continue
            root = _site_root(host)
            if root != base_root:
                continue
            normalized = urldefrag(link)[0]
            if normalized not in seen:
                seen.add(normalized)
                filtered.append(normalized)
        return filtered

    async def _wait_for_domain(self, url: str) -> None:
        domain = (urlparse(url).hostname or "").lower()
        lock = self._domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            previous = self._last_request_at.get(domain)
            if previous is not None:
                wait_seconds = self.request_interval_seconds - (time.monotonic() - previous)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
            self._last_request_at[domain] = time.monotonic()

    def _html_to_text(self, html: str) -> str:
        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.body_width = 0
        return converter.handle(html).strip()

    def _extract_links(self, html: str, base_url: str) -> list[str]:
        parser = _LinkParser()
        parser.feed(html)
        links: list[str] = []
        seen: set[str] = set()
        for href in parser.links:
            absolute = urldefrag(urljoin(base_url, href))[0]
            scheme = urlparse(absolute).scheme.lower()
            if scheme not in {"http", "https"}:
                continue
            if absolute not in seen:
                seen.add(absolute)
                links.append(absolute)
        return links


def _site_root(host: str) -> str:
    parts = [part for part in host.split(".") if part]
    if len(parts) >= 3 and parts[-1] == "cn" and parts[-2] in {"edu", "ac", "com", "net", "org", "gov"}:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _is_html_content(content_type: str) -> bool:
    """Return True if the Content-Type header indicates HTML content."""
    ct = content_type.lower().split(";")[0].strip()
    return ct in {"text/html", "application/xhtml+xml", ""}


def _coerce_link(value: Any) -> str | None:
    """Coerce LLM-produced link payloads into plain URL strings."""
    if isinstance(value, dict):
        for key in ("url", "href", "link"):
            candidate = value.get(key)
            if isinstance(candidate, (str, bytes)):
                value = candidate
                break
        else:
            return None

    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return None

    if not isinstance(value, str):
        return None

    cleaned = value.strip()
    return cleaned or None


def _is_ssl_error(exc: BaseException) -> bool:
    """Return True if *exc* (or its chain) originates from an SSL failure."""
    import ssl

    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ssl.SSLError):
            return True
        msg = str(cur).lower()
        if "ssl" in msg or "certificate" in msg:
            return True
        cur = cur.__cause__
    return False
