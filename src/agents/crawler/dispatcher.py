from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from agents.crawler import db as crawler_db
from agents.crawler.agent import AgentResult, CrawlerAgent
from agents.crawler.config import CrawlerSettings
from agents.crawler.cookies import cookies_to_httpx, load_cookies
from agents.crawler.fetchers import Fetcher, _site_root
from agents.crawler.models import CrawlLogStatus, CrawlStatus
from runtime.context import ContextManager
from runtime.database import DatabaseManager
from runtime.llm import LLMClient
from runtime.logger import get_logger
from runtime.skills import SkillManager


@dataclass(frozen=True)
class DispatcherSummary:
    success: int
    failed: int
    skipped: int
    results: list[AgentResult]


@dataclass(frozen=True)
class _UniversityTarget:
    name: str
    url: str
    location: str
    db_path: Path


AgentFactory = Callable[..., CrawlerAgent]
LLMClientFactory = Callable[[], LLMClient]
FetcherFactory = Callable[[], Fetcher]


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _university_db_path(university_db_dir: Path, start_url: str) -> Path:
    host = (urlparse(start_url).hostname or "").lower()
    root = _site_root(host) or host
    if not root:
        root = "unknown"
    filename = root.replace(":", "_") + ".db"
    return university_db_dir / filename


class CrawlDispatcher:
    """Concurrent dispatcher for one CrawlerAgent per university (one DB per university)."""

    def __init__(
        self,
        *,
        settings: CrawlerSettings,
        agent_factory: AgentFactory | None = None,
        llm_client_factory: LLMClientFactory | None = None,
        fetcher_factory: FetcherFactory | None = None,
    ) -> None:
        self.settings = settings
        self.agent_factory = agent_factory or CrawlerAgent
        self.llm_client_factory = llm_client_factory or (
            lambda: LLMClient(
                settings.openai_base_url,
                settings.openai_api_key,
                settings.openai_model,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        )
        self.fetcher_factory = fetcher_factory or self._default_fetcher_factory(settings)
        self.logger = get_logger("crawler.dispatcher")

    @staticmethod
    def _default_fetcher_factory(settings: CrawlerSettings) -> FetcherFactory:
        if settings.fetcher_backend == "playwright":
            from agents.crawler.fetchers.playwright_fetcher import PlaywrightFetcher

            return lambda: PlaywrightFetcher(
                request_interval_seconds=settings.request_interval_seconds,
                max_retries=settings.max_retries,
                timeout_seconds=settings.request_timeout_seconds,
            )
        if settings.fetcher_backend == "curl_cffi":
            from agents.crawler.fetchers.curl_cffi_fetcher import CurlCffiFetcher

            return lambda: CurlCffiFetcher(
                request_interval_seconds=settings.request_interval_seconds,
                max_retries=settings.max_retries,
                timeout_seconds=settings.request_timeout_seconds,
            )
        if settings.fetcher_backend == "crawl4ai":
            from agents.crawler.fetchers.crawl4ai_fetcher import Crawl4aiFetcher

            return lambda: Crawl4aiFetcher(
                base_url=settings.crawl4ai_base_url,
                api_token=settings.crawl4ai_api_token,
                request_interval_seconds=settings.request_interval_seconds,
                max_retries=settings.max_retries,
                timeout_seconds=settings.crawl4ai_timeout_seconds,
            )
        if settings.fetcher_backend == "hybrid":
            from agents.crawler.fetchers.hybrid_fetcher import HybridFetcher

            return lambda: HybridFetcher(
                request_interval_seconds=settings.request_interval_seconds,
                max_retries=settings.max_retries,
                timeout_seconds=settings.request_timeout_seconds,
                crawl4ai_base_url=settings.crawl4ai_base_url,
                crawl4ai_api_token=settings.crawl4ai_api_token,
                crawl4ai_timeout_seconds=settings.crawl4ai_timeout_seconds,
            )
        if settings.fetcher_backend == "human":
            from agents.crawler.fetchers.human_bridge import HumanFetcherBridge

            return lambda: HumanFetcherBridge(
                host=settings.human_server_host,
                port=settings.human_server_port,
                job_timeout_seconds=settings.human_job_timeout_seconds,
            )
        return lambda: Fetcher(
            request_interval_seconds=settings.request_interval_seconds,
            max_retries=settings.max_retries,
            timeout_seconds=settings.request_timeout_seconds,
        )

    async def run(self, universities: list[str] | None = None) -> DispatcherSummary:
        university_db_dir = Path(self.settings.university_db_dir)
        university_db_dir.mkdir(parents=True, exist_ok=True)

        all_targets = crawler_db.load_university_targets_from_csv(self.settings.websites_path)
        selected = set(universities or [])
        targets: list[_UniversityTarget] = []
        for item in all_targets:
            if selected and item["name"] not in selected:
                continue
            db_path = _university_db_path(university_db_dir, item["url"])
            targets.append(
                _UniversityTarget(
                    name=item["name"],
                    url=item["url"],
                    location=item.get("location", ""),
                    db_path=db_path,
                )
            )

        semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        results: list[AgentResult] = []
        skipped = 0

        async with self.fetcher_factory() as fetcher:
            tasks = []
            for university in targets:
                if await self._should_skip(university):
                    skipped += 1
                    continue
                tasks.append(self._run_one(university, fetcher, semaphore))
            if tasks:
                results = list(await asyncio.gather(*tasks))

        success = sum(1 for result in results if result.status == CrawlStatus.COMPLETED.value)
        failed = sum(1 for result in results if result.status == CrawlStatus.FAILED.value)
        self.logger.info("Summary success=%s failed=%s skipped=%s", success, failed, skipped)
        return DispatcherSummary(success=success, failed=failed, skipped=skipped, results=results)

    def _make_cookie_fetcher(self, raw_cookies: list[dict]) -> Fetcher:
        """Create a fetcher of the configured backend type with cookies injected."""
        s = self.settings
        httpx_cookies = cookies_to_httpx(raw_cookies)
        backend = s.fetcher_backend

        if backend == "playwright":
            from agents.crawler.fetchers.playwright_fetcher import PlaywrightFetcher

            return PlaywrightFetcher(
                request_interval_seconds=s.request_interval_seconds,
                max_retries=s.max_retries,
                timeout_seconds=s.request_timeout_seconds,
                cookies=raw_cookies,
            )
        if backend == "curl_cffi":
            from agents.crawler.fetchers.curl_cffi_fetcher import CurlCffiFetcher

            return CurlCffiFetcher(
                request_interval_seconds=s.request_interval_seconds,
                max_retries=s.max_retries,
                timeout_seconds=s.request_timeout_seconds,
                cookies=httpx_cookies,
            )
        if backend == "crawl4ai":
            from agents.crawler.fetchers.crawl4ai_fetcher import Crawl4aiFetcher

            return Crawl4aiFetcher(
                base_url=s.crawl4ai_base_url,
                api_token=s.crawl4ai_api_token,
                request_interval_seconds=s.request_interval_seconds,
                max_retries=s.max_retries,
                timeout_seconds=s.crawl4ai_timeout_seconds,
                cookies=raw_cookies,
            )
        if backend == "hybrid":
            from agents.crawler.fetchers.hybrid_fetcher import HybridFetcher

            return HybridFetcher(
                request_interval_seconds=s.request_interval_seconds,
                max_retries=s.max_retries,
                timeout_seconds=s.request_timeout_seconds,
                crawl4ai_base_url=s.crawl4ai_base_url,
                crawl4ai_api_token=s.crawl4ai_api_token,
                crawl4ai_timeout_seconds=s.crawl4ai_timeout_seconds,
                cookies=raw_cookies,
            )
        return Fetcher(
            request_interval_seconds=s.request_interval_seconds,
            max_retries=s.max_retries,
            timeout_seconds=s.request_timeout_seconds,
            cookies=httpx_cookies,
        )

    async def _should_skip(self, university: _UniversityTarget) -> bool:

        if not university.db_path.exists():
            return False
        db = DatabaseManager(_sqlite_url(university.db_path))
        try:
            await db.init_db()
            async with db.session() as session:
                await crawler_db.ensure_runtime_schema(session)
                await crawler_db.ensure_university_meta(
                    session,
                    name=university.name,
                    start_url=university.url,
                    location=university.location,
                )
                status = await crawler_db.get_university_status(session)
                if status != CrawlStatus.COMPLETED:
                    return False
                professor_count = await crawler_db.count_professors(session)
                if professor_count <= 0:
                    self.logger.info(
                        "Re-crawling %s because it is marked completed but has no professors",
                        university.name,
                    )
                    return False
                self.logger.info(
                    "Skipping completed university %s (%s professors)",
                    university.name,
                    professor_count,
                )
                return True
        finally:
            await db.close()

    async def _run_one(
        self,
        university: _UniversityTarget,
        fetcher: Fetcher,
        semaphore: asyncio.Semaphore,
    ) -> AgentResult:
        async with semaphore:
            timeout_seconds = float(self.settings.university_timeout_seconds)
            self.logger.info(
                "Dispatching %s (timeout=%ss request_timeout=%ss llm_timeout=%ss)",
                university.name,
                timeout_seconds,
                self.settings.request_timeout_seconds,
                self.settings.llm_timeout_seconds,
            )

            # Load per-university cookies; create a dedicated fetcher if any exist.
            raw_cookies = load_cookies(university.url)
            cookie_fetcher = None
            effective_fetcher = fetcher
            if raw_cookies:
                self.logger.info(
                    "Injecting %d cookies for %s", len(raw_cookies), university.name,
                )
                cookie_fetcher = self._make_cookie_fetcher(raw_cookies)
                effective_fetcher = await cookie_fetcher.__aenter__()

            db = DatabaseManager(_sqlite_url(university.db_path))
            try:
                async def _crawl_one() -> AgentResult:
                    await db.init_db()
                    async with db.session() as session:
                        await crawler_db.ensure_runtime_schema(session)
                        await crawler_db.ensure_university_meta(
                            session,
                            name=university.name,
                            start_url=university.url,
                            location=university.location,
                        )

                    skill_manager = SkillManager(
                        Path(self.settings.crawler_skills_dir),
                        db,
                        "crawler",
                    )
                    agent = self.agent_factory(
                        university_name=university.name,
                        start_url=university.url,
                        location=university.location,
                        db=db,
                        llm_client=self.llm_client_factory(),
                        skill_manager=skill_manager,
                        context_manager=ContextManager(self.settings.openai_model),
                        fetcher=effective_fetcher,
                        model_max_tokens=self.settings.model_max_tokens - self.settings.response_reserved_tokens,
                    )
                    return await agent.run()

                try:
                    return await asyncio.wait_for(_crawl_one(), timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    self.logger.warning(
                        "Timeout crawling %s after %ss",
                        university.name,
                        timeout_seconds,
                    )
                    try:
                        async with db.session() as session:
                            await crawler_db.set_university_status(session, CrawlStatus.FAILED)
                            await crawler_db.log_crawl(
                                session,
                                university.url,
                                CrawlLogStatus.FAILED,
                                f"Timeout after {timeout_seconds}s",
                            )
                    except Exception:
                        # If the DB init was part of the timed-out work, we may not be able to persist logs.
                        self.logger.exception("Failed to persist timeout status for %s", university.name)
                    return AgentResult(
                        university_name=university.name,
                        status=CrawlStatus.FAILED.value,
                        visited_count=0,
                        saved_professors=0,
                        messages=[f"Timeout after {timeout_seconds}s"],
                    )
            finally:
                if cookie_fetcher is not None:
                    await cookie_fetcher.__aexit__(None, None, None)
                await db.close()
