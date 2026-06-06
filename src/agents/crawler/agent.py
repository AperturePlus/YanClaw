from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from agents.crawler import db as crawler_db
from agents.crawler import agent_detail, agent_parsing, form_pagination
from agents.crawler.extraction_pipeline import ExtractionPipeline
from agents.crawler.faculty_discovery import FacultyDiscoveryService
from agents.crawler.fetchers import FetchResult, Fetcher
from agents.crawler.fetch_scheduler import FetchScheduler
from agents.crawler.models import (
    CrawlStatus,
    CrawlTaskKind,
    CrawlTaskStatus,
    OrgUnit,
    OrgUnitStatus,
    UniversityMeta,
)
from agents.crawler.org_unit_filter import (
    ORG_UNIT_FILTER_STATE,
    hard_filter_org_unit_payloads,
    is_teaching_experiment_center_name,
    llm_filter_org_unit_payloads,
    looks_like_sub_department_section_name,
    normalize_org_unit_match_text,
    org_unit_filter_item_keys,
)
from agents.crawler.prompt_builder import CRAWLER_SYSTEM_PROMPT, CrawlerPromptBuilder
from agents.crawler.sanitizer import (
    contains_academician_hint,
    contains_self_academician_hint,
    normalize_name,
    normalize_name_key,
)
from agents.crawler.session_state import CrawlSessionState
from agents.crawler.tools import get_crawler_tool_definitions, get_crawler_tools
from agents.crawler.url_heuristics import (
    FACULTY_PAGE_TYPE_NOISE,
    FacultyCandidateAssessment,
    FACULTY_KEYWORDS,
    DEFAULT_ORG_UNIT_EXCLUDE_KEYWORDS,
    ORG_UNIT_PAGE_KEYWORDS,
    _COMMON_FACULTY_PATHS,
    _INTERMEDIATE_ORG_PATHS,
    _allow_faculty_candidate_for_host_set,
    _allow_faculty_candidate_for_org_unit,
    _assess_faculty_candidate,
    _assess_structural_faculty_candidates,
    _contains_cjk,
    _dedupe_query_terms,
    _extract_urls_from_text,
    _is_academician_showcase_page,
    _is_category_name,
    _is_college_subdomain,
    _is_core_academic_kind,
    _is_faculty_platform,
    _is_non_faculty_noise_url,
    _is_pagination_link,
    _keyword_filter,
    _looks_like_faculty_page,
    _looks_like_org_unit_listing_url,
    _looks_like_retired_content,
    _looks_like_retired_url,
    _org_unit_faculty_priority,
    _rank_faculty_page_candidates,
    _rank_org_unit_page_candidates,
    _same_site,
    _sanitize_url,
    _select_balanced_faculty_candidates,
    _url_found_on_page,
)
from agents.crawler.db.professors import normalize_professor_homepage
from runtime.context import ContextManager
from runtime.database import DatabaseManager
from runtime.llm import LLMClient
from runtime.logger import get_logger
from runtime.skills import SkillManager


class CrawlerState(str, Enum):
    DISCOVER_ORG_UNIT_PAGES = "DISCOVER_ORG_UNIT_PAGES"
    EXTRACT_ORG_UNITS = "EXTRACT_ORG_UNITS"
    FIND_FACULTY_PAGES = "FIND_FACULTY_PAGES"
    EXTRACT_PROFESSORS = "EXTRACT_PROFESSORS"
    DONE = "DONE"


_FOLLOWUP_PAGE_LIMIT = 36
_COMPLETION_RECRAWL_LAST_ERROR_PREFIX = "completion_recrawl_"
_COMPLETION_RECRAWL_REFETCH_FAILED = "completion_recrawl_refetch_failed"
_COMPLETION_RECRAWL_REFETCH_BLOCKED_PREFIX = "completion_recrawl_refetch_blocked"
_RICH_DETAIL_NO_STRUCTURED_DATA = "rich_detail_no_structured_data"
_RICH_DETAIL_PROFILE_TOKENS = (
    "个人简介",
    "个人概况",
    "学习工作经历",
    "工作经历",
    "教育经历",
    "教学情况",
    "管理经验",
    "科研项目",
    "论文著作",
    "代表论文",
    "科研成果",
    "项目题名",
)
_ACADEMIC_TITLE_TOKENS = (
    "院士",
    "教授",
    "副教授",
    "讲师",
    "研究员",
    "副研究员",
    "助理教授",
    "高级工程师",
)
_NOTICE_ISSUANCE_TITLE_RE = re.compile(
    r"^关于印发.{1,160}?的通知(?:[（(【\[].{0,80}[\)）】\]])?(?:[。.!！])?$"
)


@dataclass(frozen=True)
class AgentResult:
    university_name: str
    status: str
    visited_count: int
    saved_professors: int
    messages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _QueuedUrl:
    url: str
    depth: int
    label: str = ""
    org_unit_id: int | None = None
    fetch_action: dict[str, Any] | None = None
    identity_url: str | None = None

    @property
    def queue_url(self) -> str:
        return self.identity_url or self.url


@dataclass
class _ExtractionTaskItem:
    task_id: int
    university: str
    org_unit_name: str
    org_unit_url: str | None
    source_url: str
    page_url: str
    page_hash: str
    page_text_snapshot: str
    allowed_tools: list[str]
    attempt: int = 0
    priority: int = 0
    strict_retry: bool = False
    detail_mode: bool = False
    task_kind: str = CrawlTaskKind.LIST_PAGE.value
    recovered: bool = False
    name_homepage_candidates: dict[str, str] = field(default_factory=dict)


@dataclass
class _SaveEvent:
    task: _ExtractionTaskItem
    payloads: list[dict[str, Any]]


@dataclass
class _ExtractionOutcome:
    payloads: list[dict[str, Any]]
    invalid_json_events: list[dict[str, Any]]
    content_fallback_used: bool = False
    skipped_by_gate: bool = False
    skip_reason: str = ""


class CrawlerAgent:
    """Single-university crawler state machine (per-university DB)."""

    def __init__(
        self,
        *,
        university_name: str,
        start_url: str,
        location: str,
        db: DatabaseManager,
        llm_client: LLMClient,
        skill_manager: SkillManager,
        context_manager: ContextManager,
        fetcher: Fetcher,
        logger_name: str | None = None,
        max_depth: int = 4,
        max_backtracks: int = 3,
        max_org_units_per_university: int = 50,
        min_org_units: int = 5,
        model_max_tokens: int = 16000,
        detail_enrich_enabled: bool = True,
        detail_profile_hard_cap_per_org_unit: int = 200,
        pipeline_enabled: bool = True,
        pipeline_llm_workers: int = 1,
        pipeline_db_workers: int = 1,
        pipeline_queue_cap: int = 64,
        invalid_json_max_retry: int = 1,
        task_recovery_enabled: bool = True,
        resume_mode: bool = False,
        resume_force_existing: bool = False,
        target_org_units: list[str] | None = None,
        org_unit_match_threshold: float = 0.60,
        org_unit_exclude_enabled: bool = True,
        org_unit_exclude_keywords: list[str] | None = None,
        org_unit_llm_filter_enabled: bool = True,
    ) -> None:
        self.university_name = university_name
        self.start_url = start_url
        self.location = location
        self.db = db
        self.llm_client = llm_client
        self.skill_manager = skill_manager
        self.context_manager = context_manager
        self.fetcher = fetcher
        self.logger = get_logger(logger_name or f"crawler.{university_name}")
        self.max_depth = max_depth
        self.max_backtracks = max_backtracks
        self.max_org_units_per_university = max_org_units_per_university
        self.min_org_units = min_org_units
        self.model_max_tokens = model_max_tokens
        self.detail_enrich_enabled = detail_enrich_enabled
        self.detail_profile_hard_cap_per_org_unit = max(1, int(detail_profile_hard_cap_per_org_unit))
        self.pipeline_enabled = bool(pipeline_enabled)
        self.pipeline_llm_workers = max(1, int(pipeline_llm_workers))
        self.pipeline_db_workers = max(1, int(pipeline_db_workers))
        self.pipeline_queue_cap = max(1, int(pipeline_queue_cap))
        self.invalid_json_max_retry = max(0, int(invalid_json_max_retry))
        self.task_recovery_enabled = bool(task_recovery_enabled)
        self.resume_mode = bool(resume_mode)
        self.resume_force_existing = bool(resume_force_existing)
        self.target_org_units = [str(item).strip() for item in (target_org_units or []) if str(item).strip()]
        self.org_unit_match_threshold = min(1.0, max(0.0, float(org_unit_match_threshold)))
        self.org_unit_exclude_enabled = bool(org_unit_exclude_enabled)
        configured_exclude_keywords = (
            list(DEFAULT_ORG_UNIT_EXCLUDE_KEYWORDS)
            if org_unit_exclude_keywords is None
            else list(org_unit_exclude_keywords)
        )
        self.org_unit_exclude_keywords = [
            str(item).strip() for item in configured_exclude_keywords if str(item).strip()
        ]
        self.org_unit_llm_filter_enabled = bool(org_unit_llm_filter_enabled)
        self.session_state = CrawlSessionState.create(pipeline_enabled=self.pipeline_enabled)
        self.visited_urls = self.session_state.visited_urls
        self._fetch_cache = self.session_state.fetch_cache
        self.backtrack_count = 0
        self.execution_log: list[str] = []
        self.saved_professors = 0
        self._current_state: str = ""
        self._university_cache: UniversityMeta | None = None
        self._skip_cross_run_dedup = False
        self._blocked_hosts = self.session_state.blocked_hosts
        self._detail_visited_urls = self.session_state.detail_visited_urls
        self._detail_processed_by_org_unit = self.session_state.detail_processed_by_org_unit
        self._enriched_names_by_org_unit = self.session_state.enriched_names_by_org_unit
        self._active_detail_llm_queue: asyncio.Queue[_ExtractionTaskItem | None] | None = None
        self._target_org_unit_ids = self.session_state.target_org_unit_ids
        self._org_units_marked_no_faculty = self.session_state.org_units_marked_no_faculty
        self._pipeline_stats = self.session_state.pipeline_stats
        self._last_org_unit_validated_count = 0
        self._resume_force_refetch_urls: set[str] = set()
        self.extraction_pipeline = ExtractionPipeline(self._pipeline_stats)
        self.fetch_scheduler = FetchScheduler(self)
        self.faculty_discovery = FacultyDiscoveryService(self)
        self.detail_enricher = agent_detail.DetailEnricher(self)
        self.prompt_builder = CrawlerPromptBuilder(
            context_manager=self.context_manager,
            fetcher=self.fetcher,
            start_url=self.start_url,
            university_name=self.university_name,
            location=self.location,
            visited_urls=self.visited_urls,
        )

    @property
    def _is_interactive(self) -> bool:
        """True when using a human-assisted fetcher (streaming per-org-unit is preferred)."""
        return hasattr(self.fetcher, 'set_context')

    def _all_target_org_units_marked_no_faculty(self) -> bool:
        return bool(self._target_org_unit_ids) and self._target_org_unit_ids.issubset(
            self._org_units_marked_no_faculty
        )

    async def run(self) -> AgentResult:
        await self._ensure_university()
        await self._set_status(CrawlStatus.IN_PROGRESS)
        if hasattr(self.fetcher, "set_status_provider"):
            try:
                self.fetcher.set_status_provider(self.status_snapshot)  # type: ignore[attr-defined]
            except Exception:
                pass

        try:
            self.logger.info(
                "Starting crawl for %s strict_resume=%s",
                self.university_name,
                self.resume_mode,
            )
            resume_seed_org_units: list[OrgUnit] = []
            if self.resume_mode:
                resume_seed_org_units = await self._prepare_resume_start()

            initial_professor_count = await self._professor_count()
            home = await self._fetch_url(self.start_url, 0)
            if home is None:
                if self.resume_mode:
                    return await self._resume_without_start_page(initial_professor_count)
                self.logger.warning("Start URL could not be fetched: %s", self.start_url)
                await self._set_status(CrawlStatus.FAILED)
                return self._result(CrawlStatus.FAILED, ["Failed to fetch start URL"])

            # Retry loop: re-attempt the pipeline when a phase fails.
            org_unit_pages: list[_QueuedUrl] = (
                [_QueuedUrl(home.url, 0, label="resume_seed")] if resume_seed_org_units else []
            )
            org_units: list[OrgUnit] = list(resume_seed_org_units)
            faculty_links: list[_QueuedUrl] = []

            while self.backtrack_count <= self.max_backtracks:
                if not org_unit_pages:
                    org_unit_pages = await self._discover_org_unit_pages(home)
                if not org_unit_pages:
                    if self._too_many_backtracks("no org-unit listing pages found"):
                        break
                    self._skip_cross_run_dedup = True
                    continue

                if not org_units:
                    org_units = await self._extract_org_units(org_unit_pages)
                if not org_units and not self._skip_cross_run_dedup:
                    only_home_candidate = (
                        len(org_unit_pages) == 1
                        and _sanitize_url(org_unit_pages[0].url) == _sanitize_url(home.url)
                    )
                    if only_home_candidate and home.block_reason:
                        self.logger.info(
                            "Org-unit discovery only returned blocked homepage; skipping dedup bypass retry"
                        )
                    else:
                        all_previously_crawled = await self._all_urls_previously_crawled(
                            [item.url for item in org_unit_pages]
                        )
                        if all_previously_crawled:
                            self.logger.info(
                                "All org-unit pages were already crawled in previous runs; bypassing cross-run dedup for this retry"
                            )
                            self._skip_cross_run_dedup = True
                            org_units = await self._extract_org_units(org_unit_pages)
                if not org_units:
                    if self._too_many_backtracks("no org units extracted"):
                        break
                    org_unit_pages = []
                    self._skip_cross_run_dedup = True
                    continue
                if self.target_org_units:
                    org_units, unmatched = self._filter_target_org_units(org_units)
                    if unmatched:
                        await self._set_status(CrawlStatus.FAILED)
                        self.logger.warning(
                            "Target org units unmatched university=%s requested=%s unmatched=%s threshold=%.2f",
                            self.university_name,
                            self.target_org_units,
                            unmatched,
                            self.org_unit_match_threshold,
                        )
                        return self._result(
                            CrawlStatus.FAILED,
                            [f"Target org units unmatched: {', '.join(unmatched)}"],
                        )
                    self._target_org_unit_ids = {int(unit.id) for unit in org_units if unit.id is not None}
                    self.logger.info(
                        "Target org units matched university=%s requested=%s matched=%s threshold=%.2f",
                        self.university_name,
                        self.target_org_units,
                        [unit.name for unit in org_units],
                        self.org_unit_match_threshold,
                    )
                org_unit_count_for_minimum = max(len(org_units), self._last_org_unit_validated_count)
                if (not self.target_org_units) and org_unit_count_for_minimum < self.min_org_units:
                    self.logger.info(
                        "Only %s org units found (min=%s), likely category pages; retrying",
                        len(org_units),
                        self.min_org_units,
                    )
                    if self._too_many_backtracks(
                        f"too few org units ({len(org_units)} < {self.min_org_units})"
                    ):
                        break
                    org_unit_pages = []
                    org_units = []
                    self._skip_cross_run_dedup = True
                    continue
                if not self.target_org_units:
                    org_units = await self._filter_existing_org_units_for_discovery(
                        org_units,
                        source="crawl",
                    )
                    if not org_units:
                        if self._too_many_backtracks("all org units excluded by scope filter"):
                            break
                        org_unit_pages = []
                        org_units = []
                        self._skip_cross_run_dedup = True
                        continue

                if self._is_interactive:
                    # Interactive (human) mode: find faculty + extract professors
                    # per org unit to avoid spending all time on discovery.
                    await self._find_and_extract_streaming(org_units)
                    break

                if not faculty_links:
                    faculty_links = await self._find_faculty_pages(org_units)
                if not faculty_links:
                    if self._all_target_org_units_marked_no_faculty():
                        self.logger.info(
                            "No faculty links found, but all targeted org units are marked no_faculty_page; stop backtracking"
                        )
                        break
                    if self._too_many_backtracks("no faculty links found"):
                        break
                    org_units = []
                    self._skip_cross_run_dedup = True
                    continue

                break

            if not self._is_interactive:
                if not faculty_links:
                    if self._all_target_org_units_marked_no_faculty():
                        self.logger.info(
                            "Skip fallback extraction from homepage because all targeted org units are marked no_faculty_page"
                        )
                    else:
                        faculty_links = [_QueuedUrl(home.url, 0, label="Unknown")]
                if faculty_links:
                    await self._extract_professors(faculty_links)


            recoverable_task_result = await self._fail_if_recoverable_tasks_remain(context="crawl_completion")
            if recoverable_task_result is not None:
                return recoverable_task_result

            total_professor_count = await self._professor_count()
            newly_saved_count = max(0, total_professor_count - initial_professor_count)
            if total_professor_count <= 0:
                if (
                    self._all_target_org_units_marked_no_faculty()
                ):
                    message = "No professors saved: all targeted org units are marked no_faculty_page"
                    retryable_failure_result = await self._fail_if_retryable_fetch_failures_remain(
                        context="target_no_faculty_completion",
                    )
                    if retryable_failure_result is not None:
                        return retryable_failure_result
                    await self._set_status(CrawlStatus.COMPLETED)
                    self.logger.warning(
                        "Crawler completed with no professors because all targeted org units were marked no_faculty_page university=%s targets=%s",
                        self.university_name,
                        sorted(self._target_org_unit_ids),
                    )
                    return self._result(CrawlStatus.COMPLETED, [message])
                await self._set_status(CrawlStatus.FAILED)
                self.logger.warning(
                    "Crawler did not save any professors for %s; marking failed",
                    self.university_name,
                )
                return self._result(CrawlStatus.FAILED, ["No professors saved"])
            if newly_saved_count <= 0:
                self.logger.warning(
                    "Crawl finished with no new professors this run university=%s existing_total=%s tool_saved=%s",
                    self.university_name,
                    total_professor_count,
                    self.saved_professors,
                )

            retryable_failure_result = await self._fail_if_retryable_fetch_failures_remain(
                context="crawl_completion",
            )
            if retryable_failure_result is not None:
                return retryable_failure_result
            await self._set_status(CrawlStatus.COMPLETED)
            self.logger.info(
                "Completed crawl for %s professors_total=%s professors_new=%s",
                self.university_name,
                total_professor_count,
                newly_saved_count,
            )
            return self._result(CrawlStatus.COMPLETED, [])
        except Exception as error:
            self.logger.exception("Crawler failed for %s", self.university_name)
            await self._set_status(CrawlStatus.FAILED)
            return self._result(CrawlStatus.FAILED, [str(error)])

    async def _resume_without_start_page(self, initial_professor_count: int) -> AgentResult:
        async with self.db.session() as session:
            org_units = await crawler_db.list_org_units(session, limit=self.max_org_units_per_university)
            task_summary = await crawler_db.summarize_crawl_task_status(session)

        recoverable_tasks = self._recoverable_task_count(task_summary)
        if not org_units and recoverable_tasks <= 0:
            message = (
                "resume_blocked_missing_cache: start URL was already crawled but no page cache, "
                "org units, or recoverable crawl tasks were available"
            )
            self.logger.warning(
                "Strict resume blocked university=%s reason=missing_cache_no_seed start_url=%s",
                self.university_name,
                self.start_url,
            )
            await self._set_status(CrawlStatus.FAILED)
            return self._result(CrawlStatus.FAILED, [message])

        if recoverable_tasks > 0:
            self.logger.info(
                "Strict resume recovering extraction tasks university=%s recoverable_tasks=%s",
                self.university_name,
                recoverable_tasks,
            )
            await self._extract_professors([], recovery_limit=recoverable_tasks)
            async with self.db.session() as session:
                task_summary = await crawler_db.summarize_crawl_task_status(session)
            remaining_recoverable = self._recoverable_task_count(task_summary)
            self.logger.info(
                "Strict resume recovery progress university=%s before=%s remaining=%s done=%s failed=%s",
                self.university_name,
                recoverable_tasks,
                remaining_recoverable,
                task_summary.get(CrawlTaskStatus.DONE.value, 0),
                task_summary.get(CrawlTaskStatus.FAILED.value, 0),
            )

        if org_units:
            if self.target_org_units:
                org_units, unmatched = self._filter_target_org_units(org_units)
                if unmatched:
                    self.logger.warning(
                        "Strict resume target org units unmatched university=%s requested=%s unmatched=%s threshold=%.2f",
                        self.university_name,
                        self.target_org_units,
                        unmatched,
                        self.org_unit_match_threshold,
                    )
                    org_units = []
            else:
                org_units = await self._filter_existing_org_units_for_discovery(
                    org_units,
                    source="strict_resume",
                )

        if org_units:
            self.logger.info(
                "Strict resume seeding from existing org_units university=%s org_units=%s",
                self.university_name,
                len(org_units),
            )
            if self._is_interactive:
                await self._find_and_extract_streaming(org_units)
            else:
                faculty_links = await self._find_faculty_pages(org_units)
                if faculty_links:
                    await self._extract_professors(faculty_links)

        async with self.db.session() as session:
            final_task_summary = await crawler_db.summarize_crawl_task_status(session)
        final_recoverable_tasks = self._recoverable_task_count(final_task_summary)
        if final_recoverable_tasks > 0:
            message = (
                "resume_incomplete_recoverable_tasks: strict resume still has "
                f"{final_recoverable_tasks} pending/retry/in_progress crawl tasks"
            )
            self.logger.warning(
                "Strict resume incomplete university=%s recoverable_remaining=%s pending=%s retry=%s in_progress=%s",
                self.university_name,
                final_recoverable_tasks,
                final_task_summary.get(CrawlTaskStatus.PENDING.value, 0),
                final_task_summary.get(CrawlTaskStatus.RETRY.value, 0),
                final_task_summary.get(CrawlTaskStatus.IN_PROGRESS.value, 0),
            )
            await self._set_status(CrawlStatus.FAILED)
            return self._result(CrawlStatus.FAILED, [message])

        total_professor_count = await self._professor_count()
        newly_saved_count = max(0, total_professor_count - initial_professor_count)
        if total_professor_count > 0:
            retryable_failure_result = await self._fail_if_retryable_fetch_failures_remain(
                context="strict_resume_completion",
            )
            if retryable_failure_result is not None:
                return retryable_failure_result
            await self._set_status(CrawlStatus.COMPLETED)
            self.logger.info(
                "Strict resume completed university=%s professors_total=%s professors_new=%s",
                self.university_name,
                total_professor_count,
                newly_saved_count,
            )
            return self._result(CrawlStatus.COMPLETED, [])

        message = (
            "resume_blocked_missing_cache: strict resume found existing state but no cached pages "
            "or extracted professors were available"
        )
        self.logger.warning(
            "Strict resume blocked university=%s reason=missing_cache_no_output org_units=%s recoverable_tasks=%s",
            self.university_name,
            len(org_units),
            recoverable_tasks,
        )
        await self._set_status(CrawlStatus.FAILED)
        return self._result(CrawlStatus.FAILED, [message])

    async def _prepare_resume_start(self) -> list[OrgUnit]:
        async with self.db.session() as session:
            org_units = await crawler_db.list_org_units(session)
            retryable_failure_urls = await crawler_db.list_retryable_fetch_failure_urls(session)
            if retryable_failure_urls:
                self._add_resume_force_refetch_urls(retryable_failure_urls)
                self.logger.info(
                    "Strict resume will retry fetch failures university=%s urls=%s sample=%s",
                    self.university_name,
                    len(retryable_failure_urls),
                    retryable_failure_urls[:5],
                )
            if self.org_unit_exclude_enabled and not self.target_org_units:
                excluded_units, excluded_preview = self._hard_excluded_org_units(org_units)
                cleanup_summary: dict[str, int] = {}
                if excluded_units:
                    cleanup_summary = await crawler_db.cleanup_excluded_org_units(session, excluded_units)
                    self.logger.info(
                        "Strict resume cleaned excluded org units university=%s excluded=%s sample=%s summary=%s",
                        self.university_name,
                        len(excluded_units),
                        excluded_preview[:5],
                        cleanup_summary,
                    )
                sub_cleanup_summary = await crawler_db.merge_sub_department_sections(session)
                if int(sub_cleanup_summary.get("sub_org_units_merged", 0) or 0):
                    cleanup_summary.update(sub_cleanup_summary)
                    self.logger.info(
                        "Strict resume merged sub-department org units university=%s summary=%s",
                        self.university_name,
                        sub_cleanup_summary,
                    )
                if cleanup_summary:
                    org_units = await crawler_db.list_org_units(session)

            if self.resume_force_existing:
                reset_count = 0
                forced_urls: list[str] = []
                for unit in org_units:
                    if unit.status != OrgUnitStatus.NO_FACULTY_PAGE.value:
                        continue
                    if self.target_org_units and not self._matches_target_org_unit_name(unit.name):
                        continue
                    await crawler_db.set_org_unit_status(session, int(unit.id), OrgUnitStatus.PENDING)
                    reset_count += 1
                    url = _sanitize_url(unit.url or "")
                    if url:
                        self._resume_force_refetch_urls.add(url)
                        self._resume_force_refetch_urls.add(url.rstrip("/"))
                        forced_urls.append(url)
                if reset_count:
                    self.logger.info(
                        "Strict resume reset no_faculty org units university=%s reset=%s forced_refetch_sample=%s",
                        self.university_name,
                        reset_count,
                        forced_urls[:5],
                    )
                    org_units = await crawler_db.list_org_units(session)

            if self.resume_force_existing:
                return org_units[: self.max_org_units_per_university]
            return []

    def _add_resume_force_refetch_urls(self, urls: list[str]) -> None:
        for raw_url in urls:
            url = _sanitize_url(str(raw_url or ""))
            if not url:
                continue
            self._resume_force_refetch_urls.add(url)
            self._resume_force_refetch_urls.add(url.rstrip("/"))

    async def _fail_if_retryable_fetch_failures_remain(self, *, context: str) -> AgentResult | None:
        async with self.db.session() as session:
            urls = await crawler_db.list_retryable_fetch_failure_urls(session)
        if not urls:
            return None

        message = f"retryable_fetch_failures_remaining: {len(urls)} fetch failures still need retry"
        await self._set_status(CrawlStatus.FAILED)
        self.logger.warning(
            "Crawler finished with retryable fetch failures university=%s context=%s count=%s sample=%s",
            self.university_name,
            context,
            len(urls),
            urls[:5],
        )
        return self._result(CrawlStatus.FAILED, [message])

    async def _fail_if_recoverable_tasks_remain(self, *, context: str) -> AgentResult | None:
        async with self.db.session() as session:
            task_summary = await crawler_db.summarize_crawl_task_status(session)
        recoverable_tasks = self._recoverable_task_count(task_summary)
        if recoverable_tasks <= 0:
            return None

        message = (
            "recoverable_extraction_tasks_remaining: "
            f"{recoverable_tasks} pending/retry/in_progress crawl tasks still need resume"
        )
        await self._set_status(CrawlStatus.FAILED)
        self.logger.warning(
            "Crawler finished with recoverable extraction tasks university=%s context=%s pending=%s retry=%s in_progress=%s",
            self.university_name,
            context,
            task_summary.get(CrawlTaskStatus.PENDING.value, 0),
            task_summary.get(CrawlTaskStatus.RETRY.value, 0),
            task_summary.get(CrawlTaskStatus.IN_PROGRESS.value, 0),
        )
        return self._result(CrawlStatus.FAILED, [message])

    @staticmethod
    def _recoverable_task_count(task_summary: dict[str, int]) -> int:
        return (
            int(task_summary.get(CrawlTaskStatus.PENDING.value, 0) or 0)
            + int(task_summary.get(CrawlTaskStatus.RETRY.value, 0) or 0)
            + int(task_summary.get(CrawlTaskStatus.IN_PROGRESS.value, 0) or 0)
        )

    async def _discover_org_unit_pages(self, home: FetchResult) -> list[_QueuedUrl]:
        self._log_state(CrawlerState.DISCOVER_ORG_UNIT_PAGES)
        links = _keyword_filter(home.links, ORG_UNIT_PAGE_KEYWORDS)
        self.logger.debug(
            "Org-page discovery homepage_links=%s keyword_hits=%s block_reason=%s",
            len(home.links),
            len(links),
            home.block_reason or "-",
        )
        if not links:
            skills = await self._select_skills(CrawlerState.DISCOVER_ORG_UNIT_PAGES)
            result = await self._ask_llm(
                CrawlerState.DISCOVER_ORG_UNIT_PAGES,
                "Find links that lead to pages listing colleges/schools/departments/research institutes "
                "(e.g. org-structure, jgsz, yxsz, colleges, schools, departments). Return only links.",
                home,
                skills,
            )
            links = self._links_from_result(result.content)
            if not links:
                links = self._links_from_tool_call_log(result, tool_name="extract_links")
                if links:
                    self.logger.debug("Org-page discovery consumed extract_links tool output: %s", links[:5])

        links = self.fetcher.filter_same_domain(links, self.start_url)
        links = [l for l in links if not _is_faculty_platform(l)]
        links = _rank_org_unit_page_candidates(links, self.start_url)
        preferred_org_pages = [link for link in links if _looks_like_org_unit_listing_url(link)]
        if preferred_org_pages:
            links = preferred_org_pages
        if links:
            self.logger.debug(
                "Org-page discovery same_domain_ranked=%s sample=%s",
                len(links),
                links[:5],
            )

        if not links:
            self.logger.info("No org unit pages from keywords/LLM, probing intermediate paths")
            links = await self._probe_intermediate_org_pages()

        if not links:
            self.logger.info("No org unit pages from homepage, trying search engine fallback")
            search_links = await self._search_engine_fallback("org unit colleges schools departments jgsz yxsz")
            links = self.fetcher.filter_same_domain(search_links, self.start_url)
            links = _rank_org_unit_page_candidates(links, self.start_url)

        if not links:
            if home.block_reason:
                self.logger.warning(
                    "Org-page discovery exhausted fallbacks while homepage looked blocked: %s",
                    home.block_reason,
                )
            links = [home.url]

        return [
            _QueuedUrl(url=link, depth=1, label="org_unit_page")
            for link in links[:20]
            if self._within_depth(1)
        ]
    async def _extract_org_units(self, org_unit_pages: list[_QueuedUrl]) -> list[OrgUnit]:
        self._log_state(CrawlerState.EXTRACT_ORG_UNITS)
        skills = await self._select_skills(CrawlerState.EXTRACT_ORG_UNITS)
        self._last_org_unit_validated_count = 0

        max_pages = min(max(8, len(org_unit_pages)), 20)
        pending = list(org_unit_pages[:max_pages])
        seen_pages = {item.url for item in pending}
        extracted_any = False
        skipped_blocked_or_empty = 0
        core_validated_total = 0
        processed = 0
        while pending and processed < max_pages:
            page = pending.pop(0)
            processed += 1
            fetched = await self._fetch_url(page.url, page.depth)
            if fetched is None:
                continue

            # Skip LLM extraction when the page is clearly blocked/empty -
            # the LLM would hallucinate URLs from training data.
            if fetched.block_reason or (not fetched.links and len(fetched.text) < 200):
                skipped_blocked_or_empty += 1
                self.logger.debug(
                    "Skipping org-unit extraction for blocked/empty page url=%s block_reason=%s text_len=%s",
                    fetched.url,
                    fetched.block_reason or "-",
                    len(fetched.text),
                )
                continue

            result = await self._ask_llm(
                CrawlerState.EXTRACT_ORG_UNITS,
                "Extract academic org units (colleges/schools/departments/research institutes). "
                "Exclude admin offices. Return only JSON with top-level key "
                "{\"org_units\": [{\"name\": ..., \"url\": ..., \"kind\": ...}]}. "
                "Do not return included_org_units or excluded_org_units for this extraction task.",
                fetched,
                skills,
            )
            units = self._org_units_from_result(result.content)
            if not units:
                followups = self._org_unit_followup_links_from_result(result.content, fetched.url)
                ranked_followups = _rank_org_unit_page_candidates(followups, self.start_url)
                preferred_followups = [link for link in ranked_followups if _looks_like_org_unit_listing_url(link)]
                if preferred_followups:
                    followups = preferred_followups
                else:
                    followups = ranked_followups
                added_followups: list[str] = []
                for link in followups:
                    if link in seen_pages:
                        continue
                    if not _same_site(link, self.start_url) or _is_faculty_platform(link):
                        continue
                    next_depth = page.depth + (0 if link == fetched.url else 1)
                    if not self._within_depth(next_depth):
                        continue
                    seen_pages.add(link)
                    pending.append(_QueuedUrl(url=link, depth=next_depth, label=page.label))
                    added_followups.append(link)
                if added_followups:
                    self.logger.debug(
                        "Org-unit extraction followups from LLM hint page=%s added=%s sample=%s",
                        fetched.url,
                        len(added_followups),
                        added_followups[:3],
                    )
                continue

            # Build a set of URLs actually present on the page for validation.
            page_urls = set(fetched.links)
            page_text_lower = fetched.text.lower()

            extracted_any = True
            validated = 0
            hallucinated = 0
            validated_units: list[dict[str, Any]] = []
            for unit in units:
                name = str(unit.get("name") or "").strip()
                raw_url = _sanitize_url(str(unit.get("url") or "").strip())
                url = urljoin(fetched.url, raw_url) if raw_url and not urlparse(raw_url).scheme else raw_url
                kind = str(unit.get("kind") or "").strip() or None
                if not name or not url:
                    continue
                if _is_category_name(name):
                    continue
                if not _same_site(url, self.start_url):
                    continue
                if _is_faculty_platform(url):
                    continue
                # Validate: URL must appear in page links or page text.
                if not _url_found_on_page(url, page_urls, page_text_lower):
                    hallucinated += 1
                    continue
                validated += 1
                validated_units.append(
                    {
                        "name": name,
                        "url": url,
                        "kind": kind,
                        "discovered_from_url": fetched.url,
                    }
                )

            self._last_org_unit_validated_count += len(validated_units)
            filtered_units = await self._filter_org_unit_payloads_for_discovery(
                validated_units,
                source_url=fetched.url,
                source="extract_org_units",
            )

            async with self.db.session() as session:
                for unit in filtered_units:
                    kind = str(unit.get("kind") or "").strip() or None
                    if _is_core_academic_kind(kind):
                        core_validated_total += 1
                    await crawler_db.get_or_create_org_unit(
                        session,
                        name=str(unit.get("name") or ""),
                        url=str(unit.get("url") or ""),
                        kind=kind,
                        discovered_from_url=str(unit.get("discovered_from_url") or fetched.url),
                    )

            if hallucinated:
                self.logger.info(
                    "Org-unit URL validation: %s accepted, %s rejected (not found on page)",
                    validated,
                    hallucinated,
                )
            if validated == 0:
                followups = self._org_unit_followup_links_from_result(result.content, fetched.url)
                if not followups:
                    followups = _keyword_filter(fetched.links, ORG_UNIT_PAGE_KEYWORDS) or list(fetched.links)
                followups = self.fetcher.filter_same_domain(followups, self.start_url)
                followups = [link for link in followups if not _is_faculty_platform(link)]
                followups = _rank_org_unit_page_candidates(followups, self.start_url)
                preferred_followups = [link for link in followups if _looks_like_org_unit_listing_url(link)]
                if preferred_followups:
                    followups = preferred_followups
                added_followups: list[str] = []
                for link in followups[:8]:
                    if link in seen_pages:
                        continue
                    next_depth = page.depth + (0 if link == fetched.url else 1)
                    if not self._within_depth(next_depth):
                        continue
                    seen_pages.add(link)
                    pending.append(_QueuedUrl(url=link, depth=next_depth, label=page.label))
                    added_followups.append(link)
                if added_followups:
                    self.logger.debug(
                        "Org-unit validation produced 0 accepted; queued followups page=%s added=%s sample=%s",
                        fetched.url,
                        len(added_followups),
                        added_followups[:3],
                    )
            if core_validated_total >= self.max_org_units_per_university:
                self.logger.info(
                    "Collected %s core org units (limit=%s); stop org-unit extraction",
                    core_validated_total,
                    self.max_org_units_per_university,
                )
                break

        if not extracted_any:
            if skipped_blocked_or_empty:
                self.logger.warning(
                    "Org-unit extraction skipped %s/%s pages due to blocked/empty content; likely fetch-layer issue rather than LLM extraction",
                    skipped_blocked_or_empty,
                    processed,
                )
            return []

        async with self.db.session() as session:
            return await crawler_db.list_org_units(session, limit=self.max_org_units_per_university)

    @staticmethod
    def _normalize_org_unit_match_text(value: str) -> str:
        return normalize_org_unit_match_text(value)

    @classmethod
    def _org_unit_match_score(cls, query: str, candidate: str) -> float:
        q = cls._normalize_org_unit_match_text(query)
        c = cls._normalize_org_unit_match_text(candidate)
        if not q or not c:
            return 0.0
        if q == c:
            return 1.0
        if q in c:
            coverage = float(len(q)) / float(max(1, len(c)))
            return min(0.99, max(0.90, 0.85 + coverage * 0.15))
        if c in q:
            coverage = float(len(c)) / float(max(1, len(q)))
            return min(0.89, max(0.75, 0.68 + coverage * 0.21))
        return float(difflib.SequenceMatcher(None, q, c).ratio())

    def _filter_target_org_units(
        self,
        org_units: list[OrgUnit],
    ) -> tuple[list[OrgUnit], list[str]]:
        if not self.target_org_units:
            return org_units, []
        selected: dict[str, OrgUnit] = {}
        unmatched: list[str] = []
        for raw_target in self.target_org_units:
            target = str(raw_target or "").strip()
            if not target:
                continue
            best_unit: OrgUnit | None = None
            best_score = 0.0
            for unit in org_units:
                score = self._org_unit_match_score(target, unit.name)
                if score > best_score:
                    best_unit = unit
                    best_score = score
            if best_unit is None or best_score < self.org_unit_match_threshold:
                unmatched.append(target)
                continue
            key = (
                f"id:{int(best_unit.id)}"
                if best_unit.id is not None
                else f"url:{_sanitize_url(best_unit.url) or best_unit.url}"
            )
            selected[key] = best_unit
        return list(selected.values()), unmatched

    def _matches_target_org_unit_name(self, name: str) -> bool:
        if not self.target_org_units:
            return False
        return any(
            self._org_unit_match_score(target, name) >= self.org_unit_match_threshold
            for target in self.target_org_units
        )

    def _hard_excluded_org_units(self, org_units: list[OrgUnit]) -> tuple[list[OrgUnit], list[dict[str, str]]]:
        if not self.org_unit_exclude_enabled:
            return [], []
        payloads = [
            {
                "id": int(unit.id) if unit.id is not None else None,
                "name": unit.name,
                "url": unit.url,
                "kind": unit.kind,
            }
            for unit in org_units
        ]
        result = hard_filter_org_unit_payloads(
            payloads,
            exclude_enabled=True,
            keywords=self.org_unit_exclude_keywords,
            target_name_matcher=self._matches_target_org_unit_name,
        )
        excluded_keys = set()
        for item in result.hard_excluded:
            excluded_keys.update(org_unit_filter_item_keys(item.to_evidence()))
        excluded = [
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
        preview = [
            {
                "name": item.name,
                "url": item.url,
                "reason": item.reason,
            }
            for item in result.hard_excluded[:10]
        ]
        return excluded, preview

    async def _filter_existing_org_units_for_discovery(
        self,
        org_units: list[OrgUnit],
        *,
        source: str,
    ) -> list[OrgUnit]:
        if not org_units or self.target_org_units or not self.org_unit_exclude_enabled:
            return org_units

        payloads = [
            {
                "id": int(unit.id) if unit.id is not None else None,
                "name": unit.name,
                "url": unit.url,
                "kind": unit.kind,
            }
            for unit in org_units
        ]
        filtered_payloads = await self._filter_org_unit_payloads_for_discovery(
            payloads,
            source_url=self.start_url,
            source=source,
        )
        kept_keys: set[str] = set()
        for payload in filtered_payloads:
            kept_keys.update(self._org_unit_filter_item_keys(payload))
        return [
            unit
            for unit in org_units
            if self._org_unit_filter_item_keys(
                {
                    "id": int(unit.id) if unit.id is not None else None,
                    "name": unit.name,
                    "url": unit.url,
                }
            )
            & kept_keys
        ]

    async def _filter_org_unit_payloads_for_discovery(
        self,
        units: list[dict[str, Any]],
        *,
        source_url: str,
        source: str,
    ) -> list[dict[str, Any]]:
        if not units or self.target_org_units or not self.org_unit_exclude_enabled:
            return units

        hard_result = hard_filter_org_unit_payloads(
            units,
            exclude_enabled=True,
            keywords=self.org_unit_exclude_keywords,
            target_name_matcher=self._matches_target_org_unit_name,
        )
        if hard_result.hard_excluded:
            self.logger.info(
                "Org-unit hard exclusion university=%s source=%s page=%s excluded=%s sample=%s",
                self.university_name,
                source,
                source_url,
                len(hard_result.hard_excluded),
                [item.to_evidence() for item in hard_result.hard_excluded[:5]],
            )

        return await self._filter_org_unit_payloads_with_llm(
            hard_result.kept,
            source_url=source_url,
            source=source,
        )

    async def _filter_org_unit_payloads_with_llm(
        self,
        units: list[dict[str, Any]],
        *,
        source_url: str,
        source: str,
    ) -> list[dict[str, Any]]:
        if not units or not self.org_unit_llm_filter_enabled:
            return units

        skills_text = self.skill_manager.select_for_state(ORG_UNIT_FILTER_STATE, set()).rendered_text
        result = await llm_filter_org_unit_payloads(
            units,
            llm_client=self.llm_client,
            context_manager=self.context_manager,
            skills_text=skills_text,
            university=self.university_name,
            source_url=source_url,
            source=source,
            model_max_tokens=self.model_max_tokens,
            logger=self.logger,
        )
        dropped = len(units) - len(result.kept)
        if dropped:
            self.logger.info(
                "Org-unit LLM exclusion university=%s source=%s page=%s excluded=%s sample=%s",
                self.university_name,
                source,
                source_url,
                dropped,
                [item.to_evidence() for item in result.llm_excluded[:5]],
            )
        return result.kept

    def _org_unit_filter_item_keys(self, item: Any) -> set[str]:
        return org_unit_filter_item_keys(item)

    async def _set_org_unit_status(self, org_unit_id: int | None, status: OrgUnitStatus) -> None:
        if org_unit_id is None:
            return
        async with self.db.session() as session:
            await crawler_db.set_org_unit_status(session, int(org_unit_id), status)

    async def _mark_org_unit_no_faculty(self, org_unit: OrgUnit, page_url: str) -> None:
        if org_unit.id is None:
            return
        org_unit_id = int(org_unit.id)
        self._org_units_marked_no_faculty.add(org_unit_id)
        await self._set_org_unit_status(org_unit_id, OrgUnitStatus.NO_FACULTY_PAGE)
        self.logger.info(
            "Org unit marked no_faculty_page university=%s org_unit=%s page=%s",
            self.university_name,
            org_unit.name,
            page_url,
        )

    async def _mark_org_unit_has_faculty(self, org_unit: OrgUnit) -> None:
        if org_unit.id is None:
            return
        org_unit_id = int(org_unit.id)
        self._org_units_marked_no_faculty.discard(org_unit_id)
        await self._set_org_unit_status(org_unit_id, OrgUnitStatus.IN_PROGRESS)

    async def _find_and_extract_streaming(self, org_units: list[OrgUnit]) -> None:
        await self.faculty_discovery.find_and_extract_streaming(org_units)

    async def _find_and_extract_streaming_impl(self, org_units: list[OrgUnit]) -> None:
        """Interactive mode: for each org unit, find faculty pages then immediately extract professors."""
        self._log_state(CrawlerState.FIND_FACULTY_PAGES)
        max_links_per_org_unit = 4
        skills_find = await self._select_skills(CrawlerState.FIND_FACULTY_PAGES)
        llm_fallback_budget = 3

        preferred: list[OrgUnit] = []
        fallback: list[OrgUnit] = []
        for unit in org_units:
            kind = (unit.kind or "").strip().lower()
            if kind in {"division", "xuebu"}:
                continue
            if _is_core_academic_kind(kind):
                preferred.append(unit)
            else:
                fallback.append(unit)
        candidates = preferred or fallback or org_units

        start_host = (urlparse(self.start_url).hostname or "").lower()
        candidates = sorted(candidates, key=lambda unit: _org_unit_faculty_priority(unit, start_host))
        self._log_org_unit_queue_preview(candidates, start_host, stage="streaming")

        for org_unit in candidates[: self.max_org_units_per_university]:
            item = _QueuedUrl(url=org_unit.url, depth=1, label=org_unit.name, org_unit_id=org_unit.id)

            if _is_faculty_platform(item.url):
                continue
            item_host = (urlparse(item.url).hostname or "").lower()
            if item_host in self._blocked_hosts:
                continue

            fetched = await self._fetch_url(item.url, item.depth)
            if fetched is None:
                continue

            # --- Find faculty links for this org unit ---
            links, used_budget = await self._select_faculty_candidates(
                links=list(fetched.links or []),
                fetched=fetched,
                org_unit_name=org_unit.name,
                org_unit_url=fetched.url,
                llm_budget=llm_fallback_budget,
                max_candidates=max_links_per_org_unit,
                link_signals=getattr(fetched, "link_signals", ()),
            )
            llm_fallback_budget = max(0, llm_fallback_budget - used_budget)
            low_info, low_info_reason = self._should_skip_faculty_discovery_llm(fetched)

            if not links and _is_college_subdomain(fetched.url, self.start_url):
                links, used_budget = await self._select_faculty_candidates(
                    links=await self._probe_faculty_paths(fetched.url),
                    fetched=fetched,
                    org_unit_name=org_unit.name,
                    org_unit_url=fetched.url,
                    llm_budget=llm_fallback_budget,
                    max_candidates=max_links_per_org_unit,
                )
                llm_fallback_budget = max(0, llm_fallback_budget - used_budget)
            if not links and _looks_like_faculty_page(fetched.url):
                links, used_budget = await self._select_faculty_candidates(
                    links=[fetched.url],
                    fetched=fetched,
                    org_unit_name=org_unit.name,
                    org_unit_url=fetched.url,
                    llm_budget=llm_fallback_budget,
                    max_candidates=max_links_per_org_unit,
                )
                llm_fallback_budget = max(0, llm_fallback_budget - used_budget)
            if not links and llm_fallback_budget > 0 and not low_info:
                llm_fallback_budget -= 1
                result = await self._ask_llm(
                    CrawlerState.FIND_FACULTY_PAGES,
                    "Find links that lead to faculty list pages for this org unit.",
                    fetched, skills_find,
                )
                llm_links = self._links_from_result(result.content)
                if not llm_links:
                    llm_links = self._links_from_tool_call_log(result, tool_name="extract_links")
                links, used_budget = await self._select_faculty_candidates(
                    links=llm_links,
                    fetched=fetched,
                    org_unit_name=org_unit.name,
                    org_unit_url=fetched.url,
                    llm_budget=llm_fallback_budget,
                    max_candidates=max_links_per_org_unit,
                )
                llm_fallback_budget = max(0, llm_fallback_budget - used_budget)
            elif not links and low_info:
                self.logger.debug(
                    "Skip faculty-page LLM fallback by gate org_unit=%s url=%s reason=%s",
                    org_unit.name,
                    fetched.url,
                    low_info_reason,
                )

            if not links:
                await self._mark_org_unit_no_faculty(org_unit, fetched.url)
                continue
            await self._mark_org_unit_has_faculty(org_unit)

            # --- Immediately extract professors from found links ---
            faculty_for_unit: list[_QueuedUrl] = []
            for link in links[:max_links_per_org_unit]:
                depth = item.depth + (0 if link == fetched.url else 1)
                if self._within_depth(depth):
                    faculty_for_unit.append(_QueuedUrl(url=link, depth=depth, label=item.label, org_unit_id=item.org_unit_id))

            if faculty_for_unit:
                self.logger.info(
                    "Streaming: extracting professors for %s (%d faculty pages)",
                    org_unit.name, len(faculty_for_unit),
                )
                await self._extract_professors(faculty_for_unit)


    async def _find_faculty_pages(self, org_units: list[OrgUnit]) -> list[_QueuedUrl]:
        return await self.faculty_discovery.find_faculty_pages(org_units)

    async def _find_faculty_pages_impl(self, org_units: list[OrgUnit]) -> list[_QueuedUrl]:
        self._log_state(CrawlerState.FIND_FACULTY_PAGES)
        faculty_links: list[_QueuedUrl] = []
        max_links_per_org_unit = 4
        skills = await self._select_skills(CrawlerState.FIND_FACULTY_PAGES)
        llm_fallback_budget = 3

        preferred: list[OrgUnit] = []
        fallback: list[OrgUnit] = []
        for unit in org_units:
            kind = (unit.kind or "").strip().lower()
            if kind in {"division", "xuebu"}:
                continue
            if _is_core_academic_kind(kind):
                preferred.append(unit)
            else:
                fallback.append(unit)
        candidates = preferred or fallback or org_units
        max_links = min(max(40, len(candidates) * 2), 160)

        start_host = (urlparse(self.start_url).hostname or "").lower()
        candidates = sorted(candidates, key=lambda unit: _org_unit_faculty_priority(unit, start_host))
        self._log_org_unit_queue_preview(candidates, start_host, stage="batch")

        for org_unit in candidates[: self.max_org_units_per_university]:
            item = _QueuedUrl(
                url=org_unit.url,
                depth=1,
                label=org_unit.name,
                org_unit_id=org_unit.id,
            )

            if _is_faculty_platform(item.url):
                self.logger.info("Skipping faculty platform URL: %s", item.url)
                self.execution_log.append(f"skip faculty_platform url={item.url}")
                continue

            item_host = (urlparse(item.url).hostname or "").lower()
            if item_host in self._blocked_hosts:
                self.logger.debug("Skipping %s: host already marked blocked", item.url)
                continue

            fetched = await self._fetch_url(item.url, item.depth)
            if fetched is None:
                continue

            links, used_budget = await self._select_faculty_candidates(
                links=list(fetched.links or []),
                fetched=fetched,
                org_unit_name=org_unit.name,
                org_unit_url=fetched.url,
                llm_budget=llm_fallback_budget,
                max_candidates=max_links_per_org_unit,
                link_signals=getattr(fetched, "link_signals", ()),
            )
            llm_fallback_budget = max(0, llm_fallback_budget - used_budget)
            low_info, low_info_reason = self._should_skip_faculty_discovery_llm(fetched)

            if not links and _is_college_subdomain(fetched.url, self.start_url):
                links, used_budget = await self._select_faculty_candidates(
                    links=await self._probe_faculty_paths(fetched.url),
                    fetched=fetched,
                    org_unit_name=org_unit.name,
                    org_unit_url=fetched.url,
                    llm_budget=llm_fallback_budget,
                    max_candidates=max_links_per_org_unit,
                )
                llm_fallback_budget = max(0, llm_fallback_budget - used_budget)

            if not links and _looks_like_faculty_page(fetched.url):
                links, used_budget = await self._select_faculty_candidates(
                    links=[fetched.url],
                    fetched=fetched,
                    org_unit_name=org_unit.name,
                    org_unit_url=fetched.url,
                    llm_budget=llm_fallback_budget,
                    max_candidates=max_links_per_org_unit,
                )
                llm_fallback_budget = max(0, llm_fallback_budget - used_budget)

            if not links and llm_fallback_budget > 0 and not low_info:
                llm_fallback_budget -= 1
                result = await self._ask_llm(
                    CrawlerState.FIND_FACULTY_PAGES,
                    "Find links that lead to faculty list pages for this org unit (e.g. faculty, teacher, staff, people, szdw, jsdw). Return only links.",
                    fetched,
                    skills,
                )
                llm_links = self._links_from_result(result.content)
                if not llm_links:
                    llm_links = self._links_from_tool_call_log(result, tool_name="extract_links")
                    if llm_links:
                        self.logger.debug(
                            "Faculty discovery consumed extract_links tool output org_unit=%s sample=%s",
                            item.label,
                            llm_links[:5],
                        )
                links, used_budget = await self._select_faculty_candidates(
                    links=llm_links,
                    fetched=fetched,
                    org_unit_name=org_unit.name,
                    org_unit_url=fetched.url,
                    llm_budget=llm_fallback_budget,
                    max_candidates=max_links_per_org_unit,
                )
                llm_fallback_budget = max(0, llm_fallback_budget - used_budget)
            elif not links and low_info:
                self.logger.debug(
                    "Skip faculty-page LLM fallback by gate org_unit=%s url=%s reason=%s",
                    org_unit.name,
                    fetched.url,
                    low_info_reason,
                )

            if not links:
                await self._mark_org_unit_no_faculty(org_unit, fetched.url)
                continue
            await self._mark_org_unit_has_faculty(org_unit)

            for link in links[:max_links_per_org_unit]:
                depth = item.depth + (0 if link == fetched.url else 1)
                if self._within_depth(depth):
                    faculty_links.append(
                        _QueuedUrl(
                            url=link,
                            depth=depth,
                            label=item.label,
                            org_unit_id=item.org_unit_id,
                        )
                    )

        if not faculty_links:
            self.logger.info("No faculty links from org units, trying search engine fallback")
            search_links = await self._search_engine_fallback("faculty teachers professors list szdw jsdw")
            allowed_hosts = {
                (urlparse(unit.url).hostname or "").lower()
                for unit in candidates
                if (urlparse(unit.url).hostname or "").lower()
            }
            search_links, used_budget = await self._select_faculty_candidates(
                links=search_links,
                fetched=FetchResult(
                    url=self.start_url,
                    text="",
                    links=search_links,
                    status_code=200,
                ),
                org_unit_name="Unknown",
                org_unit_hosts=allowed_hosts,
                llm_budget=llm_fallback_budget,
                max_candidates=max_links_per_org_unit,
            )
            llm_fallback_budget = max(0, llm_fallback_budget - used_budget)
            for link in search_links:
                if self._within_depth(2):
                    faculty_links.append(_QueuedUrl(url=link, depth=2, label="Unknown"))
                if len(faculty_links) >= max_links:
                    break

        return _dedupe_queue(faculty_links)[:max_links]
    async def _probe_faculty_paths(self, org_unit_url: str) -> list[str]:
        """Try common Chinese university faculty page paths on an org unit subdomain."""
        parsed = urlparse(org_unit_url)
        host = (parsed.hostname or "").lower()
        if host in self._blocked_hosts:
            self.logger.debug("Skip faculty path probing for blocked host: %s", host)
            return []
        base = f"{parsed.scheme}://{parsed.hostname}"
        found: list[str] = []
        for suffix in _COMMON_FACULTY_PATHS:
            probe_url = base + suffix
            if probe_url in self.visited_urls:
                continue
            result = await self._fetch_url(probe_url, 2)
            if result is None:
                continue
            if result.block_reason:
                self.logger.info(
                    "Faculty probe blocked url=%s status=%s reason=%s",
                    probe_url,
                    result.status_code,
                    result.block_reason,
                )
                self.execution_log.append(
                    f"probe_blocked url={probe_url} status={result.status_code} reason={result.block_reason}"
                )
                continue
            if result.status_code == 200 and len(result.text) > 200:
                found.append(probe_url)
                self.logger.info("Probed faculty path found: %s", probe_url)
                self.execution_log.append(f"probe_found url={probe_url}")
                break
            self.logger.debug(
                "Faculty probe miss url=%s status=%s text_chars=%s links=%s",
                probe_url,
                result.status_code,
                len(result.text),
                len(result.links),
            )
        return found

    async def _probe_intermediate_org_pages(self) -> list[str]:
        """Try common org-unit listing paths on the university main domain."""
        parsed = urlparse(self.start_url)
        base = f"{parsed.scheme}://{parsed.hostname}"
        found: list[str] = []
        for suffix in _INTERMEDIATE_ORG_PATHS:
            probe_url = base + suffix
            if probe_url in self.visited_urls:
                continue
            result = await self._fetch_url(probe_url, 1)
            if result is None:
                continue
            if result.block_reason:
                self.logger.info(
                    "Org-page probe blocked url=%s status=%s reason=%s",
                    probe_url,
                    result.status_code,
                    result.block_reason,
                )
                self.execution_log.append(
                    f"probe_org_blocked url={probe_url} status={result.status_code} reason={result.block_reason}"
                )
                continue
            if result.status_code == 200 and len(result.text) > 200:
                found.append(probe_url)
                self.logger.info("Probed org page found: %s", probe_url)
                self.execution_log.append(f"probe_org_found url={probe_url}")
                break
            self.logger.debug(
                "Org-page probe miss url=%s status=%s text_chars=%s links=%s",
                probe_url,
                result.status_code,
                len(result.text),
                len(result.links),
            )
        return found
    async def _extract_professors(
        self,
        faculty_links: list[_QueuedUrl],
        *,
        recovery_limit: int | None = None,
    ) -> None:
        self._log_state(CrawlerState.EXTRACT_PROFESSORS)
        skills = await self._select_skills(CrawlerState.EXTRACT_PROFESSORS)
        max_pages = min(max(40, len(faculty_links)), 120)
        self.logger.info(
            "Extraction pipeline enabled=%s llm_workers=%s db_workers=%s queue_cap=%s retry=%s llm_max_concurrent=%s llm_min_interval_seconds=%s",
            self.pipeline_enabled,
            self.pipeline_llm_workers,
            self.pipeline_db_workers,
            self.pipeline_queue_cap,
            self.invalid_json_max_retry,
            getattr(self.llm_client, "max_concurrent", "unknown"),
            getattr(self.llm_client, "min_interval", "unknown"),
        )

        scheduled_urls: set[str] = set()
        processed_urls: set[str] = set()

        def _queue_key(url: str) -> str:
            return _sanitize_url(url) or (url or "").strip()

        def _mark_scheduled(item_or_url: _QueuedUrl | str) -> bool:
            url = item_or_url.queue_url if isinstance(item_or_url, _QueuedUrl) else item_or_url
            key = _queue_key(url)
            if not key:
                return False
            if key in scheduled_urls or key in processed_urls:
                self._pipeline_stats["duplicate_tasks_skipped"] = int(
                    self._pipeline_stats.get("duplicate_tasks_skipped", 0)
                ) + 1
                return False
            scheduled_urls.add(key)
            return True

        def _mark_processing(item_or_url: _QueuedUrl | str) -> bool:
            url = item_or_url.queue_url if isinstance(item_or_url, _QueuedUrl) else item_or_url
            key = _queue_key(url)
            if not key:
                return False
            if key in processed_urls:
                self._pipeline_stats["duplicate_tasks_skipped"] = int(
                    self._pipeline_stats.get("duplicate_tasks_skipped", 0)
                ) + 1
                return False
            processed_urls.add(key)
            return True

        def _schedule_related_pages(
            current: _QueuedUrl,
            fetched: FetchResult,
            pages_to_process: list[_QueuedUrl],
        ) -> set[str]:
            added_followups: list[str] = []
            skipped_duplicates = 0
            followups = self._extract_followup_faculty_links(fetched.links, fetched.url)
            for link in followups[:_FOLLOWUP_PAGE_LIMIT]:
                next_depth = current.depth + 1
                if not self._within_depth(next_depth):
                    continue
                followup_item = _QueuedUrl(
                    url=link,
                    depth=next_depth,
                    label=current.label,
                    org_unit_id=current.org_unit_id,
                )
                if not _mark_scheduled(followup_item):
                    skipped_duplicates += 1
                    continue
                pages_to_process.append(followup_item)
                added_followups.append(link)

            added_pagination: list[str] = []
            pagination_links = self._extract_pagination_links(fetched.links, fetched.url)
            for plink in pagination_links:
                if not self._within_depth(current.depth):
                    continue
                page_item = _QueuedUrl(
                    url=plink,
                    depth=current.depth,
                    label=current.label,
                    org_unit_id=current.org_unit_id,
                )
                if not _mark_scheduled(page_item):
                    skipped_duplicates += 1
                    continue
                pages_to_process.append(page_item)
                added_pagination.append(plink)

            for state in getattr(fetched, "pagination_states", ()) or ():
                action = form_pagination.pagination_state_to_fetch_action(state)
                if not action:
                    continue
                identity_url = str(action.get("synthetic_url") or "").strip()
                if not identity_url:
                    continue
                page_item = _QueuedUrl(
                    url=str(action.get("url") or fetched.url),
                    depth=current.depth,
                    label=current.label,
                    org_unit_id=current.org_unit_id,
                    fetch_action=action,
                    identity_url=identity_url,
                )
                if not _mark_scheduled(page_item):
                    skipped_duplicates += 1
                    continue
                pages_to_process.append(page_item)
                added_pagination.append(identity_url)

            if added_followups:
                self._pipeline_stats["followups_scheduled"] = int(
                    self._pipeline_stats.get("followups_scheduled", 0)
                ) + len(added_followups)
            if added_pagination:
                self._pipeline_stats["pagination_scheduled"] = int(
                    self._pipeline_stats.get("pagination_scheduled", 0)
                ) + len(added_pagination)
            if skipped_duplicates:
                self._pipeline_stats["duplicate_followups_skipped"] = int(
                    self._pipeline_stats.get("duplicate_followups_skipped", 0)
                ) + skipped_duplicates
            if added_followups or added_pagination or skipped_duplicates:
                sample = (added_followups + added_pagination)[:5]
                self.logger.debug(
                    "Queued faculty followups current=%s added_followups=%s added_pagination=%s skipped_duplicates=%s sample=%s",
                    fetched.url,
                    len(added_followups),
                    len(added_pagination),
                    skipped_duplicates,
                    sample,
                )
            return {_queue_key(url) for url in added_followups + added_pagination if _queue_key(url)}

        if not self.pipeline_enabled:
            for item in faculty_links[:max_pages]:
                if not _mark_scheduled(item):
                    continue
                pages_to_process = [item]
                while pages_to_process:
                    current = pages_to_process.pop(0)
                    if not _mark_processing(current):
                        continue
                    if self._is_noise_or_login_candidate(current.url):
                        self.logger.debug("Skip noise/login candidate before fetch url=%s", current.url)
                        continue
                    fetched = await self._fetch_url(current.url, current.depth, action=current.fetch_action, identity_url=current.identity_url)
                    if fetched is None:
                        continue
                    if self._is_noise_or_login_candidate(fetched.url):
                        self.logger.info("Skip noise/login faculty page url=%s", fetched.url)
                        continue
                    if self._is_retired_page(fetched):
                        self.logger.info("Skip retired faculty page url=%s", fetched.url)
                        continue
                    skip_llm, skip_reason = self._should_skip_professor_llm(url=fetched.url, text=fetched.text)
                    if skip_llm:
                        self._pipeline_stats["llm_calls_skipped_by_gate"] = int(
                            self._pipeline_stats.get("llm_calls_skipped_by_gate", 0)
                        ) + 1
                        self.logger.debug(
                            "Skip professor LLM extraction by gate url=%s reason=%s",
                            fetched.url,
                            skip_reason,
                        )
                    else:
                        await self._extract_professors_from_page(
                            current,
                            fetched,
                            skills,
                            detail_mode=False,
                        )
                    reserved_urls = _schedule_related_pages(current, fetched, pages_to_process)
                    await self._enrich_profiles_with_detail_backend(
                        current,
                        fetched,
                        skills,
                        reserved_urls=reserved_urls,
                    )
            return

        llm_queue: asyncio.Queue[_ExtractionTaskItem | None] = asyncio.Queue(maxsize=self.pipeline_queue_cap)
        db_queue: asyncio.Queue[_SaveEvent | None] = asyncio.Queue(maxsize=self.pipeline_queue_cap)

        llm_workers = [
            asyncio.create_task(self._pipeline_llm_worker(llm_queue, db_queue, skills), name=f"llm_worker_{i}")
            for i in range(self.pipeline_llm_workers)
        ]
        db_workers = [
            asyncio.create_task(self._pipeline_db_worker(db_queue), name=f"db_worker_{i}")
            for i in range(self.pipeline_db_workers)
        ]

        try:
            if self.pipeline_enabled and self.task_recovery_enabled:
                default_recovery_limit = self.pipeline_queue_cap * 4
                effective_recovery_limit = max(default_recovery_limit, int(recovery_limit or 0))
                recovered_count = await self._recover_pipeline_tasks(
                    llm_queue=llm_queue,
                    limit=effective_recovery_limit,
                )
                if recovered_count:
                    self.logger.info(
                        "Recovered %s pending extraction tasks from DB queue_cap=%s recovery_limit=%s",
                        recovered_count,
                        self.pipeline_queue_cap,
                        effective_recovery_limit,
                    )

            for item in faculty_links[:max_pages]:
                if not _mark_scheduled(item):
                    continue
                pages_to_process = [item]
                while pages_to_process:
                    current = pages_to_process.pop(0)
                    if not _mark_processing(current):
                        continue
                    if self._is_noise_or_login_candidate(current.url):
                        self.logger.debug("Skip noise/login candidate before fetch url=%s", current.url)
                        continue
                    fetched = await self._fetch_url(current.url, current.depth, action=current.fetch_action, identity_url=current.identity_url)
                    if fetched is None:
                        continue
                    if self._is_noise_or_login_candidate(fetched.url):
                        self.logger.info("Skip noise/login faculty page url=%s", fetched.url)
                        continue
                    if self._is_retired_page(fetched):
                        self.logger.info("Skip retired faculty page url=%s", fetched.url)
                        continue
                    skip_llm, skip_reason = self._should_skip_professor_llm(url=fetched.url, text=fetched.text)
                    if skip_llm:
                        self._pipeline_stats["llm_calls_skipped_by_gate"] = int(
                            self._pipeline_stats.get("llm_calls_skipped_by_gate", 0)
                        ) + 1
                        self.logger.debug(
                            "Skip professor LLM enqueue by gate url=%s reason=%s",
                            fetched.url,
                            skip_reason,
                        )
                    else:
                        await self._enqueue_extraction_task(
                            current,
                            fetched,
                            llm_queue=llm_queue,
                            detail_mode=False,
                            priority=0,
                        )
                    reserved_urls = _schedule_related_pages(current, fetched, pages_to_process)
                    previous_detail_queue = self._active_detail_llm_queue
                    self._active_detail_llm_queue = llm_queue
                    try:
                        await self._enrich_profiles_with_detail_backend(
                            current,
                            fetched,
                            skills,
                            reserved_urls=reserved_urls,
                        )
                    finally:
                        self._active_detail_llm_queue = previous_detail_queue
        finally:
            await llm_queue.join()
            for _ in llm_workers:
                await llm_queue.put(None)
            await asyncio.gather(*llm_workers, return_exceptions=False)

            await db_queue.join()
            for _ in db_workers:
                await db_queue.put(None)
            await asyncio.gather(*db_workers, return_exceptions=False)

            self.logger.info(
                "Extraction pipeline stats queue_depth=%s processed=%s retries=%s failed=%s list_processed=%s list_failed=%s detail_enqueued=%s detail_processed=%s detail_failed=%s detail_skipped=%s records_accepted=%s records_created=%s records_updated=%s records_unchanged=%s deduped_by_name_key=%s deduped_by_homepage=%s list_roster_overlap_high=%s stale_in_progress_recovered=%s recovery_refetched=%s recovery_enqueued=%s recovery_consumed=%s recovery_refetch_skipped_with_snapshot=%s avg_task_ms=%.1f llm_calls=%s skipped_by_gate=%s followups=%s pagination=%s duplicate_skipped=%s detail_dirs_skipped=%s detail_reserved_for_list=%s detail_directory_skipped=%s avg_payload_bytes=%.1f",
                self._pipeline_stats.get("queue_depth", 0),
                self._pipeline_stats.get("processed_tasks", 0),
                self._pipeline_stats.get("retries", 0),
                self._pipeline_stats.get("failed", 0),
                self._pipeline_stats.get("list_processed", 0),
                self._pipeline_stats.get("list_failed", 0),
                self._pipeline_stats.get("detail_enqueued", 0),
                self._pipeline_stats.get("detail_processed", 0),
                self._pipeline_stats.get("detail_failed", 0),
                self._pipeline_stats.get("detail_skipped", 0),
                self._pipeline_stats.get("records_accepted", 0),
                self._pipeline_stats.get("records_created", 0),
                self._pipeline_stats.get("records_updated", 0),
                self._pipeline_stats.get("records_unchanged", 0),
                self._pipeline_stats.get("deduped_by_name_key", 0),
                self._pipeline_stats.get("deduped_by_homepage", 0),
                self._pipeline_stats.get("list_roster_overlap_high", 0),
                self._pipeline_stats.get("stale_in_progress_recovered", 0),
                self._pipeline_stats.get("recovery_refetched", 0),
                self._pipeline_stats.get("recovery_enqueued", 0),
                self._pipeline_stats.get("recovery_consumed", 0),
                self._pipeline_stats.get("recovery_refetch_skipped_with_snapshot", 0),
                float(self._pipeline_stats.get("average_task_ms", 0.0)),
                self._pipeline_stats.get("llm_calls_total", 0),
                self._pipeline_stats.get("llm_calls_skipped_by_gate", 0),
                self._pipeline_stats.get("followups_scheduled", 0),
                self._pipeline_stats.get("pagination_scheduled", 0),
                self._pipeline_stats.get("duplicate_tasks_skipped", 0),
                self._pipeline_stats.get("detail_links_dropped_directory", 0),
                self._pipeline_stats.get("detail_links_reserved_for_list", 0),
                self._pipeline_stats.get("detail_directory_skipped", 0),
                float(self._pipeline_stats.get("avg_payload_bytes", 0.0)),
            )

    @staticmethod
    def _redirect_identity_key(url: str) -> tuple[str, str, str]:
        clean = _sanitize_url(url) or url or ""
        parsed = urlparse(clean)
        path = (parsed.path or "/").lower()
        path = re.sub(r"/(?:index|default)\.(?:html?|shtml|php|jsp|aspx?)$", "/", path)
        if path != "/":
            path = path.rstrip("/")
        return ((parsed.hostname or "").lower(), path or "/", parsed.query)

    @staticmethod
    def _is_home_path(path: str) -> bool:
        normalized = (path or "/").lower()
        normalized = re.sub(r"/(?:index|default)\.(?:html?|shtml|php|jsp|aspx?)$", "/", normalized)
        return normalized in {"", "/"}

    def _should_skip_redirected_extraction(
        self,
        requested_url: str,
        final_url: str,
        *,
        detail_mode: bool,
    ) -> tuple[bool, str]:
        requested = _sanitize_url(requested_url) or ""
        final = _sanitize_url(final_url) or requested
        if not requested or not final:
            return False, ""
        if self._redirect_identity_key(requested) == self._redirect_identity_key(final):
            return False, ""

        requested_path = urlparse(requested).path or "/"
        final_path = urlparse(final).path or "/"
        if self._is_home_path(final_path) and not self._is_home_path(requested_path):
            return True, "redirect_to_home"

        current_dir = self._derive_section_prefix(requested_path)
        if not current_dir:
            return False, ""
        prefix = current_dir.rstrip("/")
        normalized_final_path = (final_path or "/").lower().rstrip("/") or "/"
        if normalized_final_path == prefix or normalized_final_path.startswith(prefix + "/"):
            return False, ""
        if detail_mode and agent_detail._looks_like_profile_detail_url(final):
            return False, ""
        if not detail_mode and _looks_like_faculty_page(final):
            return False, ""
        return True, "redirect_left_section"

    def _record_redirected_extraction_skip(self, *, detail_mode: bool) -> None:
        self._pipeline_stats["redirect_skipped"] = int(self._pipeline_stats.get("redirect_skipped", 0)) + 1
        key = "detail_redirect_skipped" if detail_mode else "list_redirect_skipped"
        self._pipeline_stats[key] = int(self._pipeline_stats.get(key, 0)) + 1
        if detail_mode:
            self._pipeline_stats["detail_skipped"] = int(self._pipeline_stats.get("detail_skipped", 0)) + 1
        else:
            self._pipeline_stats["list_skipped"] = int(self._pipeline_stats.get("list_skipped", 0)) + 1

    async def _enqueue_extraction_task(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        *,
        llm_queue: asyncio.Queue[_ExtractionTaskItem | None],
        detail_mode: bool,
        priority: int,
        requested_url: str | None = None,
    ) -> None:
        source_url = _sanitize_url(requested_url or current.identity_url or current.url) or _sanitize_url(fetched.url) or ""
        if not source_url:
            return
        final_url = _sanitize_url(fetched.url) or source_url
        skip_redirect, redirect_reason = self._should_skip_redirected_extraction(
            source_url,
            final_url,
            detail_mode=detail_mode,
        )
        if skip_redirect:
            self._record_redirected_extraction_skip(detail_mode=detail_mode)
            self.logger.warning(
                "Skip extraction task after redirect source=%s final=%s detail_mode=%s reason=%s",
                source_url,
                final_url,
                detail_mode,
                redirect_reason,
            )
            return
        if final_url != source_url:
            self.logger.debug(
                "Extraction task keeps requested URL after redirect source=%s final=%s detail_mode=%s",
                source_url,
                final_url,
                detail_mode,
            )
        text_limit = self._state_text_limit(CrawlerState.EXTRACT_PROFESSORS, detail_mode=detail_mode)
        snapshot = self._compact_page_text(fetched.text or "", text_limit)
        page_hash = hashlib.sha1(f"{source_url}|{snapshot}".encode("utf-8", errors="ignore")).hexdigest()
        allowed_tools = ["save_professors"]
        task_kind = CrawlTaskKind.DETAIL_PAGE.value if detail_mode else CrawlTaskKind.LIST_PAGE.value
        name_homepage_candidates = self._extract_name_homepage_candidates(
            fetched,
            source_url=source_url,
            detail_mode=detail_mode,
        )
        async with self.db.session() as session:
            row = await crawler_db.upsert_crawl_task(
                session,
                university=self.university_name,
                org_unit_name=current.label or "Unknown",
                org_unit_url=current.url,
                source_url=source_url,
                page_url=source_url,
                page_hash=page_hash,
                task_kind=task_kind,
                page_text_snapshot=snapshot,
                allowed_tools=json.dumps(sorted(allowed_tools), ensure_ascii=False, separators=(",", ":")),
                attempt=0,
                priority=priority,
                status=CrawlTaskStatus.PENDING,
            )
            if row.status != CrawlTaskStatus.PENDING.value:
                self._pipeline_stats["duplicate_tasks_skipped"] = int(
                    self._pipeline_stats.get("duplicate_tasks_skipped", 0)
                ) + 1
                if detail_mode:
                    self._pipeline_stats["detail_skipped"] = int(self._pipeline_stats.get("detail_skipped", 0)) + 1
                else:
                    self._pipeline_stats["list_skipped"] = int(self._pipeline_stats.get("list_skipped", 0)) + 1
                self.logger.debug(
                    "Skip existing extraction task source=%s status=%s task_id=%s",
                    source_url,
                    row.status,
                    row.id,
                )
                return
            task = _ExtractionTaskItem(
                task_id=int(row.id),
                university=self.university_name,
                org_unit_name=row.org_unit_name,
                org_unit_url=row.org_unit_url,
                source_url=row.source_url,
                page_url=row.page_url,
                page_hash=row.page_hash,
                page_text_snapshot=row.page_text_snapshot,
                allowed_tools=allowed_tools,
                attempt=int(row.attempt or 0),
                priority=int(row.priority or 0),
                strict_retry=False,
                detail_mode=detail_mode,
                task_kind=str(getattr(row, "task_kind", None) or task_kind),
                name_homepage_candidates=name_homepage_candidates,
            )
        await llm_queue.put(task)
        self._pipeline_stats["pending"] = int(self._pipeline_stats.get("pending", 0)) + 1
        if detail_mode:
            self._pipeline_stats["detail_enqueued"] = int(self._pipeline_stats.get("detail_enqueued", 0)) + 1
        else:
            self._pipeline_stats["list_enqueued"] = int(self._pipeline_stats.get("list_enqueued", 0)) + 1
        self._pipeline_stats["queue_depth"] = llm_queue.qsize()

    def _extract_name_homepage_candidates(
        self,
        fetched: FetchResult | None,
        *,
        source_url: str,
        detail_mode: bool,
    ) -> dict[str, str]:
        if detail_mode or fetched is None:
            return {}
        candidates: dict[str, str] = {}
        signals = getattr(fetched, "link_signals", ()) or ()
        for sig in signals:
            anchor = str(getattr(sig, "anchor_text", "") or "").strip()
            url = str(getattr(sig, "url", "") or "").strip()
            self._add_name_homepage_candidate(candidates, anchor, url, source_url=source_url)
        text = str(getattr(fetched, "text", "") or "")
        for match in re.finditer(r"\[([^\]\n]{1,40})\]\(([^)\s]+)\)", text):
            self._add_name_homepage_candidate(
                candidates,
                match.group(1),
                match.group(2),
                source_url=source_url,
            )
        return candidates

    def _extract_name_homepage_candidates_from_snapshot(self, snapshot: str, *, source_url: str) -> dict[str, str]:
        candidates: dict[str, str] = {}
        for match in re.finditer(r"\[([^\]\n]{1,40})\]\(([^)\s]+)\)", str(snapshot or "")):
            self._add_name_homepage_candidate(
                candidates,
                match.group(1),
                match.group(2),
                source_url=source_url,
            )
        return candidates

    def _add_name_homepage_candidate(
        self,
        candidates: dict[str, str],
        anchor: str,
        url: str,
        *,
        source_url: str,
    ) -> None:
        key = self._name_homepage_anchor_key(anchor)
        if not key:
            return
        homepage = self._normalize_primary_profile_homepage(url, source_url=source_url)
        if not homepage:
            return
        existing = candidates.get(key)
        if existing and len(existing) >= len(homepage):
            return
        candidates[key] = homepage

    def _name_homepage_anchor_key(self, anchor: Any) -> str:
        cleaned = agent_detail._normalize_anchor_for_name_match(str(anchor or ""))
        if not cleaned:
            return ""
        compact = re.sub(r"[\s·•\-_/|:：,，.。()（）\[\]【】]+", "", cleaned)
        if not compact:
            return ""
        if any(token in compact for token in agent_detail._PERSON_ANCHOR_BLOCKLIST):
            return ""
        cjk_chars = re.findall(r"[\u4e00-\u9fff]", compact)
        if 2 <= len(cjk_chars) <= 4 and len(compact) <= 6:
            return normalize_name_key(compact)
        words = re.findall(r"[a-z][a-z'.-]+", cleaned.lower())
        if 2 <= len(words) <= 4 and len("".join(words)) >= 4:
            return normalize_name_key(" ".join(words))
        return ""

    def _normalize_primary_profile_homepage(self, url: Any, *, source_url: str) -> str | None:
        candidate = str(url or "").strip()
        if not candidate:
            return None
        parsed = urlparse(candidate)
        if not parsed.scheme or not parsed.netloc:
            candidate = urljoin(source_url or self.start_url, candidate)
        homepage = normalize_professor_homepage(candidate)
        if not homepage:
            return None
        if _is_faculty_platform(homepage):
            return None
        if not _same_site(homepage, source_url or self.start_url):
            return None
        if not agent_detail._looks_like_profile_detail_url(homepage):
            return None
        return homepage

    async def _recover_pipeline_tasks(
        self,
        *,
        llm_queue: asyncio.Queue[_ExtractionTaskItem | None],
        limit: int,
    ) -> int:
        async with self.db.session() as session:
            stale_count = await crawler_db.recover_stale_in_progress_crawl_tasks(session)
            rows = await crawler_db.list_recoverable_crawl_tasks(session, limit=limit)
        if stale_count:
            self._pipeline_stats["stale_in_progress_recovered"] = int(
                self._pipeline_stats.get("stale_in_progress_recovered", 0)
            ) + int(stale_count)
            self.logger.info("Recovered %s stale in_progress extraction tasks", stale_count)
        recovered_count = 0
        for row in rows:
            allowed_tools = ["save_professors"]
            task_kind = str(getattr(row, "task_kind", None) or CrawlTaskKind.LIST_PAGE.value)
            detail_mode = task_kind == CrawlTaskKind.DETAIL_PAGE.value
            row_data = {
                "id": int(row.id),
                "university": row.university or self.university_name,
                "org_unit_name": row.org_unit_name,
                "org_unit_url": row.org_unit_url,
                "source_url": row.source_url,
                "page_url": row.page_url,
                "page_hash": row.page_hash,
                "page_text_snapshot": row.page_text_snapshot,
                "attempt": int(row.attempt or 0),
                "priority": int(row.priority or 0),
                "status": row.status,
                "last_error": row.last_error,
            }
            if self._recovered_task_needs_refetch(row_data, detail_mode=detail_mode):
                refreshed = await self._refetch_recovered_detail_task(row_data)
                if refreshed is None:
                    continue
                row_data = refreshed
            if row.allowed_tools:
                try:
                    parsed = json.loads(row.allowed_tools)
                    if isinstance(parsed, list) and parsed:
                        allowed_tools = [str(item) for item in parsed]
                except Exception:
                    pass
            task = _ExtractionTaskItem(
                task_id=int(row_data["id"]),
                university=str(row_data["university"] or self.university_name),
                org_unit_name=str(row_data["org_unit_name"] or "Unknown"),
                org_unit_url=row_data["org_unit_url"],
                source_url=str(row_data["source_url"] or ""),
                page_url=str(row_data["page_url"] or row_data["source_url"] or ""),
                page_hash=str(row_data["page_hash"] or ""),
                page_text_snapshot=str(row_data["page_text_snapshot"] or ""),
                allowed_tools=allowed_tools,
                attempt=int(row_data["attempt"] or 0),
                priority=int(row_data["priority"] or 0),
                strict_retry=(int(row_data["attempt"] or 0) > 0),
                detail_mode=detail_mode,
                task_kind=task_kind,
                recovered=True,
                name_homepage_candidates=(
                    {}
                    if detail_mode
                    else self._extract_name_homepage_candidates_from_snapshot(
                        str(row_data["page_text_snapshot"] or ""),
                        source_url=str(row_data["source_url"] or row_data["page_url"] or ""),
                    )
                ),
            )
            if row_data["status"] == CrawlTaskStatus.RETRY.value:
                self._pipeline_stats["retry"] = int(self._pipeline_stats.get("retry", 0)) + 1
            else:
                self._pipeline_stats["pending"] = int(self._pipeline_stats.get("pending", 0)) + 1
            if detail_mode:
                self._pipeline_stats["detail_enqueued"] = int(self._pipeline_stats.get("detail_enqueued", 0)) + 1
            else:
                self._pipeline_stats["list_enqueued"] = int(self._pipeline_stats.get("list_enqueued", 0)) + 1
            await llm_queue.put(task)
            recovered_count += 1
            self._pipeline_stats["recovery_enqueued"] = int(
                self._pipeline_stats.get("recovery_enqueued", 0)
            ) + 1
            self._pipeline_stats["queue_depth"] = llm_queue.qsize()
        return recovered_count

    def _recovered_task_needs_refetch(self, row_data: dict[str, Any], *, detail_mode: bool) -> bool:
        if not detail_mode:
            return False
        last_error = str(row_data.get("last_error") or "")
        snapshot = str(row_data.get("page_text_snapshot") or "")
        if not snapshot.strip():
            return True
        if last_error == "completion_recrawl_refetched":
            self._pipeline_stats["recovery_refetch_skipped_with_snapshot"] = int(
                self._pipeline_stats.get("recovery_refetch_skipped_with_snapshot", 0)
            ) + 1
            return False
        return last_error.startswith(_COMPLETION_RECRAWL_LAST_ERROR_PREFIX)

    async def _refetch_recovered_detail_task(self, row_data: dict[str, Any]) -> dict[str, Any] | None:
        task_id = int(row_data["id"])
        source_url = _sanitize_url(str(row_data.get("source_url") or row_data.get("page_url") or ""))
        if not source_url:
            await self._mark_recovered_refetch_retry(task_id, _COMPLETION_RECRAWL_REFETCH_FAILED)
            return None

        self._add_resume_force_refetch_urls([source_url])
        fetched = await self._fetch_url(source_url, 1)
        if fetched is None:
            await self._mark_recovered_refetch_retry(task_id, _COMPLETION_RECRAWL_REFETCH_FAILED)
            self.logger.warning(
                "Recovered detail task refetch failed task_id=%s source=%s",
                task_id,
                source_url,
            )
            return None
        if fetched.block_reason:
            last_error = f"{_COMPLETION_RECRAWL_REFETCH_BLOCKED_PREFIX}:{fetched.block_reason}"
            await self._mark_recovered_refetch_retry(task_id, last_error)
            self.logger.warning(
                "Recovered detail task refetch blocked task_id=%s source=%s reason=%s",
                task_id,
                source_url,
                fetched.block_reason,
            )
            return None

        final_url = _sanitize_url(fetched.url) or source_url
        skip_redirect, redirect_reason = self._should_skip_redirected_extraction(
            source_url,
            final_url,
            detail_mode=True,
        )
        if skip_redirect:
            await self._mark_recovered_refetch_retry(task_id, _COMPLETION_RECRAWL_REFETCH_FAILED)
            self.logger.warning(
                "Recovered detail task refetch redirected away task_id=%s source=%s final=%s reason=%s",
                task_id,
                source_url,
                final_url,
                redirect_reason,
            )
            return None

        text_limit = self._state_text_limit(CrawlerState.EXTRACT_PROFESSORS, detail_mode=True)
        snapshot = self._compact_page_text(fetched.text or "", text_limit)
        if not snapshot.strip():
            await self._mark_recovered_refetch_retry(task_id, _COMPLETION_RECRAWL_REFETCH_FAILED)
            self.logger.warning(
                "Recovered detail task refetch produced empty snapshot task_id=%s source=%s final=%s",
                task_id,
                source_url,
                final_url,
            )
            return None

        page_hash = hashlib.sha1(f"{source_url}|{snapshot}".encode("utf-8", errors="ignore")).hexdigest()
        allowed_tools = json.dumps(["save_professors"], ensure_ascii=False, separators=(",", ":"))
        async with self.db.session() as session:
            refreshed = await crawler_db.upsert_crawl_task(
                session,
                university=str(row_data.get("university") or self.university_name),
                org_unit_name=str(row_data.get("org_unit_name") or "Unknown"),
                org_unit_url=(str(row_data.get("org_unit_url")) if row_data.get("org_unit_url") else None),
                source_url=source_url,
                page_url=source_url,
                page_hash=page_hash,
                task_kind=CrawlTaskKind.DETAIL_PAGE,
                page_text_snapshot=snapshot,
                allowed_tools=allowed_tools,
                attempt=int(row_data.get("attempt") or 0),
                priority=int(row_data.get("priority") or 0),
                status=CrawlTaskStatus.RETRY,
                last_error="completion_recrawl_refetched",
            )
            refreshed.page_text_snapshot = snapshot
            refreshed.page_hash = page_hash
            refreshed.page_url = source_url
            refreshed.allowed_tools = allowed_tools
            refreshed.task_kind = CrawlTaskKind.DETAIL_PAGE.value
            refreshed.status = CrawlTaskStatus.RETRY.value
            refreshed.last_error = "completion_recrawl_refetched"
            await session.flush()
            self._pipeline_stats["recovery_refetched"] = int(
                self._pipeline_stats.get("recovery_refetched", 0)
            ) + 1
            return {
                "id": int(refreshed.id),
                "university": refreshed.university or self.university_name,
                "org_unit_name": refreshed.org_unit_name,
                "org_unit_url": refreshed.org_unit_url,
                "source_url": refreshed.source_url,
                "page_url": refreshed.page_url,
                "page_hash": refreshed.page_hash,
                "page_text_snapshot": refreshed.page_text_snapshot,
                "attempt": int(refreshed.attempt or 0),
                "priority": int(refreshed.priority or 0),
                "status": refreshed.status,
                "last_error": refreshed.last_error,
            }

    async def _mark_recovered_refetch_retry(self, task_id: int, last_error: str) -> None:
        async with self.db.session() as session:
            await crawler_db.set_crawl_task_status(
                session,
                task_id,
                status=CrawlTaskStatus.RETRY,
                last_error=last_error,
            )
        self._pipeline_stats["detail_skipped"] = int(self._pipeline_stats.get("detail_skipped", 0)) + 1

    async def _mark_extraction_task_skipped_by_gate(self, task: _ExtractionTaskItem, reason: str) -> None:
        async with self.db.session() as session:
            await crawler_db.set_crawl_task_status(
                session,
                task.task_id,
                status=CrawlTaskStatus.DONE,
                attempt=task.attempt,
                last_error=f"skipped_by_gate:{reason or 'unknown'}",
            )
        self._pipeline_stats["done"] += 1
        self._pipeline_stats["processed_tasks"] += 1
        self._increment_task_kind_stat(task, "skipped")
        self.logger.info(
            "Skip extraction task by gate task_id=%s org_unit=%s url=%s reason=%s",
            task.task_id,
            task.org_unit_name,
            task.source_url,
            reason or "unknown",
        )

    async def _pipeline_llm_worker(
        self,
        llm_queue: asyncio.Queue[_ExtractionTaskItem | None],
        db_queue: asyncio.Queue[_SaveEvent | None],
        skills: str,
    ) -> None:
        while True:
            task = await llm_queue.get()
            if task is None:
                llm_queue.task_done()
                return

            started = time.perf_counter()
            if task.recovered:
                self._pipeline_stats["recovery_consumed"] = int(
                    self._pipeline_stats.get("recovery_consumed", 0)
                ) + 1
            self._pipeline_stats["in_progress"] = int(self._pipeline_stats.get("in_progress", 0)) + 1
            self._pipeline_stats["pending"] = max(0, int(self._pipeline_stats.get("pending", 0)) - 1)
            current_task = task
            retry_exhausted = False
            while True:
                async with self.db.session() as session:
                    await crawler_db.set_crawl_task_status(
                        session,
                        current_task.task_id,
                        status=CrawlTaskStatus.IN_PROGRESS,
                        attempt=current_task.attempt,
                    )

                outcome = await self._run_extraction_task(current_task, skills)
                if outcome.skipped_by_gate:
                    await self._mark_extraction_task_skipped_by_gate(current_task, outcome.skip_reason)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    self._update_pipeline_timing(elapsed_ms)
                    self._pipeline_stats["in_progress"] = max(0, int(self._pipeline_stats.get("in_progress", 0)) - 1)
                    llm_queue.task_done()
                    retry_exhausted = True
                    break
                invalid_events = [
                    event for event in outcome.invalid_json_events if event.get("name") == "save_professors"
                ]
                if not invalid_events:
                    break

                retry_task = await self._handle_invalid_json_retry(current_task, invalid_events)
                if retry_task is None:
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    self._update_pipeline_timing(elapsed_ms)
                    self._pipeline_stats["in_progress"] = max(0, int(self._pipeline_stats.get("in_progress", 0)) - 1)
                    llm_queue.task_done()
                    retry_exhausted = True
                    break
                current_task = retry_task
            if retry_exhausted:
                continue

            task = current_task

            if not outcome.payloads:
                await self._handle_no_structured_data_task(task)
                elapsed_ms = (time.perf_counter() - started) * 1000
                self._update_pipeline_timing(elapsed_ms)
                self._pipeline_stats["in_progress"] = max(0, int(self._pipeline_stats.get("in_progress", 0)) - 1)
                llm_queue.task_done()
                continue

            await db_queue.put(_SaveEvent(task=task, payloads=outcome.payloads))
            self._pipeline_stats["queue_depth"] = llm_queue.qsize()
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._update_pipeline_timing(elapsed_ms)
            self._pipeline_stats["in_progress"] = max(0, int(self._pipeline_stats.get("in_progress", 0)) - 1)
            llm_queue.task_done()

    async def _pipeline_db_worker(self, db_queue: asyncio.Queue[_SaveEvent | None]) -> None:
        while True:
            event = await db_queue.get()
            if event is None:
                db_queue.task_done()
                return
            task = event.task
            try:
                save_summary = await self._save_payloads_to_db(event.payloads, task=task)
            except Exception as error:
                async with self.db.session() as session:
                    await crawler_db.set_crawl_task_status(
                        session,
                        task.task_id,
                        status=CrawlTaskStatus.FAILED,
                        attempt=task.attempt,
                        last_error=f"save_error:{error}",
                    )
                    await crawler_db.log_extraction_failure(
                        session,
                        task_id=task.task_id,
                        failure_type="save_error",
                        org_unit_name=task.org_unit_name,
                        source_url=task.source_url,
                        raw_arguments_preview=str(event.payloads)[:5000],
                        attempt=task.attempt,
                        resolver="dropped",
                    )
                self._pipeline_stats["failed"] += 1
                self._increment_task_kind_stat(task, "failed")
                self._pipeline_stats["save_errors"] += 1
                db_queue.task_done()
                continue

            async with self.db.session() as session:
                await crawler_db.set_crawl_task_status(
                    session,
                    task.task_id,
                    status=CrawlTaskStatus.DONE,
                    attempt=task.attempt,
                )
            self._pipeline_stats["done"] += 1
            self._pipeline_stats["processed_tasks"] += 1
            self._increment_task_kind_stat(task, "processed")
            self.logger.debug(
                "Extraction task done task_id=%s org_unit=%s accepted=%s created=%s updated=%s unchanged=%s deduped_by_name_key=%s deduped_by_homepage=%s",
                task.task_id,
                task.org_unit_name,
                save_summary.get("accepted", 0),
                save_summary.get("created", 0),
                save_summary.get("updated", 0),
                save_summary.get("unchanged", 0),
                save_summary.get("deduped_by_name_key", 0),
                save_summary.get("deduped_by_homepage", 0),
            )
            db_queue.task_done()

    async def _handle_no_structured_data_task(self, task: _ExtractionTaskItem) -> None:
        if self._looks_like_rich_detail_profile_task(task):
            async with self.db.session() as session:
                await crawler_db.set_crawl_task_status(
                    session,
                    task.task_id,
                    status=CrawlTaskStatus.RETRY,
                    attempt=task.attempt,
                    last_error=_RICH_DETAIL_NO_STRUCTURED_DATA,
                )
                await crawler_db.log_extraction_failure(
                    session,
                    task_id=task.task_id,
                    failure_type="no_structured_data",
                    org_unit_name=task.org_unit_name,
                    source_url=task.source_url,
                    attempt=task.attempt,
                    resolver="retry",
                )
            self._pipeline_stats["retry"] = int(self._pipeline_stats.get("retry", 0)) + 1
            self._pipeline_stats["no_structured_data_recoverable"] = int(
                self._pipeline_stats.get("no_structured_data_recoverable", 0)
            ) + 1
            self._pipeline_stats["no_structured_data_failures"] += 1
            self.logger.warning(
                "Keep rich detail task recoverable after no_structured_data task_id=%s org_unit=%s url=%s",
                task.task_id,
                task.org_unit_name,
                task.source_url,
            )
            return

        async with self.db.session() as session:
            await crawler_db.set_crawl_task_status(
                session,
                task.task_id,
                status=CrawlTaskStatus.FAILED,
                attempt=task.attempt,
                last_error="no_structured_data",
            )
            await crawler_db.log_extraction_failure(
                session,
                task_id=task.task_id,
                failure_type="no_structured_data",
                org_unit_name=task.org_unit_name,
                source_url=task.source_url,
                attempt=task.attempt,
                resolver="dropped",
            )
        self._pipeline_stats["failed"] += 1
        self._increment_task_kind_stat(task, "failed")
        self._pipeline_stats["no_structured_data_failures"] += 1

    def _looks_like_rich_detail_profile_task(self, task: _ExtractionTaskItem) -> bool:
        if not task.detail_mode:
            return False
        text = str(task.page_text_snapshot or "")
        if len(text.strip()) < 160:
            return False
        url = task.page_url or task.source_url
        if not agent_detail._looks_like_profile_detail_url(url):
            return False
        if not any(token in text for token in _RICH_DETAIL_PROFILE_TOKENS):
            return False
        return any(token in text for token in _ACADEMIC_TITLE_TOKENS)

    async def _handle_invalid_json_retry(
        self,
        task: _ExtractionTaskItem,
        invalid_events: list[dict[str, Any]],
    ) -> _ExtractionTaskItem | None:
        preview = (invalid_events[0].get("raw_args_preview") or "")[:5000] if invalid_events else None
        if task.attempt < self.invalid_json_max_retry:
            next_attempt = task.attempt + 1
            retry_task = _ExtractionTaskItem(
                task_id=task.task_id,
                university=task.university,
                org_unit_name=task.org_unit_name,
                org_unit_url=task.org_unit_url,
                source_url=task.source_url,
                page_url=task.page_url,
                page_hash=task.page_hash,
                page_text_snapshot=task.page_text_snapshot,
                allowed_tools=task.allowed_tools,
                attempt=next_attempt,
                priority=task.priority,
                strict_retry=True,
                detail_mode=task.detail_mode,
                task_kind=task.task_kind,
                recovered=task.recovered,
                name_homepage_candidates=task.name_homepage_candidates,
            )
            async with self.db.session() as session:
                await crawler_db.set_crawl_task_status(
                    session,
                    task.task_id,
                    status=CrawlTaskStatus.RETRY,
                    attempt=next_attempt,
                    last_error="invalid_json_retry",
                )
                await crawler_db.log_extraction_failure(
                    session,
                    task_id=task.task_id,
                    failure_type="invalid_json",
                    org_unit_name=task.org_unit_name,
                    source_url=task.source_url,
                    raw_arguments_preview=preview,
                    attempt=next_attempt,
                    resolver="retry",
                )
            self._pipeline_stats["retries"] += 1
            return retry_task

        async with self.db.session() as session:
            await crawler_db.set_crawl_task_status(
                session,
                task.task_id,
                status=CrawlTaskStatus.RETRY,
                attempt=task.attempt,
                last_error="invalid_json_retry_exhausted",
            )
            await crawler_db.log_extraction_failure(
                session,
                task_id=task.task_id,
                failure_type="invalid_json",
                org_unit_name=task.org_unit_name,
                source_url=task.source_url,
                raw_arguments_preview=preview,
                attempt=task.attempt,
                resolver="retry",
            )
        self._pipeline_stats["retry"] = int(self._pipeline_stats.get("retry", 0)) + 1
        self._increment_task_kind_stat(task, "failed")
        self._pipeline_stats["invalid_json_failures"] += 1
        return None

    async def _run_extraction_task(self, task: _ExtractionTaskItem, skills: str) -> _ExtractionOutcome:
        skip_llm, skip_reason = self._should_skip_professor_llm(
            url=task.page_url or task.source_url,
            text=task.page_text_snapshot,
        )
        if skip_llm:
            self._pipeline_stats["llm_calls_skipped_by_gate"] = int(
                self._pipeline_stats.get("llm_calls_skipped_by_gate", 0)
            ) + 1
            self.logger.info(
                "Skip professor LLM task by gate task_id=%s url=%s detail_mode=%s reason=%s",
                task.task_id,
                task.page_url or task.source_url,
                task.detail_mode,
                skip_reason,
            )
            return _ExtractionOutcome(
                payloads=[],
                invalid_json_events=[],
                skipped_by_gate=True,
                skip_reason=skip_reason,
            )
        instruction = self._build_professor_instruction(task.org_unit_name, detail_mode=task.detail_mode, strict_retry=task.strict_retry)
        allowed_tools = {"save_professors"}
        tool_defs = [tool for tool in get_crawler_tool_definitions() if tool.get("name") in allowed_tools]
        user_content, payload_meta = self._build_llm_payload(
            state=CrawlerState.EXTRACT_PROFESSORS,
            instruction=instruction,
            url=task.page_url or task.source_url,
            page_text=task.page_text_snapshot,
            links=[],
            allowed_tools=allowed_tools,
            detail_mode=task.detail_mode,
        )
        self._record_llm_payload(int(payload_meta.get("payload_bytes", 0)))
        self.logger.debug(
            "LLM extraction payload task_id=%s url=%s raw_chars=%s compacted_chars=%s links_raw=%s links_kept=%s payload_bytes=%s",
            task.task_id,
            task.page_url or task.source_url,
            payload_meta.get("raw_chars", 0),
            payload_meta.get("compacted_chars", 0),
            payload_meta.get("links_raw", 0),
            payload_meta.get("links_kept", 0),
            payload_meta.get("payload_bytes", 0),
        )
        dynamic_system_content = CrawlerPromptBuilder.build_dynamic_system_content(
            allowed_tools,
            strict_json=True,
        )
        prompt_max_tokens = min(int(self.model_max_tokens), 16000)
        batches = self.context_manager.build_messages(
            CRAWLER_SYSTEM_PROMPT,
            tool_defs,
            skills,
            user_content,
            prompt_max_tokens,
            dynamic_system_content=dynamic_system_content,
        )

        captured_payloads: list[dict[str, Any]] = []

        async def _capture_save_professors(
            org_unit_name: str,
            professors: list[dict[str, Any]],
            org_unit_url: str | None = None,
            source_url: str | None = None,
        ) -> dict[str, Any]:
            normalized_professors = [item for item in professors if isinstance(item, dict)]
            if task.strict_retry and len(normalized_professors) > 25:
                normalized_professors = normalized_professors[:25]
            normalized_payload = self._normalize_extraction_payload_for_task(
                {
                    "org_unit_name": org_unit_name,
                    "org_unit_url": org_unit_url or task.org_unit_url,
                    "source_url": source_url or task.source_url,
                    "professors": normalized_professors,
                },
                task=task,
            )
            if normalized_payload is None:
                return {"accepted": 0}
            captured_payloads.append(normalized_payload)
            return {"accepted": len(normalized_professors)}

        final_result = None
        for batch in batches:
            final_result = await self.llm_client.chat(
                batch,
                tools=tool_defs or None,
                tool_handlers={"save_professors": _capture_save_professors},
            )
        assert final_result is not None
        invalid_events = [
            {
                "name": item.name,
                "raw_args_preview": item.raw_args_preview,
                "error_type": item.error_type,
            }
            for item in (getattr(final_result, "invalid_tool_calls", None) or [])
        ]

        used_fallback = False
        if not captured_payloads:
            payload = self._parse_json_from_text(final_result.content)
            if isinstance(payload, dict):
                professors = payload.get("professors")
                if isinstance(professors, list) and professors:
                    normalized_payload = self._normalize_extraction_payload_for_task(
                        {
                            "org_unit_name": str(payload.get("org_unit_name") or task.org_unit_name),
                            "org_unit_url": str(payload.get("org_unit_url") or task.org_unit_url or "").strip() or None,
                            "source_url": str(payload.get("source_url") or task.source_url or "").strip() or task.source_url,
                            "professors": [item for item in professors if isinstance(item, dict)],
                        },
                        task=task,
                    )
                    if normalized_payload is not None:
                        captured_payloads.append(normalized_payload)
                        used_fallback = True

        snapshot_fallback_used = self._apply_detail_snapshot_profile_fallback(captured_payloads, task)
        used_fallback = used_fallback or snapshot_fallback_used

        return _ExtractionOutcome(
            payloads=captured_payloads,
            invalid_json_events=invalid_events,
            content_fallback_used=used_fallback,
        )

    def _apply_detail_snapshot_profile_fallback(
        self,
        payloads: list[dict[str, Any]],
        task: _ExtractionTaskItem,
    ) -> bool:
        if not task.detail_mode:
            return False
        record = agent_detail.extract_detail_profile_record_from_snapshot(
            task.page_text_snapshot,
            page_url=task.page_url or task.source_url,
        )
        if not record:
            return False

        if payloads:
            changed = self._fill_payloads_from_detail_snapshot_record(payloads, record)
            if changed:
                self._pipeline_stats["detail_snapshot_fields_filled"] = int(
                    self._pipeline_stats.get("detail_snapshot_fields_filled", 0)
                ) + changed
                self.logger.info(
                    "Filled missing detail fields from snapshot task_id=%s url=%s name=%s fields=%s",
                    task.task_id,
                    task.source_url,
                    record.get("name"),
                    changed,
                )
            return changed > 0

        normalized_payload = self._normalize_extraction_payload_for_task(
            {
                "org_unit_name": task.org_unit_name,
                "org_unit_url": task.org_unit_url,
                "source_url": task.source_url,
                "professors": [record],
            },
            task=task,
        )
        if normalized_payload is None:
            return False
        payloads.append(normalized_payload)
        self._pipeline_stats["detail_snapshot_payloads_synthesized"] = int(
            self._pipeline_stats.get("detail_snapshot_payloads_synthesized", 0)
        ) + 1
        self.logger.info(
            "Synthesized detail payload from snapshot task_id=%s url=%s name=%s",
            task.task_id,
            task.source_url,
            record.get("name"),
        )
        return True

    def _fill_payloads_from_detail_snapshot_record(
        self,
        payloads: list[dict[str, Any]],
        record: dict[str, Any],
    ) -> int:
        snapshot_name = normalize_name(record.get("name"))
        if not snapshot_name:
            return 0
        fillable_fields = (
            "title",
            "email",
            "phone",
            "homepage",
            "external_link",
            "research_areas",
            "bio",
        )
        changed = 0
        for payload in payloads:
            professors = payload.get("professors")
            if not isinstance(professors, list):
                continue
            for index, professor in enumerate(professors):
                if not isinstance(professor, dict):
                    continue
                if normalize_name(professor.get("name")) != snapshot_name:
                    continue
                updated = dict(professor)
                for field_name in fillable_fields:
                    if self._has_profile_value(updated.get(field_name)):
                        continue
                    value = record.get(field_name)
                    if not self._has_profile_value(value):
                        continue
                    updated[field_name] = value
                    changed += 1
                if record.get("is_academician") is True and updated.get("is_academician") is not True:
                    updated["is_academician"] = True
                    changed += 1
                if (
                    record.get("_self_academician_evidence") is True
                    and updated.get("_self_academician_evidence") is not True
                ):
                    updated["_self_academician_evidence"] = True
                    changed += 1
                professors[index] = updated
        return changed

    @staticmethod
    def _has_profile_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, set, dict)):
            return bool(value)
        return True

    def _normalize_extraction_payload_for_task(
        self,
        payload: dict[str, Any],
        *,
        task: _ExtractionTaskItem,
    ) -> dict[str, Any] | None:
        incoming_name = str(payload.get("org_unit_name") or "").strip()
        task_name = str(task.org_unit_name or "").strip()
        effective_name = incoming_name or task_name or "Unknown"
        if is_teaching_experiment_center_name(effective_name):
            self._pipeline_stats["teaching_center_payloads_dropped"] = int(
                self._pipeline_stats.get("teaching_center_payloads_dropped", 0)
            ) + 1
            self.logger.info(
                "Drop professor payload for teaching/experiment center task_org=%s payload_org=%s source=%s",
                task_name,
                incoming_name,
                payload.get("source_url") or task.source_url,
            )
            return None

        normalized = dict(payload)
        if (
            incoming_name
            and task_name
            and task_name != "Unknown"
            and incoming_name != task_name
            and looks_like_sub_department_section_name(incoming_name)
        ):
            normalized["org_unit_name"] = task_name
            normalized["org_unit_url"] = task.org_unit_url or normalized.get("org_unit_url")
            self._pipeline_stats["sub_department_payloads_rewritten"] = int(
                self._pipeline_stats.get("sub_department_payloads_rewritten", 0)
            ) + 1
            self.logger.info(
                "Rewrite sub-department professor payload to parent org_unit parent=%s child=%s source=%s",
                task_name,
                incoming_name,
                normalized.get("source_url") or task.source_url,
            )
            self._fill_missing_homepages_from_name_links(normalized, task=task)
            self._infer_academician_flags_from_detail_context(normalized, task=task)
            return normalized

        normalized["org_unit_name"] = effective_name
        if not normalized.get("org_unit_url"):
            normalized["org_unit_url"] = task.org_unit_url
        if not normalized.get("source_url"):
            normalized["source_url"] = task.source_url
        self._fill_missing_homepages_from_name_links(normalized, task=task)
        self._infer_academician_flags_from_detail_context(normalized, task=task)
        return normalized

    def _fill_missing_homepages_from_name_links(
        self,
        payload: dict[str, Any],
        *,
        task: _ExtractionTaskItem,
    ) -> int:
        name_homepage_candidates = getattr(task, "name_homepage_candidates", None) or {}
        if getattr(task, "detail_mode", False) or not name_homepage_candidates:
            return 0
        professors = payload.get("professors")
        if not isinstance(professors, list):
            return 0
        changed = 0
        for index, professor in enumerate(professors):
            if not isinstance(professor, dict):
                continue
            if self._has_profile_value(professor.get("homepage")):
                continue
            name_key = normalize_name_key(professor.get("name"))
            if not name_key:
                continue
            homepage = name_homepage_candidates.get(name_key)
            if not homepage:
                continue
            updated = dict(professor)
            updated["homepage"] = homepage
            professors[index] = updated
            changed += 1
        if changed:
            self._pipeline_stats["homepage_filled_from_list_links"] = int(
                self._pipeline_stats.get("homepage_filled_from_list_links", 0)
            ) + changed
            self.logger.debug(
                "Filled %s missing professor homepages from list links task_id=%s org_unit=%s source=%s",
                changed,
                task.task_id,
                task.org_unit_name,
                task.source_url,
            )
        return changed

    def _infer_academician_flags_from_detail_context(
        self,
        payload: dict[str, Any],
        *,
        task: _ExtractionTaskItem,
    ) -> None:
        if not task.detail_mode:
            return
        page_text = str(task.page_text_snapshot or "")
        if not page_text or not contains_academician_hint(page_text):
            return
        professors = payload.get("professors")
        if not isinstance(professors, list):
            return
        changed = 0
        for index, professor in enumerate(professors):
            if not isinstance(professor, dict):
                continue
            if contains_self_academician_hint(
                professor.get("name"),
                professor.get("title"),
                professor.get("bio"),
            ):
                continue
            name = normalize_name(professor.get("name"))
            if not name:
                continue
            window = self._name_context_window(page_text, name)
            if not window or not contains_self_academician_hint(name, window):
                continue
            updated = dict(professor)
            updated["is_academician"] = True
            updated["_self_academician_evidence"] = True
            professors[index] = updated
            changed += 1
        if changed:
            self._pipeline_stats["academician_flags_inferred_from_detail"] = int(
                self._pipeline_stats.get("academician_flags_inferred_from_detail", 0)
            ) + changed

    @staticmethod
    def _name_context_window(text: str, name: str, *, radius: int = 160) -> str:
        if not text or not name:
            return ""
        index = text.find(name)
        if index < 0:
            return ""
        start = max(0, index - radius)
        end = min(len(text), index + len(name) + radius)
        return text[start:end]

    async def _save_payloads_to_db(
        self,
        payloads: list[dict[str, Any]],
        *,
        task: _ExtractionTaskItem | None = None,
    ) -> dict[str, int]:
        tools = get_crawler_tools(self.db, self.skill_manager)
        totals = {
            "accepted": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "deduped_by_name_key": 0,
            "deduped_by_homepage": 0,
        }
        for payload in payloads:
            if task is not None:
                normalized_payload = self._normalize_extraction_payload_for_task(payload, task=task)
                if normalized_payload is None:
                    continue
                payload = normalized_payload
            result = await tools["save_professors"](
                org_unit_name=str(payload.get("org_unit_name") or "Unknown"),
                org_unit_url=(str(payload.get("org_unit_url")) if payload.get("org_unit_url") else None),
                source_url=(str(payload.get("source_url")) if payload.get("source_url") else None),
                professors=[item for item in payload.get("professors", []) if isinstance(item, dict)],
            )
            if isinstance(result, dict):
                accepted = int(result.get("accepted", 0) or 0)
                created = int(result.get("created", result.get("saved", 0)) or 0)
                updated = int(result.get("updated", 0) or 0)
                unchanged = int(result.get("unchanged", 0) or 0)
                deduped_by_name_key = int(result.get("deduped_by_name_key", 0) or 0)
                deduped_by_homepage = int(result.get("deduped_by_homepage", 0) or 0)
                totals["accepted"] += accepted
                totals["created"] += created
                totals["updated"] += updated
                totals["unchanged"] += unchanged
                totals["deduped_by_name_key"] += deduped_by_name_key
                totals["deduped_by_homepage"] += deduped_by_homepage
                self.saved_professors += created
        self._pipeline_stats["records_accepted"] = int(self._pipeline_stats.get("records_accepted", 0)) + totals["accepted"]
        self._pipeline_stats["records_created"] = int(self._pipeline_stats.get("records_created", 0)) + totals["created"]
        self._pipeline_stats["records_updated"] = int(self._pipeline_stats.get("records_updated", 0)) + totals["updated"]
        self._pipeline_stats["records_unchanged"] = int(self._pipeline_stats.get("records_unchanged", 0)) + totals["unchanged"]
        self._pipeline_stats["deduped_by_name_key"] = int(self._pipeline_stats.get("deduped_by_name_key", 0)) + totals["deduped_by_name_key"]
        self._pipeline_stats["deduped_by_homepage"] = int(self._pipeline_stats.get("deduped_by_homepage", 0)) + totals["deduped_by_homepage"]
        if task is not None:
            self._record_roster_overlap_observation(task, totals)
        return totals

    def _record_roster_overlap_observation(self, task: _ExtractionTaskItem, summary: dict[str, int]) -> None:
        if self._task_kind_prefix(task) != "list":
            return
        accepted = int(summary.get("accepted", 0) or 0)
        if accepted < 10:
            return
        created = int(summary.get("created", 0) or 0)
        non_new = max(0, accepted - created)
        ratio = non_new / float(accepted)
        if ratio < 0.8:
            return
        self._pipeline_stats["list_roster_overlap_high"] = int(
            self._pipeline_stats.get("list_roster_overlap_high", 0)
        ) + 1
        self.logger.info(
            "High roster overlap observed task_id=%s org_unit=%s accepted=%s created=%s updated=%s unchanged=%s overlap_ratio=%.2f",
            task.task_id,
            task.org_unit_name,
            accepted,
            created,
            summary.get("updated", 0),
            summary.get("unchanged", 0),
            ratio,
        )

    def _build_professor_instruction(self, org_unit_name: str, *, detail_mode: bool, strict_retry: bool) -> str:
        return self.prompt_builder.build_professor_instruction(
            org_unit_name,
            detail_mode=detail_mode,
            strict_retry=strict_retry,
        )

    def _update_pipeline_timing(self, elapsed_ms: float) -> None:
        self.extraction_pipeline.update_timing(elapsed_ms)

    @staticmethod
    def _task_kind_prefix(task: _ExtractionTaskItem) -> str:
        return ExtractionPipeline.task_kind_prefix(task)

    def _increment_task_kind_stat(self, task: _ExtractionTaskItem, suffix: str, amount: int = 1) -> None:
        self.extraction_pipeline.increment_task_kind_stat(task, suffix, amount)

    @staticmethod
    def _state_link_limit(state: CrawlerState) -> int:
        return CrawlerPromptBuilder.state_link_limit(state)

    @staticmethod
    def _state_text_limit(state: CrawlerState, *, detail_mode: bool = False) -> int:
        return CrawlerPromptBuilder.state_text_limit(state, detail_mode=detail_mode)

    def _compact_page_text(self, text: str, max_chars: int, *, min_chars: int = 300) -> str:
        return self.prompt_builder.compact_page_text(text, max_chars, min_chars=min_chars)

    @staticmethod
    def _serialize_payload(payload: dict[str, Any]) -> str:
        return CrawlerPromptBuilder.serialize_payload(payload)

    @staticmethod
    def _build_tool_call_policy(allowed_tools: set[str]) -> str:
        return CrawlerPromptBuilder.build_tool_call_policy(allowed_tools)

    def _record_llm_payload(self, payload_bytes: int) -> None:
        self.extraction_pipeline.record_llm_payload(payload_bytes)

    def _build_llm_payload(
        self,
        *,
        state: CrawlerState,
        instruction: str,
        url: str,
        page_text: str,
        links: list[str],
        allowed_tools: set[str],
        detail_mode: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        self.prompt_builder.fetcher = self.fetcher
        self.prompt_builder.visited_urls = self.visited_urls
        self.prompt_builder.update_context(
            start_url=self.start_url,
            university_name=self.university_name,
            location=self.location,
        )
        return self.prompt_builder.build_llm_payload(
            state=state,
            instruction=instruction,
            url=url,
            page_text=page_text,
            links=links,
            allowed_tools=allowed_tools,
            detail_mode=detail_mode,
        )

    def _host_gate_allows_faculty_candidate(
        self,
        url: str,
        *,
        org_unit_url: str | None = None,
        org_unit_hosts: set[str] | None = None,
    ) -> bool:
        use_host_set_gate = org_unit_hosts is not None
        allowed_hosts = {(host or "").strip().lower() for host in (org_unit_hosts or set()) if (host or "").strip()}
        if org_unit_url:
            return _allow_faculty_candidate_for_org_unit(
                url,
                org_unit_url=org_unit_url,
                start_url=self.start_url,
            )
        if use_host_set_gate:
            return _allow_faculty_candidate_for_host_set(
                url,
                start_url=self.start_url,
                org_unit_hosts=allowed_hosts,
            )
        return not _is_faculty_platform(url)

    def _assess_faculty_candidates(
        self,
        links: list[str],
        *,
        org_unit_url: str | None = None,
        org_unit_hosts: set[str] | None = None,
        link_signals: tuple[Any, ...] | list[Any] | None = None,
        max_candidates: int = 4,
    ) -> tuple[list[FacultyCandidateAssessment], list[FacultyCandidateAssessment]]:
        same_domain = self.fetcher.filter_same_domain(links, self.start_url)
        structured = _assess_structural_faculty_candidates(
            same_domain,
            link_signals=link_signals,
        )
        gated_structured = [
            item
            for item in structured
            if self._host_gate_allows_faculty_candidate(
                item.url,
                org_unit_url=org_unit_url,
                org_unit_hosts=org_unit_hosts,
            )
            and not _looks_like_retired_url(item.url)
        ]
        selected = _select_balanced_faculty_candidates(gated_structured, limit=max_candidates)
        if selected:
            return gated_structured, selected

        legacy_candidates = _keyword_filter(same_domain, FACULTY_KEYWORDS) or same_domain
        filtered: list[str] = []
        for link in legacy_candidates:
            if not self._host_gate_allows_faculty_candidate(
                link,
                org_unit_url=org_unit_url,
                org_unit_hosts=org_unit_hosts,
            ):
                continue
            if _looks_like_retired_url(link) or _is_non_faculty_noise_url(link):
                continue
            filtered.append(link)

        ranked = _rank_faculty_page_candidates(filtered)
        non_showcase = [link for link in ranked if not _is_academician_showcase_page(link)]
        if non_showcase:
            ranked = non_showcase
        legacy_assessments = [_assess_faculty_candidate(link) for link in ranked]
        selected_legacy = _select_balanced_faculty_candidates(legacy_assessments, limit=max_candidates)
        if selected_legacy:
            return legacy_assessments, selected_legacy
        return legacy_assessments, legacy_assessments[:max_candidates]

    async def _select_faculty_candidates(
        self,
        *,
        links: list[str],
        fetched: FetchResult,
        org_unit_name: str,
        org_unit_url: str | None = None,
        org_unit_hosts: set[str] | None = None,
        llm_budget: int = 0,
        max_candidates: int = 4,
        link_signals: tuple[Any, ...] | list[Any] | None = None,
    ) -> tuple[list[str], int]:
        assessments, selected = self._assess_faculty_candidates(
            links,
            org_unit_url=org_unit_url,
            org_unit_hosts=org_unit_hosts,
            link_signals=link_signals,
            max_candidates=max_candidates,
        )
        if assessments:
            preview_items = assessments[:8]
            preview = " | ".join(
                f"{item.page_type}:{item.score}:{'H' if item.hard_reject else 'N'}:{item.url}"
                for item in preview_items
            )
            self.logger.debug(
                "Faculty assessment details org_unit=%s url=%s candidates=%s preview=%s",
                org_unit_name,
                fetched.url,
                len(assessments),
                preview,
            )
        selected_urls = [item.url for item in selected]
        uncertain = [item for item in selected if item.uncertain]
        llm_adopted = 0
        budget_used = 0

        if uncertain and llm_budget > 0:
            budget_used = 1
            adopted_urls = await self._resolve_uncertain_faculty_candidates_with_llm(
                fetched=fetched,
                org_unit_name=org_unit_name,
                uncertain_candidates=uncertain,
            )
            if adopted_urls is not None:
                stable_uncertain = [item.url for item in uncertain]
                adopted_set = set(adopted_urls)
                definite_urls = [item.url for item in selected if not item.uncertain]
                selected_urls = definite_urls + [url for url in stable_uncertain if url in adopted_set]
                if not selected_urls:
                    selected_urls = [item.url for item in selected]
                llm_adopted = len([url for url in stable_uncertain if url in adopted_set])

        self.logger.debug(
            "Faculty candidates org_unit=%s url=%s total=%s selected=%s uncertain=%s llm_adopted=%s selected_urls=%s",
            org_unit_name,
            fetched.url,
            len(assessments),
            len(selected_urls),
            len(uncertain),
            llm_adopted,
            selected_urls,
        )
        return selected_urls[:max_candidates], budget_used

    async def _resolve_uncertain_faculty_candidates_with_llm(
        self,
        *,
        fetched: FetchResult,
        org_unit_name: str,
        uncertain_candidates: list[FacultyCandidateAssessment],
    ) -> list[str] | None:
        if not uncertain_candidates:
            return []

        candidate_payload = [
            {
                "url": item.url,
                "anchor_text": item.anchor_text,
                "heading_text": item.heading_text,
                "rule_score": item.score,
                "rule_type": item.page_type,
            }
            for item in uncertain_candidates
        ]
        payload = {
            "state": CrawlerState.FIND_FACULTY_PAGES.value,
            "org_unit": org_unit_name,
            "page_url": fetched.url,
            "candidates": candidate_payload,
            "instruction": (
                "Select faculty list pages only from candidates. "
                "Return JSON {\"links\": [...]} and do not output URLs outside the candidate set."
            ),
        }

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a deterministic URL selector. "
                    "Only choose from the provided candidates and return strict JSON."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]
        try:
            result = await self.llm_client.chat(messages, tools=None, tool_handlers={}, max_tokens=256)
        except Exception as error:
            self.logger.warning(
                "Uncertain faculty candidate adjudication failed org_unit=%s url=%s error=%s",
                org_unit_name,
                fetched.url,
                error,
            )
            return None

        allowed = {item.url for item in uncertain_candidates}
        links = self._links_from_result(result.content)
        if not links:
            parsed = self._parse_json_from_text(result.content)
            if isinstance(parsed, dict):
                values = parsed.get("links") or parsed.get("selected") or parsed.get("urls") or []
                if isinstance(values, list):
                    links = [_sanitize_url(str(value)) for value in values if _sanitize_url(str(value))]
        if not links:
            links = _extract_urls_from_text(result.content)

        adopted = [url for url in links if url in allowed]
        return adopted

    @staticmethod
    def _is_noise_or_login_candidate(url: str) -> bool:
        assessment = _assess_faculty_candidate(url)
        return assessment.page_type == FACULTY_PAGE_TYPE_NOISE and (
            assessment.hard_reject or assessment.score <= 0
        )

    @staticmethod
    def _should_skip_faculty_discovery_llm(fetched: FetchResult) -> tuple[bool, str]:
        text_len = len((fetched.text or "").strip())
        if fetched.block_reason:
            return True, f"blocked={fetched.block_reason}"
        if not fetched.links and text_len < 200:
            return True, f"low_info links=0 text_len={text_len}"
        return False, ""

    def _should_skip_professor_llm(self, *, url: str, text: str) -> tuple[bool, str]:
        lowered_url = (url or "").lower()
        lowered_text = (text or "").lower()

        strong_noise_url_tokens = (
            "/news",
            "/notice",
            "/tzgg",
            "/gonggao",
            "/announcement",
            "/policy",
            "/zcwj",
            "规章制度",
            "/renshi",
            "/rszc",
            "/hr",
            "/rczp",
            "/zhaopin",
            "/jobs",
            "/dangjian",
            "/party",
            "/xsgz",
            "/zsjy",
        )
        faculty_signal_tokens = (
            "faculty",
            "teacher",
            "staff",
            "professor",
            "research",
            "email",
            "phone",
            "导师",
            "教师",
            "师资",
            "教授",
            "副教授",
            "讲师",
            "研究员",
            "邮箱",
            "电话",
            "研究方向",
            "博导",
            "硕导",
        )
        strong_faculty_evidence_tokens = (
            "email",
            "mail",
            "phone",
            "tel",
            "professor",
            "associate professor",
            "assistant professor",
            "lecturer",
            "researcher",
            "\u5bfc\u5e08",
            "\u6559\u5e08",
            "\u6559\u6388",
            "\u526f\u6559\u6388",
            "\u8bb2\u5e08",
            "\u7814\u7a76\u5458",
            "\u90ae\u7bb1",
            "\u7535\u8bdd",
            "\u535a\u5bfc",
            "\u7855\u5bfc",
        )
        noise_text_tokens = (
            "通知",
            "公告",
            "新闻",
            "政策",
            "规章制度",
            "规章",
            "招聘",
            "人事",
            "党建",
            "招生",
            "就业",
            "notice",
            "announcement",
            "news",
            "policy",
            "recruit",
            "personnel",
            "hr",
        )

        if self._looks_like_notice_issuance_page(text):
            return True, "notice_issuance_title"

        has_faculty_signal = ("@" in (text or "")) or any(token in lowered_text for token in faculty_signal_tokens)
        evidence_hits = sum(1 for token in strong_faculty_evidence_tokens if token in lowered_text)
        has_strong_faculty_evidence = ("@" in (text or "")) or evidence_hits >= 2
        if any(token in lowered_url for token in strong_noise_url_tokens) and not has_strong_faculty_evidence:
            return True, "url_noise_token"

        lines = [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]
        if not lines:
            return False, ""
        noise_hits = sum(1 for line in lines if any(token in line.lower() for token in noise_text_tokens))
        noise_ratio = noise_hits / float(len(lines))
        if noise_ratio >= 0.35 and not has_faculty_signal:
            return True, f"text_noise_ratio={noise_ratio:.2f}"
        return False, ""

    @staticmethod
    def _looks_like_notice_issuance_page(text: str) -> bool:
        lines = [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]
        for line in lines[:8]:
            normalized = CrawlerAgent._normalize_notice_issuance_title_candidate(line)
            if normalized and _NOTICE_ISSUANCE_TITLE_RE.search(normalized):
                return True
        return False

    @staticmethod
    def _normalize_notice_issuance_title_candidate(line: str) -> str:
        cleaned = str(line or "").strip()
        cleaned = re.sub(r"^\s*#{1,6}\s*", "", cleaned)
        cleaned = re.sub(r"^\s*(?:当前位置|您现在的位置|位置)\s*[:：].*?[>›»]\s*", "", cleaned)
        cleaned = re.sub(r"^\s*(?:标题|题目)\s*[:：]\s*", "", cleaned)
        cleaned = re.sub(r"\s+", "", cleaned)
        cleaned = cleaned.strip(" \t\r\n\"'“”‘’")
        return cleaned

    async def _extract_professors_from_page(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        skills: str,
        *,
        detail_mode: bool,
        requested_url: str | None = None,
    ) -> int:
        saved_before = self.saved_professors
        source_url = _sanitize_url(requested_url or current.identity_url or current.url) or _sanitize_url(fetched.url) or ""
        if not source_url:
            return 0
        final_url = _sanitize_url(fetched.url) or source_url
        skip_redirect, redirect_reason = self._should_skip_redirected_extraction(
            source_url,
            final_url,
            detail_mode=detail_mode,
        )
        if skip_redirect:
            self._record_redirected_extraction_skip(detail_mode=detail_mode)
            self.logger.warning(
                "Skip synchronous extraction after redirect source=%s final=%s detail_mode=%s reason=%s",
                source_url,
                final_url,
                detail_mode,
                redirect_reason,
            )
            return 0
        if final_url != source_url:
            self.logger.debug(
                "Synchronous extraction keeps requested URL after redirect source=%s final=%s detail_mode=%s",
                source_url,
                final_url,
                detail_mode,
            )
        snapshot = self._compact_page_text(
            fetched.text or "",
            self._state_text_limit(CrawlerState.EXTRACT_PROFESSORS, detail_mode=detail_mode),
        )
        page_hash = hashlib.sha1(f"{source_url}|{snapshot}".encode("utf-8", errors="ignore")).hexdigest()
        name_homepage_candidates = self._extract_name_homepage_candidates(
            fetched,
            source_url=source_url,
            detail_mode=detail_mode,
        )
        task = _ExtractionTaskItem(
            task_id=0,
            university=self.university_name,
            org_unit_name=current.label or "Unknown",
            org_unit_url=current.url,
            source_url=source_url,
            page_url=source_url,
            page_hash=page_hash,
            page_text_snapshot=snapshot,
            allowed_tools=["save_professors"],
            attempt=0,
            priority=0,
            strict_retry=False,
            detail_mode=detail_mode,
            task_kind=CrawlTaskKind.DETAIL_PAGE.value if detail_mode else CrawlTaskKind.LIST_PAGE.value,
            name_homepage_candidates=name_homepage_candidates,
        )
        outcome = await self._run_extraction_task(task, skills)
        if outcome.skipped_by_gate:
            return self.saved_professors - saved_before
        invalid_events = [event for event in outcome.invalid_json_events if event.get("name") == "save_professors"]
        if invalid_events and self.invalid_json_max_retry > 0:
            task.attempt = 1
            task.strict_retry = True
            outcome = await self._run_extraction_task(task, skills)
            if outcome.skipped_by_gate:
                return self.saved_professors - saved_before
            invalid_events = [event for event in outcome.invalid_json_events if event.get("name") == "save_professors"]

        if invalid_events:
            preview = (invalid_events[0].get("raw_args_preview") or "")[:5000]
            async with self.db.session() as session:
                await crawler_db.log_extraction_failure(
                    session,
                    task_id=None,
                    failure_type="invalid_json",
                    org_unit_name=task.org_unit_name,
                    source_url=task.source_url,
                    raw_arguments_preview=preview,
                    attempt=task.attempt,
                    resolver="dropped",
                )
            return self.saved_professors - saved_before

        if outcome.payloads:
            await self._save_payloads_to_db(outcome.payloads, task=task)
        else:
            async with self.db.session() as session:
                await crawler_db.log_extraction_failure(
                    session,
                    task_id=None,
                    failure_type="no_structured_data",
                    org_unit_name=task.org_unit_name,
                    source_url=task.source_url,
                    attempt=task.attempt,
                    resolver="dropped",
                )

        return self.saved_professors - saved_before
    async def _enrich_profiles_with_detail_backend(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        skills: str,
        *,
        reserved_urls: set[str] | None = None,
    ) -> None:
        await self.detail_enricher.enrich_profiles_with_detail_backend(
            current,
            fetched,
            skills,
            reserved_urls=reserved_urls,
        )

    async def _enrich_profiles_with_human(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        skills: str,
        *,
        reserved_urls: set[str] | None = None,
    ) -> None:
        await self.detail_enricher.enrich_profiles_with_human(
            current,
            fetched,
            skills,
            reserved_urls=reserved_urls,
        )

    async def _process_detail_urls_with_human(self, urls: list[str], current: _QueuedUrl, skills: str) -> None:
        await self.detail_enricher.process_detail_urls_with_human(urls, current, skills)

    def _extract_detail_profile_links(
        self,
        links: list[str],
        current_url: str,
        *,
        link_signals: tuple[Any, ...] | list[Any] | None = None,
    ) -> list[str]:
        return self.detail_enricher.extract_detail_profile_links(links, current_url, link_signals=link_signals)

    def _detail_org_unit_key(self, current: _QueuedUrl) -> str:
        return self.detail_enricher.detail_org_unit_key(current)

    @staticmethod
    def _derive_section_prefix(path: str) -> str:
        return agent_detail.DetailEnricher.derive_section_prefix(path)

    def _log_org_unit_queue_preview(self, candidates: list[OrgUnit], start_host: str, *, stage: str) -> None:
        if not candidates:
            return
        preview = []
        for unit in candidates[:8]:
            priority = _org_unit_faculty_priority(unit, start_host)
            preview.append(f"{unit.name}:{priority}")
        self.logger.info(
            "Org-unit queue ordered stage=%s total=%s preview=%s",
            stage,
            len(candidates),
            " | ".join(preview),
        )

    def _is_failed_detail_fetch(self, fetched: FetchResult) -> bool:
        return self.detail_enricher.is_failed_detail_fetch(fetched)

    def _is_retired_page(self, fetched: FetchResult) -> bool:
        return self.detail_enricher.is_retired_page(fetched)

    def _looks_like_detail_directory_page(self, fetched: FetchResult) -> bool:
        return self.detail_enricher.looks_like_detail_directory_page(fetched)

    async def _ask_llm(
        self,
        state: CrawlerState,
        instruction: str,
        fetched: FetchResult,
        skills_text: str,
    ) -> Any:
        prompt_max_tokens = min(int(self.model_max_tokens), 16000)
        allowed_tools: set[str] = set()
        if state in {CrawlerState.DISCOVER_ORG_UNIT_PAGES, CrawlerState.FIND_FACULTY_PAGES}:
            allowed_tools = {"extract_links"}
        elif state is CrawlerState.EXTRACT_PROFESSORS:
            allowed_tools = {"save_professors"}
        user_content, payload_meta = self._build_llm_payload(
            state=state,
            instruction=instruction,
            url=fetched.url,
            page_text=fetched.text or "",
            links=list(fetched.links or []),
            allowed_tools=allowed_tools,
        )
        self._record_llm_payload(int(payload_meta.get("payload_bytes", 0)))
        tool_defs = get_crawler_tool_definitions()
        tool_defs = [tool for tool in tool_defs if tool.get("name") in allowed_tools] if allowed_tools else []
        dynamic_system_content = CrawlerPromptBuilder.build_dynamic_system_content(allowed_tools)

        batches = self.context_manager.build_messages(
            CRAWLER_SYSTEM_PROMPT,
            tool_defs,
            skills_text,
            user_content,
            prompt_max_tokens,
            dynamic_system_content=dynamic_system_content,
        )
        handlers = get_crawler_tools(self.db, self.skill_manager)
        final_result = None
        if int(payload_meta.get("links_kept", 0)) == 0 and int(payload_meta.get("compacted_chars", 0)) == 0:
            self.logger.debug(
                "LLM prompt has empty links/page_text state=%s url=%s; output may rely on URL heuristics",
                state.value,
                fetched.url,
            )
        self.logger.debug(
            "LLM request state=%s url=%s links_raw=%s links_kept=%s raw_chars=%s compacted_chars=%s payload_bytes=%s tools=%s",
            state.value,
            fetched.url,
            payload_meta.get("links_raw", 0),
            payload_meta.get("links_kept", 0),
            payload_meta.get("raw_chars", 0),
            payload_meta.get("compacted_chars", 0),
            payload_meta.get("payload_bytes", 0),
            [tool.get("name") for tool in tool_defs],
        )
        for batch in batches:
            final_result = await self.llm_client.chat(
                batch,
                tools=tool_defs or None,
                tool_handlers=handlers,
            )
        assert final_result is not None
        self.logger.debug(
            "LLM response state=%s content_chars=%s tool_calls=%s",
            state.value,
            len(final_result.content or ""),
            len(getattr(final_result, "tool_call_log", []) or []),
        )
        return final_result

    async def _select_skills(self, state: CrawlerState) -> str:
        if state in {CrawlerState.DISCOVER_ORG_UNIT_PAGES, CrawlerState.FIND_FACULTY_PAGES}:
            allowed_tools = {"extract_links"}
        elif state is CrawlerState.EXTRACT_PROFESSORS:
            allowed_tools = {"save_professors"}
        else:
            allowed_tools = set()
        return self.skill_manager.select_for_state(state.value, allowed_tools).rendered_text
    async def _fetch_url(
        self,
        url: str,
        depth: int,
        *,
        action: dict[str, Any] | None = None,
        identity_url: str | None = None,
        allow_depth_excess: bool = False,
    ) -> FetchResult | None:
        return await self.fetch_scheduler.fetch_url(
            url,
            depth,
            action=action,
            identity_url=identity_url,
            allow_depth_excess=allow_depth_excess,
        )

    async def _save_professors_from_content(self, content: str, fallback_org_unit: str) -> None:
        payload = self._parse_json_from_text(content)
        if payload is None or not isinstance(payload, dict):
            return

        professors = payload.get("professors")
        if not isinstance(professors, list) or not professors:
            return

        org_unit_name = str(
            payload.get("org_unit_name")
            or payload.get("college_name")
            or fallback_org_unit
            or "Unknown"
        )
        source_url = str(payload.get("source_url") or "").strip() or None
        org_unit_url = str(payload.get("org_unit_url") or "").strip() or None

        task = _ExtractionTaskItem(
            task_id=0,
            university=self.university_name,
            org_unit_name=fallback_org_unit or "Unknown",
            org_unit_url=org_unit_url,
            source_url=source_url or "",
            page_url=source_url or "",
            page_hash="content-fallback",
            page_text_snapshot="",
            allowed_tools=["save_professors"],
        )
        normalized_payload = self._normalize_extraction_payload_for_task(
            {
                "org_unit_name": org_unit_name,
                "org_unit_url": org_unit_url,
                "source_url": source_url,
                "professors": [item for item in professors if isinstance(item, dict)],
            },
            task=task,
        )
        if normalized_payload is None:
            return

        tools = get_crawler_tools(self.db, self.skill_manager)
        result = await tools["save_professors"](
            org_unit_name=str(normalized_payload.get("org_unit_name") or "Unknown"),
            org_unit_url=(
                str(normalized_payload.get("org_unit_url")) if normalized_payload.get("org_unit_url") else None
            ),
            source_url=(
                str(normalized_payload.get("source_url")) if normalized_payload.get("source_url") else None
            ),
            professors=[item for item in normalized_payload.get("professors", []) if isinstance(item, dict)],
        )
        if isinstance(result, dict):
            self.saved_professors += int(result.get("saved", 0) or 0)

    async def _ensure_university(self) -> UniversityMeta:
        if self._university_cache is not None:
            return self._university_cache
        async with self.db.session() as session:
            self._university_cache = await crawler_db.ensure_university_meta(
                session,
                name=self.university_name,
                start_url=self.start_url,
                location=self.location,
            )
            return self._university_cache

    async def _set_status(self, status: CrawlStatus) -> None:
        async with self.db.session() as session:
            await crawler_db.set_university_status(session, status)

    async def _professor_count(self) -> int:
        async with self.db.session() as session:
            return await crawler_db.count_professors(session)

    async def _all_urls_previously_crawled(self, urls: list[str]) -> bool:
        candidates = [_sanitize_url(url) for url in urls if _sanitize_url(url)]
        if not candidates:
            return False
        async with self.db.session() as session:
            checks = [await crawler_db.is_url_crawled(session, url) for url in candidates]
        return all(checks)

    def _links_from_result(self, content: str) -> list[str]:
        return agent_parsing.links_from_result(self, content)

    def _org_unit_followup_links_from_result(self, content: str, current_url: str) -> list[str]:
        return agent_parsing.org_unit_followup_links_from_result(self, content, current_url)

    def _links_from_tool_call_log(self, result: Any, *, tool_name: str = "extract_links") -> list[str]:
        return agent_parsing.links_from_tool_call_log(self, result, tool_name=tool_name)

    def _org_units_from_result(self, content: str) -> list[dict[str, Any]]:
        return agent_parsing.org_units_from_result(self, content)

    def _parse_json_from_text(self, content: str) -> Any | None:
        return agent_parsing.parse_json_from_text(self, content)

    def _within_depth(self, depth: int) -> bool:
        return depth <= self.max_depth

    def _too_many_backtracks(self, reason: str) -> bool:
        self.backtrack_count += 1
        self.execution_log.append(f"backtrack {self.backtrack_count}: {reason}")
        self.logger.info("Backtrack %s/%s: %s", self.backtrack_count, self.max_backtracks, reason)
        return self.backtrack_count > self.max_backtracks

    def _log_state(self, state: CrawlerState) -> None:
        self._current_state = state.value
        self.execution_log.append(f"state={state.value}")
        self.logger.info("State %s", state.value)

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "university": self.university_name,
            "state": self._current_state or "unknown",
            "saved_professors": self.saved_professors,
            "visited_count": len(self.visited_urls),
            "pipeline": dict(self._pipeline_stats),
        }

    def _result(self, status: CrawlStatus, messages: list[str]) -> AgentResult:
        return AgentResult(
            university_name=self.university_name,
            status=status.value,
            visited_count=len(self.visited_urls),
            saved_professors=self.saved_professors,
            messages=messages,
        )

    def _extract_pagination_links(self, links: list[str], current_url: str) -> list[str]:
        return agent_parsing.extract_pagination_links(self, links, current_url)

    def _extract_followup_faculty_links(self, links: list[str], current_url: str) -> list[str]:
        return agent_parsing.extract_followup_faculty_links(self, links, current_url)

    async def _search_engine_fallback(self, query_suffix: str) -> list[str]:
        return await agent_parsing.search_engine_fallback(self, query_suffix)

def _dedupe_queue(items: list[_QueuedUrl]) -> list[_QueuedUrl]:
    seen: set[str] = set()
    result: list[_QueuedUrl] = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        result.append(item)
    return result





