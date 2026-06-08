from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from agents.crawler import db as crawler_db
from agents.crawler.agent import AgentResult, CrawlerAgent
from agents.crawler.config import CrawlerSettings
from agents.crawler.entrances import ManualOrgUnitEntrance, load_university_entrance_targets
from agents.crawler.fetchers import Fetcher, _site_root
from agents.crawler.models import CrawlLogStatus, CrawlStatus, CrawlTaskStatus
from agents.crawler.org_unit_filter import (
    ORG_UNIT_FILTER_STATE,
    hard_filter_org_unit_payloads,
    llm_filter_org_unit_payloads,
    org_unit_filter_item_keys,
)
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
    org_unit_listing_urls: tuple[str, ...] = ()
    manual_org_units: tuple[ManualOrgUnitEntrance, ...] = ()

    @property
    def has_manual_entrances(self) -> bool:
        return bool(self.manual_org_units or self.org_unit_listing_urls)


@dataclass(frozen=True)
class _UniversityProgress:
    has_db: bool
    status: CrawlStatus | None
    professor_count: int
    retryable_fetch_failure_count: int = 0
    recoverable_task_count: int = 0


AgentFactory = Callable[..., CrawlerAgent]
LLMClientFactory = Callable[[], LLMClient]
FetcherFactory = Callable[[], Any]


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _university_db_path(university_db_dir: Path, start_url: str) -> Path:
    host = (urlparse(start_url).hostname or "").lower()
    root = _site_root(host) or host
    if not root:
        root = "unknown"
    filename = root.replace(":", "_") + ".db"
    return university_db_dir / filename


class FreshRunPreparationError(RuntimeError):
    """Raised when preparing fresh-run backups fails."""


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
                max_concurrent=settings.llm_max_concurrent,
                min_interval=settings.llm_min_interval_seconds,
                timeout_seconds=settings.llm_timeout_seconds,
                temperature=settings.llm_temperature,
                top_p=settings.llm_top_p,
                seed=settings.llm_seed,
            )
        )
        self.fetcher_factory = fetcher_factory or self._default_fetcher_factory(settings)
        self.logger = get_logger("crawler.dispatcher")
        self._resume_cleaned_db_paths: set[Path] = set()

    @staticmethod
    def _default_fetcher_factory(settings: CrawlerSettings) -> FetcherFactory:
        from agents.crawler.fetchers.human_bridge import HumanFetcherBridge

        return lambda: HumanFetcherBridge(
            host=settings.human_server_host,
            port=settings.human_server_port,
            job_timeout_seconds=settings.human_job_timeout_seconds,
        )

    async def run(self, universities: list[str] | None = None, *, resume: bool = False) -> DispatcherSummary:
        university_db_dir = Path(self.settings.university_db_dir)
        university_db_dir.mkdir(parents=True, exist_ok=True)
        self._resume_cleaned_db_paths.clear()

        all_targets = load_university_entrance_targets(self.settings.websites_path)
        explicit_universities = bool(universities)
        selected = set(universities or [])
        targets: list[_UniversityTarget] = []
        for item in all_targets:
            if selected and item.name not in selected:
                continue
            db_path = _university_db_path(university_db_dir, item.url)
            targets.append(
                _UniversityTarget(
                    name=item.name,
                    url=item.url,
                    location=item.location,
                    db_path=db_path,
                    org_unit_listing_urls=item.org_unit_listing_urls,
                    manual_org_units=item.manual_org_units,
                )
            )

        runnable_targets = [target for target in targets if target.has_manual_entrances]
        missing_entrance_results = [
            self._manual_entrance_missing_result(target)
            for target in targets
            if not target.has_manual_entrances
        ]
        for result in missing_entrance_results:
            self.logger.warning(
                "Skip university=%s reason=manual_entrance_missing; no DB will be touched",
                result.university_name,
            )

        if resume:
            self.logger.info("Run mode=resume; preserving existing per-university databases")
            await self._inspect_progress(runnable_targets, force_existing=explicit_universities)
        else:
            self.logger.info("Run mode=fresh; backing up and rebuilding selected per-university databases")
            self._prepare_fresh_run(runnable_targets)

        semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        results: list[AgentResult] = list(missing_entrance_results)
        skipped = 0

        if runnable_targets:
            async with self.fetcher_factory() as fetcher:
                tasks = []
                for university in runnable_targets:
                    if resume and not explicit_universities and await self._should_skip(university):
                        skipped += 1
                        continue
                    tasks.append(
                        self._run_one(
                            university,
                            fetcher,
                            semaphore,
                            resume_mode=resume,
                            resume_force_existing=resume and explicit_universities,
                        )
                    )
                if tasks:
                    results.extend(await asyncio.gather(*tasks))

        success = sum(1 for result in results if result.status == CrawlStatus.COMPLETED.value)
        failed = sum(1 for result in results if result.status == CrawlStatus.FAILED.value)
        self.logger.info("Summary success=%s failed=%s skipped=%s", success, failed, skipped)
        return DispatcherSummary(success=success, failed=failed, skipped=skipped, results=results)

    @staticmethod
    def _manual_entrance_missing_result(university: _UniversityTarget) -> AgentResult:
        return AgentResult(
            university_name=university.name,
            status=CrawlStatus.FAILED.value,
            visited_count=0,
            saved_professors=0,
            messages=["manual_entrance_missing"],
        )

    def _prepare_fresh_run(self, targets: list[_UniversityTarget]) -> None:
        existing_paths = sorted({target.db_path for target in targets if target.db_path.exists()})
        if not existing_paths:
            self.logger.info("Fresh run: no existing university DB files found for selected targets")
            return

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_root = self._next_backup_dir(Path(self.settings.university_db_dir) / "backup" / timestamp)
        backup_root.mkdir(parents=True, exist_ok=False)

        copied: list[tuple[Path, Path]] = []
        try:
            for db_path in existing_paths:
                backup_path = backup_root / db_path.name
                shutil.copy2(db_path, backup_path)
                copied.append((db_path, backup_path))
        except Exception as error:
            raise FreshRunPreparationError(
                f"Failed to back up selected university DB files into {backup_root}: {error}"
            ) from error

        for db_path, _ in copied:
            try:
                db_path.unlink()
            except Exception as error:
                raise FreshRunPreparationError(
                    f"Backups were created at {backup_root}, but failed to remove original DB {db_path}: {error}"
                ) from error

        self.logger.info(
            "Fresh run prepared: backed up %s DB files into %s and removed originals",
            len(copied),
            backup_root,
        )

    def _next_backup_dir(self, preferred: Path) -> Path:
        candidate = preferred
        index = 1
        while candidate.exists():
            candidate = preferred.with_name(f"{preferred.name}-{index:02d}")
            index += 1
        return candidate

    async def _inspect_progress(self, targets: list[_UniversityTarget], *, force_existing: bool = False) -> None:
        for university in targets:
            cleanup_summary = await self._cleanup_excluded_org_units_for_resume(university)
            progress = await self._get_university_progress(university)
            if not progress.has_db:
                self.logger.info(
                    "Resume progress university=%s status=missing_db professors=0 action=crawl",
                    university.name,
                )
                continue
            action = (
                "crawl"
                if force_existing or university.db_path in self._resume_cleaned_db_paths
                else (
                    "skip"
                    if (
                        progress.status == CrawlStatus.COMPLETED
                        and progress.professor_count > 0
                        and progress.retryable_fetch_failure_count <= 0
                        and progress.recoverable_task_count <= 0
                    )
                    else "crawl"
                )
            )
            status_text = progress.status.value if progress.status else "unknown"
            self.logger.info(
                "Resume progress university=%s status=%s professors=%s retryable_fetch_failures=%s recoverable_tasks=%s action=%s db=%s cleanup=%s",
                university.name,
                status_text,
                progress.professor_count,
                progress.retryable_fetch_failure_count,
                progress.recoverable_task_count,
                action,
                university.db_path,
                cleanup_summary or {},
            )

    async def _should_skip(self, university: _UniversityTarget) -> bool:
        if university.db_path in self._resume_cleaned_db_paths:
            self.logger.info(
                "Re-crawling %s because resume cleanup removed excluded org units",
                university.name,
            )
            return False
        progress = await self._get_university_progress(university)
        if not progress.has_db:
            return False
        if progress.status != CrawlStatus.COMPLETED:
            return False
        if progress.professor_count <= 0:
            self.logger.info(
                "Re-crawling %s because it is marked completed but has no professors",
                university.name,
            )
            return False
        if progress.retryable_fetch_failure_count > 0:
            self.logger.info(
                "Re-crawling %s because %s retryable fetch failures remain",
                university.name,
                progress.retryable_fetch_failure_count,
            )
            return False
        if progress.recoverable_task_count > 0:
            self.logger.info(
                "Re-crawling %s because %s recoverable extraction tasks remain",
                university.name,
                progress.recoverable_task_count,
            )
            return False
        self.logger.info(
            "Skipping completed university %s (%s professors)",
            university.name,
            progress.professor_count,
        )
        return True

    async def _cleanup_excluded_org_units_for_resume(self, university: _UniversityTarget) -> dict[str, int]:
        if not university.db_path.exists():
            return {}
        if not self.settings.org_unit_exclude_enabled or self.settings.target_org_units:
            return {}

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
                org_units = await crawler_db.list_org_units(session)
                if not org_units:
                    return {}

                payloads = [
                    {
                        "id": int(unit.id) if unit.id is not None else None,
                        "name": unit.name,
                        "url": unit.url,
                        "kind": unit.kind,
                    }
                    for unit in org_units
                ]
                hard_result = hard_filter_org_unit_payloads(
                    payloads,
                    exclude_enabled=True,
                    keywords=list(self.settings.org_unit_exclude_keywords or []),
                )
                llm_result = None
                if (
                    self.settings.org_unit_llm_filter_enabled
                    and self.settings.openai_api_key
                    and hard_result.kept
                ):
                    skill_manager = SkillManager(
                        Path(self.settings.crawler_skills_dir),
                        db,
                        "crawler",
                    )
                    llm_result = await llm_filter_org_unit_payloads(
                        hard_result.kept,
                        llm_client=self.llm_client_factory(),
                        context_manager=ContextManager(self.settings.openai_model),
                        skills_text=skill_manager.select_for_state(ORG_UNIT_FILTER_STATE, set()).rendered_text,
                        university=university.name,
                        source_url=university.url,
                        source="dispatcher_resume",
                        model_max_tokens=self.settings.model_max_tokens - self.settings.response_reserved_tokens,
                        logger=self.logger,
                    )

                excluded = list(hard_result.hard_excluded)
                if llm_result is not None:
                    excluded.extend(llm_result.llm_excluded)
                excluded_for_delete = [
                    item for item in excluded if item.category != "sub_department_section"
                ]

                cleanup_summary: dict[str, int] = {}
                excluded_units: list[Any] = []
                excluded_keys: set[str] = set()
                for item in excluded_for_delete:
                    excluded_keys.update(org_unit_filter_item_keys(item.to_evidence()))
                if excluded_keys:
                    excluded_units = [
                        unit
                        for unit in org_units
                        if org_unit_filter_item_keys(
                            {
                                "id": int(unit.id) if unit.id is not None else None,
                                "name": unit.name,
                                "url": unit.url,
                            }
                        )
                        & excluded_keys
                    ]

                if excluded_units:
                    cleanup_summary.update(await crawler_db.cleanup_excluded_org_units(session, excluded_units))
                    self.logger.info(
                        "Resume cleaned excluded org units university=%s excluded=%s sample=%s summary=%s",
                        university.name,
                        len(excluded_units),
                        [item.to_evidence() for item in excluded_for_delete[:5]],
                        cleanup_summary,
                    )

                sub_cleanup_summary = await crawler_db.merge_sub_department_sections(session)
                if int(sub_cleanup_summary.get("sub_org_units_merged", 0) or 0):
                    cleanup_summary.update(sub_cleanup_summary)
                    self.logger.info(
                        "Resume merged sub-department org units university=%s summary=%s",
                        university.name,
                        sub_cleanup_summary,
                    )

                if any(int(value or 0) for value in cleanup_summary.values()):
                    self._resume_cleaned_db_paths.add(university.db_path)
                    return {key: int(value or 0) for key, value in cleanup_summary.items()}
                return {}
        finally:
            await db.close()

    async def _get_university_progress(self, university: _UniversityTarget) -> _UniversityProgress:
        if not university.db_path.exists():
            return _UniversityProgress(has_db=False, status=None, professor_count=0)
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
                professor_count = await crawler_db.count_professors(session)
                retryable_fetch_failure_count = await crawler_db.count_retryable_fetch_failure_urls(session)
                task_summary = await crawler_db.summarize_crawl_task_status(session)
                recoverable_task_count = (
                    int(task_summary.get(CrawlTaskStatus.PENDING.value, 0) or 0)
                    + int(task_summary.get(CrawlTaskStatus.RETRY.value, 0) or 0)
                    + int(task_summary.get(CrawlTaskStatus.IN_PROGRESS.value, 0) or 0)
                )
                return _UniversityProgress(
                    has_db=True,
                    status=status,
                    professor_count=int(professor_count),
                    retryable_fetch_failure_count=int(retryable_fetch_failure_count),
                    recoverable_task_count=int(recoverable_task_count),
                )
        finally:
            await db.close()

    async def _run_one(
        self,
        university: _UniversityTarget,
        fetcher: Fetcher,
        semaphore: asyncio.Semaphore,
        *,
        resume_mode: bool,
        resume_force_existing: bool = False,
    ) -> AgentResult:
        async with semaphore:
            timeout_seconds = float(self.settings.university_timeout_seconds)
            self.logger.info(
                "Dispatching %s (timeout=%ss human_bridge=%s:%s human_job_timeout=%ss llm_timeout=%ss llm_max_concurrent=%s llm_min_interval_seconds=%s pipeline_llm_workers=%s max_org_units_per_university=%s)",
                university.name,
                timeout_seconds,
                self.settings.human_server_host,
                self.settings.human_server_port,
                self.settings.human_job_timeout_seconds,
                self.settings.llm_timeout_seconds,
                self.settings.llm_max_concurrent,
                self.settings.llm_min_interval_seconds,
                self.settings.pipeline_llm_workers,
                self.settings.max_org_units_per_university,
            )

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
                        fetcher=fetcher,
                        max_org_units_per_university=self.settings.max_org_units_per_university,
                        model_max_tokens=self.settings.model_max_tokens - self.settings.response_reserved_tokens,
                        detail_enrich_enabled=self.settings.detail_enrich_enabled,
                        detail_profile_hard_cap_per_org_unit=self.settings.detail_profile_hard_cap_per_org_unit,
                        pipeline_enabled=self.settings.pipeline_enabled,
                        pipeline_llm_workers=self.settings.pipeline_llm_workers,
                        pipeline_db_workers=self.settings.pipeline_db_workers,
                        pipeline_queue_cap=self.settings.pipeline_queue_cap,
                        invalid_json_max_retry=self.settings.invalid_json_max_retry,
                        task_recovery_enabled=self.settings.task_recovery_enabled,
                        resume_mode=resume_mode,
                        resume_force_existing=resume_force_existing,
                        target_org_units=list(self.settings.target_org_units or []),
                        org_unit_match_threshold=self.settings.org_unit_match_threshold,
                        org_unit_exclude_enabled=self.settings.org_unit_exclude_enabled,
                        org_unit_exclude_keywords=list(self.settings.org_unit_exclude_keywords or []),
                        org_unit_llm_filter_enabled=self.settings.org_unit_llm_filter_enabled,
                        org_unit_listing_urls=list(university.org_unit_listing_urls),
                        manual_org_units=list(university.manual_org_units),
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
                await db.close()
