from __future__ import annotations

from typing import Any

from agents.crawler import db as crawler_db
from agents.crawler.fetchers import Fetcher
from agents.crawler.sanitizer import contains_postdoc_hint, contains_retired_hint, sanitize_professor_payload
from runtime.database import DatabaseManager
from runtime.skills import SkillManager


SAVE_PROFESSORS_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "save_professors",
    "description": "Persist extracted professor records to the per-university crawler database.",
    "parameters": {
        "type": "object",
        "properties": {
            "org_unit_name": {"type": "string"},
            "org_unit_url": {"type": "string"},
            "source_url": {"type": "string"},
            "professors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "title": {"type": "string"},
                        "research_areas": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}},
                            ]
                        },
                        "email": {"type": "string"},
                        "phone": {"type": "string"},
                        "homepage": {"type": "string"},
                        "external_link": {"type": "string"},
                        "is_academician": {"type": "boolean"},
                        "bio": {"type": "string"},
                        "enrollment_pref": {"type": "string"},
                        "publications": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}},
                            ]
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        "required": ["org_unit_name", "professors"],
    },
}

EXTRACT_LINKS_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "extract_links",
    "description": "Filter candidate links for crawler navigation.",
    "parameters": {
        "type": "object",
        "properties": {
            "links": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "href": {"type": "string"},
                                "link": {"type": "string"},
                            },
                        },
                    ]
                },
            },
            "base_url": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["links", "base_url"],
    },
}


def get_crawler_tool_definitions() -> list[dict[str, Any]]:
    # Intentionally keep tools minimal: no skill creation/update from the crawler.
    return [SAVE_PROFESSORS_TOOL, EXTRACT_LINKS_TOOL]


def get_crawler_tools(
    db: DatabaseManager,
    skill_manager: SkillManager,
) -> dict[str, Any]:
    # Reserved for future use (tool access policy may depend on active skills).
    _ = skill_manager

    def _normalize_link_item(item: Any) -> str | None:
        if isinstance(item, dict):
            for key in ("url", "href", "link"):
                value = item.get(key)
                if isinstance(value, (str, bytes)):
                    item = value
                    break
            else:
                return None

        if isinstance(item, bytes):
            try:
                item = item.decode("utf-8", errors="ignore")
            except Exception:
                return None

        if not isinstance(item, str):
            return None

        normalized = item.strip().strip("`").strip("*").strip("_").strip("<").strip(">").strip('"').strip("'")
        while normalized and normalized[-1] in {")", "]", ">", "'", '"', ",", ";", "."}:
            normalized = normalized[:-1]
        normalized = normalized.strip()
        return normalized or None

    async def save_professors(
        org_unit_name: str,
        professors: list[dict[str, Any]],
        org_unit_url: str | None = None,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        accepted = 0
        created = 0
        updated = 0
        unchanged = 0
        deduped_by_name_key = 0
        deduped_by_homepage = 0
        academicians_saved = 0
        academicians_updated = 0
        academicians_unchanged = 0
        deduped_by_academician = 0
        academicians_enriched = 0
        professors_deleted_as_academician_duplicates = 0
        filtered_retired = 0
        filtered_postdoc = 0
        errors: list[str] = []
        prepared_professors: list[tuple[dict[str, Any], bool]] = []
        for professor in professors:
            try:
                if contains_retired_hint(
                    professor.get("name"),
                    professor.get("title"),
                    professor.get("bio"),
                    source_url,
                ):
                    filtered_retired += 1
                    continue
                if contains_postdoc_hint(
                    name=professor.get("name"),
                    title=professor.get("title"),
                    bio=professor.get("bio"),
                    source_url=source_url,
                ):
                    filtered_postdoc += 1
                    continue
                cleaned, is_academician = sanitize_professor_payload(
                    professor,
                    org_unit_name=org_unit_name,
                )
                accepted += 1
                prepared_professors.append((cleaned, is_academician))
            except Exception as exc:
                errors.append(f"{professor.get('name', '?')}: {exc}")

        if prepared_professors:
            async with db.session() as session:
                for cleaned, is_academician in prepared_professors:
                    data = {
                        **cleaned,
                        "org_unit_url": org_unit_url,
                        "source_url": source_url,
                    }
                    if is_academician:
                        upsert_result = await crawler_db.upsert_academician_with_status(session, data)
                        academician = upsert_result.entity
                        if upsert_result.status == "created":
                            academicians_saved += 1
                        elif upsert_result.status == "updated":
                            academicians_updated += 1
                        else:
                            academicians_unchanged += 1
                        if upsert_result.deduped_by_name_key:
                            deduped_by_name_key += 1
                        if upsert_result.deduped_by_homepage:
                            deduped_by_homepage += 1
                        removed = await crawler_db.delete_professor_duplicates_for_academician(
                            session,
                            academician,
                        )
                        professors_deleted_as_academician_duplicates += int(removed or 0)
                    else:
                        matched_academician, _reason = await crawler_db.match_academician_for_professor(
                            session,
                            name=str(cleaned.get("name") or ""),
                            org_unit_name=str(cleaned.get("org_unit_name") or org_unit_name or ""),
                            email=cleaned.get("email"),
                            homepage=cleaned.get("homepage"),
                            external_link=cleaned.get("external_link"),
                        )
                        if matched_academician is not None:
                            enriched = await crawler_db.merge_into_academician_from_professor(
                                session,
                                matched_academician,
                                title=cleaned.get("title"),
                                research_areas=cleaned.get("research_areas"),
                                email=cleaned.get("email"),
                                phone=cleaned.get("phone"),
                                homepage=cleaned.get("homepage"),
                                external_link=cleaned.get("external_link"),
                                bio=cleaned.get("bio"),
                                enrollment_pref=cleaned.get("enrollment_pref"),
                                publications=cleaned.get("publications"),
                            )
                            deduped_by_academician += 1
                            if enriched:
                                academicians_enriched += 1
                            removed = await crawler_db.delete_professor_duplicates_for_academician(
                                session,
                                matched_academician,
                            )
                            professors_deleted_as_academician_duplicates += int(removed or 0)
                            continue
                        upsert_result = await crawler_db.upsert_professor_with_status(session, data)
                        if upsert_result.status == "created":
                            created += 1
                        elif upsert_result.status == "updated":
                            updated += 1
                        else:
                            unchanged += 1
                        if upsert_result.deduped_by_name_key:
                            deduped_by_name_key += 1
                        if upsert_result.deduped_by_homepage:
                            deduped_by_homepage += 1
        result: dict[str, Any] = {
            "accepted": accepted,
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "deduped_by_name_key": deduped_by_name_key,
            "deduped_by_homepage": deduped_by_homepage,
            "saved": created,
        }
        if academicians_saved:
            result["academicians_saved"] = academicians_saved
        if academicians_updated:
            result["academicians_updated"] = academicians_updated
        if academicians_unchanged:
            result["academicians_unchanged"] = academicians_unchanged
        if deduped_by_academician:
            result["deduped_by_academician"] = deduped_by_academician
        if academicians_enriched:
            result["academicians_enriched"] = academicians_enriched
        if professors_deleted_as_academician_duplicates:
            result["professors_deleted_as_academician_duplicates"] = professors_deleted_as_academician_duplicates
        if filtered_retired:
            result["filtered_retired"] = filtered_retired
        if filtered_postdoc:
            result["filtered_postdoc"] = filtered_postdoc
        if errors:
            result["errors"] = errors
        return result

    async def extract_links(
        links: list[Any],
        base_url: str,
        keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_links = [item for link in links if (item := _normalize_link_item(link))]
        same_domain = Fetcher.filter_same_domain(normalized_links, base_url)
        if not keywords:
            return {"links": same_domain}

        lowered = [str(keyword).strip().lower() for keyword in keywords if str(keyword).strip()]
        if not lowered:
            return {"links": same_domain}

        filtered = [link for link in same_domain if any(keyword in link.lower() for keyword in lowered)]
        # Many Chinese university sites use non-semantic/pinyin URL paths, while the LLM may provide
        # human-language keywords. Avoid filtering everything out: only apply keyword filtering when
        # it yields at least one candidate.
        return {"links": filtered or same_domain}

    return {
        "save_professors": save_professors,
        "extract_links": extract_links,
    }
