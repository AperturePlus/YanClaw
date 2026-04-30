from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from agents.crawler import db as crawler_db
from agents.crawler.fetchers import FetchResult, Fetcher
from agents.crawler.models import CrawlLogStatus, CrawlStatus, OrgUnit, UniversityMeta
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
    _is_pagination_link,
    _keyword_filter,
    _looks_like_faculty_page,
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
        self.visited_urls: set[str] = set()
        self._fetch_cache: dict[str, FetchResult] = {}
        self.backtrack_count = 0
        self.execution_log: list[str] = []
        self.saved_professors = 0
        self._university_cache: UniversityMeta | None = None
        self._skip_cross_run_dedup = False
        self._blocked_hosts: set[str] = set()

    async def run(self) -> AgentResult:
        await self._ensure_university()
        await self._set_status(CrawlStatus.IN_PROGRESS)

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

                if not faculty_links:
                    faculty_links = await self._find_faculty_pages(org_units)
                if not faculty_links:
                    if self._too_many_backtracks("no faculty links found"):
                        break
                    org_units = []
                    self._skip_cross_run_dedup = True
                    continue

                break

            if not faculty_links:
                # Last resort: treat home page as faculty page
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
            if core_validated_total >= self.min_org_units:
                self.logger.info(
                    "Collected %s core org units (min=%s); stop org-unit extraction early",
                    core_validated_total,
                    self.min_org_units,
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
    async def _find_faculty_pages(self, org_units: list[OrgUnit]) -> list[_QueuedUrl]:
        self._log_state(CrawlerState.FIND_FACULTY_PAGES)
        faculty_links: list[_QueuedUrl] = []
        max_links_per_org_unit = 3
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
            search_links = [l for l in search_links if not _is_faculty_platform(l)]
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
        for item in faculty_links[:max_pages]:
            pages_to_process = [item]
            while pages_to_process:
                current = pages_to_process.pop(0)
                fetched = await self._fetch_url(current.url, current.depth)
                if fetched is None:
                    continue

                saved_before = self.saved_professors
                result = await self._ask_llm(
                    CrawlerState.EXTRACT_PROFESSORS,
                    "Extract public professor records and call save_professors when records are found. "
                    f"Use org_unit_name={current.label!r}. Set source_url to the current page URL. "
                    "If this is a paginated list, also return pagination links (next page, page 2, etc.).",
                    fetched,
                    skills,
                )

                tool_saved = False
                for record in result.tool_call_log:
                    if record.name == "save_professors":
                        saved = int(record.result.get("saved", 0)) if isinstance(record.result, dict) else 0
                        self.saved_professors += saved
                        if saved > 0:
                            tool_saved = True

                if not tool_saved:
                    await self._save_professors_from_content(result.content, current.label or "Unknown")

                saved_delta = self.saved_professors - saved_before
                if saved_delta <= 0:
                    followups = self._extract_followup_faculty_links(fetched.links, fetched.url)
                    for link in followups[:3]:
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

    async def _ask_llm(
        self,
        state: CrawlerState,
        instruction: str,
        fetched: FetchResult,
        skills_text: str,
    ) -> Any:
        prompt_max_tokens = min(int(self.model_max_tokens), 16000)
        raw_links = list(fetched.links or [])
        same_domain_links = self.fetcher.filter_same_domain(raw_links, self.start_url) if raw_links else []
        if state in {CrawlerState.DISCOVER_ORG_UNIT_PAGES, CrawlerState.EXTRACT_ORG_UNITS}:
            candidate_links = _keyword_filter(same_domain_links, ORG_UNIT_PAGE_KEYWORDS) or same_domain_links
        elif state in {CrawlerState.FIND_FACULTY_PAGES, CrawlerState.EXTRACT_PROFESSORS}:
            candidate_links = _keyword_filter(same_domain_links, FACULTY_KEYWORDS) or same_domain_links
        else:
            candidate_links = same_domain_links
        candidate_links = candidate_links[:250]

        text_limits = {
            CrawlerState.DISCOVER_ORG_UNIT_PAGES: 8000,
            CrawlerState.EXTRACT_ORG_UNITS: 20000,
            CrawlerState.FIND_FACULTY_PAGES: 12000,
            CrawlerState.EXTRACT_PROFESSORS: 20000,
        }
        page_text = _truncate_middle(fetched.text or "", text_limits.get(state, 12000))

        allowed_tools: set[str] = set()
        if state in {CrawlerState.DISCOVER_ORG_UNIT_PAGES, CrawlerState.FIND_FACULTY_PAGES}:
            allowed_tools = {"extract_links"}
        elif state is CrawlerState.EXTRACT_PROFESSORS:
            allowed_tools = {"save_professors"}

        user_content = json.dumps(
            {
                "university": self.university_name,
                "location": self.location,
                "state": state.value,
                "url": fetched.url,
                "instruction": instruction,
                "visited_urls": sorted(self.visited_urls)[-15:],
                "links": candidate_links,
                "page_text": page_text,
                "allowed_tools": sorted(allowed_tools),
                "tool_call_policy": "Only call tools listed in allowed_tools. Do not invent tool names.",
            },
            ensure_ascii=False,
        )
        tool_defs = get_crawler_tool_definitions()
        tool_defs = [tool for tool in tool_defs if tool.get("name") in allowed_tools] if allowed_tools else []

        batches = self.context_manager.build_messages(
            "You are a cautious university faculty crawler. Stay on the same university domain.",
            tool_defs,
            skills_text,
            user_content,
            prompt_max_tokens,
        )
        handlers = get_crawler_tools(self.db, self.skill_manager)
        final_result = None
        if len(candidate_links) == 0 and len(page_text) == 0:
            self.logger.debug(
                "LLM prompt has empty links/page_text state=%s url=%s; output may rely on URL heuristics",
                state.value,
                fetched.url,
            )
        self.logger.debug(
            "LLM request state=%s url=%s candidate_links=%s page_text_chars=%s tools=%s",
            state.value,
            fetched.url,
            len(candidate_links),
            len(page_text),
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
        payload = self._parse_json_from_text(content)
        if payload is None:
            return []
        if isinstance(payload, list):
            return [_sanitize_url(str(item)) for item in payload if _sanitize_url(str(item))]
        if not isinstance(payload, dict):
            return []
        for key in ("links", "org_unit_pages", "faculty_links", "urls"):
            value = payload.get(key)
            if isinstance(value, list):
                raw = [str(item.get("url") if isinstance(item, dict) else item) for item in value]
                return [_sanitize_url(u) for u in raw if _sanitize_url(u)]
        return []

    def _org_unit_followup_links_from_result(self, content: str, current_url: str) -> list[str]:
        payload = self._parse_json_from_text(content)
        links: list[str] = []
        if isinstance(payload, dict):
            for key in ("next_url", "url", "next_page", "target_url", "org_unit_page"):
                value = payload.get(key)
                if isinstance(value, str):
                    link = _sanitize_url(value)
                    if link:
                        links.append(urljoin(current_url, link))

        links.extend(self._links_from_result(content))

        deduped: list[str] = []
        seen: set[str] = set()
        for link in links:
            clean = _sanitize_url(link)
            if not clean or clean in seen:
                continue
            seen.add(clean)
            deduped.append(clean)
        return deduped

    def _links_from_tool_call_log(self, result: Any, *, tool_name: str = "extract_links") -> list[str]:
        records = getattr(result, "tool_call_log", None)
        if not isinstance(records, list):
            return []
        for record in records:
            if getattr(record, "name", "") != tool_name:
                continue
            payload = getattr(record, "result", None)
            if isinstance(payload, dict):
                links = payload.get("links")
                if isinstance(links, list):
                    return [_sanitize_url(str(link)) for link in links if _sanitize_url(str(link))]
            if isinstance(payload, list):
                return [_sanitize_url(str(link)) for link in payload if _sanitize_url(str(link))]
        return []

    def _org_units_from_result(self, content: str) -> list[dict[str, Any]]:
        payload = self._parse_json_from_text(content)
        if payload is None or not isinstance(payload, dict):
            return []
        units = payload.get("org_units")
        if isinstance(units, list):
            return [item for item in units if isinstance(item, dict)]
        return []

    def _parse_json_from_text(self, content: str) -> Any | None:
        if not content:
            return None
        text = content.strip()

        def _try_load(candidate: str) -> Any | None:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                return None

        loaded = _try_load(text)
        if loaded is not None:
            return loaded

        fence = "```"
        if fence in text:
            start = text.find(fence)
            end = text.find(fence, start + len(fence))
            if start != -1 and end != -1 and end > start:
                block = text[start + len(fence) : end]
                if "\n" in block:
                    block = block.split("\n", 1)[1]
                loaded = _try_load(block.strip())
                if loaded is not None:
                    return loaded

        for open_char, close_char in (("{", "}"), ("[", "]")):
            start = text.find(open_char)
            end = text.rfind(close_char)
            if start == -1 or end == -1 or end <= start:
                continue
            loaded = _try_load(text[start : end + 1].strip())
            if loaded is not None:
                return loaded

        return None

    def _within_depth(self, depth: int) -> bool:
        return depth <= self.max_depth

    def _too_many_backtracks(self, reason: str) -> bool:
        self.backtrack_count += 1
        self.execution_log.append(f"backtrack {self.backtrack_count}: {reason}")
        self.logger.info("Backtrack %s/%s: %s", self.backtrack_count, self.max_backtracks, reason)
        return self.backtrack_count > self.max_backtracks

    def _log_state(self, state: CrawlerState) -> None:
        self.execution_log.append(f"state={state.value}")
        self.logger.info("State %s", state.value)

    def _result(self, status: CrawlStatus, messages: list[str]) -> AgentResult:
        return AgentResult(
            university_name=self.university_name,
            status=status.value,
            visited_count=len(self.visited_urls),
            saved_professors=self.saved_professors,
            messages=messages,
        )

    def _extract_pagination_links(self, links: list[str], current_url: str) -> list[str]:
        same_domain = self.fetcher.filter_same_domain(links, self.start_url)
        pagination: list[str] = []
        for link in same_domain:
            if link == current_url or link in self.visited_urls:
                continue
            if _is_pagination_link(link):
                pagination.append(link)
        return pagination

    def _extract_followup_faculty_links(self, links: list[str], current_url: str) -> list[str]:
        same_domain = self.fetcher.filter_same_domain(links, self.start_url)
        candidates = [link for link in same_domain if link != current_url and not _is_faculty_platform(link)]
        candidates = _rank_faculty_page_candidates(candidates)
        non_showcase = [link for link in candidates if not _is_academician_showcase_page(link)]
        if non_showcase:
            candidates = non_showcase
        return candidates

    async def _search_engine_fallback(self, query_suffix: str) -> list[str]:
        """Use Bing search as fallback to find relevant pages on the university domain."""
        hostname = urlparse(self.start_url).hostname or ""
        domain = hostname.removeprefix("www.")
        suffix = (query_suffix or "").strip()
        if suffix and not _contains_cjk(suffix) and any(ord(ch) > 127 for ch in suffix):
            suffix = ""
        extra = "jgsz yxsz xysz zzjg xy yx xygk xxgk szdw jsdw faculty teacher staff people"
        query = _dedupe_query_terms(f"{suffix} {extra} site:{domain}".strip())
        search_url = f"https://www.bing.com/search?q={quote(query)}&count=20&setlang=en&cc=us"
        self.logger.info("Search engine fallback: %s", query)
        try:
            fetched = await self.fetcher.fetch(search_url)
            self.logger.info(
                "Search fallback response status=%s final_url=%s block_reason=%s",
                fetched.status_code,
                fetched.url,
                fetched.block_reason or "-",
            )
            text_urls = _extract_urls_from_text(fetched.text)
            all_urls = list(dict.fromkeys(fetched.links + text_urls))
            same_domain = self.fetcher.filter_same_domain(all_urls, self.start_url)
            same_domain = [u for u in same_domain if not _is_faculty_platform(u)]
            self.execution_log.append(f"search_fallback query={query!r} found={len(same_domain)} links")
            self.logger.info("Search fallback found %d same-domain links", len(same_domain))
            return same_domain
        except Exception as error:
            self.logger.warning("Search engine fallback failed: %s", error)
            self.execution_log.append(f"search_fallback failed: {error}")
            return []

def _dedupe_queue(items: list[_QueuedUrl]) -> list[_QueuedUrl]:
    seen: set[str] = set()
    result: list[_QueuedUrl] = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        result.append(item)
    return result

