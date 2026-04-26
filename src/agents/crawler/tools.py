from __future__ import annotations

from typing import Any

from agents.crawler import db as crawler_db
from agents.crawler.fetcher import Fetcher
from runtime.database import DatabaseManager
from runtime.skills import SkillManager


SAVE_PROFESSORS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "save_professors",
        "description": "Persist extracted professor records to the crawler database.",
        "parameters": {
            "type": "object",
            "properties": {
                "university_name": {"type": "string"},
                "college_name": {"type": "string"},
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
            "required": ["university_name", "college_name", "professors"],
        },
    },
}

EXTRACT_LINKS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
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
    },
}

UPDATE_SKILL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_skill",
        "description": "Update an existing skill. The runtime stores the old version first.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "new_content": {"type": "string"},
                "change_summary": {"type": "string"},
            },
            "required": ["name", "new_content", "change_summary"],
        },
    },
}

CREATE_SKILL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "create_skill",
        "description": "Create a new markdown skill.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "content": {"type": "string"},
                "description": {"type": "string"},
                "change_summary": {"type": "string"},
            },
            "required": ["name", "content", "description"],
        },
    },
}


def get_crawler_tool_definitions(include_skill_tools: bool = True) -> list[dict[str, Any]]:
    tools = [SAVE_PROFESSORS_TOOL, EXTRACT_LINKS_TOOL]
    if include_skill_tools:
        tools.extend([UPDATE_SKILL_TOOL, CREATE_SKILL_TOOL])
    return tools


def get_crawler_tools(
    db: DatabaseManager,
    skill_manager: SkillManager,
) -> dict[str, Any]:
    async def save_professors(
        university_name: str,
        college_name: str,
        professors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        saved = 0
        errors: list[str] = []
        for professor in professors:
            try:
                async with db.session() as session:
                    data = {
                        **professor,
                        "university_name": university_name,
                        "college_name": college_name,
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

    async def update_skill(name: str, new_content: str, change_summary: str) -> dict[str, Any]:
        version = await skill_manager.update_skill(name, new_content, change_summary)
        return {"name": name, "version": version}

    async def create_skill(
        name: str,
        content: str,
        description: str,
        change_summary: str = "Created by crawler reflection",
    ) -> dict[str, Any]:
        await skill_manager.create_skill(name, content, description, change_summary)
        return {"name": name, "version": 1}

    return {
        "save_professors": save_professors,
        "extract_links": extract_links,
        "update_skill": update_skill,
        "create_skill": create_skill,
    }
