from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from agents.crawler import db as crawler_db
from agents.crawler import agent_detail, agent_parsing
from agents.crawler.fetchers import FetchResult, Fetcher
from agents.crawler.models import CrawlLogStatus, CrawlStatus, CrawlTaskStatus, OrgUnit, UniversityMeta
from agents.crawler.tools import get_crawler_tool_definitions, get_crawler_tools
from agents.crawler.url_heuristics import (
    FACULTY_KEYWORDS,
    ORG_UNIT_PAGE_KEYWORDS,
    _COMMON_FACULTY_PATHS,
    _INTERMEDIATE_ORG_PATHS,
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
    _truncate_middle,
    _url_found_on_page,
)
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


@dataclass
class _SaveEvent:
    task: _ExtractionTaskItem
    payloads: list[dict[str, Any]]


@dataclass
class _ExtractionOutcome:
    payloads: list[dict[str, Any]]
    invalid_json_events: list[dict[str, Any]]
    content_fallback_used: bool = False


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
        detail_fetch_backend: str = "human",
        detail_profile_hard_cap_per_org_unit: int = 200,
        detail_failure_threshold: int = 10,
        pipeline_enabled: bool = True,
        pipeline_fetch_workers: int = 1,
        pipeline_llm_workers: int = 1,
        pipeline_db_workers: int = 1,
        pipeline_queue_cap: int = 64,
        invalid_json_max_retry: int = 1,
        task_recovery_enabled: bool = True,
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
        self.detail_fetch_backend = detail_fetch_backend
        self.detail_profile_hard_cap_per_org_unit = max(1, int(detail_profile_hard_cap_per_org_unit))
        self.detail_failure_threshold = max(1, int(detail_failure_threshold))
        self.pipeline_enabled = bool(pipeline_enabled)
        self.pipeline_fetch_workers = max(1, int(pipeline_fetch_workers))
        self.pipeline_llm_workers = max(1, int(pipeline_llm_workers))
        self.pipeline_db_workers = max(1, int(pipeline_db_workers))
        self.pipeline_queue_cap = max(1, int(pipeline_queue_cap))
        self.invalid_json_max_retry = max(0, int(invalid_json_max_retry))
        self.task_recovery_enabled = bool(task_recovery_enabled)
        self.visited_urls: set[str] = set()
        self._fetch_cache: dict[str, FetchResult] = {}
        self.backtrack_count = 0
        self.execution_log: list[str] = []
        self.saved_professors = 0
        self._current_state: str = ""
        self._university_cache: UniversityMeta | None = None
        self._skip_cross_run_dedup = False
        self._blocked_hosts: set[str] = set()
        self._detail_fetcher: Fetcher | None = None
        self._detail_visited_urls: set[str] = set()
        self._detail_processed_by_org_unit: dict[str, int] = {}
        self._pipeline_stats: dict[str, Any] = {
            "enabled": self.pipeline_enabled,
            "pending": 0,
            "in_progress": 0,
            "retry": 0,
            "done": 0,
            "failed": 0,
            "retries": 0,
            "invalid_json_failures": 0,
            "no_structured_data_failures": 0,
            "save_errors": 0,
            "processed_tasks": 0,
            "timed_tasks": 0,
            "average_task_ms": 0.0,
            "queue_depth": 0,
            "llm_calls_total": 0,
            "llm_calls_skipped_by_gate": 0,
            "llm_payload_bytes_total": 0,
            "avg_payload_bytes": 0.0,
            "followup_dropped_noise": 0,
            "detail_links_dropped_noise": 0,
        }

    @property
    def _is_interactive(self) -> bool:
        """True when using a human-assisted fetcher (streaming per-org-unit is preferred)."""
        return hasattr(self.fetcher, 'set_context')

    async def run(self) -> AgentResult:
        await self._ensure_university()
        await self._set_status(CrawlStatus.IN_PROGRESS)
        if hasattr(self.fetcher, "set_status_provider"):
            try:
                self.fetcher.set_status_provider(self.status_snapshot)  # type: ignore[attr-defined]
            except Exception:
                pass

        detail_fetcher_cm: Fetcher | None = None
        if self._is_interactive and self.detail_enrich_enabled and self.detail_fetch_backend == "httpx":
            detail_fetcher_cm = Fetcher(
                request_interval_seconds=self.fetcher.request_interval_seconds if hasattr(self.fetcher, "request_interval_seconds") else 2.0,
                max_retries=self.fetcher.max_retries if hasattr(self.fetcher, "max_retries") else 3,
                timeout_seconds=self.fetcher._timeout_seconds if hasattr(self.fetcher, "_timeout_seconds") else 30.0,
            )
            self._detail_fetcher = await detail_fetcher_cm.__aenter__()

        try:
            self.logger.info("Starting crawl for %s", self.university_name)
            initial_professor_count = await self._professor_count()
            home = await self._fetch_url(self.start_url, 0)
            if home is None:
                self.logger.warning("Start URL could not be fetched: %s", self.start_url)
                await self._set_status(CrawlStatus.FAILED)
                return self._result(CrawlStatus.FAILED, ["Failed to fetch start URL"])

            # Retry loop: re-attempt the pipeline when a phase fails.
            org_unit_pages: list[_QueuedUrl] = []
            org_units: list[OrgUnit] = []
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
                if len(org_units) < self.min_org_units:
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

                if self._is_interactive:
                    # Interactive (human) mode: find faculty + extract professors
                    # per org unit to avoid spending all time on discovery.
                    await self._find_and_extract_streaming(org_units)
                    break

                if not faculty_links:
                    faculty_links = await self._find_faculty_pages(org_units)
                if not faculty_links:
                    if self._too_many_backtracks("no faculty links found"):
                        break
                    org_units = []
                    self._skip_cross_run_dedup = True
                    continue

                break

            if not self._is_interactive:
                if not faculty_links:
                    faculty_links = [_QueuedUrl(home.url, 0, label="Unknown")]
                await self._extract_professors(faculty_links)


            total_professor_count = await self._professor_count()
            newly_saved_count = max(0, total_professor_count - initial_professor_count)
            if total_professor_count <= 0:
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
        finally:
            if detail_fetcher_cm is not None:
                await detail_fetcher_cm.__aexit__(None, None, None)
            self._detail_fetcher = None
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
                "Exclude admin offices. Return JSON: {\"org_units\": [{\"name\": ..., \"url\": ..., \"kind\": ...}]}",
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
            async with self.db.session() as session:
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
                    if _is_core_academic_kind(kind):
                        core_validated_total += 1
                    await crawler_db.get_or_create_org_unit(
                        session,
                        name=name,
                        url=url,
                        kind=kind,
                        discovered_from_url=fetched.url,
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
    async def _find_and_extract_streaming(self, org_units: list[OrgUnit]) -> None:
        """Interactive mode: for each org unit, find faculty pages then immediately extract professors."""
        self._log_state(CrawlerState.FIND_FACULTY_PAGES)
        max_links_per_org_unit = 8
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
            links = _keyword_filter(fetched.links, FACULTY_KEYWORDS)
            links = [
                l
                for l in self.fetcher.filter_same_domain(links, self.start_url)
                if not _is_faculty_platform(l)
                and not _looks_like_retired_url(l)
                and not _is_non_faculty_noise_url(l)
            ]
            links = _rank_faculty_page_candidates(links)
            non_showcase = [l for l in links if not _is_academician_showcase_page(l)]
            if non_showcase:
                links = non_showcase

            if not links and _is_college_subdomain(fetched.url, self.start_url):
                links.extend(await self._probe_faculty_paths(fetched.url))
            if not links and _looks_like_faculty_page(fetched.url):
                links = [fetched.url]
            if not links and llm_fallback_budget > 0:
                llm_fallback_budget -= 1
                result = await self._ask_llm(
                    CrawlerState.FIND_FACULTY_PAGES,
                    "Find links that lead to faculty list pages for this org unit.",
                    fetched, skills_find,
                )
                links = self._links_from_result(result.content)
                if not links:
                    links = self._links_from_tool_call_log(result, tool_name="extract_links")
                links = [
                    l
                    for l in self.fetcher.filter_same_domain(links, self.start_url)
                    if not _is_faculty_platform(l)
                    and not _looks_like_retired_url(l)
                    and not _is_non_faculty_noise_url(l)
                ]
                links = _rank_faculty_page_candidates(links)
                non_showcase = [l for l in links if not _is_academician_showcase_page(l)]
                if non_showcase:
                    links = non_showcase

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
        self._log_state(CrawlerState.FIND_FACULTY_PAGES)
        faculty_links: list[_QueuedUrl] = []
        max_links_per_org_unit = 8
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

            links = _keyword_filter(fetched.links, FACULTY_KEYWORDS)
            links = [
                l
                for l in self.fetcher.filter_same_domain(links, self.start_url)
                if not _is_faculty_platform(l)
                and not _looks_like_retired_url(l)
                and not _is_non_faculty_noise_url(l)
            ]
            links = _rank_faculty_page_candidates(links)
            non_showcase_links = [l for l in links if not _is_academician_showcase_page(l)]
            if non_showcase_links:
                links = non_showcase_links

            if not links and _is_college_subdomain(fetched.url, self.start_url):
                links.extend(await self._probe_faculty_paths(fetched.url))

            if not links and _looks_like_faculty_page(fetched.url):
                links = [fetched.url]

            if not links and llm_fallback_budget > 0:
                llm_fallback_budget -= 1
                result = await self._ask_llm(
                    CrawlerState.FIND_FACULTY_PAGES,
                    "Find links that lead to faculty list pages for this org unit (e.g. faculty, teacher, staff, people, szdw, jsdw). Return only links.",
                    fetched,
                    skills,
                )
                links = self._links_from_result(result.content)
                if not links:
                    links = self._links_from_tool_call_log(result, tool_name="extract_links")
                    if links:
                        self.logger.debug(
                            "Faculty discovery consumed extract_links tool output org_unit=%s sample=%s",
                            item.label,
                            links[:5],
                        )
                links = [
                    l
                    for l in self.fetcher.filter_same_domain(links, self.start_url)
                    if not _is_faculty_platform(l)
                    and not _looks_like_retired_url(l)
                    and not _is_non_faculty_noise_url(l)
                ]
                links = _rank_faculty_page_candidates(links)
                non_showcase_links = [l for l in links if not _is_academician_showcase_page(l)]
                if non_showcase_links:
                    links = non_showcase_links

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
            search_links = [
                l
                for l in search_links
                if not _is_faculty_platform(l) and not _is_non_faculty_noise_url(l)
            ]
            search_links = _rank_faculty_page_candidates(search_links)
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
            try:
                result = await self.fetcher.fetch(probe_url)
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
                    async with self.db.session() as session:
                        await crawler_db.log_crawl(
                            session,
                            probe_url,
                            CrawlLogStatus.SUCCESS,
                            "probed",
                        )
                    break
                self.logger.debug(
                    "Faculty probe miss url=%s status=%s text_chars=%s links=%s",
                    probe_url,
                    result.status_code,
                    len(result.text),
                    len(result.links),
                )
            except Exception:
                pass
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
            try:
                result = await self.fetcher.fetch(probe_url)
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
            except Exception:
                pass
        return found
    async def _extract_professors(self, faculty_links: list[_QueuedUrl]) -> None:
        self._log_state(CrawlerState.EXTRACT_PROFESSORS)
        skills = await self._select_skills(CrawlerState.EXTRACT_PROFESSORS)
        max_pages = min(max(40, len(faculty_links)), 120)
        self.logger.info(
            "Extraction pipeline enabled=%s fetch_workers=%s llm_workers=%s db_workers=%s queue_cap=%s retry=%s",
            self.pipeline_enabled,
            self.pipeline_fetch_workers,
            self.pipeline_llm_workers,
            self.pipeline_db_workers,
            self.pipeline_queue_cap,
            self.invalid_json_max_retry,
        )
        if not self.pipeline_enabled:
            for item in faculty_links[:max_pages]:
                pages_to_process = [item]
                while pages_to_process:
                    current = pages_to_process.pop(0)
                    fetched = await self._fetch_url(current.url, current.depth)
                    if fetched is None:
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
                    await self._enrich_profiles_with_detail_backend(current, fetched, skills)
                    followups = self._extract_followup_faculty_links(fetched.links, fetched.url)
                    for link in followups[:10]:
                        if link in self.visited_urls:
                            continue
                        next_depth = current.depth + 1
                        if not self._within_depth(next_depth):
                            continue
                        pages_to_process.append(
                            _QueuedUrl(
                                url=link,
                                depth=next_depth,
                                label=current.label,
                                org_unit_id=current.org_unit_id,
                            )
                        )
                    pagination_links = self._extract_pagination_links(fetched.links, fetched.url)
                    for plink in pagination_links:
                        if plink not in self.visited_urls and self._within_depth(current.depth):
                            pages_to_process.append(
                                _QueuedUrl(
                                    url=plink,
                                    depth=current.depth,
                                    label=current.label,
                                    org_unit_id=current.org_unit_id,
                                )
                            )
            return

        llm_queue: asyncio.Queue[_ExtractionTaskItem | None] = asyncio.Queue(maxsize=self.pipeline_queue_cap)
        db_queue: asyncio.Queue[_SaveEvent | None] = asyncio.Queue(maxsize=self.pipeline_queue_cap)

        if self.pipeline_enabled and self.task_recovery_enabled:
            recovered = await self._recover_pipeline_tasks(limit=self.pipeline_queue_cap * 4)
            for task in recovered:
                await llm_queue.put(task)
            if recovered:
                self.logger.info("Recovered %s pending extraction tasks from DB", len(recovered))

        llm_workers = [
            asyncio.create_task(self._pipeline_llm_worker(llm_queue, db_queue, skills), name=f"llm_worker_{i}")
            for i in range(self.pipeline_llm_workers)
        ]
        db_workers = [
            asyncio.create_task(self._pipeline_db_worker(db_queue), name=f"db_worker_{i}")
            for i in range(self.pipeline_db_workers)
        ]

        try:
            for item in faculty_links[:max_pages]:
                pages_to_process = [item]
                while pages_to_process:
                    current = pages_to_process.pop(0)
                    fetched = await self._fetch_url(current.url, current.depth)
                    if fetched is None:
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
                    await self._enrich_profiles_with_detail_backend(current, fetched, skills)

                    followups = self._extract_followup_faculty_links(fetched.links, fetched.url)
                    for link in followups[:10]:
                        if link in self.visited_urls:
                            continue
                        next_depth = current.depth + 1
                        if not self._within_depth(next_depth):
                            continue
                        pages_to_process.append(
                            _QueuedUrl(
                                url=link,
                                depth=next_depth,
                                label=current.label,
                                org_unit_id=current.org_unit_id,
                            )
                        )

                    pagination_links = self._extract_pagination_links(fetched.links, fetched.url)
                    for plink in pagination_links:
                        if plink not in self.visited_urls and self._within_depth(current.depth):
                            pages_to_process.append(
                                _QueuedUrl(
                                    url=plink,
                                    depth=current.depth,
                                    label=current.label,
                                    org_unit_id=current.org_unit_id,
                                )
                            )
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
                "Extraction pipeline stats queue_depth=%s processed=%s retries=%s failed=%s avg_task_ms=%.1f llm_calls=%s skipped_by_gate=%s avg_payload_bytes=%.1f",
                self._pipeline_stats.get("queue_depth", 0),
                self._pipeline_stats.get("processed_tasks", 0),
                self._pipeline_stats.get("retries", 0),
                self._pipeline_stats.get("failed", 0),
                float(self._pipeline_stats.get("average_task_ms", 0.0)),
                self._pipeline_stats.get("llm_calls_total", 0),
                self._pipeline_stats.get("llm_calls_skipped_by_gate", 0),
                float(self._pipeline_stats.get("avg_payload_bytes", 0.0)),
            )

    async def _enqueue_extraction_task(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        *,
        llm_queue: asyncio.Queue[_ExtractionTaskItem | None],
        detail_mode: bool,
        priority: int,
    ) -> None:
        source_url = _sanitize_url(fetched.url) or _sanitize_url(current.url) or ""
        if not source_url:
            return
        text_limit = self._state_text_limit(CrawlerState.EXTRACT_PROFESSORS, detail_mode=detail_mode)
        snapshot = self._compact_page_text(fetched.text or "", text_limit)
        page_hash = hashlib.sha1(f"{source_url}|{snapshot}".encode("utf-8", errors="ignore")).hexdigest()
        allowed_tools = ["save_professors"]
        async with self.db.session() as session:
            row = await crawler_db.upsert_crawl_task(
                session,
                university=self.university_name,
                org_unit_name=current.label or "Unknown",
                org_unit_url=current.url,
                source_url=source_url,
                page_url=source_url,
                page_hash=page_hash,
                page_text_snapshot=snapshot,
                allowed_tools=json.dumps(sorted(allowed_tools), ensure_ascii=False, separators=(",", ":")),
                attempt=0,
                priority=priority,
                status=CrawlTaskStatus.PENDING,
            )
            if row.status == CrawlTaskStatus.DONE.value:
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
            )
        await llm_queue.put(task)
        self._pipeline_stats["pending"] = int(self._pipeline_stats.get("pending", 0)) + 1
        self._pipeline_stats["queue_depth"] = llm_queue.qsize()

    async def _recover_pipeline_tasks(self, *, limit: int) -> list[_ExtractionTaskItem]:
        async with self.db.session() as session:
            rows = await crawler_db.list_recoverable_crawl_tasks(session, limit=limit)
        recovered: list[_ExtractionTaskItem] = []
        for row in rows:
            allowed_tools = ["save_professors"]
            if row.allowed_tools:
                try:
                    parsed = json.loads(row.allowed_tools)
                    if isinstance(parsed, list) and parsed:
                        allowed_tools = [str(item) for item in parsed]
                except Exception:
                    pass
            recovered.append(
                _ExtractionTaskItem(
                    task_id=int(row.id),
                    university=row.university or self.university_name,
                    org_unit_name=row.org_unit_name,
                    org_unit_url=row.org_unit_url,
                    source_url=row.source_url,
                    page_url=row.page_url,
                    page_hash=row.page_hash,
                    page_text_snapshot=row.page_text_snapshot,
                    allowed_tools=allowed_tools,
                    attempt=int(row.attempt or 0),
                    priority=int(row.priority or 0),
                    strict_retry=(int(row.attempt or 0) > 0),
                    detail_mode=False,
                )
            )
            if row.status == CrawlTaskStatus.RETRY.value:
                self._pipeline_stats["retry"] = int(self._pipeline_stats.get("retry", 0)) + 1
            else:
                self._pipeline_stats["pending"] = int(self._pipeline_stats.get("pending", 0)) + 1
        return recovered

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
            self._pipeline_stats["in_progress"] = int(self._pipeline_stats.get("in_progress", 0)) + 1
            self._pipeline_stats["pending"] = max(0, int(self._pipeline_stats.get("pending", 0)) - 1)
            async with self.db.session() as session:
                await crawler_db.set_crawl_task_status(
                    session,
                    task.task_id,
                    status=CrawlTaskStatus.IN_PROGRESS,
                    attempt=task.attempt,
                )

            outcome = await self._run_extraction_task(task, skills)
            invalid_events = [event for event in outcome.invalid_json_events if event.get("name") == "save_professors"]
            if invalid_events:
                await self._handle_invalid_json_retry(task, invalid_events, llm_queue)
                elapsed_ms = (time.perf_counter() - started) * 1000
                self._update_pipeline_timing(elapsed_ms)
                self._pipeline_stats["in_progress"] = max(0, int(self._pipeline_stats.get("in_progress", 0)) - 1)
                llm_queue.task_done()
                continue

            if not outcome.payloads:
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
                self._pipeline_stats["no_structured_data_failures"] += 1
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
                saved = await self._save_payloads_to_db(event.payloads)
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
            if saved > 0:
                self.logger.debug(
                    "Extraction task done task_id=%s org_unit=%s saved=%s",
                    task.task_id,
                    task.org_unit_name,
                    saved,
                )
            db_queue.task_done()

    async def _handle_invalid_json_retry(
        self,
        task: _ExtractionTaskItem,
        invalid_events: list[dict[str, Any]],
        llm_queue: asyncio.Queue[_ExtractionTaskItem | None],
    ) -> None:
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
            self._pipeline_stats["retry"] = int(self._pipeline_stats.get("retry", 0)) + 1
            self._pipeline_stats["pending"] = int(self._pipeline_stats.get("pending", 0)) + 1
            await llm_queue.put(retry_task)
            return

        async with self.db.session() as session:
            await crawler_db.set_crawl_task_status(
                session,
                task.task_id,
                status=CrawlTaskStatus.FAILED,
                attempt=task.attempt,
                last_error="invalid_json_dropped",
            )
            await crawler_db.log_extraction_failure(
                session,
                task_id=task.task_id,
                failure_type="invalid_json",
                org_unit_name=task.org_unit_name,
                source_url=task.source_url,
                raw_arguments_preview=preview,
                attempt=task.attempt,
                resolver="dropped",
            )
        self._pipeline_stats["failed"] += 1
        self._pipeline_stats["invalid_json_failures"] += 1
        self._pipeline_stats["retry"] = max(0, int(self._pipeline_stats.get("retry", 0)) - 1)

    async def _run_extraction_task(self, task: _ExtractionTaskItem, skills: str) -> _ExtractionOutcome:
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
        dynamic_system_content = (
            "Tool call policy: "
            + self._build_tool_call_policy(allowed_tools)
            + " Keep output short and strict JSON."
        )
        prompt_max_tokens = min(int(self.model_max_tokens), 16000)
        batches = self.context_manager.build_messages(
            "You are a cautious university faculty crawler. Stay on the same university domain.",
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
            captured_payloads.append(
                {
                    "org_unit_name": org_unit_name,
                    "org_unit_url": org_unit_url or task.org_unit_url,
                    "source_url": source_url or task.source_url,
                    "professors": normalized_professors,
                }
            )
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
                    captured_payloads.append(
                        {
                            "org_unit_name": str(payload.get("org_unit_name") or task.org_unit_name),
                            "org_unit_url": str(payload.get("org_unit_url") or task.org_unit_url or "").strip() or None,
                            "source_url": str(payload.get("source_url") or task.source_url or "").strip() or task.source_url,
                            "professors": [item for item in professors if isinstance(item, dict)],
                        }
                    )
                    used_fallback = True

        return _ExtractionOutcome(
            payloads=captured_payloads,
            invalid_json_events=invalid_events,
            content_fallback_used=used_fallback,
        )

    async def _save_payloads_to_db(self, payloads: list[dict[str, Any]]) -> int:
        tools = get_crawler_tools(self.db, self.skill_manager)
        total_saved = 0
        for payload in payloads:
            result = await tools["save_professors"](
                org_unit_name=str(payload.get("org_unit_name") or "Unknown"),
                org_unit_url=(str(payload.get("org_unit_url")) if payload.get("org_unit_url") else None),
                source_url=(str(payload.get("source_url")) if payload.get("source_url") else None),
                professors=[item for item in payload.get("professors", []) if isinstance(item, dict)],
            )
            if isinstance(result, dict):
                saved = int(result.get("saved", 0) or 0)
                self.saved_professors += saved
                total_saved += saved
        return total_saved

    def _build_professor_instruction(self, org_unit_name: str, *, detail_mode: bool, strict_retry: bool) -> str:
        if detail_mode:
            base = (
                "Extract professor records from this detail page and call save_professors when records are found. "
                + f"Use org_unit_name={org_unit_name!r}. Set source_url to the current page URL. "
                + "Prioritize fields: email, phone, research_areas. "
                + "Only save records that include at least one of email/phone/research_areas. "
                + "If this page only contains category/list names without these fields, do not save placeholders. "
                + "Do not include retired/emeritus records. "
                + "If content is mainly notices/news/policies/recruitment/personnel announcements, skip saving."
            )
        else:
            base = (
                "Extract public professor records and call save_professors when records are found. "
                + f"Use org_unit_name={org_unit_name!r}. Set source_url to the current page URL. "
                + "If this is a paginated list, also return pagination links (next page, page 2, etc.). "
                + "Do not include retired/emeritus records. "
                + "Skip noise pages dominated by notices/news/policies/recruitment/personnel content."
            )
        if not strict_retry:
            return base
        return (
            base
            + " Retry mode: output only key fields {name,title,email,phone,research_areas}; "
            + "keep response concise, max 25 records, avoid extra keys."
        )

    def _update_pipeline_timing(self, elapsed_ms: float) -> None:
        total = float(self._pipeline_stats.get("average_task_ms", 0.0))
        timed = int(self._pipeline_stats.get("timed_tasks", 0))
        current = timed + 1
        self._pipeline_stats["average_task_ms"] = ((total * timed) + elapsed_ms) / current
        self._pipeline_stats["timed_tasks"] = current

    @staticmethod
    def _state_link_limit(state: CrawlerState) -> int:
        limits = {
            CrawlerState.DISCOVER_ORG_UNIT_PAGES: 40,
            CrawlerState.EXTRACT_ORG_UNITS: 40,
            CrawlerState.FIND_FACULTY_PAGES: 30,
            CrawlerState.EXTRACT_PROFESSORS: 0,
        }
        return limits.get(state, 30)

    @staticmethod
    def _state_text_limit(state: CrawlerState, *, detail_mode: bool = False) -> int:
        if detail_mode:
            return 8000
        limits = {
            CrawlerState.DISCOVER_ORG_UNIT_PAGES: 3000,
            CrawlerState.EXTRACT_ORG_UNITS: 8000,
            CrawlerState.FIND_FACULTY_PAGES: 6000,
            CrawlerState.EXTRACT_PROFESSORS: 6000,
        }
        return limits.get(state, 6000)

    def _compact_page_text(self, text: str, max_chars: int, *, min_chars: int = 300) -> str:
        if max_chars <= 0 or not text:
            return ""
        compacted = self.context_manager.compact_text(text)
        if compacted and len(compacted) > max_chars:
            compacted = _truncate_middle(compacted, max_chars)

        # Avoid over-filtering: fallback to middle truncation when compacted text is too short.
        if not compacted or (len(compacted) < min(min_chars, max_chars // 2) and len(text) > len(compacted) * 2):
            return _truncate_middle(text, max_chars)
        return compacted

    @staticmethod
    def _serialize_payload(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _build_tool_call_policy(allowed_tools: set[str]) -> str:
        if not allowed_tools:
            return "Do not call any tools."
        names = sorted(allowed_tools)
        if len(names) == 1:
            return f"Only call {names[0]}. Do not invent tool names."
        return f"Only call tools listed in allowed_tools ({', '.join(names)}). Do not invent tool names."

    def _record_llm_payload(self, payload_bytes: int) -> None:
        calls = int(self._pipeline_stats.get("llm_calls_total", 0)) + 1
        total = int(self._pipeline_stats.get("llm_payload_bytes_total", 0)) + max(0, int(payload_bytes))
        self._pipeline_stats["llm_calls_total"] = calls
        self._pipeline_stats["llm_payload_bytes_total"] = total
        self._pipeline_stats["avg_payload_bytes"] = float(total) / float(calls)

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
        raw_links = list(links or [])
        same_domain_links = self.fetcher.filter_same_domain(raw_links, self.start_url) if raw_links else []
        if state in {CrawlerState.DISCOVER_ORG_UNIT_PAGES, CrawlerState.EXTRACT_ORG_UNITS}:
            candidate_links = _keyword_filter(same_domain_links, ORG_UNIT_PAGE_KEYWORDS) or same_domain_links
        elif state in {CrawlerState.FIND_FACULTY_PAGES, CrawlerState.EXTRACT_PROFESSORS}:
            candidate_links = _keyword_filter(same_domain_links, FACULTY_KEYWORDS) or same_domain_links
        else:
            candidate_links = same_domain_links
        link_limit = self._state_link_limit(state)
        kept_links = candidate_links[:link_limit] if link_limit > 0 else []

        text_limit = self._state_text_limit(state, detail_mode=detail_mode)
        compacted_text = self._compact_page_text(page_text or "", text_limit)

        payload: dict[str, Any] = {
            "allowed_tools": sorted(allowed_tools),
            "instruction": instruction,
            "links": kept_links,
            "location": self.location,
            "page_text": compacted_text,
            "state": state.value,
            "university": self.university_name,
            "url": url,
            "visited_count": len(self.visited_urls),
        }
        if state is CrawlerState.DISCOVER_ORG_UNIT_PAGES:
            payload["visited_urls"] = sorted(self.visited_urls)[-15:]

        user_content = self._serialize_payload(payload)
        payload_bytes = len(user_content.encode("utf-8", errors="ignore"))
        metadata = {
            "raw_chars": len(page_text or ""),
            "compacted_chars": len(compacted_text),
            "links_raw": len(raw_links),
            "links_kept": len(kept_links),
            "payload_bytes": payload_bytes,
        }
        return user_content, metadata

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

    async def _extract_professors_from_page(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        skills: str,
        *,
        detail_mode: bool,
    ) -> int:
        saved_before = self.saved_professors
        source_url = _sanitize_url(fetched.url) or _sanitize_url(current.url) or ""
        if not source_url:
            return 0
        snapshot = self._compact_page_text(
            fetched.text or "",
            self._state_text_limit(CrawlerState.EXTRACT_PROFESSORS, detail_mode=detail_mode),
        )
        page_hash = hashlib.sha1(f"{source_url}|{snapshot}".encode("utf-8", errors="ignore")).hexdigest()
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
        )
        outcome = await self._run_extraction_task(task, skills)
        invalid_events = [event for event in outcome.invalid_json_events if event.get("name") == "save_professors"]
        if invalid_events and self.invalid_json_max_retry > 0:
            task.attempt = 1
            task.strict_retry = True
            outcome = await self._run_extraction_task(task, skills)
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
            await self._save_payloads_to_db(outcome.payloads)
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
        self, current: _QueuedUrl, fetched: FetchResult, skills: str
    ) -> None:
        await agent_detail.enrich_profiles_with_detail_backend(self, current, fetched, skills)

    async def _enrich_profiles_with_human(self, current: _QueuedUrl, fetched: FetchResult, skills: str) -> None:
        await agent_detail.enrich_profiles_with_human(self, current, fetched, skills)

    async def _enrich_profiles_with_httpx(self, current: _QueuedUrl, fetched: FetchResult, skills: str) -> None:
        await agent_detail.enrich_profiles_with_httpx(self, current, fetched, skills)

    async def _handle_detail_failure_decision(
        self,
        current: _QueuedUrl,
        failed_urls: list[str],
        skills: str,
    ) -> bool:
        return await agent_detail.handle_detail_failure_decision(self, current, failed_urls, skills)

    async def _process_detail_urls_with_human(self, urls: list[str], current: _QueuedUrl, skills: str) -> None:
        await agent_detail.process_detail_urls_with_human(self, urls, current, skills)

    def _extract_detail_profile_links(self, links: list[str], current_url: str) -> list[str]:
        return agent_detail.extract_detail_profile_links(self, links, current_url)

    def _detail_org_unit_key(self, current: _QueuedUrl) -> str:
        return agent_detail.detail_org_unit_key(self, current)

    @staticmethod
    def _derive_section_prefix(path: str) -> str:
        return agent_detail.derive_section_prefix(path)

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
        return agent_detail.is_failed_detail_fetch(self, fetched)

    def _is_retired_page(self, fetched: FetchResult) -> bool:
        return agent_detail.is_retired_page(self, fetched)

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
        dynamic_system_content = "Tool call policy: " + self._build_tool_call_policy(allowed_tools)

        batches = self.context_manager.build_messages(
            "You are a cautious university faculty crawler. Stay on the same university domain.",
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
        metas = self.skill_manager.list_skills()
        if not metas:
            return ""

        available = {meta.name for meta in metas}
        if state in {CrawlerState.DISCOVER_ORG_UNIT_PAGES, CrawlerState.FIND_FACULTY_PAGES}:
            desired = ["extract-links", "crawler-loop-detection"]
        elif state is CrawlerState.EXTRACT_PROFESSORS:
            desired = ["save-professors", "crawler-loop-detection"]
        else:
            desired = ["crawler-loop-detection"]

        names = [name for name in desired if name in available]
        if not names:
            names = [meta.name for meta in metas]
        return "\n\n".join(self.skill_manager.load_skills(names).values())
    async def _fetch_url(self, url: str, depth: int) -> FetchResult | None:
        url = _sanitize_url(url)
        if not url:
            return None
        if not self._within_depth(depth):
            self.execution_log.append(f"skip depth url={url} depth={depth}")
            self.logger.info("Skipping %s: depth %s exceeds max_depth=%s", url, depth, self.max_depth)
            return None
        if not _same_site(url, self.start_url):
            self.execution_log.append(f"skip external url={url}")
            self.logger.info("Skipping external URL: %s", url)
            return None

        cached = self._fetch_cache.get(url)
        if cached is not None:
            self.execution_log.append(f"fetch cache url={url} depth={depth}")
            self.logger.debug("Using cached URL: %s", url)
            return cached

        if url in self.visited_urls and not self._skip_cross_run_dedup:
            self.execution_log.append(f"skip visited url={url}")
            self.logger.info("Skipping already visited URL: %s", url)
            return None

        if url != self.start_url and not self._skip_cross_run_dedup:
            async with self.db.session() as session:
                if await crawler_db.is_url_crawled(session, url):
                    self.visited_urls.add(url)
                    self.execution_log.append(f"skip already_crawled url={url}")
                    self.logger.info("Skipping previously crawled URL: %s", url)
                    return None

        self.visited_urls.add(url)
        try:
            fetched = await self.fetcher.fetch(url)
        except Exception as error:
            async with self.db.session() as session:
                await crawler_db.log_crawl(
                    session,
                    url,
                    CrawlLogStatus.FAILED,
                    str(error),
                )
            self.execution_log.append(f"fetch failed url={url} error={error}")
            self.logger.warning("Fetch failed for %s: %s", url, error)
            return None

        canonical = _sanitize_url(fetched.url)
        if canonical:
            self.visited_urls.add(canonical)
            self._fetch_cache.setdefault(canonical, fetched)
        self._fetch_cache.setdefault(url, fetched)

        async with self.db.session() as session:
            crawl_status = CrawlLogStatus.SUCCESS
            crawl_message = f"depth={depth} status_code={fetched.status_code}"
            if fetched.block_reason:
                crawl_status = CrawlLogStatus.FAILED
                crawl_message = (
                    f"{crawl_message} blocked={fetched.block_reason} links={len(fetched.links)}"
                )
            await crawler_db.log_crawl(
                session,
                fetched.url,
                crawl_status,
                crawl_message,
            )

        if fetched.block_reason:
            blocked_host = (urlparse(fetched.url).hostname or "").lower()
            if blocked_host:
                self._blocked_hosts.add(blocked_host)
            self.logger.warning(
                "WAF/challenge page detected url=%s status=%s reason=%s links=%s",
                fetched.url,
                fetched.status_code,
                fetched.block_reason,
                len(fetched.links),
            )
            if type(self.fetcher).__name__ == "Fetcher":
                self.logger.warning(
                    "Detected anti-bot blocking on httpx backend; consider rerun with --fetcher-backend playwright"
                )
            self.execution_log.append(
                f"fetch blocked url={fetched.url} depth={depth} status={fetched.status_code} reason={fetched.block_reason}"
            )
        else:
            self.execution_log.append(
                f"fetch ok url={fetched.url} depth={depth} status={fetched.status_code} links={len(fetched.links)}"
            )
        return fetched

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

        tools = get_crawler_tools(self.db, self.skill_manager)
        result = await tools["save_professors"](
            org_unit_name=org_unit_name,
            org_unit_url=org_unit_url,
            source_url=source_url,
            professors=[item for item in professors if isinstance(item, dict)],
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





