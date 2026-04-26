from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from agents.crawler import db as crawler_db
from agents.crawler.fetcher import FetchResult, Fetcher
from agents.crawler.models import CrawlLogStatus, CrawlStatus, University
from agents.crawler.tools import get_crawler_tool_definitions, get_crawler_tools
from runtime.context import ContextManager
from runtime.database import DatabaseManager
from runtime.llm import LLMClient
from runtime.logger import get_logger
from runtime.skills import SkillManager


class CrawlerState(str, Enum):
    FIND_COLLEGES = "FIND_COLLEGES"
    FIND_FACULTY_PAGES = "FIND_FACULTY_PAGES"
    EXTRACT_PROFESSORS = "EXTRACT_PROFESSORS"
    REFLECT = "REFLECT"
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


class CrawlerAgent:
    """Single-university crawler state machine."""

    def __init__(
        self,
        *,
        university_name: str,
        start_url: str,
        db: DatabaseManager,
        llm_client: LLMClient,
        skill_manager: SkillManager,
        context_manager: ContextManager,
        fetcher: Fetcher,
        logger_name: str | None = None,
        max_depth: int = 4,
        max_backtracks: int = 3,
        model_max_tokens: int = 16000,
    ) -> None:
        self.university_name = university_name
        self.start_url = start_url
        self.db = db
        self.llm_client = llm_client
        self.skill_manager = skill_manager
        self.context_manager = context_manager
        self.fetcher = fetcher
        self.logger = get_logger(logger_name or f"crawler.{university_name}")
        self.max_depth = max_depth
        self.max_backtracks = max_backtracks
        self.model_max_tokens = model_max_tokens
        self.visited_urls: set[str] = set()
        self.backtrack_count = 0
        self.execution_log: list[str] = []
        self.saved_professors = 0
        self._university_cache: University | None = None
        self._skip_cross_run_dedup = False

    async def run(self) -> AgentResult:
        messages: list[str] = []
        university = await self._ensure_university()
        await self._set_status(CrawlStatus.IN_PROGRESS)

        try:
            self.logger.info("Starting crawl for %s", self.university_name)
            home = await self._fetch_url(self.start_url, 0, university.id)
            if home is None:
                self.logger.warning("Start URL could not be fetched: %s", self.start_url)
                await self._set_status(CrawlStatus.FAILED)
                return self._result(CrawlStatus.FAILED, ["Failed to fetch start URL"])

            # Retry loop: re-attempt the pipeline when a phase fails
            college_links: list[_QueuedUrl] = []
            faculty_links: list[_QueuedUrl] = []

            while self.backtrack_count <= self.max_backtracks:
                if not college_links:
                    college_links = await self._find_colleges(home)
                if not college_links:
                    if self._too_many_backtracks("no college links found"):
                        break
                    # Disable cross-run dedup so retried phases can explore previously-crawled URLs
                    self._skip_cross_run_dedup = True
                    continue

                if not faculty_links:
                    faculty_links = await self._find_faculty_pages(college_links)
                if not faculty_links:
                    if self._too_many_backtracks("no faculty links found"):
                        break
                    # Reset college_links to force re-discovery with different LLM response
                    college_links = []
                    self._skip_cross_run_dedup = True
                    continue

                # Found both college and faculty links, proceed
                break

            if not faculty_links:
                # Last resort: treat home page as faculty page
                faculty_links = [_QueuedUrl(home.url, 0)]

            await self._extract_professors(faculty_links)
            await self._reflect()
            professor_count = await self._professor_count(university.id)
            if professor_count <= 0:
                await self._set_status(CrawlStatus.FAILED)
                self.logger.warning(
                    "Crawler did not save any professors for %s; marking failed",
                    self.university_name,
                )
                return self._result(CrawlStatus.FAILED, ["No professors saved"])

            await self._set_status(CrawlStatus.COMPLETED)
            self.logger.info(
                "Completed crawl for %s with %s professors",
                self.university_name,
                professor_count,
            )
            return self._result(CrawlStatus.COMPLETED, messages)
        except Exception as error:
            self.logger.exception("Crawler failed for %s", self.university_name)
            await self._set_status(CrawlStatus.FAILED)
            return self._result(CrawlStatus.FAILED, [str(error)])

    async def _find_colleges(self, home: FetchResult) -> list[_QueuedUrl]:
        self._log_state(CrawlerState.FIND_COLLEGES)
        skills = await self._select_skills(CrawlerState.FIND_COLLEGES)
        result = await self._ask_llm(
            CrawlerState.FIND_COLLEGES,
            "Find links that lead to college, school, department, or academy pages."
            " If skills mention specific fallback URLs for this university, include them.",
            home,
            skills,
            include_skill_tools=False,
        )
        links = self._links_from_result(result.content)
        if not links:
            links = _keyword_filter(home.links, COLLEGE_KEYWORDS)
        # Also extract any URLs mentioned in skills as fallback
        skill_urls = _extract_urls_from_text(skills)
        skill_urls = self.fetcher.filter_same_domain(skill_urls, self.start_url)
        links = list(dict.fromkeys(links + skill_urls))  # dedupe preserving order
        links = self.fetcher.filter_same_domain(links, self.start_url)

        # Search engine fallback when no college links found
        if not links:
            self.logger.info("No college links from homepage, trying search engine fallback")
            search_links = await self._search_engine_fallback("学院 院系列表")
            links = self.fetcher.filter_same_domain(search_links, self.start_url)

        return [
            _QueuedUrl(url=link, depth=1, label="college")
            for link in links
            if self._within_depth(1)
        ]

    async def _find_faculty_pages(self, college_links: list[_QueuedUrl]) -> list[_QueuedUrl]:
        self._log_state(CrawlerState.FIND_FACULTY_PAGES)
        skills = await self._select_skills(CrawlerState.FIND_FACULTY_PAGES)
        faculty_links: list[_QueuedUrl] = []
        for item in college_links[:20]:
            fetched = await self._fetch_url(item.url, item.depth, (await self._ensure_university()).id)
            if fetched is None:
                continue
            result = await self._ask_llm(
                CrawlerState.FIND_FACULTY_PAGES,
                "Find faculty list, teacher team, tutor, staff, or people pages.",
                fetched,
                skills,
                include_skill_tools=False,
            )
            links = self._links_from_result(result.content)
            if not links:
                links = _keyword_filter(fetched.links, FACULTY_KEYWORDS)
            links = self.fetcher.filter_same_domain(links, self.start_url)
            if not links and _looks_like_faculty_page(fetched.url):
                links = [fetched.url]
            for link in links:
                depth = item.depth + (0 if link == fetched.url else 1)
                if self._within_depth(depth):
                    faculty_links.append(_QueuedUrl(url=link, depth=depth, label=item.label or "faculty"))

        # Search engine fallback when no faculty links found from any college page
        if not faculty_links:
            self.logger.info("No faculty links from college pages, trying search engine fallback")
            search_links = await self._search_engine_fallback("师资队伍 教师名录")
            for link in search_links:
                if self._within_depth(2):
                    faculty_links.append(_QueuedUrl(url=link, depth=2, label="faculty"))

        return _dedupe_queue(faculty_links)

    async def _extract_professors(self, faculty_links: list[_QueuedUrl]) -> None:
        self._log_state(CrawlerState.EXTRACT_PROFESSORS)
        skills = await self._select_skills(CrawlerState.EXTRACT_PROFESSORS)
        for item in faculty_links[:30]:
            # Process the faculty page and any pagination pages
            pages_to_process = [item]
            while pages_to_process:
                current = pages_to_process.pop(0)
                fetched = await self._fetch_url(current.url, current.depth, (await self._ensure_university()).id)
                if fetched is None:
                    continue
                result = await self._ask_llm(
                    CrawlerState.EXTRACT_PROFESSORS,
                    "Extract public professor records and call save_professors when records are found."
                    " If this is a paginated list, also return pagination links (next page, page 2, etc.).",
                    fetched,
                    skills,
                    include_skill_tools=False,
                )
                tool_saved = False
                for record in result.tool_call_log:
                    if record.name == "save_professors":
                        saved = int(record.result.get("saved", 0)) if isinstance(record.result, dict) else 0
                        self.saved_professors += saved
                        tool_saved = True
                # Fallback: parse content only when LLM did not use the tool
                if not tool_saved:
                    await self._save_professors_from_content(result.content, current.label or "Unknown College")
                # Detect pagination links
                pagination_links = self._extract_pagination_links(fetched.links, fetched.url)
                for plink in pagination_links:
                    if plink not in self.visited_urls and self._within_depth(current.depth):
                        pages_to_process.append(_QueuedUrl(url=plink, depth=current.depth, label=current.label))

    async def _reflect(self) -> None:
        self._log_state(CrawlerState.REFLECT)
        skills = await self._select_skills(CrawlerState.REFLECT)
        user_content = "\n".join(self.execution_log[-100:])
        messages = self.context_manager.build_messages(
            "Reflect on the crawl. Update or create skills only when durable improvements are clear.",
            get_crawler_tool_definitions(include_skill_tools=True),
            skills,
            user_content,
            self.model_max_tokens,
        )
        handlers = get_crawler_tools(self.db, self.skill_manager)
        for batch in messages:
            await self.llm_client.chat(
                batch,
                tools=get_crawler_tool_definitions(include_skill_tools=True),
                tool_handlers=handlers,
            )

    async def _ask_llm(
        self,
        state: CrawlerState,
        instruction: str,
        fetched: FetchResult,
        skills_text: str,
        *,
        include_skill_tools: bool,
    ) -> Any:
        user_content = json.dumps(
            {
                "university": self.university_name,
                "state": state.value,
                "url": fetched.url,
                "instruction": instruction,
                "visited_urls": sorted(self.visited_urls)[-30:],
                "links": fetched.links,
                "page_text": fetched.text,
            },
            ensure_ascii=False,
        )
        tool_defs = get_crawler_tool_definitions(include_skill_tools=include_skill_tools)
        batches = self.context_manager.build_messages(
            "You are a cautious university faculty crawler. Stay on the same university domain.",
            tool_defs,
            skills_text,
            user_content,
            self.model_max_tokens,
        )
        handlers = get_crawler_tools(self.db, self.skill_manager)
        final_result = None
        for batch in batches:
            final_result = await self.llm_client.chat(batch, tools=tool_defs, tool_handlers=handlers)
        assert final_result is not None
        return final_result

    async def _select_skills(self, state: CrawlerState) -> str:
        metas = self.skill_manager.list_skills()
        if not metas:
            return ""
        meta_text = "\n".join(f"- {meta.name}: {meta.description}" for meta in metas)
        messages = [
            {
                "role": "system",
                "content": "Select relevant skill names as a JSON array. Return only JSON.",
            },
            {"role": "user", "content": f"State: {state.value}\nSkills:\n{meta_text}"},
        ]
        try:
            result = await self.llm_client.chat(messages, tools=None, tool_handlers={})
            selected = json.loads(result.content)
            names = [name for name in selected if any(meta.name == name for meta in metas)]
        except Exception:
            names = [meta.name for meta in metas]
        if not names:
            names = [meta.name for meta in metas]
        return "\n\n".join(self.skill_manager.load_skills(names).values())

    async def _fetch_url(self, url: str, depth: int, university_id: int) -> FetchResult | None:
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
        if url in self.visited_urls:
            self.execution_log.append(f"skip visited url={url}")
            self.logger.info("Skipping already visited URL: %s", url)
            return None

        # Cross-run dedup: skip URLs already successfully crawled in previous runs
        # Exception: always re-fetch the start URL to allow re-crawling incomplete universities
        # Exception: disabled during backtrack retries to allow exploring new paths
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
                    university_id,
                    url,
                    CrawlLogStatus.FAILED,
                    str(error),
                )
            self.execution_log.append(f"fetch failed url={url} error={error}")
            self.logger.warning("Fetch failed for %s: %s", url, error)
            return None

        async with self.db.session() as session:
            await crawler_db.log_crawl(
                session,
                university_id,
                fetched.url,
                CrawlLogStatus.SUCCESS,
                f"status={fetched.status_code}",
            )
        self.execution_log.append(f"fetch success url={fetched.url} status={fetched.status_code}")
        self.logger.info("Fetched %s status=%s", fetched.url, fetched.status_code)
        return fetched

    async def _save_professors_from_content(self, content: str, fallback_college: str) -> None:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return
        professors = payload.get("professors") if isinstance(payload, dict) else None
        if not professors:
            return
        college_name = str(payload.get("college_name") or fallback_college or "Unknown College")
        async with self.db.session() as session:
            for professor in professors:
                await crawler_db.upsert_professor(
                    session,
                    {
                        **professor,
                        "university_name": self.university_name,
                        "college_name": college_name,
                    },
                )
                self.saved_professors += 1

    async def _ensure_university(self) -> University:
        if self._university_cache is not None:
            return self._university_cache
        async with self.db.session() as session:
            self._university_cache = await crawler_db.get_or_create_university(session, self.university_name, self.start_url)
            return self._university_cache

    async def _set_status(self, status: CrawlStatus) -> None:
        async with self.db.session() as session:
            await crawler_db.set_university_status(session, self.university_name, status)

    async def _professor_count(self, university_id: int) -> int:
        async with self.db.session() as session:
            return await crawler_db.count_professors_for_university(session, university_id)

    def _links_from_result(self, content: str) -> list[str]:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return []
        if isinstance(payload, list):
            return [_sanitize_url(str(item)) for item in payload if _sanitize_url(str(item))]
        if not isinstance(payload, dict):
            return []
        for key in ("links", "college_links", "faculty_links", "urls"):
            value = payload.get(key)
            if isinstance(value, list):
                raw = [str(item.get("url") if isinstance(item, dict) else item) for item in value]
                return [_sanitize_url(u) for u in raw if _sanitize_url(u)]
        return []

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
        """Detect pagination links (e.g. ?page=2, ?p=3, /list_2.htm)."""
        same_domain = self.fetcher.filter_same_domain(links, self.start_url)
        pagination: list[str] = []
        for link in same_domain:
            if link == current_url or link in self.visited_urls:
                continue
            if _is_pagination_link(link):
                pagination.append(link)
        return pagination

    async def _search_engine_fallback(self, query_suffix: str) -> list[str]:
        """Use Bing search as fallback to find relevant pages on the university domain."""
        domain = urlparse(self.start_url).hostname or ""
        query = f"{self.university_name} {query_suffix} site:{domain}"
        search_url = f"https://www.bing.com/search?q={query}&count=20"
        self.logger.info("Search engine fallback: %s", query)
        try:
            fetched = await self.fetcher.fetch(search_url)
            # Bing wraps links in redirects; extract university URLs from page text instead
            text_urls = _extract_urls_from_text(fetched.text)
            same_domain = self.fetcher.filter_same_domain(text_urls, self.start_url)
            # Also check raw links in case some are direct
            link_urls = self.fetcher.filter_same_domain(fetched.links, self.start_url)
            combined = list(dict.fromkeys(same_domain + link_urls))
            self.execution_log.append(f"search_fallback query={query!r} found={len(combined)} links")
            self.logger.info("Search fallback found %d same-domain links", len(combined))
            return combined
        except Exception as error:
            self.logger.warning("Search engine fallback failed: %s", error)
            self.execution_log.append(f"search_fallback failed: {error}")
            return []


COLLEGE_KEYWORDS = (
    "college",
    "school",
    "department",
    "academy",
    "faculty",
    "yuan",
    "xueyuan",
    "院",
    "系",
)

FACULTY_KEYWORDS = (
    "teacher",
    "faculty",
    "staff",
    "people",
    "team",
    "tutor",
    "supervisor",
    "professor",
    "师资",
    "教师",
    "导师",
)


def _keyword_filter(links: list[str], keywords: tuple[str, ...]) -> list[str]:
    lowered = [(keyword, keyword.lower()) for keyword in keywords]
    return [
        link
        for link in links
        if any(keyword in link or ascii_keyword in link.lower() for keyword, ascii_keyword in lowered)
    ]


def _looks_like_faculty_page(url: str) -> bool:
    return bool(_keyword_filter([url], FACULTY_KEYWORDS))


def _dedupe_queue(items: list[_QueuedUrl]) -> list[_QueuedUrl]:
    seen: set[str] = set()
    result: list[_QueuedUrl] = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        result.append(item)
    return result


def _same_site(url: str, base_url: str) -> bool:
    return bool(Fetcher.filter_same_domain([url], base_url))


def _sanitize_url(url: str) -> str:
    """Remove markdown formatting artifacts from LLM-returned URLs."""
    url = url.strip().strip("`").strip("*").strip("_").strip("<").strip(">").strip('"').strip("'")
    # Remove trailing markdown punctuation
    while url and url[-1] in ("`", "*", "_", ")", "]", ">", "'", '"'):
        url = url[:-1]
    return url


_PAGINATION_RE = re.compile(
    r"[?&](page|p|pagenum|pn|start|offset)=\d+"
    r"|/list_\d+\.htm"
    r"|/index_\d+\.htm"
    r"|/page/\d+"
    r"|-\d+\.htm$",
    re.IGNORECASE,
)


def _is_pagination_link(url: str) -> bool:
    return bool(_PAGINATION_RE.search(url))


_URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+")


def _extract_urls_from_text(text: str) -> list[str]:
    """Extract HTTP(S) URLs from free-form text (e.g. skill content)."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.findall(text):
        url = _sanitize_url(match.rstrip(".,;:)"))
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls
