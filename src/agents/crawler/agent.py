from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from agents.crawler import db as crawler_db
from agents.crawler import agent_detail, agent_parsing
from agents.crawler.entrances import ManualOrgUnitEntrance
from agents.crawler.agent_state import CrawlerState
from agents.crawler.extraction_models import (
    ExtractionOutcome as _ExtractionOutcome,
    ExtractionTaskItem as _ExtractionTaskItem,
    QueuedUrl as _QueuedUrl,
    SaveEvent as _SaveEvent,
)
from agents.crawler.extraction_pipeline import ExtractionPipeline, ExtractionPipelineService
from agents.crawler.faculty_discovery import FacultyDiscoveryService
from agents.crawler.fetch_failures import is_retryable_fetch_failure
from agents.crawler.fetchers import FetchResult, Fetcher
from agents.crawler.fetch_scheduler import FetchScheduler
from agents.crawler.models import (
    CrawlGraphEdgeType,
    CrawlGraphNodeStatus,
    CrawlGraphNodeType,
    CrawlStatus,
    CrawlTaskKind,
    CrawlTaskStatus,
    OrgUnit,
    OrgUnitStatus,
    UniversityMeta,
)
from agents.crawler.graph_frontier import GraphFetchCandidate, GraphFrontier
from agents.crawler.org_unit_filter import (
    ORG_UNIT_FILTER_STATE,
    hard_filter_org_unit_payloads,
    llm_filter_org_unit_payloads,
    normalize_org_unit_match_text,
    org_unit_filter_item_keys,
)
from agents.crawler.professor_noise import should_skip_professor_llm
from agents.crawler.prompt_builder import CRAWLER_SYSTEM_PROMPT, CrawlerPromptBuilder
from agents.crawler.sanitizer import normalize_org_unit_name
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
from runtime.context import ContextManager
from runtime.database import DatabaseManager
from runtime.llm import LLMClient
from runtime.logger import get_logger
from runtime.skills import SkillManager


@dataclass(frozen=True)
class AgentResult:
    university_name: str
    status: str
    visited_count: int
    saved_professors: int
    messages: list[str] = field(default_factory=list)


class CrawlerAgent(ExtractionPipelineService):
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
        max_org_units_per_university: int = 100,
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
        org_unit_listing_urls: list[str] | None = None,
        manual_org_units: list[ManualOrgUnitEntrance | dict[str, Any]] | None = None,
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
        self.org_unit_listing_urls: list[str] = []
        for item in org_unit_listing_urls or []:
            url = _sanitize_url(str(item or ""))
            if url:
                self.org_unit_listing_urls.append(url)
        self.manual_org_units: list[ManualOrgUnitEntrance] = []
        for item in manual_org_units or []:
            coerced = self._coerce_manual_org_unit(item)
            if coerced is not None:
                self.manual_org_units.append(coerced)
        self._manual_org_unit_aliases_by_name = self._build_manual_org_unit_alias_index(
            self.manual_org_units
        )
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
        self.graph_frontier = GraphFrontier(self)
        self.prompt_builder = CrawlerPromptBuilder(
            context_manager=self.context_manager,
            fetcher=self.fetcher,
            start_url=self.start_url,
            university_name=self.university_name,
            location=self.location,
            visited_urls=self.visited_urls,
        )

    @staticmethod
    def _coerce_manual_org_unit(item: ManualOrgUnitEntrance | dict[str, Any]) -> ManualOrgUnitEntrance | None:
        if isinstance(item, ManualOrgUnitEntrance):
            return item
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or item.get("org_unit_name") or "").strip()
        if not name:
            return None
        raw_aliases = item.get("aliases", [])
        if isinstance(raw_aliases, str):
            aliases = (raw_aliases.strip(),) if raw_aliases.strip() else ()
        elif isinstance(raw_aliases, (list, tuple)):
            aliases = tuple(str(alias).strip() for alias in raw_aliases if str(alias).strip())
        else:
            aliases = ()
        return ManualOrgUnitEntrance(
            name=name,
            url=str(item.get("url") or item.get("org_unit_url") or "").strip(),
            faculty_url=str(item.get("faculty_url") or item.get("faculty_entrance") or "").strip(),
            kind=str(item.get("kind") or item.get("org_unit_kind") or "").strip(),
            raw_name=str(item.get("raw_name") or item.get("alias") or "").strip(),
            aliases=aliases,
        )

    @classmethod
    def _build_manual_org_unit_alias_index(
        cls,
        items: list[ManualOrgUnitEntrance],
    ) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for item in items:
            canonical_key = cls._normalize_org_unit_match_text(item.name)
            if not canonical_key:
                continue
            aliases = result.setdefault(canonical_key, set())
            for value in (item.name, item.raw_name, *item.aliases):
                key = cls._normalize_org_unit_match_text(str(value or ""))
                if key:
                    aliases.add(key)
        return result

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
            if self.manual_org_units or self.org_unit_listing_urls:
                early_result = await self._run_manual_entrance_flow(resume_seed_org_units)
                if early_result is not None:
                    return early_result
                return await self._finalize_run(initial_professor_count)

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


            return await self._finalize_run(initial_professor_count)
        except Exception as error:
            self.logger.exception("Crawler failed for %s", self.university_name)
            await self._set_status(CrawlStatus.FAILED)
            return self._result(CrawlStatus.FAILED, [str(error)])

    async def _finalize_run(self, initial_professor_count: int) -> AgentResult:
        recoverable_task_result = await self._fail_if_recoverable_tasks_remain(context="crawl_completion")
        if recoverable_task_result is not None:
            return recoverable_task_result

        total_professor_count = await self._professor_count()
        newly_saved_count = max(0, total_professor_count - initial_professor_count)
        if total_professor_count <= 0:
            if self._all_target_org_units_marked_no_faculty():
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

    async def _resume_without_start_page(self, initial_professor_count: int) -> AgentResult:
        async with self.db.session() as session:
            org_units = await crawler_db.list_org_units(session, limit=self.max_org_units_per_university)
            task_summary = await crawler_db.summarize_crawl_task_status(session)

        recoverable_tasks = self._recoverable_task_count(task_summary)
        ready_graph_candidates = await self.graph_frontier.next_fetch_candidates(
            limit=self.pipeline_queue_cap,
            node_types=[
                CrawlGraphNodeType.FACULTY_LIST_URL,
                CrawlGraphNodeType.PAGINATION_URL,
                CrawlGraphNodeType.FACULTY_FOLLOWUP_URL,
            ],
        )
        if not org_units and recoverable_tasks <= 0 and not ready_graph_candidates:
            message = (
                "resume_blocked_missing_cache: start URL was already crawled but no page cache, "
                "org units, recoverable crawl tasks, or graph frontier URLs were available"
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

        if ready_graph_candidates:
            self.logger.info(
                "Strict resume recovering graph frontier university=%s ready_urls=%s",
                self.university_name,
                len(ready_graph_candidates),
            )
            await self._extract_professors([])

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

    async def _run_manual_entrance_flow(self, resume_seed_org_units: list[OrgUnit]) -> AgentResult | None:
        self.logger.info(
            "Manual entrance flow university=%s manual_org_units=%s org_listing_urls=%s resume_seed_org_units=%s",
            self.university_name,
            len(self.manual_org_units),
            len(self.org_unit_listing_urls),
            len(resume_seed_org_units),
        )

        manual_org_units = await self._seed_manual_org_units()
        if manual_org_units or self.org_unit_listing_urls:
            await self.graph_frontier.seed_manual_entrances(
                org_units=manual_org_units,
                manual_org_units=self.manual_org_units,
                org_unit_listing_urls=self.org_unit_listing_urls,
            )
        org_units = manual_org_units or list(resume_seed_org_units)
        if not org_units and self.org_unit_listing_urls:
            graph_candidates = await self.graph_frontier.next_fetch_candidates(
                limit=max(1, len(self.org_unit_listing_urls)),
                node_types=[CrawlGraphNodeType.ORG_LISTING_URL],
            )
            listing_url_keys = {_sanitize_url(url) for url in self.org_unit_listing_urls if _sanitize_url(url)}
            org_unit_pages = [
                self.graph_frontier.to_queued_url(candidate, _QueuedUrl)
                for candidate in graph_candidates
                if _sanitize_url(candidate.url) in listing_url_keys
            ] or [
                _QueuedUrl(url=url, depth=1, label="manual_org_listing")
                for url in self.org_unit_listing_urls
            ]
            org_units = await self._extract_org_units(org_unit_pages)

        if not org_units:
            await self._set_status(CrawlStatus.FAILED)
            return self._result(CrawlStatus.FAILED, ["No org units from manual entrances"])

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
        elif not manual_org_units:
            org_units = await self._filter_existing_org_units_for_discovery(
                org_units,
                source="manual_org_listing",
            )
            if not org_units:
                await self._set_status(CrawlStatus.FAILED)
                return self._result(CrawlStatus.FAILED, ["all org units excluded by scope filter"])

        self._target_org_unit_ids = {int(unit.id) for unit in org_units if unit.id is not None}
        if manual_org_units:
            manual_faculty_links = await self._manual_faculty_links_for_org_units(org_units)
            if manual_faculty_links:
                self.logger.info(
                    "Manual faculty entrances university=%s links=%s",
                    self.university_name,
                    len(manual_faculty_links),
                )
                await self._extract_professors(manual_faculty_links)

            remaining_org_units = self._org_units_without_manual_faculty(org_units)
            if remaining_org_units:
                self.logger.info(
                    "Manual org units without faculty entrance university=%s org_units=%s mode=%s",
                    self.university_name,
                    len(remaining_org_units),
                    "streaming" if self._is_interactive else "batch",
                )
                if self._is_interactive:
                    await self._find_and_extract_streaming(remaining_org_units)
                else:
                    faculty_links = await self._find_faculty_pages(remaining_org_units)
                    if faculty_links:
                        await self._extract_professors(faculty_links)
            return None

        if self._is_interactive:
            await self._find_and_extract_streaming(org_units)
            return None

        faculty_links = await self._find_faculty_pages(org_units)
        if faculty_links:
            await self._extract_professors(faculty_links)
        return None

    async def _seed_manual_org_units(self) -> list[OrgUnit]:
        if not self.manual_org_units:
            return []
        seeded: list[OrgUnit] = []
        async with self.db.session() as session:
            for item in self.manual_org_units[: self.max_org_units_per_university]:
                name = normalize_org_unit_name(item.name, default="")
                if not name:
                    continue
                org_unit_url = _sanitize_url(item.url or item.faculty_url or "")
                if not org_unit_url:
                    continue
                row = await crawler_db.get_or_create_org_unit(
                    session,
                    name=name,
                    url=org_unit_url,
                    kind=item.kind or "college",
                    discovered_from_url=self.start_url,
                )
                seeded.append(row)
        return seeded

    async def _manual_faculty_links_for_org_units(self, org_units: list[OrgUnit]) -> list[_QueuedUrl]:
        if not self.manual_org_units or not org_units:
            return []
        units_by_name: dict[str, OrgUnit] = {}
        for unit in org_units:
            canonical_key = self._normalize_org_unit_match_text(unit.name)
            if not canonical_key:
                continue
            units_by_name[canonical_key] = unit
            for alias in self._manual_org_unit_aliases_by_name.get(canonical_key, set()):
                units_by_name.setdefault(alias, unit)
        result: list[_QueuedUrl] = []
        for item in self.manual_org_units:
            faculty_url = _sanitize_url(item.faculty_url)
            if not faculty_url:
                continue
            unit = None
            for value in (item.name, item.raw_name, *item.aliases):
                key = self._normalize_org_unit_match_text(str(value or ""))
                if key:
                    unit = units_by_name.get(key)
                if unit is not None:
                    break
            if unit is None:
                continue
            result.append(
                _QueuedUrl(
                    url=faculty_url,
                    depth=1,
                    label=unit.name,
                    org_unit_id=unit.id,
                )
            )
        result = _dedupe_queue(result)
        if not result:
            return []
        graph_candidates = await self.graph_frontier.record_discovered_links(
            source_url=self.start_url,
            links=result,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            edge_type=CrawlGraphEdgeType.SEEDED_FROM_MANIFEST,
            source_node_type=CrawlGraphNodeType.ORG_LISTING_URL,
            depth=1,
            confidence=1.0,
            metadata={"source": "manifest"},
            source_status=CrawlGraphNodeStatus.DONE,
        )
        if not graph_candidates:
            return result
        return [
            self.graph_frontier.to_queued_url(candidate, _QueuedUrl)
            for candidate in graph_candidates
        ]

    def _org_units_without_manual_faculty(self, org_units: list[OrgUnit]) -> list[OrgUnit]:
        with_faculty = {
            self._normalize_org_unit_match_text(item.name)
            for item in self.manual_org_units
            if _sanitize_url(item.faculty_url)
        }
        if not with_faculty:
            return org_units
        return [
            unit
            for unit in org_units
            if self._normalize_org_unit_match_text(unit.name) not in with_faculty
        ]

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

        org_page_items = [
            _QueuedUrl(url=link, depth=1, label="org_unit_page")
            for link in links[:20]
            if self._within_depth(1)
        ]
        graph_candidates = await self.graph_frontier.record_discovered_links(
            source_url=home.url,
            links=org_page_items,
            node_type=CrawlGraphNodeType.ORG_LISTING_URL,
            edge_type=CrawlGraphEdgeType.DISCOVERED_ON_PAGE,
            source_node_type=CrawlGraphNodeType.ORG_LISTING_URL,
            depth=1,
            confidence=1.0,
            metadata={"source": "org_page_discovery"},
            source_status=CrawlGraphNodeStatus.DONE,
        )
        if graph_candidates:
            return [
                self.graph_frontier.to_queued_url(candidate, _QueuedUrl)
                for candidate in graph_candidates
            ]
        return org_page_items

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
                followup_items: list[_QueuedUrl] = []
                for link in followups:
                    if link in seen_pages:
                        continue
                    if not _same_site(link, self.start_url) or _is_faculty_platform(link):
                        continue
                    next_depth = page.depth + (0 if link == fetched.url else 1)
                    if not self._within_depth(next_depth):
                        continue
                    seen_pages.add(link)
                    followup_item = _QueuedUrl(url=link, depth=next_depth, label=page.label)
                    pending.append(followup_item)
                    followup_items.append(followup_item)
                    added_followups.append(link)
                if added_followups:
                    await self.graph_frontier.record_discovered_links(
                        source_url=fetched.url,
                        links=followup_items,
                        node_type=CrawlGraphNodeType.ORG_LISTING_URL,
                        edge_type=CrawlGraphEdgeType.DISCOVERED_ON_PAGE,
                        source_node_type=CrawlGraphNodeType.ORG_LISTING_URL,
                        depth=page.depth + 1,
                        confidence=0.8,
                        metadata={"source": "org_unit_llm_followup"},
                    )
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

            stored_units: list[OrgUnit] = []
            async with self.db.session() as session:
                for unit in filtered_units:
                    kind = str(unit.get("kind") or "").strip() or None
                    if _is_core_academic_kind(kind):
                        core_validated_total += 1
                    row = await crawler_db.get_or_create_org_unit(
                        session,
                        name=str(unit.get("name") or ""),
                        url=str(unit.get("url") or ""),
                        kind=kind,
                        discovered_from_url=str(unit.get("discovered_from_url") or fetched.url),
                    )
                    stored_units.append(row)
            if stored_units:
                await self.graph_frontier.record_org_units(stored_units, source_url=fetched.url)

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
                followup_items: list[_QueuedUrl] = []
                for link in followups[:8]:
                    if link in seen_pages:
                        continue
                    next_depth = page.depth + (0 if link == fetched.url else 1)
                    if not self._within_depth(next_depth):
                        continue
                    seen_pages.add(link)
                    followup_item = _QueuedUrl(url=link, depth=next_depth, label=page.label)
                    pending.append(followup_item)
                    followup_items.append(followup_item)
                    added_followups.append(link)
                if added_followups:
                    await self.graph_frontier.record_discovered_links(
                        source_url=fetched.url,
                        links=followup_items,
                        node_type=CrawlGraphNodeType.ORG_LISTING_URL,
                        edge_type=CrawlGraphEdgeType.DISCOVERED_ON_PAGE,
                        source_node_type=CrawlGraphNodeType.ORG_LISTING_URL,
                        depth=page.depth + 1,
                        confidence=0.7,
                        metadata={"source": "org_unit_zero_validated_followup"},
                    )
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

    def _org_unit_match_score_with_aliases(self, query: str, candidate: str) -> float:
        score = self._org_unit_match_score(query, candidate)
        candidate_key = self._normalize_org_unit_match_text(candidate)
        for alias in self._manual_org_unit_aliases_by_name.get(candidate_key, set()):
            score = max(score, self._org_unit_match_score(query, alias))
        return score

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
                score = self._org_unit_match_score_with_aliases(target, unit.name)
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
            self._org_unit_match_score_with_aliases(target, name) >= self.org_unit_match_threshold
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
                graph_candidates = await self.graph_frontier.record_discovered_links(
                    source_url=fetched.url,
                    links=faculty_for_unit,
                    node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
                    edge_type=CrawlGraphEdgeType.DISCOVERED_ON_PAGE,
                    source_node_type=CrawlGraphNodeType.ORG_UNIT,
                    org_unit_name=org_unit.name,
                    org_unit_id=org_unit.id,
                    depth=item.depth + 1,
                    confidence=1.0,
                    metadata={"source": "streaming_faculty_discovery"},
                )
                if graph_candidates:
                    faculty_for_unit = [
                        self.graph_frontier.to_queued_url(candidate, _QueuedUrl)
                        for candidate in graph_candidates
                    ]
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
                    faculty_for_unit = [
                        _QueuedUrl(
                            url=link,
                            depth=depth,
                            label=item.label,
                            org_unit_id=item.org_unit_id,
                        )
                    ]
                    graph_candidates = await self.graph_frontier.record_discovered_links(
                        source_url=fetched.url,
                        links=faculty_for_unit,
                        node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
                        edge_type=CrawlGraphEdgeType.DISCOVERED_ON_PAGE,
                        source_node_type=CrawlGraphNodeType.ORG_UNIT,
                        org_unit_name=org_unit.name,
                        org_unit_id=org_unit.id,
                        depth=depth,
                        confidence=1.0,
                        metadata={"source": "batch_faculty_discovery"},
                    )
                    if graph_candidates:
                        faculty_links.extend(
                            self.graph_frontier.to_queued_url(candidate, _QueuedUrl)
                            for candidate in graph_candidates
                        )
                    else:
                        faculty_links.extend(faculty_for_unit)

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
            and not _is_non_faculty_noise_url(item.url)
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
        return should_skip_professor_llm(url=url, text=text)

    async def _extract_professors_from_page(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        skills: str,
        *,
        detail_mode: bool,
        requested_url: str | None = None,
    ) -> int:
        if is_retryable_fetch_failure(fetched.block_reason):
            result = await self._mark_retryable_fetch_failure(
                current,
                fetched,
                detail_mode=detail_mode,
            )
            return -1 if result == "skipped" else 0
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
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error=f"redirect:{redirect_reason}",
            )
            return -1
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

    async def _process_detail_urls_with_human(
        self,
        urls: list[str | GraphFetchCandidate],
        current: _QueuedUrl,
        skills: str,
    ) -> None:
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
