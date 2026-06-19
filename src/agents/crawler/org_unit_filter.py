from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable

from agents.crawler.url_heuristics import (
    DEFAULT_ORG_UNIT_EXCLUDE_KEYWORDS,
    _org_unit_exclusion_match,
    _sanitize_url,
)


ORG_UNIT_FILTER_STATE = "ORG_UNIT_FILTER"

ORG_UNIT_FILTER_INSTRUCTION = (
    "Filter only org units that clearly belong to excluded categories: arts, sports, "
    "Sino-foreign/joint programs, basic teaching centers, teaching/experiment/training centers, "
    "continuing/adult education, "
    "undergraduate teaching units such as residential colleges, or excellent engineer "
    "teaching programs. Treat person-named colleges as excluded only when the evidence "
    "shows they are undergraduate/honor/residential teaching groupings with no independent "
    "faculty. Treat ordinary department sections under a college as sub_department_section, "
    "not as independent org units. Do not apply a broad academic whitelist. When uncertain, keep the org unit. "
    "Return JSON with included_org_units and excluded_org_units."
)


@dataclass(frozen=True)
class ExcludedOrgUnit:
    id: int | None
    name: str
    url: str
    kind: str
    reason: str
    category: str
    keyword: str
    source: str

    def to_evidence(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "kind": self.kind,
            "reason": self.reason,
            "category": self.category,
            "keyword": self.keyword,
            "source": self.source,
        }


@dataclass(frozen=True)
class OrgUnitFilterResult:
    kept: list[dict[str, Any]]
    hard_excluded: list[ExcludedOrgUnit]
    llm_excluded: list[ExcludedOrgUnit]

    @property
    def excluded(self) -> list[ExcludedOrgUnit]:
        return [*self.hard_excluded, *self.llm_excluded]


NameMatcher = Callable[[str], bool]


def org_unit_exclusion_category(
    *,
    name: str,
    kind: str | None = None,
    url: str | None = None,
    keywords: tuple[str, ...] | list[str] | None = None,
) -> str | None:
    match = _org_unit_exclusion_match(name=name, kind=kind, url=url, keywords=keywords)
    return match.category if match is not None else None


def is_teaching_experiment_center_name(name: str, *, kind: str | None = None, url: str | None = None) -> bool:
    return org_unit_exclusion_category(name=name, kind=kind, url=url) == "teaching_experiment_center"


def looks_like_sub_department_section_name(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if is_teaching_experiment_center_name(text):
        return False
    normalized = re.sub(r"\s+", "", text)
    if normalized.endswith("系") and normalized not in {"院系"}:
        return True
    return bool(re.search(r"[-－—–]\s*[^-－—–]*(系)$", text))


def hard_filter_org_unit_payloads(
    units: list[dict[str, Any]],
    *,
    exclude_enabled: bool,
    keywords: tuple[str, ...] | list[str] | None = None,
    target_name_matcher: NameMatcher | None = None,
) -> OrgUnitFilterResult:
    if not units or not exclude_enabled:
        return OrgUnitFilterResult(kept=list(units), hard_excluded=[], llm_excluded=[])

    active_keywords = list(DEFAULT_ORG_UNIT_EXCLUDE_KEYWORDS) if keywords is None else list(keywords)
    kept: list[dict[str, Any]] = []
    excluded: list[ExcludedOrgUnit] = []
    for raw in units:
        unit = normalize_org_unit_payload(raw)
        name = str(unit.get("name") or "").strip()
        if not name:
            continue
        if target_name_matcher is not None and target_name_matcher(name):
            kept.append(unit)
            continue
        match = _org_unit_exclusion_match(
            name=name,
            kind=str(unit.get("kind") or "").strip() or None,
            url=str(unit.get("url") or "").strip() or None,
            keywords=active_keywords,
        )
        if match is None:
            kept.append(unit)
            continue
        excluded.append(
            ExcludedOrgUnit(
                id=_safe_int(unit.get("id")),
                name=name,
                url=str(unit.get("url") or ""),
                kind=str(unit.get("kind") or ""),
                reason=match.reason,
                category=match.category,
                keyword=match.keyword,
                source="hard",
            )
        )
    return OrgUnitFilterResult(kept=kept, hard_excluded=excluded, llm_excluded=[])


async def llm_filter_org_unit_payloads(
    units: list[dict[str, Any]],
    *,
    llm_client: Any,
    context_manager: Any,
    skills_text: str,
    university: str,
    source_url: str,
    source: str,
    model_max_tokens: int,
    logger: Any,
    state: str = ORG_UNIT_FILTER_STATE,
    instruction: str = ORG_UNIT_FILTER_INSTRUCTION,
) -> OrgUnitFilterResult:
    if not units or llm_client is None:
        return OrgUnitFilterResult(kept=list(units), hard_excluded=[], llm_excluded=[])

    payload = {
        "allowed_tools": [],
        "filter_task": "org_unit_exclusion",
        "instruction": instruction,
        "org_units": [
            {
                "id": unit.get("id"),
                "name": str(unit.get("name") or ""),
                "url": str(unit.get("url") or ""),
                "kind": str(unit.get("kind") or ""),
            }
            for unit in units
        ],
        "source": source,
        "state": state,
        "university": university,
        "url": source_url,
    }
    user_content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    batches = context_manager.build_messages(
        "You are a cautious university org-unit filter. When uncertain, keep the org unit.",
        [],
        skills_text,
        user_content,
        min(int(model_max_tokens), 16000),
        dynamic_system_content="Tool call policy: Do not call any tools.",
    )

    try:
        final_result = None
        for batch in batches:
            final_result = await llm_client.chat(batch, tools=None, tool_handlers={})
    except Exception as error:
        logger.warning(
            "Org-unit LLM exclusion failed university=%s source=%s page=%s error=%s; keeping candidates",
            university,
            source,
            source_url,
            error,
        )
        return OrgUnitFilterResult(kept=list(units), hard_excluded=[], llm_excluded=[])

    parsed = parse_json_object(getattr(final_result, "content", "") if final_result else "")
    if not isinstance(parsed, dict):
        logger.warning(
            "Org-unit LLM exclusion returned invalid JSON university=%s source=%s page=%s; keeping candidates",
            university,
            source,
            source_url,
        )
        return OrgUnitFilterResult(kept=list(units), hard_excluded=[], llm_excluded=[])

    included = parsed.get("included_org_units")
    excluded = parsed.get("excluded_org_units")
    if not isinstance(included, list) or not isinstance(excluded, list):
        logger.warning(
            "Org-unit LLM exclusion missing included/excluded lists university=%s source=%s page=%s; keeping candidates",
            university,
            source,
            source_url,
        )
        return OrgUnitFilterResult(kept=list(units), hard_excluded=[], llm_excluded=[])

    excluded_by_key: dict[str, ExcludedOrgUnit] = {}
    for item in excluded:
        if not isinstance(item, dict):
            continue
        keys = org_unit_filter_item_keys(item)
        if not keys:
            continue
        excluded_unit = _excluded_from_llm_item(item)
        for key in keys:
            excluded_by_key[key] = excluded_unit

    if not excluded_by_key:
        return OrgUnitFilterResult(kept=list(units), hard_excluded=[], llm_excluded=[])

    kept: list[dict[str, Any]] = []
    llm_excluded: list[ExcludedOrgUnit] = []
    seen_excluded: set[tuple[int | None, str, str]] = set()
    for raw in units:
        unit = normalize_org_unit_payload(raw)
        matched_keys = org_unit_filter_item_keys(unit) & excluded_by_key.keys()
        if not matched_keys:
            kept.append(unit)
            continue
        excluded_unit = excluded_by_key[sorted(matched_keys)[0]]
        identity = (excluded_unit.id, excluded_unit.name, excluded_unit.url)
        if identity not in seen_excluded:
            seen_excluded.add(identity)
            llm_excluded.append(excluded_unit)

    return OrgUnitFilterResult(kept=kept, hard_excluded=[], llm_excluded=llm_excluded)


def normalize_org_unit_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        raw_id = item.get("id") or item.get("org_unit_id")
        return {
            **item,
            "id": _safe_int(raw_id),
            "name": str(item.get("name") or item.get("org_unit_name") or "").strip(),
            "url": str(item.get("url") or item.get("org_unit_url") or "").strip(),
            "kind": str(item.get("kind") or "").strip(),
        }
    return {
        "id": None,
        "name": str(item or "").strip(),
        "url": "",
        "kind": "",
    }


def org_unit_filter_item_keys(item: Any) -> set[str]:
    unit = normalize_org_unit_payload(item)
    keys: set[str] = set()
    raw_id = unit.get("id")
    if raw_id is not None:
        keys.add(f"id:{raw_id}")
    normalized_name = normalize_org_unit_match_text(str(unit.get("name") or ""))
    if normalized_name:
        keys.add(f"name:{normalized_name}")
    normalized_url = _sanitize_url(str(unit.get("url") or ""))
    if normalized_url:
        keys.add(f"url:{normalized_url}")
    return keys


def normalize_org_unit_match_text(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return re.sub(r"[\s\-_·,，、/\\|:：;；\(\)（）\[\]【】{}<>《》]+", "", text)


def parse_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def _excluded_from_llm_item(item: dict[str, Any]) -> ExcludedOrgUnit:
    reason = str(item.get("reason") or "llm").strip() or "llm"
    return ExcludedOrgUnit(
        id=_safe_int(item.get("id") or item.get("org_unit_id")),
        name=str(item.get("name") or item.get("org_unit_name") or "").strip(),
        url=str(item.get("url") or item.get("org_unit_url") or "").strip(),
        kind=str(item.get("kind") or "").strip(),
        reason=reason,
        category=reason.split(":", 1)[0],
        keyword="",
        source="llm",
    )


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
