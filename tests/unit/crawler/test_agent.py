from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.agent import (
    CrawlerAgent,
    _QueuedUrl,
    _dedupe_query_terms,
    _is_core_academic_kind,
    _is_academician_showcase_page,
    _keyword_filter,
    _org_unit_faculty_priority,
    _rank_faculty_page_candidates,
    _rank_org_unit_page_candidates,
    ORG_UNIT_PAGE_KEYWORDS,
)
from agents.crawler.fetchers import FetchResult, Fetcher
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
        min_org_units=1,
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


async def test_agent_bypasses_cross_run_dedup_when_all_org_pages_are_history(tmp_path):
    agent, fetcher, db = await _agent(tmp_path, FakeLLM())
    async with db.session() as session:
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/orgs",
            CrawlLogStatus.SUCCESS,
            "seeded-history",
        )

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert result.saved_professors == 1
    assert agent.backtrack_count == 0
    assert fetcher.calls.count("https://www.example.edu.cn/orgs") == 1
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


async def test_agent_still_follows_sub_faculty_links_after_saving_from_parent_page(tmp_path):
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
            "faculty landing",
            ["https://www.example.edu.cn/cs/software"],
            200,
        ),
        "https://www.example.edu.cn/cs/software": FetchResult(
            "https://www.example.edu.cn/cs/software",
            "faculty software",
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
    assert result.saved_professors == 2
    assert "https://www.example.edu.cn/cs/software" in fetcher.calls
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
            "org unit listing page with colleges " + "x" * 200,
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
        min_org_units=1,
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
        min_org_units=1,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://www.example.edu.cn/orgs" in fetcher.calls
    await db.close()


def test_dedupe_query_terms_removes_case_insensitive_duplicates():
    query = "org unit jgsz yxsz jgsz YXSZ faculty site:scu.edu.cn"
    assert _dedupe_query_terms(query) == "org unit jgsz yxsz faculty site:scu.edu.cn"


def test_keyword_filter_matches_uestc_xybm_jxkydw():
    links = [
        "https://www.uestc.edu.cn/xybm/jxkydw_yjjg.htm",
        "https://www.uestc.edu.cn/xxgk/xxjj.htm",
    ]
    matched = _keyword_filter(links, ORG_UNIT_PAGE_KEYWORDS)
    assert "https://www.uestc.edu.cn/xybm/jxkydw_yjjg.htm" in matched


def test_rank_org_unit_page_candidates_prefers_jxkydw_over_xxgk():
    ranked = _rank_org_unit_page_candidates(
        [
            "https://www.uestc.edu.cn/xxgk/xxjj.htm",
            "https://www.uestc.edu.cn/xybm/jxkydw_yjjg.htm",
            "https://xxgkw.uestc.edu.cn/",
        ],
        "https://www.uestc.edu.cn/",
    )
    assert ranked[0] == "https://www.uestc.edu.cn/xybm/jxkydw_yjjg.htm"


async def test_agent_extract_org_units_follows_llm_next_url_hint(tmp_path):
    pages = {
        "https://www.example.edu.cn/": FetchResult(
            "https://www.example.edu.cn/",
            "home",
            ["https://www.example.edu.cn/xxgk/xxjj.htm"],
            200,
        ),
        "https://www.example.edu.cn/xxgk/xxjj.htm": FetchResult(
            "https://www.example.edu.cn/xxgk/xxjj.htm",
            "overview",
            ["https://www.example.edu.cn/xybm/jxkydw_yjjg.htm"],
            200,
        ),
        "https://www.example.edu.cn/xybm/jxkydw_yjjg.htm": FetchResult(
            "https://www.example.edu.cn/xybm/jxkydw_yjjg.htm",
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

    class FollowupLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            state = payload.get("state")
            url = payload.get("url", "")
            if state == "EXTRACT_ORG_UNITS" and url.endswith("/xxgk/xxjj.htm"):
                return LLMResult(
                    '{"org_units": [], "next_url": "https://www.example.edu.cn/xybm/jxkydw_yjjg.htm"}'
                )
            if state == "EXTRACT_ORG_UNITS" and url.endswith("/xybm/jxkydw_yjjg.htm"):
                return LLMResult(
                    '{"org_units": [{"name": "CS", "url": "https://www.example.edu.cn/cs", "kind": "college"}]}'
                )
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    class FollowupFetcher(FakeFetcher):
        async def fetch(self, url):
            self.calls.append(url)
            if url in self.pages:
                return self.pages[url]
            raise RuntimeError(f"Failed to fetch {url}")

    fetcher = FollowupFetcher(pages)
    db = DatabaseManager(sqlite_url(tmp_path / "followup.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")

    agent = CrawlerAgent(
        university_name="FollowupU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=FollowupLLM(),
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        max_depth=4,
        min_org_units=1,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://www.example.edu.cn/xybm/jxkydw_yjjg.htm" in fetcher.calls
    await db.close()


def test_is_core_academic_kind_and_priority():
    assert _is_core_academic_kind("college")
    assert not _is_core_academic_kind("research_institute")

    start_host = "www.uestc.edu.cn"
    college = SimpleNamespace(kind="college", url="https://www.ese.uestc.edu.cn/", id=1)
    research_detail = SimpleNamespace(
        kind="research_institute",
        url="https://www.rd.uestc.edu.cn/info/1009/1030.htm",
        id=2,
    )
    assert _org_unit_faculty_priority(college, start_host) < _org_unit_faculty_priority(
        research_detail, start_host
    )


def test_org_unit_priority_prefers_computing_and_electronics():
    start_host = "www.scu.edu.cn"
    generic_college = SimpleNamespace(
        name="History College",
        kind="college",
        url="https://history.scu.edu.cn/",
        id=1,
    )
    software_college = SimpleNamespace(
        name="School of Software",
        kind="college",
        url="https://software.scu.edu.cn/",
        id=2,
    )
    ece_college = SimpleNamespace(
        name="School of Electronic Information",
        kind="college",
        url="https://eie.scu.edu.cn/",
        id=3,
    )

    assert _org_unit_faculty_priority(software_college, start_host) < _org_unit_faculty_priority(
        generic_college, start_host
    )
    assert _org_unit_faculty_priority(ece_college, start_host) < _org_unit_faculty_priority(
        generic_college, start_host
    )


async def test_extract_org_units_keeps_processing_candidates_after_minimum_core_units(tmp_path):
    pages = {
        "https://www.example.edu.cn/xybm/bm.htm": FetchResult(
            "https://www.example.edu.cn/xybm/bm.htm",
            "bm",
            ["https://www.example.edu.cn/xybm/jxkydw_yjjg.htm"],
            200,
        ),
        "https://www.example.edu.cn/xybm/jxkydw_yjjg.htm": FetchResult(
            "https://www.example.edu.cn/xybm/jxkydw_yjjg.htm",
            "core list",
            [
                "https://www.example.edu.cn/cs",
                "https://www.example.edu.cn/ee",
                "https://www.example.edu.cn/math",
            ],
            200,
        ),
        "https://www.example.edu.cn/xxgk/xxjj.htm": FetchResult(
            "https://www.example.edu.cn/xxgk/xxjj.htm",
            "overview",
            [],
            200,
        ),
    }

    class EarlyStopLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            state = payload.get("state")
            url = payload.get("url", "")
            if state == "EXTRACT_ORG_UNITS" and url.endswith("/xybm/bm.htm"):
                return LLMResult(
                    '{"org_units": [{"name": "教学科研单位、研究机构", "url": "https://www.example.edu.cn/xybm/jxkydw_yjjg.htm", "kind": "category"}]}'
                )
            if state == "EXTRACT_ORG_UNITS" and url.endswith("/xybm/jxkydw_yjjg.htm"):
                return LLMResult(
                    '{"org_units": ['
                    '{"name": "CS", "url": "https://www.example.edu.cn/cs", "kind": "college"},'
                    '{"name": "EE", "url": "https://www.example.edu.cn/ee", "kind": "college"},'
                    '{"name": "Math", "url": "https://www.example.edu.cn/math", "kind": "school"}'
                    ']}'
                )
            return LLMResult('{"org_units": []}')

    fetcher = FakeFetcher(pages)
    db = DatabaseManager(sqlite_url(tmp_path / "early_stop.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")

    agent = CrawlerAgent(
        university_name="EarlyStopU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=EarlyStopLLM(),
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        max_depth=4,
        min_org_units=2,
    )

    units = await agent._extract_org_units(
        [
            _QueuedUrl(url="https://www.example.edu.cn/xybm/bm.htm", depth=1),
            _QueuedUrl(url="https://www.example.edu.cn/xybm/jxkydw_yjjg.htm", depth=1),
            _QueuedUrl(url="https://www.example.edu.cn/xxgk/xxjj.htm", depth=1),
        ]
    )

    names = {u.name for u in units}
    assert {"CS", "EE", "Math"} <= names
    # Do not stop after only reaching a low minimum; continue scanning candidates for better coverage.
    assert "https://www.example.edu.cn/xxgk/xxjj.htm" in fetcher.calls
    await db.close()


async def test_detail_profile_links_are_scoped_to_same_host_and_related_paths(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    links = [
        "https://www.example.edu.cn/szdw/zzjs1/jjx.htm",
        "https://www.example.edu.cn/gywm/jxdw1/jjx.htm",
        "https://sub.example.edu.cn/info/1012/3958.htm",
        "https://www.example.edu.cn/news/1234.htm",
    ]
    out = agent._extract_detail_profile_links(links, "https://www.example.edu.cn/szdw.htm")
    assert "https://www.example.edu.cn/szdw/zzjs1/jjx.htm" in out
    assert "https://www.example.edu.cn/gywm/jxdw1/jjx.htm" not in out
    assert "https://sub.example.edu.cn/info/1012/3958.htm" not in out
    assert "https://www.example.edu.cn/news/1234.htm" not in out
    await db.close()


async def test_followup_faculty_links_filter_noise_sections(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    links = [
        "https://www.example.edu.cn/szdw/zzjs1.htm",
        "https://www.example.edu.cn/djgz1/lilubn/zzxx.htm",
        "https://www.example.edu.cn/rcpy/sys.htm",
        "https://sub.example.edu.cn/szdw/xx.htm",
    ]
    out = agent._extract_followup_faculty_links(links, "https://www.example.edu.cn/szdw.htm")
    assert "https://www.example.edu.cn/szdw/zzjs1.htm" in out
    assert "https://www.example.edu.cn/djgz1/lilubn/zzxx.htm" not in out
    assert "https://www.example.edu.cn/rcpy/sys.htm" not in out
    assert "https://sub.example.edu.cn/szdw/xx.htm" not in out
    await db.close()
