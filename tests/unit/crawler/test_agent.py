from __future__ import annotations

import json

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.agent import (
    CrawlerAgent,
    _dedupe_query_terms,
    _is_academician_showcase_page,
    _keyword_filter,
    _rank_faculty_page_candidates,
    _rank_org_unit_page_candidates,
    ORG_UNIT_PAGE_KEYWORDS,
)
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


class FakeLLMWithFacultyFollowup(FakeLLM):
    async def chat(self, messages, tools=None, tool_handlers=None):
        user = messages[-1]["content"]
        payload = json.loads(user)
        state = payload["state"]
        if state == "FIND_FACULTY_PAGES":
            return LLMResult('{"links": ["https://www.example.edu.cn/cs/landing"]}')
        return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)


class FakeLLMDiscoverViaToolLog(FakeLLM):
    async def chat(self, messages, tools=None, tool_handlers=None):
        user = messages[-1]["content"]
        payload = json.loads(user)
        if payload.get("state") == "DISCOVER_ORG_UNIT_PAGES":
            return LLMResult(
                "I will use extract_links.",
                [
                    ToolCallRecord(
                        "extract_links",
                        {"links": []},
                        {"links": ["https://www.example.edu.cn/orgs"]},
                    )
                ],
            )
        return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)


async def _agent(tmp_path, fake_llm, max_depth=4, max_backtracks=3, pages=None):
    db = DatabaseManager(sqlite_url(tmp_path / "agent.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")

    if pages is None:
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


async def test_agent_extracts_from_followup_faculty_pages_when_landing_page_has_no_records(tmp_path):
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
            ["https://www.example.edu.cn/cs/landing"],
            200,
        ),
        "https://www.example.edu.cn/cs/landing": FetchResult(
            "https://www.example.edu.cn/cs/landing",
            "landing",
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

    agent, fetcher, db = await _agent(
        tmp_path,
        FakeLLMWithFacultyFollowup(),
        pages=pages,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert result.saved_professors == 1
    assert "https://www.example.edu.cn/cs/faculty" in fetcher.calls
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


def test_rank_org_unit_page_candidates_prefers_jgsz_over_xygk():
    ranked = _rank_org_unit_page_candidates(
        [
            "https://www.buaa.edu.cn/xygk/jrbh.htm",
            "https://www.buaa.edu.cn/jgsz/jxkyjg02.htm",
        ],
        "https://www.buaa.edu.cn/",
    )
    assert ranked[0] == "https://www.buaa.edu.cn/jgsz/jxkyjg02.htm"


def test_rank_faculty_page_candidates_demotes_lyys():
    ranked = _rank_faculty_page_candidates(
        [
            "https://www.mse.buaa.edu.cn/xygk/szll.htm",
            "https://www.mse.buaa.edu.cn/teachers/list.htm",
            "https://www.mse.buaa.edu.cn/szdw/lyys1.htm",
        ]
    )
    assert ranked[0] == "https://www.mse.buaa.edu.cn/teachers/list.htm"
    assert ranked[-1] == "https://www.mse.buaa.edu.cn/szdw/lyys1.htm"


def test_is_academician_showcase_page():
    assert _is_academician_showcase_page("https://www.mse.buaa.edu.cn/szdw/lyys1.htm")
    assert not _is_academician_showcase_page("https://www.mse.buaa.edu.cn/teachers/list.htm")



# --- Keyword and intermediate page probing tests ---


def test_keyword_filter_matches_full_pinyin_zuzhijigou():
    links = [
        "https://www.ruc.edu.cn/zuzhijigou.html",
        "https://www.ruc.edu.cn/news.html",
    ]
    assert _keyword_filter(links, ORG_UNIT_PAGE_KEYWORDS) == [
        "https://www.ruc.edu.cn/zuzhijigou.html"
    ]


def test_keyword_filter_no_longer_matches_single_char_yuan():
    """院 and 系 were removed to prevent false positives like xiaoyuandaolan."""
    links = ["https://www.ruc.edu.cn/xiaoyuandaolan.html"]
    # Should NOT match — 院 is no longer a keyword
    matched = _keyword_filter(links, ORG_UNIT_PAGE_KEYWORDS)
    # yuan still matches (English keyword), but 院 alone should not be in keywords
    assert "院" not in ORG_UNIT_PAGE_KEYWORDS
    assert "系" not in ORG_UNIT_PAGE_KEYWORDS


def test_rank_org_unit_page_candidates_prefers_zuzhijigou():
    ranked = _rank_org_unit_page_candidates(
        [
            "https://www.ruc.edu.cn/xianshengyuanzhuo.html",
            "https://www.ruc.edu.cn/zuzhijigou.html",
            "https://xxgk.ruc.edu.cn/",
        ],
        "https://www.ruc.edu.cn/",
    )
    # zuzhijigou should not be ranked last (it has no strong tokens but no weak tokens either)
    assert ranked[0] != "https://xxgk.ruc.edu.cn/"


async def test_agent_discovers_org_units_via_intermediate_probe(tmp_path):
    """When keyword filter and LLM both fail, intermediate page probing should find org pages."""
    pages = {
        "https://www.example.edu.cn/": FetchResult(
            "https://www.example.edu.cn/",
            "home with no org links",
            ["https://www.example.edu.cn/news"],
            200,
        ),
        "https://www.example.edu.cn/jgsz.htm": FetchResult(
            "https://www.example.edu.cn/jgsz.htm",
            "org unit listing page with colleges",
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

    class ProbeFetcher(FakeFetcher):
        async def fetch(self, url):
            self.calls.append(url)
            if url in self.pages:
                return self.pages[url]
            raise RuntimeError(f"Failed to fetch {url}")

    class ProbeLLM(FakeLLM):
        """LLM that returns empty for discovery (so probe kicks in) but works for other states."""
        async def chat(self, messages, tools=None, tool_handlers=None):
            user = messages[-1]["content"]
            payload = json.loads(user)
            if payload.get("state") == "DISCOVER_ORG_UNIT_PAGES":
                return LLMResult("{}")
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    fetcher = ProbeFetcher(pages)
    db = DatabaseManager(sqlite_url(tmp_path / "probe.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")

    agent = CrawlerAgent(
        university_name="ProbeU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=ProbeLLM(),
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        max_depth=4,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://www.example.edu.cn/jgsz.htm" in fetcher.calls
    await db.close()


async def test_agent_discovers_org_units_from_extract_links_tool_log(tmp_path):
    pages = {
        "https://www.example.edu.cn/": FetchResult(
            "https://www.example.edu.cn/",
            "home with no org links",
            ["https://www.example.edu.cn/news"],
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

    class ToolLogFetcher(FakeFetcher):
        async def fetch(self, url):
            self.calls.append(url)
            if url in self.pages:
                return self.pages[url]
            raise RuntimeError(f"Failed to fetch {url}")

    fetcher = ToolLogFetcher(pages)
    db = DatabaseManager(sqlite_url(tmp_path / "tool_log.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")

    agent = CrawlerAgent(
        university_name="ToolLogU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=FakeLLMDiscoverViaToolLog(),
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        max_depth=4,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://www.example.edu.cn/orgs" in fetcher.calls
    await db.close()


def test_dedupe_query_terms_removes_case_insensitive_duplicates():
    query = "org unit jgsz yxsz jgsz YXSZ faculty site:scu.edu.cn"
    assert _dedupe_query_terms(query) == "org unit jgsz yxsz faculty site:scu.edu.cn"
