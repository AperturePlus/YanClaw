"""HumanFetcherBridge — Fetcher-compatible interface backed by a human operator.

The bridge starts an aiohttp HTTP server. When ``fetch(url)`` is called, it
creates a :class:`FetchJob`, enqueues it, and blocks until the human operator
(via the Tampermonkey script) submits the page HTML.  HTML-to-text conversion
and link extraction reuse the existing :class:`Fetcher` helpers.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

from aiohttp import web

from agents.crawler.fetchers.httpx_fetcher import FetchResult, Fetcher
from agents.crawler.fetchers.human_models import (
    FetchJob,
    FetchJobStatus,
    JobContext,
    JobQueue,
)
from agents.crawler.fetchers.human_server import create_app
from runtime.logger import get_logger


class HumanFetcherBridge:
    """Fetcher interface backed by a human-operated browser."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 21520,
        job_timeout_seconds: float = 300.0,
    ) -> None:
        self.host = host
        self.port = port
        self.job_timeout_seconds = job_timeout_seconds
        self.queue = JobQueue()
        self._context = JobContext()
        self._runner: web.AppRunner | None = None
        self._helper = Fetcher()  # for html_to_text / extract_links
        self.logger = get_logger("crawler.human_bridge")

    # -- async context manager (matches Fetcher / HybridFetcher) --

    async def __aenter__(self) -> "HumanFetcherBridge":
        app = create_app(self.queue, agent_status_fn=None)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.logger.info(
            "Human-assisted API server listening on http://%s:%s",
            self.host,
            self.port,
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- Fetcher-compatible interface --

    async def fetch(self, url: str) -> FetchResult:
        job = FetchJob(
            url=url,
            context=self._context,
            timeout_seconds=self.job_timeout_seconds,
        )
        await self.queue.submit(job)
        self.logger.info("Job queued id=%s url=%s", job.id, url)

        try:
            await asyncio.wait_for(job.done_event.wait(), timeout=self.job_timeout_seconds)
        except asyncio.TimeoutError:
            self.queue.fail(job.id, "timeout")
            self.logger.warning("Job timed out id=%s url=%s", job.id, url)

        if job.status == FetchJobStatus.COMPLETED and job.result_html:
            text = self._helper._html_to_text(job.result_html)
            links = self._helper._extract_links(job.result_html, job.result_url or url)
            return FetchResult(
                url=job.result_url or url,
                text=text,
                links=links,
                status_code=200,
            )

        reason = "human_skip" if job.status == FetchJobStatus.SKIPPED else (job.error_message or "human_failed")
        return FetchResult(url=url, text="", links=[], status_code=0, block_reason=reason)

    def set_context(self, context: JobContext) -> None:
        """Update the context attached to subsequent jobs."""
        self._context = context

    @staticmethod
    def filter_same_domain(links: Iterable[Any], base_url: str) -> list[str]:
        return Fetcher.filter_same_domain(links, base_url)
