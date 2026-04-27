from __future__ import annotations

from typing import Any

from agents.crawler import db as crawler_db
from agents.crawler.fetcher import Fetcher
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
            "links": {"type": "array", "items": {"type": "string"}},
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
    async def save_professors(
        org_unit_name: str,
        professors: list[dict[str, Any]],
        org_unit_url: str | None = None,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        saved = 0
        errors: list[str] = []
        for professor in professors:
            try:
                async with db.session() as session:
                    data = {
                        **professor,
                        "org_unit_name": org_unit_name,
                        "org_unit_url": org_unit_url,
                        "source_url": source_url,
                    }
                    await crawler_db.upsert_professor(session, data)
                    saved += 1
            except Exception as exc:
                errors.append(f"{professor.get('name', '?')}: {exc}")
        result: dict[str, Any] = {"saved": saved}
        if errors:
            result["errors"] = errors
        return result

    async def extract_links(
        links: list[str],
        base_url: str,
        keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        same_domain = Fetcher.filter_same_domain(links, base_url)
        if keywords:
            lowered = [keyword.lower() for keyword in keywords]
            same_domain = [
                link for link in same_domain if any(keyword in link.lower() for keyword in lowered)
            ]
        return {"links": same_domain}

    return {
        "save_professors": save_professors,
        "extract_links": extract_links,
    }
