from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from agents.crawler import agent_detail, form_pagination
from agents.crawler import db as crawler_db
from agents.crawler.agent_state import CrawlerState
from agents.crawler.db.professors import normalize_professor_homepage
from agents.crawler.extraction_models import (
    ExtractionOutcome as _ExtractionOutcome,
    ExtractionTaskItem as _ExtractionTaskItem,
    QueuedUrl as _QueuedUrl,
    SaveEvent as _SaveEvent,
)
from agents.crawler.extraction_payloads import ExtractionPayloadService
from agents.crawler.fetch_failures import is_retryable_fetch_failure
from agents.crawler.fetchers import FetchResult
from agents.crawler.graph_frontier import GraphFetchCandidate
from agents.crawler.models import (
    CrawlGraphEdgeType,
    CrawlGraphNodeStatus,
    CrawlGraphNodeType,
    CrawlTaskKind,
    CrawlTaskStatus,
)
from agents.crawler.prompt_builder import CRAWLER_SYSTEM_PROMPT, CrawlerPromptBuilder
from agents.crawler.sanitizer import normalize_name_key
from agents.crawler.tools import get_crawler_tool_definitions, get_crawler_tools
from agents.crawler.url_heuristics import (
    FACULTY_PAGE_TYPE_NOISE,
    _assess_faculty_candidate,
    _is_faculty_platform,
    _is_non_faculty_noise_url,
    _looks_like_faculty_page,
    _same_site,
    _sanitize_url,
)


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
_TERMINAL_REDIRECT_HOSTS = (
    "mp.weixin.qq.com",
    "weixin.qq.com",
    "jaccount.sjtu.edu.cn",
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
# Node types the faculty-onward claim-driver processes (Phase 1b). Org-listing /
# org-unit discovery is still done outside the driver until Phase 2.
_DRIVER_NODE_TYPES = (
    CrawlGraphNodeType.FACULTY_LIST_URL,
    CrawlGraphNodeType.PAGINATION_URL,
    CrawlGraphNodeType.FACULTY_FOLLOWUP_URL,
    CrawlGraphNodeType.DETAIL_URL,
)


class ExtractionPipeline:
    """Shared extraction-pipeline accounting helpers."""

    def __init__(self, stats: dict[str, Any]) -> None:
        self.stats = stats

    @staticmethod
    def task_kind_prefix(task: Any) -> str:
        if getattr(task, "task_kind", "") == CrawlTaskKind.DETAIL_PAGE.value or getattr(task, "detail_mode", False):
            return "detail"
        return "list"

    def increment_task_kind_stat(self, task: Any, suffix: str, amount: int = 1) -> None:
        key = f"{self.task_kind_prefix(task)}_{suffix}"
        self.stats[key] = int(self.stats.get(key, 0)) + int(amount)

    def update_timing(self, elapsed_ms: float) -> None:
        total = float(self.stats.get("average_task_ms", 0.0))
        timed = int(self.stats.get("timed_tasks", 0))
        current = timed + 1
        self.stats["average_task_ms"] = ((total * timed) + elapsed_ms) / current
        self.stats["timed_tasks"] = current

    def record_llm_payload(self, payload_bytes: int) -> None:
        calls = int(self.stats.get("llm_calls_total", 0)) + 1
        total = int(self.stats.get("llm_payload_bytes_total", 0)) + max(0, int(payload_bytes))
        self.stats["llm_calls_total"] = calls
        self.stats["llm_payload_bytes_total"] = total
        self.stats["avg_payload_bytes"] = float(total) / float(calls)


class ExtractionPipelineService(ExtractionPayloadService):
    """Extraction task queue, LLM workers, recovery, and DB persistence."""

    async def _extract_professors(
        self,
        faculty_links: list[_QueuedUrl],
        *,
        recovery_limit: int | None = None,
    ) -> None:
        self._log_state(CrawlerState.EXTRACT_PROFESSORS)
        skills = await self._select_skills(CrawlerState.EXTRACT_PROFESSORS)
        self.logger.info(
            "Claim-driver pipeline enabled=%s llm_workers=%s db_workers=%s queue_cap=%s",
            self.pipeline_enabled,
            self.pipeline_llm_workers,
            self.pipeline_db_workers,
            self.pipeline_queue_cap,
        )

        # B1: orphaned IN_PROGRESS nodes from a crashed run become claimable again.
        stale = await self.graph_frontier.recover_stale_in_progress()
        if stale:
            self._pipeline_stats["stale_in_progress_recovered"] = int(
                self._pipeline_stats.get("stale_in_progress_recovered", 0)
            ) + int(stale)
            self.logger.info("Recovered %s stale in_progress graph nodes", stale)

        await self._seed_faculty_link_nodes(faculty_links)

        # Org scope keeps streaming mode per-college; empty (resume) -> claim globally.
        org_names = [item.label for item in faculty_links if item.label] or None
        org_ids = [item.org_unit_id for item in faculty_links if item.org_unit_id is not None] or None

        if not self.pipeline_enabled:
            await self._run_claim_driver(
                skills, llm_queue=None, db_queue=None, org_names=org_names, org_ids=org_ids
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
            await self._run_claim_driver(
                skills, llm_queue=llm_queue, db_queue=db_queue, org_names=org_names, org_ids=org_ids
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
            self._log_pipeline_stats()

    async def _seed_faculty_link_nodes(self, faculty_links: list[_QueuedUrl]) -> None:
        for item in faculty_links:
            node_type = item.graph_node_type or CrawlGraphNodeType.FACULTY_LIST_URL.value
            await self.graph_frontier.ensure_url_node(
                url=item.identity_url or item.url,
                node_type=node_type,
                org_unit_name=item.label,
                org_unit_id=item.org_unit_id,
                depth=item.depth,
                metadata={
                    "fetch_url": item.url,
                    "identity_url": item.identity_url,
                    "fetch_action": item.fetch_action,
                    "source": "extraction_seed",
                },
            )

    async def _run_claim_driver(
        self,
        skills: str,
        *,
        llm_queue: "asyncio.Queue[_ExtractionTaskItem | None] | None",
        db_queue: "asyncio.Queue[_SaveEvent | None] | None",
        org_names: list[str] | None,
        org_ids: list[int] | None,
    ) -> None:
        # Once-per-run invariant: a node fetched this run is excluded from further
        # claims, so a transient fetch failure (-> RETRY) is retried on the *next*
        # run, never re-fetched in a hot loop. Across runs, _MAX_NODE_ATTEMPTS caps
        # total attempts. Without this guard the driver would spin forever on a
        # permanently-failing fetch (it re-claims RETRY immediately).
        attempted: set[int] = set()

        def _remember(candidate: GraphFetchCandidate) -> None:
            if candidate.node_id is not None:
                attempted.add(int(candidate.node_id))

        while True:
            candidate = await self.graph_frontier.claim_next(
                node_types=_DRIVER_NODE_TYPES,
                org_unit_names=org_names,
                org_unit_ids=org_ids,
                exclude_node_ids=attempted,
            )
            if candidate is None:
                if llm_queue is None:
                    break
                # Let in-flight detail jobs settle, then one final re-check. Workers
                # create no navigation nodes, so this only picks up a node never
                # fetched this run (already-fetched RETRY nodes stay excluded).
                await llm_queue.join()
                await db_queue.join()
                candidate = await self.graph_frontier.claim_next(
                    node_types=_DRIVER_NODE_TYPES,
                    org_unit_names=org_names,
                    org_unit_ids=org_ids,
                    exclude_node_ids=attempted,
                )
                if candidate is None:
                    break
            _remember(candidate)
            await self._drive_graph_node(candidate, skills, llm_queue=llm_queue, db_queue=db_queue)

    async def _drive_graph_node(
        self,
        candidate: GraphFetchCandidate,
        skills: str,
        *,
        llm_queue: "asyncio.Queue[_ExtractionTaskItem | None] | None",
        db_queue: "asyncio.Queue[_SaveEvent | None] | None",
    ) -> None:
        current = self.graph_frontier.to_queued_url(candidate, _QueuedUrl)
        is_detail = candidate.node_type == CrawlGraphNodeType.DETAIL_URL.value

        if self._is_noise_or_login_candidate(current.url):
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error="noise_or_login_candidate",
            )
            return
        # Detail/profile pages are the terminal goal, not navigation: a profile
        # discovered from a list page at the depth boundary sits one level beyond
        # the budget but must still be fetched (matches the pre-driver inline
        # behavior). Anything deeper, or an off-site/non-primary profile beyond the
        # budget, is skipped with a reason. List/pagination/followup fetches stay
        # depth-bounded.
        allow_depth_excess = False
        if is_detail and not self._within_depth(current.depth):
            if current.depth > self.max_depth + 1:
                self._pipeline_stats["detail_depth_excess_skipped"] = int(
                    self._pipeline_stats.get("detail_depth_excess_skipped", 0)
                ) + 1
                await self.graph_frontier.mark_node_status(
                    current.graph_node_id,
                    status=CrawlGraphNodeStatus.SKIPPED,
                    last_error="detail_depth_excess",
                )
                return
            if not agent_detail._is_same_site_primary_profile_url(
                current.url, start_url=getattr(self, "start_url", "")
            ):
                self._pipeline_stats["detail_profile_depth_gate_skipped"] = int(
                    self._pipeline_stats.get("detail_profile_depth_gate_skipped", 0)
                ) + 1
                await self.graph_frontier.mark_node_status(
                    current.graph_node_id,
                    status=CrawlGraphNodeStatus.SKIPPED,
                    last_error="profile_depth_gate",
                )
                return
            allow_depth_excess = True
        if is_detail:
            # B4 claim-dedup: the same profile URL discovered under two org units
            # yields two detail nodes; once a peer node for this URL is in-flight or
            # DONE, skip the duplicate before fetching (single fetch + single LLM
            # extraction). save_professors dedups by name_key/homepage anyway.
            dedup_url = current.queue_url or current.url
            async with self.db.session() as session:
                duplicate = await crawler_db.find_inflight_or_done_graph_node_by_url(
                    session, dedup_url, exclude_node_id=current.graph_node_id
                )
            if duplicate is not None:
                self._pipeline_stats["detail_duplicate_url_skipped"] = int(
                    self._pipeline_stats.get("detail_duplicate_url_skipped", 0)
                ) + 1
                await self.graph_frontier.mark_node_status(
                    current.graph_node_id,
                    status=CrawlGraphNodeStatus.SKIPPED,
                    last_error=f"duplicate_url:peer_node:{int(duplicate.id)}",
                )
                self.logger.debug(
                    "Skip duplicate detail URL handled by peer node url=%s peer_node=%s peer_status=%s",
                    dedup_url,
                    duplicate.id,
                    duplicate.status,
                )
                return
        fetched = await self._fetch_url(
            current.url,
            current.depth,
            action=current.fetch_action,
            identity_url=current.identity_url,
            allow_depth_excess=allow_depth_excess,
        )
        if fetched is None:
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.RETRY,
                last_error="fetch_failed",
                increment_attempt=True,
            )
            return
        if is_retryable_fetch_failure(fetched.block_reason):
            await self._mark_retryable_fetch_failure(current, fetched, detail_mode=is_detail)
            return
        if self._is_noise_or_login_candidate(fetched.url):
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error="noise_or_login_page",
            )
            return
        if self._is_retired_page(fetched):
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error="retired_page",
            )
            return

        if is_detail:
            await self._drive_detail_node(current, fetched, skills, llm_queue=llm_queue)
        else:
            await self._drive_list_node(current, fetched, skills, llm_queue=llm_queue)

    async def _drive_list_node(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        skills: str,
        *,
        llm_queue: "asyncio.Queue[_ExtractionTaskItem | None] | None",
    ) -> None:
        # Traversal-only: record the list page DONE (no save) and discover children
        # as PENDING nodes the driver will claim later.
        if await self._record_list_page_traversal_task(current, fetched) == "skipped":
            return
        reserved_urls = await self._discover_related_page_nodes(current, fetched)
        await self._enrich_profiles_with_detail_backend(
            current, fetched, skills, reserved_urls=reserved_urls
        )

    async def _drive_detail_node(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        skills: str,
        *,
        llm_queue: "asyncio.Queue[_ExtractionTaskItem | None] | None",
    ) -> None:
        if llm_queue is None:
            # Synchronous (pipeline-disabled) path: extract inline and mark terminal.
            saved = await self._extract_professors_from_page(
                current, fetched, skills, detail_mode=True, requested_url=current.queue_url
            )
            if saved >= 0:
                await self.graph_frontier.mark_node_status(
                    current.graph_node_id, status=CrawlGraphNodeStatus.DONE
                )
            return
        # Pipeline path: enqueue the LLM job; the worker pool owns terminal status.
        await self._enqueue_extraction_task(
            current,
            fetched,
            llm_queue=llm_queue,
            detail_mode=True,
            priority=1,
            requested_url=current.queue_url,
        )

    async def _discover_related_page_nodes(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
    ) -> set[str]:
        def _key(url: str) -> str:
            return _sanitize_url(url) or (url or "").strip()

        followup_items: list[_QueuedUrl] = []
        for link in self._extract_followup_faculty_links(fetched.links, fetched.url)[:_FOLLOWUP_PAGE_LIMIT]:
            next_depth = current.depth + 1
            if not self._within_depth(next_depth):
                continue
            followup_items.append(
                _QueuedUrl(url=link, depth=next_depth, label=current.label, org_unit_id=current.org_unit_id)
            )

        pagination_items: list[_QueuedUrl] = []
        for plink in self._extract_pagination_links(fetched.links, fetched.url):
            if not self._within_depth(current.depth):
                continue
            pagination_items.append(
                _QueuedUrl(url=plink, depth=current.depth, label=current.label, org_unit_id=current.org_unit_id)
            )
        for state in getattr(fetched, "pagination_states", ()) or ():
            action = form_pagination.pagination_state_to_fetch_action(state)
            if not action:
                continue
            identity_url = str(action.get("synthetic_url") or "").strip()
            if not identity_url:
                continue
            pagination_items.append(
                _QueuedUrl(
                    url=str(action.get("url") or fetched.url),
                    depth=current.depth,
                    label=current.label,
                    org_unit_id=current.org_unit_id,
                    fetch_action=action,
                    identity_url=identity_url,
                )
            )

        if followup_items:
            await self.graph_frontier.record_discovered_links(
                source_url=fetched.url,
                links=followup_items,
                node_type=CrawlGraphNodeType.FACULTY_FOLLOWUP_URL,
                edge_type=CrawlGraphEdgeType.DISCOVERED_ON_PAGE,
                source_node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
                org_unit_name=current.label,
                org_unit_id=current.org_unit_id,
                depth=current.depth + 1,
                confidence=0.8,
                metadata={"source": "faculty_followup"},
            )
            self._pipeline_stats["followups_scheduled"] = int(
                self._pipeline_stats.get("followups_scheduled", 0)
            ) + len(followup_items)
        if pagination_items:
            await self.graph_frontier.record_discovered_links(
                source_url=fetched.url,
                links=pagination_items,
                node_type=CrawlGraphNodeType.PAGINATION_URL,
                edge_type=CrawlGraphEdgeType.PAGINATION_OF,
                source_node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
                org_unit_name=current.label,
                org_unit_id=current.org_unit_id,
                depth=current.depth,
                confidence=0.9,
                metadata={"source": "pagination"},
            )
            self._pipeline_stats["pagination_scheduled"] = int(
                self._pipeline_stats.get("pagination_scheduled", 0)
            ) + len(pagination_items)

        return {
            _key(item.queue_url)
            for item in (followup_items + pagination_items)
            if _key(item.queue_url)
        }

    def _log_pipeline_stats(self) -> None:
        self.logger.info(
            "Claim-driver stats processed=%s list_processed=%s list_save_suppressed=%s "
            "detail_enqueued=%s detail_processed=%s detail_failed=%s detail_skipped=%s "
            "records_created=%s records_updated=%s deduped_by_name_key=%s deduped_by_homepage=%s "
            "stale_in_progress_recovered=%s detail_duplicate_url_skipped=%s followups=%s pagination=%s "
            "avg_task_ms=%.1f llm_calls=%s",
            self._pipeline_stats.get("processed_tasks", 0),
            self._pipeline_stats.get("list_processed", 0),
            self._pipeline_stats.get("list_save_suppressed", 0),
            self._pipeline_stats.get("detail_enqueued", 0),
            self._pipeline_stats.get("detail_processed", 0),
            self._pipeline_stats.get("detail_failed", 0),
            self._pipeline_stats.get("detail_skipped", 0),
            self._pipeline_stats.get("records_created", 0),
            self._pipeline_stats.get("records_updated", 0),
            self._pipeline_stats.get("deduped_by_name_key", 0),
            self._pipeline_stats.get("deduped_by_homepage", 0),
            self._pipeline_stats.get("stale_in_progress_recovered", 0),
            self._pipeline_stats.get("detail_duplicate_url_skipped", 0),
            self._pipeline_stats.get("followups_scheduled", 0),
            self._pipeline_stats.get("pagination_scheduled", 0),
            float(self._pipeline_stats.get("average_task_ms", 0.0)),
            self._pipeline_stats.get("llm_calls_total", 0),
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

    @staticmethod
    def _is_terminal_redirect_url(url: str) -> tuple[bool, str]:
        clean = _sanitize_url(url) or url or ""
        lowered = clean.lower()
        parsed = urlparse(clean)
        scheme = (parsed.scheme or "").lower()
        if scheme and scheme not in {"http", "https"}:
            return True, f"redirect_to_{scheme}_url"
        if lowered.startswith(("javascript:", "jaccount:")):
            return True, "redirect_to_script_or_app_url"
        host = (parsed.hostname or "").lower()
        if any(host == item or host.endswith("." + item) for item in _TERMINAL_REDIRECT_HOSTS):
            if "weixin.qq.com" in host:
                return True, "redirect_to_wechat"
            return True, "redirect_to_jaccount"
        if "/jaccount" in lowered or "/jaccount_login" in lowered:
            return True, "redirect_to_jaccount"
        return False, ""

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
        terminal_redirect, terminal_reason = self._is_terminal_redirect_url(final)
        if terminal_redirect:
            return True, terminal_reason
        if self._redirect_identity_key(requested) == self._redirect_identity_key(final):
            return False, ""

        requested_path = urlparse(requested).path or "/"
        final_path = urlparse(final).path or "/"
        if self._is_home_path(final_path) and not self._is_home_path(requested_path):
            return True, "redirect_to_home"

        if not detail_mode:
            final_assessment = _assess_faculty_candidate(final)
            if (
                final_assessment.hard_reject
                or final_assessment.page_type == FACULTY_PAGE_TYPE_NOISE
                or _is_non_faculty_noise_url(final)
            ):
                return True, "redirect_to_noise"

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

    async def _mark_retryable_fetch_failure(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        *,
        detail_mode: bool,
    ) -> str:
        reason = (fetched.block_reason or "fetch_failed").strip() or "fetch_failed"
        terminal_noise = self._is_noise_or_login_candidate(current.queue_url) or _is_non_faculty_noise_url(
            current.queue_url
        )
        if detail_mode:
            self._pipeline_stats["detail_skipped"] = int(self._pipeline_stats.get("detail_skipped", 0)) + 1
        else:
            self._pipeline_stats["list_skipped"] = int(self._pipeline_stats.get("list_skipped", 0)) + 1
        self.logger.warning(
            "Skip professor extraction after retryable fetch failure url=%s reason=%s detail_mode=%s text_len=%s links=%s",
            fetched.url,
            reason,
            detail_mode,
            len((fetched.text or "").strip()),
            len(fetched.links or []),
        )
        await self.graph_frontier.mark_node_status(
            current.graph_node_id,
            status=CrawlGraphNodeStatus.SKIPPED if terminal_noise else CrawlGraphNodeStatus.RETRY,
            last_error=f"fetch_failure_terminal_noise:{reason}" if terminal_noise else f"fetch_failure:{reason}",
            increment_attempt=not terminal_noise,
        )
        if terminal_noise:
            self._pipeline_stats["terminal_noise_fetch_failures_skipped"] = int(
                self._pipeline_stats.get("terminal_noise_fetch_failures_skipped", 0)
            ) + 1
            return "skipped"
        return "retry"

    async def _record_list_page_traversal_task(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        *,
        requested_url: str | None = None,
    ) -> str:
        source_url = _sanitize_url(requested_url or current.identity_url or current.url) or _sanitize_url(fetched.url) or ""
        if not source_url:
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error="missing_source_url",
            )
            self._pipeline_stats["list_skipped"] = int(self._pipeline_stats.get("list_skipped", 0)) + 1
            return "skipped"

        final_url = _sanitize_url(fetched.url) or source_url
        skip_redirect, redirect_reason = self._should_skip_redirected_extraction(
            source_url,
            final_url,
            detail_mode=False,
        )
        if skip_redirect:
            self._record_redirected_extraction_skip(detail_mode=False)
            self.logger.warning(
                "Skip list traversal after redirect source=%s final=%s reason=%s",
                source_url,
                final_url,
                redirect_reason,
            )
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error=f"redirect:{redirect_reason}",
            )
            return "skipped"

        snapshot = self._compact_page_text(
            fetched.text or "",
            self._state_text_limit(CrawlerState.EXTRACT_PROFESSORS, detail_mode=False),
        )
        page_hash = hashlib.sha1(f"{source_url}|{snapshot}".encode("utf-8", errors="ignore")).hexdigest()
        task_priority = -int(current.graph_priority_score) if float(current.graph_priority_score or 0.0) > 0.0 else 0
        org_unit_name = current.label or "Unknown"
        async with self.db.session() as session:
            row = await crawler_db.upsert_crawl_task(
                session,
                university=self.university_name,
                org_unit_name=org_unit_name,
                org_unit_url=current.url,
                source_url=source_url,
                page_url=source_url,
                page_hash=page_hash,
                task_kind=CrawlTaskKind.LIST_PAGE,
                page_text_snapshot=snapshot,
                allowed_tools="[]",
                attempt=0,
                priority=task_priority,
                status=CrawlTaskStatus.DONE,
                last_error="list_page_traversal_only",
            )

        if row is None:
            self._pipeline_stats["edu_cn_task_url_rejected"] = int(
                self._pipeline_stats.get("edu_cn_task_url_rejected", 0)
            ) + 1
            self._pipeline_stats["list_skipped"] = int(self._pipeline_stats.get("list_skipped", 0)) + 1
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error="non_edu_cn_task_url",
            )
            return "skipped"

        self._pipeline_stats["list_save_suppressed"] = int(
            self._pipeline_stats.get("list_save_suppressed", 0)
        ) + 1
        self._pipeline_stats["done"] = int(self._pipeline_stats.get("done", 0)) + 1
        self._pipeline_stats["processed_tasks"] = int(self._pipeline_stats.get("processed_tasks", 0)) + 1
        self._pipeline_stats["list_processed"] = int(self._pipeline_stats.get("list_processed", 0)) + 1
        if org_unit_name.strip() and org_unit_name.strip().lower() != "unknown":
            self._pipeline_stats["list_known_org_unit_processed"] = int(
                self._pipeline_stats.get("list_known_org_unit_processed", 0)
            ) + 1
        await self.graph_frontier.mark_node_status(
            current.graph_node_id,
            status=CrawlGraphNodeStatus.DONE,
            metadata={"crawl_task_id": int(row.id), "task_kind": CrawlTaskKind.LIST_PAGE.value},
        )
        self.logger.debug(
            "Suppress list-page professor save and keep traversal task_id=%s org_unit=%s url=%s",
            row.id,
            org_unit_name,
            source_url,
        )
        return "done"

    async def _enqueue_extraction_task(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        *,
        llm_queue: asyncio.Queue[_ExtractionTaskItem | None],
        detail_mode: bool,
        priority: int,
        requested_url: str | None = None,
    ) -> str:
        if is_retryable_fetch_failure(fetched.block_reason):
            return await self._mark_retryable_fetch_failure(
                current,
                fetched,
                detail_mode=detail_mode,
            )
        if not detail_mode:
            return await self._record_list_page_traversal_task(
                current,
                fetched,
                requested_url=requested_url,
            )
        source_url = _sanitize_url(requested_url or current.identity_url or current.url) or _sanitize_url(fetched.url) or ""
        if not source_url:
            return "skipped"
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
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error=f"redirect:{redirect_reason}",
            )
            return "skipped"
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
        task_priority = (
            -int(current.graph_priority_score)
            if float(current.graph_priority_score or 0.0) > 0.0
            else int(priority or 0)
        )
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
                priority=task_priority,
                status=CrawlTaskStatus.PENDING,
            )
            if row is None:
                self._pipeline_stats["edu_cn_task_url_rejected"] = int(
                    self._pipeline_stats.get("edu_cn_task_url_rejected", 0)
                ) + 1
                if detail_mode:
                    self._pipeline_stats["detail_skipped"] = int(self._pipeline_stats.get("detail_skipped", 0)) + 1
                else:
                    self._pipeline_stats["list_skipped"] = int(self._pipeline_stats.get("list_skipped", 0)) + 1
                self.logger.warning(
                    "Skip extraction task with non-edu-cn URL source=%s page=%s org_unit_url=%s detail_mode=%s",
                    source_url,
                    source_url,
                    current.url,
                    detail_mode,
                )
                await self.graph_frontier.mark_node_status(
                    current.graph_node_id,
                    status=CrawlGraphNodeStatus.SKIPPED,
                    last_error="non_edu_cn_task_url",
                )
                return "skipped"
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
                await self.graph_frontier.mark_node_status(
                    current.graph_node_id,
                    status=CrawlGraphNodeStatus.DONE,
                    last_error=f"existing_crawl_task:{row.status}",
                    metadata={"crawl_task_id": int(row.id)},
                )
                return "existing"
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
                graph_node_id=current.graph_node_id,
            )
        await self.graph_frontier.mark_node_status(
            current.graph_node_id,
            status=CrawlGraphNodeStatus.IN_PROGRESS,
            metadata={"crawl_task_id": int(task.task_id), "task_kind": task.task_kind},
        )
        await llm_queue.put(task)
        self._pipeline_stats["pending"] = int(self._pipeline_stats.get("pending", 0)) + 1
        if detail_mode:
            self._pipeline_stats["detail_enqueued"] = int(self._pipeline_stats.get("detail_enqueued", 0)) + 1
        else:
            self._pipeline_stats["list_enqueued"] = int(self._pipeline_stats.get("list_enqueued", 0)) + 1
        self._pipeline_stats["queue_depth"] = llm_queue.qsize()
        return "enqueued"

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

    async def _mark_extraction_task_skipped_by_gate(self, task: _ExtractionTaskItem, reason: str) -> None:
        async with self.db.session() as session:
            await crawler_db.set_crawl_task_status(
                session,
                task.task_id,
                status=CrawlTaskStatus.DONE,
                attempt=task.attempt,
                last_error=f"skipped_by_gate:{reason or 'unknown'}",
            )
        await self.graph_frontier.mark_node_status(
            task.graph_node_id,
            status=CrawlGraphNodeStatus.SKIPPED,
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
                await self.graph_frontier.mark_node_status(
                    current_task.graph_node_id,
                    status=CrawlGraphNodeStatus.IN_PROGRESS,
                    metadata={"crawl_task_id": current_task.task_id},
                )
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
                await self.graph_frontier.mark_node_status(
                    task.graph_node_id,
                    status=CrawlGraphNodeStatus.FAILED,
                    last_error=f"save_error:{error}",
                    metadata={"crawl_task_id": task.task_id},
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
            await self.graph_frontier.mark_node_status(
                task.graph_node_id,
                status=CrawlGraphNodeStatus.DONE,
                metadata={
                    "crawl_task_id": task.task_id,
                    "accepted": int(save_summary.get("accepted", 0) or 0),
                    "created": int(save_summary.get("created", 0) or 0),
                    "updated": int(save_summary.get("updated", 0) or 0),
                },
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
            await self.graph_frontier.mark_node_status(
                task.graph_node_id,
                status=CrawlGraphNodeStatus.RETRY,
                last_error=_RICH_DETAIL_NO_STRUCTURED_DATA,
                increment_attempt=True,
                metadata={"crawl_task_id": task.task_id},
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
        await self.graph_frontier.mark_node_status(
            task.graph_node_id,
            status=CrawlGraphNodeStatus.FAILED,
            last_error="no_structured_data",
            metadata={"crawl_task_id": task.task_id},
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
                graph_node_id=task.graph_node_id,
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
            await self.graph_frontier.mark_node_status(
                task.graph_node_id,
                status=CrawlGraphNodeStatus.RETRY,
                last_error="invalid_json_retry",
                increment_attempt=True,
                metadata={"crawl_task_id": task.task_id},
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
        await self.graph_frontier.mark_node_status(
            task.graph_node_id,
            status=CrawlGraphNodeStatus.RETRY,
            last_error="invalid_json_retry_exhausted",
            increment_attempt=True,
            metadata={"crawl_task_id": task.task_id},
        )
        self._pipeline_stats["retry"] = int(self._pipeline_stats.get("retry", 0)) + 1
        self._increment_task_kind_stat(task, "failed")
        self._pipeline_stats["invalid_json_failures"] += 1
        return None

    async def _run_extraction_task(self, task: _ExtractionTaskItem, skills: str) -> _ExtractionOutcome:
        if self._task_kind_prefix(task) != "detail":
            self._pipeline_stats["list_save_suppressed"] = int(
                self._pipeline_stats.get("list_save_suppressed", 0)
            ) + 1
            self.logger.info(
                "Suppress list-page professor extraction task_id=%s org_unit=%s url=%s",
                task.task_id,
                task.org_unit_name,
                task.source_url,
            )
            return _ExtractionOutcome(
                payloads=[],
                invalid_json_events=[],
                skipped_by_gate=True,
                skip_reason="list_save_suppressed",
            )

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
            return {"accepted": len(normalized_payload.get("professors", []) or [])}

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
        if task is not None and self._task_kind_prefix(task) != "detail":
            dropped = 0
            for payload in payloads:
                professors = payload.get("professors") if isinstance(payload, dict) else None
                dropped += len(professors) if isinstance(professors, list) else 0
            self._pipeline_stats["list_save_suppressed"] = int(
                self._pipeline_stats.get("list_save_suppressed", 0)
            ) + 1
            self._pipeline_stats["list_records_suppressed"] = int(
                self._pipeline_stats.get("list_records_suppressed", 0)
            ) + dropped
            self.logger.info(
                "Suppress list-page DB save task_id=%s org_unit=%s url=%s payloads=%s records=%s",
                getattr(task, "task_id", 0),
                getattr(task, "org_unit_name", "Unknown"),
                getattr(task, "source_url", ""),
                len(payloads),
                dropped,
            )
            return totals
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
