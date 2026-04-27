from __future__ import annotations

import json

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.agent import CrawlerAgent
from agents.crawler.fetcher import FetchResult, Fetcher
from agents.crawler.models import CrawlLogStatus, CrawlStatus, OrgUnit, UniversityMeta
from runtime.context import ContextManager
from runtime.database import DatabaseManager
from runtime.llm import LLMResult, ToolCallRecord
from runtime.skills import SkillManager
from tests.conftest import sqlite_url


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        return self.pages[url]

    def filter_same_domain(self, links, base_url):
        return Fetcher.filter_same_domain(links, base_url)


class FakeLLM:
    def __init__(self, empty_links: bool = False):
        self.empty_links = empty_links

    async def chat(self, messages, tools=None, tool_handlers=None):
        user = messages[-1]["content"]
        payload = json.loads(user)
        state = payload["state"]
        if self.empty_links:
            return LLMResult("{}")

        if state == "DISCOVER_ORG_UNIT_PAGES":
            return LLMResult('{"links": ["https://www.example.edu.cn/orgs"]}')
        if state == "EXTRACT_ORG_UNITS":
            return LLMResult(
                '{"org_units": [{"name": "CS", "url": "https://www.example.edu.cn/cs", "kind": "college"}]}'
            )
        if state == "FIND_FACULTY_PAGES":
            return LLMResult('{"links": ["https://www.example.edu.cn/cs/faculty"]}')
        if state == "EXTRACT_PROFESSORS":
            if "faculty" not in payload.get("page_text", ""):
                return LLMResult("{}")
            result = await tool_handlers["save_professors"](
                org_unit_name="CS",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=payload["url"],
                professors=[{"name": "Ada", "title": "Professor"}],
            )
            return LLMResult(
                "",
                [ToolCallRecord("save_professors", {"professors": []}, result)],
            )
        return LLMResult("{}")


class FakeLLMHomeAsOrgList(FakeLLM):
    async def chat(self, messages, tools=None, tool_handlers=None):
        user = messages[-1]["content"]
        payload = json.loads(user)
        if payload.get("state") == "DISCOVER_ORG_UNIT_PAGES":
            return LLMResult('{"links": ["https://www.example.edu.cn/"]}')
        return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)


async def _agent(tmp_path, fake_llm, max_depth=4, max_backtracks=3):
    db = DatabaseManager(sqlite_url(tmp_path / "agent.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")

    pages = {
        "https://www.example.edu.cn/": FetchResult(
            "https://www.example.edu.cn/",
            "home",
            ["https://www.example.edu.cn/orgs"],
            200,
        ),
        "https://www.example.edu.cn/orgs": FetchResult(
            "https://www.example.edu.cn/orgs",
            "org list",
            ["https://www.example.edu.cn/cs"],
            200,
        ),
        "https://www.example.edu.cn/cs": FetchResult(
            "https://www.example.edu.cn/cs",
            "cs",
            ["https://www.example.edu.cn/cs/faculty"],
            200,
        ),
        "https://www.example.edu.cn/cs/faculty": FetchResult(
            "https://www.example.edu.cn/cs/faculty",
            "faculty",
            [],
            200,
        ),
    }
    fetcher = FakeFetcher(pages)
    agent = CrawlerAgent(
        university_name="TestU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=fake_llm,
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        max_depth=max_depth,
        max_backtracks=max_backtracks,
    )
    return agent, fetcher, db


async def test_agent_state_machine_discovers_org_units_and_saves_professors(tmp_path):
    agent, fetcher, db = await _agent(tmp_path, FakeLLM())
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert result.saved_professors == 1
    assert fetcher.calls.count("https://www.example.edu.cn/cs") == 1

    async with db.session() as session:
        meta = (await session.execute(select(UniversityMeta))).scalar_one()
        assert meta.crawl_status == CrawlStatus.COMPLETED.value
        units = (await session.execute(select(OrgUnit))).scalars().all()
        assert [u.name for u in units] == ["CS"]

    await db.close()


async def test_agent_respects_max_depth(tmp_path):
    agent, fetcher, db = await _agent(tmp_path, FakeLLM(), max_depth=0)
    result = await agent.run()

    assert result.status == CrawlStatus.FAILED.value
    assert "https://www.example.edu.cn/orgs" not in fetcher.calls
    await db.close()


async def test_agent_marks_failed_when_backtrack_limit_exceeded(tmp_path):
    agent, _fetcher, db = await _agent(
        tmp_path,
        FakeLLM(empty_links=True),
        max_backtracks=0,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.FAILED.value
    await db.close()


async def test_agent_can_reuse_homepage_when_org_unit_page_is_home(tmp_path):
    agent, fetcher, db = await _agent(tmp_path, FakeLLMHomeAsOrgList())
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert fetcher.calls.count("https://www.example.edu.cn/") == 1
    await db.close()


async def test_agent_refetches_successful_urls_for_incomplete_university(tmp_path):
    agent, fetcher, db = await _agent(tmp_path, FakeLLM())
    async with db.session() as session:
        await crawler_db.ensure_university_meta(
            session,
            name="TestU",
            start_url="https://www.example.edu.cn/",
            location="TestCity",
        )
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/",
            CrawlLogStatus.SUCCESS,
            "previous partial run",
        )
        await crawler_db.set_university_status(session, CrawlStatus.FAILED)

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://www.example.edu.cn/" in fetcher.calls
    await db.close()
