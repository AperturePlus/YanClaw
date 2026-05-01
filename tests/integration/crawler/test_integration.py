from __future__ import annotations

import json
from pathlib import Path

import httpx
from sqlalchemy import select

from functools import partial

from agents.crawler.agent import CrawlerAgent
from agents.crawler.config import CrawlerSettings
from agents.crawler.dispatcher import CrawlDispatcher
from agents.crawler.fetchers import Fetcher, _site_root
from agents.crawler.models import CrawlLog, OrgUnit, Professor, UniversityMeta
from runtime.database import DatabaseManager
from runtime.llm import LLMResult, ToolCallRecord


class IntegrationLLM:
    async def chat(self, messages, tools=None, tool_handlers=None):
        user = messages[-1]["content"]
        payload = json.loads(user)
        state = payload["state"]

        if state == "DISCOVER_ORG_UNIT_PAGES":
            return LLMResult('{"links": ["https://www.example.edu.cn/orgs"]}')
        if state == "EXTRACT_ORG_UNITS":
            return LLMResult(
                '{"org_units": [{"name": "CS", "url": "https://www.example.edu.cn/cs", "kind": "college"}]}'
            )
        if state == "FIND_FACULTY_PAGES":
            return LLMResult('{"links": ["https://www.example.edu.cn/cs/faculty"]}')
        if state == "EXTRACT_PROFESSORS":
            result = await tool_handlers["save_professors"](
                org_unit_name="CS",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=payload["url"],
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


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


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
        "https://www.example.edu.cn/": '<html><body><h1>Example University</h1><a href="/orgs">Org Units</a><p>Welcome to Example University homepage with enough content to pass validation checks.</p></body></html>',
        "https://www.example.edu.cn/orgs": '<html><body><h1>Academic Units</h1><a href="/cs">Computer Science</a><p>List of all academic departments and colleges at Example University with sufficient page content.</p></body></html>',
        "https://www.example.edu.cn/cs": '<html><body><h1>CS Department</h1><a href="/cs/faculty">Faculty List</a><p>Computer Science department page with information about programs and research areas.</p></body></html>',
        "https://www.example.edu.cn/cs/faculty": "<html><body><h1>Faculty</h1><p>Ada Professor Systems ada@example.edu.cn. Our faculty members are leaders in their fields.</p></body></html>",
    }

    async def handler(request):
        return httpx.Response(
            200,
            text=pages[str(request.url)],
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    settings = CrawlerSettings(
        websites_path=websites,
        crawler_skills_dir=skills_dir,
        university_db_dir=tmp_path / "universities",
        request_interval_seconds=0,
        max_retries=0,
        max_concurrency=1,
    )
    dispatcher = CrawlDispatcher(
        settings=settings,
        agent_factory=partial(CrawlerAgent, min_org_units=1),
        llm_client_factory=lambda: IntegrationLLM(),
        fetcher_factory=lambda: Fetcher(
            request_interval_seconds=0,
            max_retries=0,
            transport=httpx.MockTransport(handler),
        ),
    )

    summary = await dispatcher.run()
    assert summary.success == 1

    root = _site_root("www.example.edu.cn")
    db_path = (Path(settings.university_db_dir) / f"{root}.db").resolve()
    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        meta = (await session.execute(select(UniversityMeta))).scalar_one()
        professors = (await session.execute(select(Professor))).scalars().all()
        units = (await session.execute(select(OrgUnit))).scalars().all()
        logs = (await session.execute(select(CrawlLog))).scalars().all()

        assert meta.crawl_status == "completed"
        assert professors[0].name == "Ada"
        assert [u.name for u in units] == ["CS"]
        assert len(logs) == 4

    second = await dispatcher.run(resume=True)
    assert second.skipped == 1

    await db.close()

