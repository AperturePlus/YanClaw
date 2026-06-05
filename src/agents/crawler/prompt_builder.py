from __future__ import annotations

import json
from typing import Any

from agents.crawler.url_heuristics import (
    FACULTY_KEYWORDS,
    ORG_UNIT_PAGE_KEYWORDS,
    _keyword_filter,
    _truncate_middle,
)
from runtime.context import ContextManager


class CrawlerPromptBuilder:
    """Builds crawler LLM payloads without owning orchestration state."""

    def __init__(
        self,
        *,
        context_manager: ContextManager,
        fetcher: Any,
        start_url: str,
        university_name: str,
        location: str,
        visited_urls: set[str],
    ) -> None:
        self.context_manager = context_manager
        self.fetcher = fetcher
        self.start_url = start_url
        self.university_name = university_name
        self.location = location
        self.visited_urls = visited_urls

    def update_context(self, *, start_url: str, university_name: str, location: str) -> None:
        self.start_url = start_url
        self.university_name = university_name
        self.location = location

    @staticmethod
    def build_professor_instruction(org_unit_name: str, *, detail_mode: bool, strict_retry: bool) -> str:
        if detail_mode:
            base = (
                "Extract professor records from this detail page and call save_professors when records are found. "
                + f"Use org_unit_name={org_unit_name!r}. Set source_url to the current page URL. "
                + "Prioritize fields: email, phone, research_areas. "
                + "Only save records that include at least one of email/phone/research_areas. "
                + "Research areas may appear as text under headings like 研究方向/研究领域 or as linked anchor text; save those visible phrases in research_areas. "
                + "If visible body text follows headings like 个人简介/简介/个人概况, save that paragraph in bio without inventing missing content. "
                + "If this page only contains category/list names without these fields, do not save placeholders. "
                + "Do not include retired/emeritus records. "
                + "If content is mainly notices/news/policies/recruitment/personnel announcements, skip saving."
            )
        else:
            base = (
                "Extract public professor records and call save_professors when records are found. "
                + f"Use org_unit_name={org_unit_name!r}. Set source_url to the current page URL. "
                + "For official roster/list pages, save visible names and academic titles even when email/phone/research_areas are absent; detail pages may enrich them later. "
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

    @staticmethod
    def state_link_limit(state: Any) -> int:
        state_value = getattr(state, "value", str(state))
        limits = {
            "DISCOVER_ORG_UNIT_PAGES": 40,
            "EXTRACT_ORG_UNITS": 40,
            "FIND_FACULTY_PAGES": 30,
            "EXTRACT_PROFESSORS": 0,
        }
        return limits.get(state_value, 30)

    @staticmethod
    def state_text_limit(state: Any, *, detail_mode: bool = False) -> int:
        if detail_mode:
            return 8000
        state_value = getattr(state, "value", str(state))
        limits = {
            "DISCOVER_ORG_UNIT_PAGES": 3000,
            "EXTRACT_ORG_UNITS": 8000,
            "FIND_FACULTY_PAGES": 6000,
            "EXTRACT_PROFESSORS": 6000,
        }
        return limits.get(state_value, 6000)

    def compact_page_text(self, text: str, max_chars: int, *, min_chars: int = 300) -> str:
        if max_chars <= 0 or not text:
            return ""
        compacted = self.context_manager.compact_text(text)
        if compacted and len(compacted) > max_chars:
            compacted = _truncate_middle(compacted, max_chars)

        if not compacted or (len(compacted) < min(min_chars, max_chars // 2) and len(text) > len(compacted) * 2):
            return _truncate_middle(text, max_chars)
        return compacted

    @staticmethod
    def serialize_payload(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def build_tool_call_policy(allowed_tools: set[str]) -> str:
        if not allowed_tools:
            return "Do not call any tools."
        names = sorted(allowed_tools)
        if len(names) == 1:
            return f"Only call {names[0]}. Do not invent tool names."
        return f"Only call tools listed in allowed_tools ({', '.join(names)}). Do not invent tool names."

    def build_llm_payload(
        self,
        *,
        state: Any,
        instruction: str,
        url: str,
        page_text: str,
        links: list[str],
        allowed_tools: set[str],
        detail_mode: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        raw_links = list(links or [])
        same_domain_links = self.fetcher.filter_same_domain(raw_links, self.start_url) if raw_links else []
        state_value = getattr(state, "value", str(state))
        if state_value in {"DISCOVER_ORG_UNIT_PAGES", "EXTRACT_ORG_UNITS"}:
            candidate_links = _keyword_filter(same_domain_links, ORG_UNIT_PAGE_KEYWORDS) or same_domain_links
        elif state_value in {"FIND_FACULTY_PAGES", "EXTRACT_PROFESSORS"}:
            candidate_links = _keyword_filter(same_domain_links, FACULTY_KEYWORDS) or same_domain_links
        else:
            candidate_links = same_domain_links
        link_limit = self.state_link_limit(state)
        kept_links = candidate_links[:link_limit] if link_limit > 0 else []

        text_limit = self.state_text_limit(state, detail_mode=detail_mode)
        compacted_text = self.compact_page_text(page_text or "", text_limit)

        payload: dict[str, Any] = {
            "allowed_tools": sorted(allowed_tools),
            "instruction": instruction,
            "links": kept_links,
            "location": self.location,
            "page_text": compacted_text,
            "state": state_value,
            "university": self.university_name,
            "url": url,
            "visited_count": len(self.visited_urls),
        }
        if state_value == "DISCOVER_ORG_UNIT_PAGES":
            payload["visited_urls"] = sorted(self.visited_urls)[-15:]

        user_content = self.serialize_payload(payload)
        payload_bytes = len(user_content.encode("utf-8", errors="ignore"))
        metadata = {
            "raw_chars": len(page_text or ""),
            "compacted_chars": len(compacted_text),
            "links_raw": len(raw_links),
            "links_kept": len(kept_links),
            "payload_bytes": payload_bytes,
        }
        return user_content, metadata
