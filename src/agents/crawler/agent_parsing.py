from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from agents.crawler.url_heuristics import (
    _allow_faculty_candidate_for_org_unit,
    _contains_cjk,
    _dedupe_query_terms,
    _extract_urls_from_text,
    _is_academician_showcase_page,
    _is_explicit_faculty_directory_url,
    _is_non_faculty_noise_url,
    _is_faculty_platform,
    _is_pagination_link,
    _looks_like_faculty_page,
    _looks_like_retired_url,
    _rank_faculty_page_candidates,
    _sanitize_url,
)


def extract_pagination_links(self: Any, links: list[str], current_url: str) -> list[str]:
    same_domain = self.fetcher.filter_same_domain(links, self.start_url)
    pagination: list[str] = []
    for link in same_domain:
        if link == current_url or link in self.visited_urls:
            continue
        if _is_pagination_link(link):
            pagination.append(link)
    return pagination


def extract_followup_faculty_links(self: Any, links: list[str], current_url: str) -> list[str]:
    same_domain = self.fetcher.filter_same_domain(links, self.start_url)
    current_host = (urlparse(current_url).hostname or "").lower()
    current_path = urlparse(current_url).path.lower()
    current_dir = self._derive_section_prefix(current_path)
    current_is_noise = _is_non_faculty_noise_url(current_url)
    noise_hints = ("/gywm/", "/djgz/", "/rcpy/", "/pxfz/", "/about/", "/intro/", "/history/", "/leader/", "/index")
    dropped_noise = 0
    dropped_parent_noise = 0
    candidates: list[str] = []
    for link in same_domain:
        if link == current_url:
            continue
        if not _allow_faculty_candidate_for_org_unit(link, org_unit_url=current_url, start_url=self.start_url):
            continue
        if _is_faculty_platform(link) or _looks_like_retired_url(link):
            continue
        if _is_non_faculty_noise_url(link):
            dropped_noise += 1
            continue
        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        if current_host and host != current_host:
            continue
        lowered = link.lower()
        if any(token in lowered for token in noise_hints):
            continue
        related_by_path = bool(current_dir) and parsed.path.lower().startswith(current_dir.rstrip("/") + "/")
        looks_like_faculty = _looks_like_faculty_page(link)
        if not (looks_like_faculty or related_by_path):
            continue
        # If current page is already identified as noise, avoid directory-wide blind expansion.
        if current_is_noise and related_by_path and not _is_explicit_faculty_directory_url(link):
            dropped_parent_noise += 1
            continue
        candidates.append(link)

    if dropped_noise:
        self._pipeline_stats["followup_dropped_noise"] = int(self._pipeline_stats.get("followup_dropped_noise", 0)) + dropped_noise
    if dropped_parent_noise:
        self._pipeline_stats["followup_dropped_noise"] = int(self._pipeline_stats.get("followup_dropped_noise", 0)) + dropped_parent_noise
    if dropped_noise or dropped_parent_noise:
        self.logger.debug(
            "Followup links filtered current=%s kept=%s dropped_noise=%s dropped_parent_noise=%s drop_reason=url_noise_token",
            current_url,
            len(candidates),
            dropped_noise,
            dropped_parent_noise,
        )

    candidates = _rank_faculty_page_candidates(candidates)
    non_showcase = [link for link in candidates if not _is_academician_showcase_page(link)]
    if non_showcase:
        candidates = non_showcase
    return candidates


def links_from_result(self: Any, content: str) -> list[str]:
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
            return [_sanitize_url(url) for url in raw if _sanitize_url(url)]
    return []


def org_unit_followup_links_from_result(self: Any, content: str, current_url: str) -> list[str]:
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


def links_from_tool_call_log(self: Any, result: Any, *, tool_name: str = "extract_links") -> list[str]:
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


def org_units_from_result(self: Any, content: str) -> list[dict[str, Any]]:
    payload = self._parse_json_from_text(content)
    if payload is None or not isinstance(payload, dict):
        return []
    units = payload.get("org_units")
    if isinstance(units, list):
        return [item for item in units if isinstance(item, dict)]
    return []


def parse_json_from_text(self: Any, content: str) -> Any | None:
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


async def search_engine_fallback(self: Any, query_suffix: str) -> list[str]:
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
        same_domain = [u for u in same_domain if not _is_faculty_platform(u) and not _is_non_faculty_noise_url(u)]
        self.execution_log.append(f"search_fallback query={query!r} found={len(same_domain)} links")
        self.logger.info("Search fallback found %d same-domain links", len(same_domain))
        return same_domain
    except Exception as error:
        self.logger.warning("Search engine fallback failed: %s", error)
        self.execution_log.append(f"search_fallback failed: {error}")
        return []
