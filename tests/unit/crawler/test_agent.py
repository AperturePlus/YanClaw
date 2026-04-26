from __future__ import annotations

import json

from agents.crawler.agent import CrawlerAgent
from agents.crawler import db as crawler_db
from agents.crawler.fetcher import FetchResult, Fetcher
from agents.crawler.models import CrawlLogStatus, CrawlStatus
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
    def __init__(self, empty_links=False, reflect_update=False):
        self.empty_links = empty_links
        self.reflect_update = reflect_update

    async def chat(self, messages, tools=None, tool_handlers=None):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "Select relevant skill names" in system:
            return LLMResult('["extract-links", "save-professors"]')
        if "Reflect on the crawl" in system:
            if self.reflect_update:
                await tool_handlers["update_skill"](
                    name="save-professors",
                    new_content="## Goal\nupdated\n",
                    change_summary="reflection update",
                )
            return LLMResult("")

        payload = json.loads(user)
        state = payload["state"]
        if self.empty_links:
            return LLMResult("{}")
        if state == "FIND_COLLEGES":
            return LLMResult('{"links": ["https://www.example.edu.cn/cs"]}')
        if state == "FIND_FACULTY_PAGES":
            return LLMResult('{"links": ["https://www.example.edu.cn/cs/faculty"]}')
        if state == "EXTRACT_PROFESSORS":
            result = await tool_handlers["save_professors"](
                university_name="TestU",
                college_name="CS",
                professors=[{"name": "Ada", "title": "Professor"}],
            )
            return LLMResult(
                "",
                [ToolCallRecord("save_professors", {"professors": []}, result)],
            )
        return LLMResult("{}")


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
            ["https://www.example.edu.cn/cs", "https://www.example.edu.cn/cs"],
            200,
        ),
        "https://www.example.edu.cn/cs": FetchResult(
            "https://www.example.edu.cn/cs",
            "college",
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
        db=db,
        llm_client=fake_llm,
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        max_depth=max_depth,
        max_backtracks=max_backtracks,
    )
    return agent, fetcher, db, manager


async def test_agent_state_machine_dedupes_and_saves_professors(tmp_path):
    agent, fetcher, db, _manager = await _agent(tmp_path, FakeLLM())
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert result.saved_professors == 1
    assert fetcher.calls.count("https://www.example.edu.cn/cs") == 1
    await db.close()


async def test_agent_respects_max_depth(tmp_path):
    agent, fetcher, db, _manager = await _agent(tmp_path, FakeLLM(), max_depth=0)
    result = await agent.run()

    assert result.status == CrawlStatus.FAILED.value
    assert "https://www.example.edu.cn/cs" not in fetcher.calls
    await db.close()


async def test_agent_marks_failed_when_backtrack_limit_exceeded(tmp_path):
    agent, _fetcher, db, _manager = await _agent(
        tmp_path,
        FakeLLM(empty_links=True),
        max_backtracks=0,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.FAILED.value
    await db.close()


async def test_agent_reflect_can_update_skill(tmp_path):
    agent, _fetcher, db, manager = await _agent(tmp_path, FakeLLM(reflect_update=True))
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    history = await manager.get_history("save-professors")
    assert [item.version for item in history] == [1, 2]
    assert "updated" in manager.load_skill("save-professors")
    await db.close()


async def test_agent_refetches_successful_urls_for_incomplete_university(tmp_path):
    agent, fetcher, db, _manager = await _agent(tmp_path, FakeLLM())
    async with db.session() as session:
        university = await crawler_db.get_or_create_university(
            session,
            "TestU",
            "https://www.example.edu.cn/",
        )
        await crawler_db.log_crawl(
            session,
            university.id,
            "https://www.example.edu.cn/",
            CrawlLogStatus.SUCCESS,
            "previous partial run",
        )
        await crawler_db.set_university_status(session, "TestU", CrawlStatus.FAILED)

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://www.example.edu.cn/" in fetcher.calls
    await db.close()
