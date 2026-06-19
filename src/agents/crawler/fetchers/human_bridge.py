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
from agents.crawler.fetchers.link_signals import extract_links_with_signals
from agents.crawler.fetchers.human_models import (
    DecisionRequest,
    FetchJob,
    FetchJobStatus,
    JobContext,
    JobQueue,
)
from agents.crawler.fetchers.human_server import create_app
from agents.crawler.form_pagination import extract_form_pagination_states, pagination_state_from_any
from agents.crawler.url_validation import normalize_crawlable_url
from runtime.logger import get_logger


class HumanFetcherBridge:
    """Fetcher interface backed by a human-operated browser."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 21520,
        job_timeout_seconds: float = 180.0,
    ) -> None:
        self.host = host
        self.port = port
        self.job_timeout_seconds = job_timeout_seconds
        self.queue = JobQueue()
        self._context = JobContext()
        self._agent_status_fn: Any = None
        self._runner: web.AppRunner | None = None
        self._helper = Fetcher()  # for html_to_text / extract_links
        self.logger = get_logger("crawler.human_bridge")

    # -- async context manager (matches fetcher interface) --

    async def __aenter__(self) -> "HumanFetcherBridge":
        app = create_app(self.queue, agent_status_fn=lambda: self._agent_status_fn() if self._agent_status_fn else None)
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

    async def fetch(
        self,
        url: str,
        *,
        action: dict[str, Any] | None = None,
        identity_url: str | None = None,
    ) -> FetchResult:
        normalized_url = normalize_crawlable_url(url)
        if not normalized_url:
            self.logger.warning("Reject invalid URL before human queue url=%s", url)
            return FetchResult(
                url=url,
                text="",
                links=[],
                status_code=0,
                block_reason="invalid_url",
                link_signals=(),
                pagination_states=(),
            )
        url = normalized_url
        job = FetchJob(
            url=url,
            context=self._context,
            timeout_seconds=self.job_timeout_seconds,
            action=action,
            identity_url=identity_url,
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
            links, link_signals = extract_links_with_signals(job.result_html, job.result_url or url)
            reported_states = tuple(
                state for state in (pagination_state_from_any(item) for item in job.result_pagination_states) if state
            )
            derived_states = extract_form_pagination_states(job.result_html, job.result_url or url)
            pagination_states = reported_states or derived_states
            return FetchResult(
                url=identity_url or job.result_url or url,
                text=text,
                links=links,
                status_code=200,
                link_signals=link_signals,
                pagination_states=pagination_states,
            )

        reason = "human_skip" if job.status == FetchJobStatus.SKIPPED else (job.error_message or "human_failed")
        return FetchResult(
            url=url,
            text="",
            links=[],
            status_code=0,
            block_reason=reason,
            link_signals=(),
            pagination_states=(),
        )

    def set_context(self, context: JobContext) -> None:
        """Update the context attached to subsequent jobs."""
        self._context = context

    def set_status_provider(self, status_fn: Any) -> None:
        """Set callback used by /api/status to expose live agent metrics."""
        self._agent_status_fn = status_fn

    async def request_decision(
        self,
        *,
        kind: str,
        org_unit_name: str,
        failure_count: int,
        sample_urls: list[str],
        suggested_action: str = "switch_failed_to_human",
    ) -> DecisionRequest:
        return await self.queue.request_decision(
            kind=kind,
            org_unit_name=org_unit_name,
            failure_count=failure_count,
            sample_urls=sample_urls,
            suggested_action=suggested_action,
        )

    async def wait_decision(self, decision_id: str, timeout: float | None = None) -> str:
        decision = await self.queue.wait_decision(decision_id, timeout=timeout)
        return decision.action or ""

    @staticmethod
    def filter_same_domain(links: Iterable[Any], base_url: str) -> list[str]:
        return Fetcher.filter_same_domain(links, base_url)
