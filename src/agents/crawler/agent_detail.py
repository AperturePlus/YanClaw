from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from agents.crawler.url_heuristics import (
    _is_faculty_platform,
    _is_pagination_link,
    _looks_like_retired_content,
    _looks_like_retired_url,
    _sanitize_url,
)


async def enrich_profiles_with_detail_backend(self: Any, current: Any, fetched: Any, skills: str) -> None:
    if not self._is_interactive or not self.detail_enrich_enabled:
        return
    if self.detail_fetch_backend == "human":
        await self._enrich_profiles_with_human(current, fetched, skills)
        return
    if self.detail_fetch_backend == "httpx":
        await self._enrich_profiles_with_httpx(current, fetched, skills)
        return
    self.logger.debug("Unsupported detail backend=%s; skip detail enrichment", self.detail_fetch_backend)


async def enrich_profiles_with_human(self: Any, current: Any, fetched: Any, skills: str) -> None:
    org_unit_key = self._detail_org_unit_key(current)
    processed = self._detail_processed_by_org_unit.get(org_unit_key, 0)
    remaining = self.detail_profile_hard_cap_per_org_unit - processed
    if remaining <= 0:
        self.logger.debug(
            "Detail enrichment cap reached org_unit=%s cap=%s",
            current.label or "Unknown",
            self.detail_profile_hard_cap_per_org_unit,
        )
        return

    candidates = self._extract_detail_profile_links(fetched.links, fetched.url)
    if not candidates:
        return

    pending: list[str] = []
    for link in candidates:
        if len(pending) >= remaining:
            break
        if link in self._detail_visited_urls or link in self.visited_urls:
            continue
        self._detail_visited_urls.add(link)
        pending.append(link)
    if not pending:
        return
    self._detail_processed_by_org_unit[org_unit_key] = processed + len(pending)
    await self._process_detail_urls_with_human(pending, current, skills)


async def enrich_profiles_with_httpx(self: Any, current: Any, fetched: Any, skills: str) -> None:
    if self._detail_fetcher is None:
        return

    org_unit_key = self._detail_org_unit_key(current)
    processed = self._detail_processed_by_org_unit.get(org_unit_key, 0)
    remaining = self.detail_profile_hard_cap_per_org_unit - processed
    if remaining <= 0:
        self.logger.debug(
            "Detail enrichment cap reached org_unit=%s cap=%s",
            current.label or "Unknown",
            self.detail_profile_hard_cap_per_org_unit,
        )
        return

    candidates = self._extract_detail_profile_links(fetched.links, fetched.url)
    if not candidates:
        return

    pending: list[str] = []
    for link in candidates:
        if len(pending) >= remaining:
            break
        if link in self._detail_visited_urls or link in self.visited_urls:
            continue
        self._detail_visited_urls.add(link)
        pending.append(link)
    if not pending:
        return
    self._detail_processed_by_org_unit[org_unit_key] = processed + len(pending)

    consecutive_failures = 0
    failed_urls: list[str] = []
    while pending:
        link = pending.pop(0)
        if _looks_like_retired_url(link):
            continue
        try:
            detail_fetched = await self._detail_fetcher.fetch(link)
        except Exception as error:
            self.logger.debug("Detail httpx fetch failed url=%s error=%s", link, error)
            consecutive_failures += 1
            failed_urls.append(link)
            if consecutive_failures >= self.detail_failure_threshold:
                switched = await self._handle_detail_failure_decision(current, failed_urls, skills)
                if switched:
                    self.logger.info(
                        "Detail enrichment switched failed batch to human org_unit=%s failed=%s remaining_httpx=%s",
                        current.label or "Unknown",
                        len(failed_urls),
                        len(pending),
                    )
                consecutive_failures = 0
                failed_urls = []
            continue

        if self._is_failed_detail_fetch(detail_fetched):
            consecutive_failures += 1
            failed_urls.append(link)
            if consecutive_failures >= self.detail_failure_threshold:
                switched = await self._handle_detail_failure_decision(current, failed_urls, skills)
                if switched:
                    self.logger.info(
                        "Detail enrichment switched failed batch to human org_unit=%s failed=%s remaining_httpx=%s",
                        current.label or "Unknown",
                        len(failed_urls),
                        len(pending),
                    )
                consecutive_failures = 0
                failed_urls = []
            continue

        consecutive_failures = 0
        failed_urls = []

        clean_url = _sanitize_url(detail_fetched.url)
        if clean_url:
            self.visited_urls.add(clean_url)
        if self._is_retired_page(detail_fetched):
            self.logger.info("Skip retired detail page url=%s", detail_fetched.url)
            continue

        await self._extract_professors_from_page(
            current,
            detail_fetched,
            skills,
            detail_mode=True,
        )


async def handle_detail_failure_decision(
    self: Any,
    current: Any,
    failed_urls: list[str],
    skills: str,
) -> bool:
    if not hasattr(self.fetcher, "request_decision") or not hasattr(self.fetcher, "wait_decision"):
        return False
    urls = list(dict.fromkeys(failed_urls))
    if not urls:
        return False
    decision = await self.fetcher.request_decision(  # type: ignore[attr-defined]
        kind="detail_fetch_failure",
        org_unit_name=current.label or "Unknown",
        failure_count=len(failed_urls),
        sample_urls=urls[:3],
        suggested_action="switch_failed_to_human",
    )
    action = await self.fetcher.wait_decision(decision.id)  # type: ignore[attr-defined]
    if action != "switch_failed_to_human":
        return False
    self.logger.info(
        "Switching failed detail links to human for org_unit=%s urls=%s",
        current.label or "Unknown",
        len(urls),
    )
    await self._process_detail_urls_with_human(urls, current, skills)
    return True


async def process_detail_urls_with_human(self: Any, urls: list[str], current: Any, skills: str) -> None:
    next_depth = current.depth + 1
    if not self._within_depth(next_depth):
        return
    for url in urls:
        if url in self.visited_urls:
            continue
        fetched = await self._fetch_url(url, next_depth)
        if fetched is None:
            continue
        if self._is_retired_page(fetched):
            self.logger.info("Skip retired human detail page url=%s", fetched.url)
            continue
        await self._extract_professors_from_page(
            current,
            fetched,
            skills,
            detail_mode=True,
        )


def extract_detail_profile_links(self: Any, links: list[str], current_url: str) -> list[str]:
    same_domain = self.fetcher.filter_same_domain(links, self.start_url)
    current_parsed = urlparse(current_url)
    current_host = (current_parsed.hostname or "").lower()
    current_path = current_parsed.path.lower()
    current_dir = self._derive_section_prefix(current_path)

    detail_hints = (
        "/info/",
        "/teacher/",
        "/teachers/",
        "/faculty/",
        "/people/",
        "/show",
        "/detail",
        "/profile",
        "/mentor",
        "teacher",
        "faculty",
        "people",
        "profile",
        "detail",
        "show",
    )
    section_hints = ("/szdw/", "/team/", "/staff/", "/jsdw/")
    noise_hints = (
        "/gywm/",
        "/djgz/",
        "/rcpy/",
        "/pxfz/",
        "/zsjy/",
        "/xsgz/",
        "/kxyj/",
        "/xwzx/",
        "/news/",
        "/notice/",
        "/tzgg/",
        "/download/",
        "/about/",
        "/intro/",
        "/history/",
        "/leader/",
        "/lxdh/",
        "/index",
    )
    file_ext_hints = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar")

    candidates: list[str] = []
    for link in same_domain:
        if link == current_url:
            continue
        if _is_faculty_platform(link) or _is_pagination_link(link):
            continue
        if _looks_like_retired_url(link):
            continue
        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        if current_host and host != current_host:
            continue
        lowered = link.lower()
        if any(token in lowered for token in noise_hints):
            continue
        if any(lowered.endswith(ext) for ext in file_ext_hints):
            continue
        path = parsed.path.lower()
        related_by_path = False
        if current_dir:
            prefix = current_dir.rstrip("/")
            related_by_path = bool(prefix and path.startswith(prefix + "/"))
        related_by_hint = any(token in lowered for token in detail_hints)
        if not related_by_path and not related_by_hint:
            continue
        candidates.append(link)

    def _score(url: str) -> tuple[int, int]:
        lowered = url.lower()
        depth = max(0, urlparse(url).path.count("/") - 1)
        score = depth
        if current_dir and urlparse(url).path.lower().startswith(current_dir.rstrip("/") + "/"):
            score += 4
        if any(token in lowered for token in detail_hints):
            score += 4
        if any(token in lowered for token in section_hints):
            score += 2
        if any(token in lowered for token in noise_hints):
            score -= 6
        return score, -len(url)

    ranked = sorted(candidates, key=_score, reverse=True)
    deduped: list[str] = []
    seen: set[str] = set()
    for link in ranked:
        if link in seen:
            continue
        if _score(link)[0] < 3:
            continue
        seen.add(link)
        deduped.append(link)
    return deduped


def detail_org_unit_key(self: Any, current: Any) -> str:
    if current.org_unit_id is not None:
        return f"id:{current.org_unit_id}"
    label = (current.label or "").strip().lower()
    if label:
        return f"label:{label}"
    return f"url:{_sanitize_url(current.url)}"


def derive_section_prefix(path: str) -> str:
    normalized = (path or "").strip().lower()
    if not normalized:
        return ""
    parent, _, leaf = normalized.rpartition("/")
    if leaf.endswith((".htm", ".html", ".shtml")):
        stem = leaf.rsplit(".", 1)[0]
        if stem:
            return f"{parent}/{stem}" if parent else f"/{stem}"
    if parent:
        return parent
    return normalized


def is_failed_detail_fetch(self: Any, fetched: Any) -> bool:
    if fetched.block_reason:
        return True
    if fetched.status_code in {0, 202, 429, 503}:
        return True
    if fetched.status_code >= 400:
        return True
    if len((fetched.text or "").strip()) < 160 and len(fetched.links) < 2:
        return True
    return False


def is_retired_page(self: Any, fetched: Any) -> bool:
    if _looks_like_retired_url(fetched.url):
        return True
    return _looks_like_retired_content(fetched.text, fetched.url)
