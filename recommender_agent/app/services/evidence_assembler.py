from datetime import datetime
from typing import Any
import json
import re

from app.models.item import Item
from app.models.org_unit import OrgUnit


MISSING_HOMEPAGE_LIMITATION = "该导师暂无公开主页信息"
MISSING_SOURCE_LIMITATION = "数据来源未标注"
MISSING_UPDATED_AT_LIMITATION = "信息更新时间未记录"
MISSING_RESEARCH_FIELDS_LIMITATION = "该导师研究方向未明确标注"
MISSING_SUMMARY_LIMITATION = "该导师暂无简介信息"
MISSING_UNIVERSITY_LIMITATION = "该导师所属学校未明确标注"
MISSING_COLLEGE_LIMITATION = "该导师所属学院未明确标注"
WEAK_MATCH_LIMITATION = "当前匹配分数较低，推荐依据较弱，建议补充研究方向或院校偏好"


def assemble_recommendation_evidence(
    item: Item,
    org_unit: OrgUnit | None = None,
    match_score: float | None = None,
    ranking_evidence: dict[str, Any] | None = None,
    retrieval_sources: list[str] | None = None,
    reasons: list[str] | None = None,
    graph_paths: list[dict[str, Any]] | None = None,
    follow_up_questions: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble recommendation metadata without inventing missing evidence."""
    metadata = _parse_metadata(item.metadata_json)
    limitations: list[str] = []

    research_fields = _split_text_list(item.research_areas)
    if not research_fields:
        limitations.append(MISSING_RESEARCH_FIELDS_LIMITATION)

    summary = _clean_text(item.description)
    if summary is None:
        limitations.append(MISSING_SUMMARY_LIMITATION)

    homepage_url = _first_text(metadata, "homepage_url", "homepage")
    if homepage_url is None:
        limitations.append(MISSING_HOMEPAGE_LIMITATION)

    source_url = _first_text(metadata, "source_url", "source_page", "url")
    if source_url is None:
        limitations.append(MISSING_SOURCE_LIMITATION)

    updated_at = _first_text(metadata, "updated_at", "source_updated_at", "last_updated")
    if updated_at is None:
        limitations.append(MISSING_UPDATED_AT_LIMITATION)

    org_name = org_unit.name if org_unit else None
    college = _clean_text(item.org_unit) or _clean_text(item.category) or _clean_text(org_name)
    if college is None:
        limitations.append(MISSING_COLLEGE_LIMITATION)

    university = _first_text(metadata, "university", "school")
    if university is None:
        limitations.append(MISSING_UNIVERSITY_LIMITATION)

    evidence = {
        **(ranking_evidence or {}),
        "retrieval_sources": retrieval_sources or [],
        "data_source": _first_text(metadata, "source"),
        "imported_at": _format_datetime(item.created_at),
        "metadata_keys": sorted(metadata.keys()),
    }

    score = _round_score(match_score)
    if score is not None and score < 0.5:
        limitations.append(WEAK_MATCH_LIMITATION)

    academic_title = _clean_text(item.tags)

    return {
        "professor_id": item.id,
        "item_id": item.id,
        "name": item.title,
        "university": university,
        "college": college,
        "title": academic_title,
        "research_fields": research_fields,
        "summary": summary,
        "homepage_url": homepage_url,
        "source_url": source_url,
        "updated_at": updated_at,
        "match_score": score,
        "match_level": match_level(score),
        "match_level_en": match_level(score, locale="en"),
        "reasons": reasons or [],
        "limitations": limitations,
        "evidence": evidence,
        "follow_up_questions": follow_up_questions or [],
        # Backward-compatible fields used by the current API/frontend.
        "category": item.category,
        "org_unit": item.org_unit,
        "tags": item.tags,
        "description": item.description,
        "score": score,
        "sources": retrieval_sources or [],
        "graph_paths": graph_paths or [],
    }


def match_level(score: float | None, locale: str = "zh") -> str | None:
    """Derive a deterministic display match level from the match score."""
    if score is None:
        return None
    if score >= 0.8:
        return "高" if locale == "zh" else "high"
    if score >= 0.5:
        return "中" if locale == "zh" else "medium"
    return "低" if locale == "zh" else "low"


def _parse_metadata(metadata_json: str | None) -> dict[str, Any]:
    if not metadata_json:
        return {}
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(metadata, dict):
        return {}
    return dict(metadata)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_text(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        text = _clean_text(metadata.get(key))
        if text:
            return text
    return None


def _split_text_list(value: str | None) -> list[str]:
    text = _clean_text(value)
    if text is None:
        return []
    return [part.strip() for part in re.split(r"[/、，,;；\n]+", text) if part.strip()]


def _round_score(score: float | None) -> float | None:
    if score is None:
        return None
    return round(float(score), 4)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
