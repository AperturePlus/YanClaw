from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.agent import AgentResult, CrawlerAgent
from agents.crawler.config import CrawlerSettings
from agents.crawler.fetcher import Fetcher
from agents.crawler.models import CrawlStatus, University
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
    """Plain data extracted from ORM within session scope."""
    id: int
    name: str
    url: str
    crawl_status: str


AgentFactory = Callable[..., CrawlerAgent]
LLMClientFactory = Callable[[], LLMClient]
FetcherFactory = Callable[[], Fetcher]


class CrawlDispatcher:
    """Concurrent dispatcher for one CrawlerAgent per university."""

    def __init__(
        self,
        *,
        settings: CrawlerSettings,
        db: DatabaseManager,
        agent_factory: AgentFactory | None = None,
        llm_client_factory: LLMClientFactory | None = None,
        fetcher_factory: FetcherFactory | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.agent_factory = agent_factory or CrawlerAgent
        self.llm_client_factory = llm_client_factory or (
            lambda: LLMClient(
                settings.openai_base_url,
                settings.openai_api_key,
                settings.openai_model,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        )
        self.fetcher_factory = fetcher_factory or (
            lambda: Fetcher(
                request_interval_seconds=settings.request_interval_seconds,
                max_retries=settings.max_retries,
                timeout_seconds=settings.request_timeout_seconds,
            )
        )
        self.logger = get_logger("crawler.dispatcher")

    async def run(self, universities: list[str] | None = None) -> DispatcherSummary:
        await self.db.init_db()
        async with self.db.session() as session:
            await crawler_db.load_universities_from_csv(session, self.settings.websites_path)

        selected_names = set(universities or [])
        targets = await self._load_targets(selected_names)
        semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        results: list[AgentResult] = []
        skipped = 0

        async with self.fetcher_factory() as fetcher:
            tasks = []
            for university in targets:
                if university.crawl_status == CrawlStatus.COMPLETED.value:
                    professor_count = await self._professor_count(university.id)
                    if professor_count > 0:
                        skipped += 1
                        self.logger.info(
                            "Skipping completed university %s (%s professors)",
                            university.name,
                            professor_count,
                        )
                        continue
                    self.logger.info(
                        "Re-crawling %s because it is marked completed but has no professors",
                        university.name,
                    )
                tasks.append(self._run_one(university, fetcher, semaphore))
            if tasks:
                results = list(await asyncio.gather(*tasks))

        success = sum(1 for result in results if result.status == CrawlStatus.COMPLETED.value)
        failed = sum(1 for result in results if result.status == CrawlStatus.FAILED.value)
        self.logger.info("Summary success=%s failed=%s skipped=%s", success, failed, skipped)
        return DispatcherSummary(success=success, failed=failed, skipped=skipped, results=results)

    async def _load_targets(self, selected_names: set[str]) -> list[_UniversityTarget]:
        async with self.db.session() as session:
            statement = select(University).order_by(University.id)
            if selected_names:
                statement = statement.where(University.name.in_(selected_names))
            rows = (await session.execute(statement)).scalars().all()
            return [
                _UniversityTarget(id=row.id, name=row.name, url=row.url, crawl_status=row.crawl_status)
                for row in rows
            ]

    async def _run_one(
        self,
        university: _UniversityTarget,
        fetcher: Fetcher,
        semaphore: asyncio.Semaphore,
    ) -> AgentResult:
        async with semaphore:
            self.logger.info("Dispatching %s", university.name)
            skill_manager = SkillManager(
                Path(self.settings.crawler_skills_dir),
                self.db,
                "crawler",
            )
            agent = self.agent_factory(
                university_name=university.name,
                start_url=university.url,
                db=self.db,
                llm_client=self.llm_client_factory(),
                skill_manager=skill_manager,
                context_manager=ContextManager(self.settings.openai_model),
                fetcher=fetcher,
                model_max_tokens=self.settings.model_max_tokens - self.settings.response_reserved_tokens,
            )
            maybe_result = agent.run()
            return await maybe_result

    async def _professor_count(self, university_id: int) -> int:
        async with self.db.session() as session:
            return await crawler_db.count_professors_for_university(session, university_id)
