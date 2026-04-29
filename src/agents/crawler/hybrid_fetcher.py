from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from agents.crawler.fetcher import FetchResult, Fetcher
from runtime.logger import get_logger


class HybridFetcher:
    """Multi-backend fetcher with automatic per-host fallback/pinning.

    Default order is:
    1) httpx
    2) curl_cffi
    3) playwright
    4) crawl4ai
    """

    def __init__(
        self,
        *,
        request_interval_seconds: float = 2.0,
        max_retries: int = 3,
        timeout_seconds: float = 30.0,
        retry_base_delay: float = 1.0,
        crawl4ai_base_url: str = "http://localhost:10086",
        crawl4ai_api_token: str | None = None,
        crawl4ai_timeout_seconds: float = 120.0,
        backend_order: tuple[str, ...] = ("httpx", "curl_cffi", "playwright", "crawl4ai"),
        backend_factories: dict[str, Callable[[], Any]] | None = None,
    ) -> None:
        self.request_interval_seconds = request_interval_seconds
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self.retry_base_delay = retry_base_delay
        self.crawl4ai_base_url = crawl4ai_base_url
        self.crawl4ai_api_token = crawl4ai_api_token
        self.crawl4ai_timeout_seconds = crawl4ai_timeout_seconds
        self.backend_order = tuple(dict.fromkeys(backend_order))
        self._custom_backend_factories = backend_factories or {}

        self._active_backends: dict[str, Any] = {}
        self._entered_order: list[str] = []
        self._preferred_backend_by_host: dict[str, str] = {}
        self._disabled_backends: set[str] = set()
        self.logger = get_logger("crawler.hybrid_fetcher")

    async def __aenter__(self) -> "HybridFetcher":
        factories = self._build_backend_factories()
        for name in self.backend_order:
            factory = factories.get(name)
            if factory is None:
                continue
            try:
                backend = factory()
                if hasattr(backend, "__aenter__"):
                    backend = await backend.__aenter__()
                self._active_backends[name] = backend
                self._entered_order.append(name)
            except ImportError as error:
                self.logger.warning("Hybrid backend unavailable name=%s error=%s", name, error)
            except Exception as error:
                self.logger.warning("Hybrid backend init failed name=%s error=%s", name, error)
        if not self._active_backends:
            raise RuntimeError("HybridFetcher has no available backend")
        self.logger.info(
            "Hybrid fetcher ready backends=%s",
            ",".join(self._entered_order),
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        for name in reversed(self._entered_order):
            backend = self._active_backends.get(name)
            if backend is None:
                continue
            exit_fn = getattr(backend, "__aexit__", None)
            if exit_fn is None:
                continue
            try:
                await exit_fn(exc_type, exc, tb)
            except Exception:
                self.logger.exception("Hybrid backend close failed name=%s", name)

    async def fetch(self, url: str) -> FetchResult:
        if not self._active_backends:
            async with self:
                return await self.fetch(url)

        host = (urlparse(url).hostname or "").lower()
        backend_names = self._ordered_backends_for_host(host)

        best: FetchResult | None = None
        best_name = ""
        for name in backend_names:
            if name in self._disabled_backends:
                continue
            backend = self._active_backends.get(name)
            if backend is None:
                continue
            try:
                result = await backend.fetch(url)
            except Exception as error:
                self.logger.debug("Hybrid backend failed name=%s url=%s error=%s", name, url, error)
                if self._should_disable_backend(name, error):
                    self._disabled_backends.add(name)
                    self.logger.warning(
                        "Hybrid backend disabled for this run name=%s reason=%s",
                        name,
                        error,
                    )
                continue

            if best is None:
                best = result
                best_name = name

            if self._is_usable_result(result):
                self._preferred_backend_by_host[host] = name
                self.logger.debug(
                    "Hybrid backend selected name=%s url=%s status=%s links=%s block=%s",
                    name,
                    url,
                    result.status_code,
                    len(result.links),
                    result.block_reason or "-",
                )
                return result

            self.logger.debug(
                "Hybrid backend fallback name=%s url=%s status=%s links=%s block=%s",
                name,
                url,
                result.status_code,
                len(result.links),
                result.block_reason or "-",
            )

        if best is not None:
            self.logger.debug(
                "Hybrid returning best-effort result name=%s url=%s status=%s links=%s block=%s",
                best_name,
                url,
                best.status_code,
                len(best.links),
                best.block_reason or "-",
            )
            return best
        raise RuntimeError(f"HybridFetcher failed to fetch {url}")

    @staticmethod
    def _is_usable_result(result: FetchResult) -> bool:
        if result.block_reason:
            return False
        if result.status_code >= 400 or result.status_code <= 0:
            return False
        if result.status_code in {202, 429, 503}:
            return False
        # Treat truly empty responses as likely challenge/mis-rendering.
        if len(result.links) == 0 and len((result.text or "").strip()) < 120:
            return False
        return True

    def _ordered_backends_for_host(self, host: str) -> list[str]:
        preferred = self._preferred_backend_by_host.get(host)
        names = [
            name
            for name in self.backend_order
            if name in self._active_backends and name not in self._disabled_backends
        ]
        if preferred and preferred in names:
            return [preferred] + [name for name in names if name != preferred]
        return names

    @staticmethod
    def _should_disable_backend(name: str, error: Exception) -> bool:
        # crawl4ai service instability should not repeatedly slow the run.
        if name != "crawl4ai":
            return False
        text = str(error).lower()
        return any(token in text for token in ("500", "503", "timeout", "/crawl"))

    def _build_backend_factories(self) -> dict[str, Callable[[], Any]]:
        if self._custom_backend_factories:
            return self._custom_backend_factories

        def _httpx_factory() -> Any:
            return Fetcher(
                request_interval_seconds=self.request_interval_seconds,
                max_retries=self.max_retries,
                timeout_seconds=self.timeout_seconds,
                retry_base_delay=self.retry_base_delay,
            )

        def _curl_factory() -> Any:
            from agents.crawler.curl_cffi_fetcher import CurlCffiFetcher

            return CurlCffiFetcher(
                request_interval_seconds=self.request_interval_seconds,
                max_retries=self.max_retries,
                timeout_seconds=self.timeout_seconds,
                retry_base_delay=self.retry_base_delay,
            )

        def _playwright_factory() -> Any:
            from agents.crawler.playwright_fetcher import PlaywrightFetcher

            return PlaywrightFetcher(
                request_interval_seconds=self.request_interval_seconds,
                max_retries=self.max_retries,
                timeout_seconds=self.timeout_seconds,
                retry_base_delay=self.retry_base_delay,
            )

        def _crawl4ai_factory() -> Any:
            from agents.crawler.crawl4ai_fetcher import Crawl4aiFetcher

            return Crawl4aiFetcher(
                base_url=self.crawl4ai_base_url,
                api_token=self.crawl4ai_api_token,
                request_interval_seconds=self.request_interval_seconds,
                max_retries=self.max_retries,
                timeout_seconds=self.crawl4ai_timeout_seconds,
                retry_base_delay=self.retry_base_delay,
            )

        return {
            "httpx": _httpx_factory,
            "curl_cffi": _curl_factory,
            "playwright": _playwright_factory,
            "crawl4ai": _crawl4ai_factory,
        }

    filter_same_domain = staticmethod(Fetcher.filter_same_domain)
