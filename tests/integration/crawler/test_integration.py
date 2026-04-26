from __future__ import annotations

import json

import httpx
from sqlalchemy import select

from agents.crawler.config import CrawlerSettings
from agents.crawler.dispatcher import CrawlDispatcher
from agents.crawler.fetcher import Fetcher
from agents.crawler.models import CrawlLog, Professor, University
from runtime.database import DatabaseManager, SkillVersion
from runtime.llm import LLMResult, ToolCallRecord
from tests.conftest import sqlite_url


class IntegrationLLM:
    async def chat(self, messages, tools=None, tool_handlers=None):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "Select relevant skill names" in system:
            return LLMResult('["extract-links", "save-professors"]')
        if "Reflect on the crawl" in system:
            await tool_handlers["update_skill"](
                name="save-professors",
                new_content="## Goal\nSave professor records with source-aware validation.\n",
                change_summary="integration reflection",
            )
            return LLMResult("")

        payload = json.loads(user)
        state = payload["state"]
        if state == "FIND_COLLEGES":
            return LLMResult('{"links": ["https://www.example.edu.cn/cs"]}')
        if state == "FIND_FACULTY_PAGES":
            return LLMResult('{"links": ["https://www.example.edu.cn/cs/faculty"]}')
        if state == "EXTRACT_PROFESSORS":
            result = await tool_handlers["save_professors"](
                university_name="TestU",
                college_name="CS",
                professors=[
                    {
                        "name": "Ada",
                        "title": "Professor",
                        "research_areas": ["Systems"],
                        "email": "ada@example.edu.cn",
                    }
                ],
            )
            return LLMResult(
                "",
                [ToolCallRecord("save_professors", {}, result)],
            )
        return LLMResult("{}")


async def test_dispatcher_agent_fetcher_llm_db_integration(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,TestCity\n",
        encoding="utf-8",
    )
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "extract-links.md").write_text(
        "---\nname: extract-links\ndescription: links\nversion: 1\ncreated_at: 2026-04-26T00:00:00\nupdated_at: 2026-04-26T00:00:00\n---\n\n## Goal\nlinks\n",
        encoding="utf-8",
    )
    (skills_dir / "save-professors.md").write_text(
        "---\nname: save-professors\ndescription: save\nversion: 1\ncreated_at: 2026-04-26T00:00:00\nupdated_at: 2026-04-26T00:00:00\n---\n\n## Goal\nsave\n",
        encoding="utf-8",
    )

    pages = {
        "https://www.example.edu.cn/": '<a href="/cs">CS</a>',
        "https://www.example.edu.cn/cs": '<a href="/cs/faculty">Faculty</a>',
        "https://www.example.edu.cn/cs/faculty": "<p>Ada Professor Systems ada@example.edu.cn</p>",
    }

    async def handler(request):
        return httpx.Response(200, text=pages[str(request.url)], request=request)

    settings = CrawlerSettings(
        database_url=sqlite_url(tmp_path / "integration.db"),
        websites_path=websites,
        crawler_skills_dir=skills_dir,
        request_interval_seconds=0,
        max_retries=0,
        max_concurrency=1,
    )
    db = DatabaseManager(settings.database_url)
    dispatcher = CrawlDispatcher(
        settings=settings,
        db=db,
        llm_client_factory=lambda: IntegrationLLM(),
        fetcher_factory=lambda: Fetcher(
            request_interval_seconds=0,
            max_retries=0,
            transport=httpx.MockTransport(handler),
        ),
    )

    summary = await dispatcher.run()
    assert summary.success == 1

    async with db.session() as session:
        universities = (await session.execute(select(University))).scalars().all()
        professors = (await session.execute(select(Professor))).scalars().all()
        logs = (await session.execute(select(CrawlLog))).scalars().all()
        versions = (await session.execute(select(SkillVersion))).scalars().all()
        assert universities[0].crawl_status == "completed"
        assert professors[0].name == "Ada"
        assert len(logs) == 3
        assert versions[0].skill_name == "save-professors"

    second = await dispatcher.run()
    assert second.skipped == 1

    await db.close()
