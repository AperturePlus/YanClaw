from __future__ import annotations

import json
from typing import Any

from agents.crawler.heuristic_constants import (
    FACULTY_KEYWORDS,
    ORG_UNIT_PAGE_KEYWORDS,
)
from agents.crawler.url_heuristics import (
    _keyword_filter,
    _truncate_middle,
)
from runtime.context import ContextManager


CRAWLER_SYSTEM_PROMPT = "You are a cautious university faculty crawler. Stay on the same university domain."

PROFESSOR_DETAIL_INSTRUCTION_TEMPLATE = (
    "Extract professor records from this detail page and call save_professors when records are found. "
    "Use org_unit_name={org_unit_name}. Set source_url to the current page URL. "
    "Prioritize fields: title, email, phone, research_areas, bio, homepage, external_link. "
    "Only save records with a visible name and at least one concrete evidence field such as "
    "title/email/phone/research_areas/bio/homepage/external_link/publications/enrollment_pref. "
    "If only a name is visible, do not save a placeholder. "
    "If this is an individual profile with a visible name, academic title, and extractable facts, "
    "call save_professors; do not return explanatory prose only. "
    "Official same-domain profile pages under sections such as 名师风采/院士 should be saved when "
    "they present the person as part of the university site, even if office address or affiliations mention another institute. "
    "Research areas may appear as text under headings like 研究方向/研究领域 or as linked anchor text; "
    "they may also be short technical phrases in sections such as 科研项目/论文著作/代表论文/科研成果/项目题名. "
    "Save only concise visible phrases in research_areas. "
    "If visible body text follows headings like 个人简介/简介/个人概况/学习工作经历/工作经历/教育经历/教学情况/管理经验, "
    "save a short factual summary in bio without inventing missing content. "
    "If this page only contains category/list names without these fields, do not save placeholders. "
    "Do not include retired/emeritus records. "
    "If content is mainly notices/news/policies/recruitment/personnel announcements, skip saving."
)

PROFESSOR_LIST_INSTRUCTION_TEMPLATE = (
    "This is a faculty roster/list traversal task for org_unit_name={org_unit_name}. "
    "Do not call save_professors for roster/list pages, even when visible names or titles appear. "
    "Professor facts are saved only from personal detail pages or strong single-person detail pages. "
    "Use the page only to support navigation to detail pages, related faculty pages, and pagination. "
    "Skip noise pages dominated by notices/news/policies/recruitment/personnel content."
)

PROFESSOR_STRICT_RETRY_SUFFIX = (
    " Retry mode: call save_professors with only key fields "
    "{name,title,email,phone,research_areas,bio}; keep response concise, max 25 records, "
    "include a short bio when visible, escape quotes inside JSON strings, "
    "avoid publications, long arrays, and extra keys."
)

TOOL_CALL_POLICY_TEMPLATE = "Tool call policy: {policy}"
STRICT_JSON_TOOL_CALL_POLICY_TEMPLATE = "Tool call policy: {policy} Keep output short and strict JSON."


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
        template = PROFESSOR_DETAIL_INSTRUCTION_TEMPLATE if detail_mode else PROFESSOR_LIST_INSTRUCTION_TEMPLATE
        base = template.format(org_unit_name=repr(org_unit_name))
        if not strict_retry:
            return base
        return f"{base}{PROFESSOR_STRICT_RETRY_SUFFIX}"

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

    @staticmethod
    def build_dynamic_system_content(allowed_tools: set[str], *, strict_json: bool = False) -> str:
        template = STRICT_JSON_TOOL_CALL_POLICY_TEMPLATE if strict_json else TOOL_CALL_POLICY_TEMPLATE
        return template.format(policy=CrawlerPromptBuilder.build_tool_call_policy(allowed_tools))

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
