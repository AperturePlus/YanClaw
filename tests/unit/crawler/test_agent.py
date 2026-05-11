from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.agent import (
    CrawlerState,
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
from agents.crawler.models import CrawlExtractionFailure, CrawlLogStatus, CrawlStatus, CrawlTask, CrawlTaskStatus, OrgUnit, UniversityMeta
from runtime.context import ContextManager
from runtime.database import DatabaseManager
from runtime.llm import LLMResult, ToolCallErrorRecord, ToolCallRecord
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


class FakeHumanFetcher(FakeFetcher):
    def set_context(self, *args, **kwargs):
        return None

    def set_status_provider(self, *args, **kwargs):
        return None


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


async def test_agent_pipeline_retries_invalid_json_once_then_saves(tmp_path):
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
            "faculty profile list",
            [],
            200,
        ),
    }

    class RetryLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.extract_calls = 0

        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            state = payload["state"]
            if state == "EXTRACT_PROFESSORS":
                self.extract_calls += 1
                if self.extract_calls == 1:
                    return LLMResult(
                        "",
                        [],
                        [ToolCallErrorRecord("save_professors", '{"org_unit_name":"CS","enrollment_pre', "invalid_json")],
                    )
                result = await tool_handlers["save_professors"](
                    org_unit_name="CS",
                    org_unit_url="https://www.example.edu.cn/cs",
                    source_url=payload["url"],
                    professors=[{"name": "Ada", "title": "Professor"}],
                )
                return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    fetcher = FakeFetcher(pages)
    db = DatabaseManager(sqlite_url(tmp_path / "retry_once.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")

    agent = CrawlerAgent(
        university_name="RetryU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=RetryLLM(),
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        min_org_units=1,
        invalid_json_max_retry=1,
    )
    result = await agent.run()
    assert result.status == CrawlStatus.COMPLETED.value
    assert result.saved_professors == 1

    async with db.session() as session:
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
        assert any(f.failure_type == "invalid_json" and f.resolver == "retry" for f in failures)
    await db.close()


async def test_agent_build_llm_payload_trims_links_and_visited_fields(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    agent.visited_urls = {f"https://www.example.edu.cn/v/{i}" for i in range(100)}
    links = [f"https://www.example.edu.cn/path/{i}" for i in range(120)]

    discover_content, _ = agent._build_llm_payload(
        state=CrawlerState.DISCOVER_ORG_UNIT_PAGES,
        instruction="discover",
        url="https://www.example.edu.cn/",
        page_text="faculty list",
        links=links,
        allowed_tools={"extract_links"},
    )
    discover_payload = json.loads(discover_content)
    assert "visited_urls" in discover_payload
    assert "visited_count" in discover_payload
    assert len(discover_payload["links"]) <= 40

    org_content, _ = agent._build_llm_payload(
        state=CrawlerState.EXTRACT_ORG_UNITS,
        instruction="org",
        url="https://www.example.edu.cn/orgs",
        page_text="org page text",
        links=links,
        allowed_tools=set(),
    )
    org_payload = json.loads(org_content)
    assert "visited_urls" not in org_payload
    assert "visited_count" in org_payload
    assert len(org_payload["links"]) <= 40

    faculty_content, _ = agent._build_llm_payload(
        state=CrawlerState.FIND_FACULTY_PAGES,
        instruction="faculty",
        url="https://www.example.edu.cn/cs",
        page_text="faculty directory",
        links=links,
        allowed_tools={"extract_links"},
    )
    faculty_payload = json.loads(faculty_content)
    assert len(faculty_payload["links"]) <= 30

    extract_content, _ = agent._build_llm_payload(
        state=CrawlerState.EXTRACT_PROFESSORS,
        instruction="extract",
        url="https://www.example.edu.cn/cs/faculty",
        page_text="faculty profile",
        links=links,
        allowed_tools={"save_professors"},
    )
    extract_payload = json.loads(extract_content)
    assert extract_payload["links"] == []
    await db.close()


async def test_agent_skips_noise_page_llm_but_keeps_followups(tmp_path):
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
            ["https://www.example.edu.cn/cs/szdw/faculty_entry.htm"],
            200,
        ),
        "https://www.example.edu.cn/cs/szdw/faculty_entry.htm": FetchResult(
            "https://www.example.edu.cn/cs/szdw/faculty_entry.htm",
            "通知 公告 人事 政策",
            ["https://www.example.edu.cn/cs/szdw/faculty.htm"],
            200,
        ),
        "https://www.example.edu.cn/cs/szdw/faculty.htm": FetchResult(
            "https://www.example.edu.cn/cs/szdw/faculty.htm",
            "faculty",
            [],
            200,
        ),
    }

    class TrackExtractUrlsLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.extract_urls: list[str] = []

        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "FIND_FACULTY_PAGES":
                return LLMResult('{"links": ["https://www.example.edu.cn/cs/szdw/faculty_entry.htm"]}')
            if payload.get("state") == "EXTRACT_PROFESSORS":
                self.extract_urls.append(payload.get("url", ""))
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    fetcher = FakeFetcher(pages)
    db = DatabaseManager(sqlite_url(tmp_path / "skip_noise.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")
    llm = TrackExtractUrlsLLM()

    agent = CrawlerAgent(
        university_name="SkipNoiseU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=llm,
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        min_org_units=1,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert result.saved_professors == 1
    assert "https://www.example.edu.cn/cs/szdw/faculty_entry.htm" not in llm.extract_urls
    assert "https://www.example.edu.cn/cs/szdw/faculty.htm" in fetcher.calls
    assert int(agent._pipeline_stats.get("llm_calls_skipped_by_gate", 0)) >= 1
    await db.close()


async def test_agent_rejects_teacher_platform_and_sibling_faculty_domains(tmp_path):
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
            [
                "https://teacher.example.edu.cn/",
                "https://www.example.edu.cn/cs/faculty",
                "https://math.example.edu.cn/faculty",
            ],
            200,
        ),
        "https://www.example.edu.cn/cs/faculty": FetchResult(
            "https://www.example.edu.cn/cs/faculty",
            "faculty list",
            [],
            200,
        ),
    }

    agent, fetcher, db = await _agent(tmp_path, FakeLLM(), pages=pages)
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert result.saved_professors == 1
    assert "https://teacher.example.edu.cn/" not in fetcher.calls
    assert "https://math.example.edu.cn/faculty" not in fetcher.calls
    assert "https://www.example.edu.cn/cs/faculty" in fetcher.calls
    await db.close()


async def test_agent_rejects_sibling_subdomain_links_for_subdomain_org_unit(tmp_path):
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
            ["https://soft.example.edu.cn/"],
            200,
        ),
        "https://soft.example.edu.cn/": FetchResult(
            "https://soft.example.edu.cn/",
            "soft",
            [
                "https://soft.example.edu.cn/faculty",
                "https://scse.example.edu.cn/faculty",
            ],
            200,
        ),
        "https://soft.example.edu.cn": FetchResult(
            "https://soft.example.edu.cn/",
            "soft",
            [
                "https://soft.example.edu.cn/faculty",
                "https://scse.example.edu.cn/faculty",
            ],
            200,
        ),
        "https://soft.example.edu.cn/faculty": FetchResult(
            "https://soft.example.edu.cn/faculty",
            "faculty list",
            [],
            200,
        ),
    }

    class SubdomainOrgUnitLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "EXTRACT_ORG_UNITS":
                return LLMResult(
                    '{"org_units": [{"name": "软件学院", "url": "https://soft.example.edu.cn/", "kind": "college"}]}'
                )
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, fetcher, db = await _agent(tmp_path, SubdomainOrgUnitLLM(), pages=pages)
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://soft.example.edu.cn/faculty" in fetcher.calls
    assert "https://scse.example.edu.cn/faculty" not in fetcher.calls
    await db.close()


async def test_agent_skips_find_faculty_llm_on_low_info_page(tmp_path):
    pages = {
        "https://www.example.edu.cn/": FetchResult(
            "https://www.example.edu.cn/",
            "home faculty",
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
            "x",
            [],
            200,
        ),
    }

    class TrackStatesLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.states: list[str] = []

        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            self.states.append(payload.get("state", ""))
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    llm = TrackStatesLLM()
    agent, _fetcher, db = await _agent(tmp_path, llm, pages=pages)
    await agent.run()

    assert "FIND_FACULTY_PAGES" not in llm.states
    await db.close()


async def test_agent_rejects_login_and_news_candidates_and_drops_elite_subset(tmp_path):
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
            ["https://scse.example.edu.cn/"],
            200,
        ),
        "https://scse.example.edu.cn/": FetchResult(
            "https://scse.example.edu.cn/",
            "cs",
            [
                "https://scse.example.edu.cn/xw_list_new.jsp?urltype=tree.TreeTempUrl&wbtreeid=1396",
                "https://scse.example.edu.cn/system/resource/tplloginaccount.jsp?owner=1756449315",
                "https://scse.example.edu.cn/szdw/teacher_list.htm",
                "https://scse.example.edu.cn/szdw/professor.htm",
                "https://scse.example.edu.cn/szdw/distinguished.htm",
            ],
            200,
        ),
        "https://scse.example.edu.cn": FetchResult(
            "https://scse.example.edu.cn/",
            "cs",
            [
                "https://scse.example.edu.cn/xw_list_new.jsp?urltype=tree.TreeTempUrl&wbtreeid=1396",
                "https://scse.example.edu.cn/system/resource/tplloginaccount.jsp?owner=1756449315",
                "https://scse.example.edu.cn/szdw/teacher_list.htm",
                "https://scse.example.edu.cn/szdw/professor.htm",
                "https://scse.example.edu.cn/szdw/distinguished.htm",
            ],
            200,
        ),
        "https://scse.example.edu.cn/szdw/teacher_list.htm": FetchResult(
            "https://scse.example.edu.cn/szdw/teacher_list.htm",
            "faculty list",
            [],
            200,
        ),
        "https://scse.example.edu.cn/szdw/professor.htm": FetchResult(
            "https://scse.example.edu.cn/szdw/professor.htm",
            "faculty profile list",
            [],
            200,
        ),
    }

    class BuaaLikeOrgLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "EXTRACT_ORG_UNITS":
                return LLMResult(
                    '{"org_units": [{"name": "计算机学院", "url": "https://scse.example.edu.cn/", "kind": "college"}]}'
                )
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, fetcher, db = await _agent(tmp_path, BuaaLikeOrgLLM(), pages=pages)
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://scse.example.edu.cn/xw_list_new.jsp?urltype=tree.TreeTempUrl&wbtreeid=1396" not in fetcher.calls
    assert "https://scse.example.edu.cn/system/resource/tplloginaccount.jsp?owner=1756449315" not in fetcher.calls
    assert "https://scse.example.edu.cn/szdw/teacher_list.htm" in fetcher.calls
    assert "https://scse.example.edu.cn/szdw/professor.htm" in fetcher.calls
    assert "https://scse.example.edu.cn/szdw/distinguished.htm" not in fetcher.calls
    await db.close()


async def test_agent_select_faculty_candidates_falls_back_when_structure_is_weak(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    fetched = FetchResult(
        "https://scse.example.edu.cn/",
        "x",
        [],
        200,
    )
    links, budget = await agent._select_faculty_candidates(
        links=["https://scse.example.edu.cn/szdw/jsdw/list_2.htm"],
        fetched=fetched,
        org_unit_name="计算机学院",
        org_unit_url="https://scse.example.edu.cn/",
        llm_budget=0,
        max_candidates=4,
        link_signals=(),
    )
    assert budget == 0
    assert links == ["https://scse.example.edu.cn/szdw/jsdw/list_2.htm"]
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
    computer_college = SimpleNamespace(
        name="School of Computer Science",
        kind="college",
        url="https://cs.scu.edu.cn/",
        id=2,
    )
    software_college = SimpleNamespace(
        name="School of Software",
        kind="college",
        url="https://software.scu.edu.cn/",
        id=3,
    )
    ai_college = SimpleNamespace(
        name="School of Artificial Intelligence",
        kind="college",
        url="https://ai.scu.edu.cn/",
        id=4,
    )
    ece_college = SimpleNamespace(
        name="School of Electronic Information",
        kind="college",
        url="https://eie.scu.edu.cn/",
        id=5,
    )

    assert _org_unit_faculty_priority(computer_college, start_host) < _org_unit_faculty_priority(
        software_college, start_host
    )
    assert _org_unit_faculty_priority(software_college, start_host) < _org_unit_faculty_priority(
        ai_college, start_host
    )
    assert _org_unit_faculty_priority(ai_college, start_host) < _org_unit_faculty_priority(
        ece_college, start_host
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
        "https://www.example.edu.cn/szdw/tzgg/202603/t20260315_1024.shtml",
        "https://www.example.edu.cn/szdw/renshi/202603/t20260310_1122.shtml",
        "https://www.example.edu.cn/szdw/rszc/4.htm",
    ]
    out = agent._extract_detail_profile_links(links, "https://www.example.edu.cn/szdw.htm")
    assert "https://www.example.edu.cn/szdw/zzjs1/jjx.htm" in out
    assert "https://www.example.edu.cn/gywm/jxdw1/jjx.htm" not in out
    assert "https://sub.example.edu.cn/info/1012/3958.htm" not in out
    assert "https://www.example.edu.cn/news/1234.htm" not in out
    assert "https://www.example.edu.cn/szdw/tzgg/202603/t20260315_1024.shtml" not in out
    assert "https://www.example.edu.cn/szdw/renshi/202603/t20260310_1122.shtml" not in out
    assert "https://www.example.edu.cn/szdw/rszc/4.htm" not in out
    assert int(agent._pipeline_stats.get("detail_links_dropped_noise", 0)) >= 1
    await db.close()


async def test_buaa_computer_category_pages_are_not_detail_profile_links(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    agent.start_url = "https://www.buaa.edu.cn/"
    links = [
        "https://scse.buaa.edu.cn/szdw/qtjs/js.htm",
        "https://scse.buaa.edu.cn/szdw/qtjs/fjs.htm",
        "https://scse.buaa.edu.cn/szdw/qtjs/6.htm",
        "https://scse.buaa.edu.cn/info/1078/2627.htm",
        "https://scse.buaa.edu.cn/teachershouw.jsp?urltype=tree.TreeTempUrl&wbtreeid=1078",
    ]
    out = agent._extract_detail_profile_links(links, "https://scse.buaa.edu.cn/szdw/qtjs.htm")

    assert "https://scse.buaa.edu.cn/szdw/qtjs/js.htm" not in out
    assert "https://scse.buaa.edu.cn/szdw/qtjs/fjs.htm" not in out
    assert "https://scse.buaa.edu.cn/szdw/qtjs/6.htm" not in out
    assert "https://scse.buaa.edu.cn/info/1078/2627.htm" in out
    assert "https://scse.buaa.edu.cn/teachershouw.jsp?urltype=tree.TreeTempUrl&wbtreeid=1078" in out
    assert int(agent._pipeline_stats.get("detail_links_dropped_directory", 0)) >= 3
    await db.close()


async def test_buaa_software_teachershouw_news_query_links_are_kept(tmp_path):
    """`teachershouw.jsp?urltype=news.NewsContentUrl&...` is the BUAA software
    school's per-teacher detail URL; the literal `news` token in the query
    string used to flag it as noise and drop the entire 41-teacher cohort."""

    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    agent.start_url = "https://www.buaa.edu.cn/"
    list_url = "https://soft.buaa.edu.cn/tu-list-1.jsp?urltype=tree.TreeTempUrl&wbtreeid=1262"
    detail_a = "https://soft.buaa.edu.cn/teachershouw.jsp?urltype=news.NewsContentUrl&wbtreeid=1262&wbnewsid=9633"
    detail_b = "https://soft.buaa.edu.cn/teachershouw.jsp?urltype=news.NewsContentUrl&wbtreeid=1262&wbnewsid=10079"
    news_listing = "https://soft.buaa.edu.cn/news_list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1078"
    links = [detail_a, detail_b, news_listing, list_url]

    out = agent._extract_detail_profile_links(links, list_url)

    assert detail_a in out
    assert detail_b in out
    assert news_listing not in out
    assert list_url not in out
    await db.close()


async def test_enrich_skips_detail_urls_when_anchor_matches_enriched_professor(tmp_path):
    """Detail enrichment should drop links whose anchor text references a
    professor that already has full details, so the same person is not
    re-fetched via different per-channel URLs (BUAA CMS quirk that surfaced
    on the 空间与地球科学学院 run)."""

    from agents.crawler.fetchers.link_signals import LinkSignal

    pages = {
        "https://www.example.edu.cn/": FetchResult(
            "https://www.example.edu.cn/", "home", [], 200,
        ),
    }
    fetcher = FakeHumanFetcher(pages)
    db = DatabaseManager(sqlite_url(tmp_path / "agent.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")
    agent = CrawlerAgent(
        university_name="TestU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=FakeLLM(),
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        max_depth=4,
        max_backtracks=3,
        min_org_units=1,
    )

    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "张三",
                "org_unit_name": "软件学院",
                "org_unit_url": "https://soft.example.edu.cn/",
                "research_areas": "软件工程",
                "title": "教授",
            },
        )

    list_url = "https://soft.example.edu.cn/tu-list-1.jsp?wbtreeid=1262"
    detail_enriched = "https://soft.example.edu.cn/teachershouw.jsp?urltype=news.NewsContentUrl&wbtreeid=1262&wbnewsid=1"
    detail_new = "https://soft.example.edu.cn/teachershouw.jsp?urltype=news.NewsContentUrl&wbtreeid=1262&wbnewsid=2"
    fetched = FetchResult(
        url=list_url,
        text="软件学院教师",
        links=[detail_enriched, detail_new],
        status_code=200,
        link_signals=(
            LinkSignal(url=detail_enriched, anchor_text="张三 教授"),
            LinkSignal(url=detail_new, anchor_text="李四 副教授"),
        ),
    )

    processed_urls: list[str] = []

    async def _capture(self, urls, current, skills):
        processed_urls.extend(urls)

    import agents.crawler.agent_detail as _agent_detail
    original = _agent_detail.process_detail_urls_with_human
    _agent_detail.process_detail_urls_with_human = _capture
    try:
        current = _QueuedUrl(url=list_url, depth=2, label="软件学院", org_unit_id=None)
        await agent._enrich_profiles_with_detail_backend(current, fetched, "")
    finally:
        _agent_detail.process_detail_urls_with_human = original

    assert processed_urls == [detail_new]
    assert int(agent._pipeline_stats.get("detail_links_dropped_already_enriched", 0)) == 1
    await db.close()


async def test_enrich_warns_when_pending_empty_with_candidates(tmp_path):
    """When every detail candidate was already attempted in an earlier list
    page, enrich must emit a WARNING so future debugging can spot the
    pagination-subpage stall pattern observed in the 计算机学院 fjs/N.htm
    pages."""

    import logging

    from agents.crawler.fetchers.link_signals import LinkSignal

    pages = {
        "https://www.example.edu.cn/": FetchResult(
            "https://www.example.edu.cn/", "home", [], 200,
        ),
    }
    fetcher = FakeHumanFetcher(pages)
    db = DatabaseManager(sqlite_url(tmp_path / "agent.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")
    agent = CrawlerAgent(
        university_name="TestU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=FakeLLM(),
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        max_depth=4,
        max_backtracks=3,
        min_org_units=1,
    )

    detail_a = "https://soft.example.edu.cn/teachershouw.jsp?urltype=news.NewsContentUrl&wbtreeid=1&wbnewsid=1"
    agent._detail_visited_urls.add(detail_a)
    agent.visited_urls.add(detail_a)
    list_url = "https://soft.example.edu.cn/tu-list-1.jsp?wbtreeid=1"
    fetched = FetchResult(
        url=list_url,
        text="师资",
        links=[detail_a],
        status_code=200,
        link_signals=(LinkSignal(url=detail_a, anchor_text="王某 教授"),),
    )
    current = _QueuedUrl(url=list_url, depth=2, label="软件学院", org_unit_id=None)

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    agent.logger.addHandler(handler)
    try:
        await agent._enrich_profiles_with_detail_backend(current, fetched, "")
    finally:
        agent.logger.removeHandler(handler)

    assert int(agent._pipeline_stats.get("detail_pending_empty_with_candidates", 0)) == 1
    assert any("0 pending" in r.getMessage() for r in records)
    await db.close()


async def test_followup_faculty_links_filter_noise_sections(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    links = [
        "https://www.example.edu.cn/szdw/zzjs1.htm",
        "https://www.example.edu.cn/szdw/rszc.htm",
        "https://www.example.edu.cn/szdw/rszc/4.htm",
        "https://www.example.edu.cn/djgz1/lilubn/zzxx.htm",
        "https://www.example.edu.cn/rcpy/sys.htm",
        "https://sub.example.edu.cn/szdw/xx.htm",
        "https://www.example.edu.cn/szdw/tzgg/list.htm",
        "https://www.example.edu.cn/faculty/renshi/recruitment.htm",
    ]
    out = agent._extract_followup_faculty_links(links, "https://www.example.edu.cn/szdw.htm")
    assert "https://www.example.edu.cn/szdw/zzjs1.htm" in out
    assert "https://www.example.edu.cn/djgz1/lilubn/zzxx.htm" not in out
    assert "https://www.example.edu.cn/rcpy/sys.htm" not in out
    assert "https://sub.example.edu.cn/szdw/xx.htm" not in out
    assert "https://www.example.edu.cn/szdw/rszc.htm" not in out
    assert "https://www.example.edu.cn/szdw/rszc/4.htm" not in out
    assert "https://www.example.edu.cn/szdw/tzgg/list.htm" not in out
    assert "https://www.example.edu.cn/faculty/renshi/recruitment.htm" not in out
    assert int(agent._pipeline_stats.get("followup_dropped_noise", 0)) >= 1
    await db.close()


async def test_buaa_computer_category_pages_are_followup_faculty_links(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    agent.start_url = "https://www.buaa.edu.cn/"
    links = [
        "https://scse.buaa.edu.cn/szdw/qtjs/js.htm",
        "https://scse.buaa.edu.cn/szdw/qtjs/fjs.htm",
        "https://scse.buaa.edu.cn/szdw/qtjs/js1.htm",
        "https://scse.buaa.edu.cn/szdw/qtjs/sys.htm",
        "https://scse.buaa.edu.cn/info/1078/2627.htm",
    ]
    out = agent._extract_followup_faculty_links(links, "https://scse.buaa.edu.cn/szdw/qtjs.htm")

    assert "https://scse.buaa.edu.cn/szdw/qtjs/js.htm" in out
    assert "https://scse.buaa.edu.cn/szdw/qtjs/fjs.htm" in out
    assert "https://scse.buaa.edu.cn/szdw/qtjs/js1.htm" in out
    assert "https://scse.buaa.edu.cn/szdw/qtjs/sys.htm" in out
    await db.close()


async def test_buaa_computer_subcategory_pages_enter_crawl_task_queue(tmp_path):
    pages = {
        "https://www.buaa.edu.cn/": FetchResult(
            "https://www.buaa.edu.cn/",
            "home",
            ["https://www.buaa.edu.cn/jgsz/jxkyjg02.htm"],
            200,
        ),
        "https://www.buaa.edu.cn/jgsz/jxkyjg02.htm": FetchResult(
            "https://www.buaa.edu.cn/jgsz/jxkyjg02.htm",
            "机构设置 计算机学院",
            ["https://scse.buaa.edu.cn/"],
            200,
        ),
        "https://scse.buaa.edu.cn/": FetchResult(
            "https://scse.buaa.edu.cn/",
            "计算机学院 师资队伍 全体教师",
            ["https://scse.buaa.edu.cn/szdw/qtjs.htm"],
            200,
        ),
        "https://scse.buaa.edu.cn": FetchResult(
            "https://scse.buaa.edu.cn/",
            "计算机学院 师资队伍 全体教师",
            ["https://scse.buaa.edu.cn/szdw/qtjs.htm"],
            200,
        ),
        "https://scse.buaa.edu.cn/szdw/qtjs.htm": FetchResult(
            "https://scse.buaa.edu.cn/szdw/qtjs.htm",
            "全体教师 faculty 教授 副教授",
            [
                "https://scse.buaa.edu.cn/szdw/qtjs/js.htm",
                "https://scse.buaa.edu.cn/szdw/qtjs/fjs.htm",
                "https://scse.buaa.edu.cn/szdw/qtjs/6.htm",
                "https://scse.buaa.edu.cn/info/1078/2627.htm",
            ],
            200,
        ),
        "https://scse.buaa.edu.cn/szdw/qtjs/js.htm": FetchResult(
            "https://scse.buaa.edu.cn/szdw/qtjs/js.htm",
            "教授 faculty 邮箱 a@buaa.edu.cn",
            [],
            200,
        ),
        "https://scse.buaa.edu.cn/szdw/qtjs/fjs.htm": FetchResult(
            "https://scse.buaa.edu.cn/szdw/qtjs/fjs.htm",
            "副教授 faculty 邮箱 b@buaa.edu.cn",
            [],
            200,
        ),
        "https://scse.buaa.edu.cn/szdw/qtjs/6.htm": FetchResult(
            "https://scse.buaa.edu.cn/szdw/qtjs/6.htm",
            "教师列表 faculty 邮箱 c@buaa.edu.cn",
            [],
            200,
        ),
        "https://scse.buaa.edu.cn/info/1078/2627.htm": FetchResult(
            "https://scse.buaa.edu.cn/info/1078/2627.htm",
            "个人主页 faculty 邮箱 d@buaa.edu.cn",
            [],
            200,
        ),
    }

    class BuaaComputerLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.extract_urls: list[str] = []

        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            state = payload.get("state")
            if state == "DISCOVER_ORG_UNIT_PAGES":
                return LLMResult('{"links": ["https://www.buaa.edu.cn/jgsz/jxkyjg02.htm"]}')
            if state == "EXTRACT_ORG_UNITS":
                return LLMResult(
                    '{"org_units": [{"name": "计算机学院", "url": "https://scse.buaa.edu.cn/", "kind": "college"}]}'
                )
            if state == "FIND_FACULTY_PAGES":
                return LLMResult('{"links": ["https://scse.buaa.edu.cn/szdw/qtjs.htm"]}')
            if state == "EXTRACT_PROFESSORS":
                self.extract_urls.append(payload["url"])
                result = await tool_handlers["save_professors"](
                    org_unit_name="计算机学院",
                    org_unit_url=payload["url"],
                    source_url=payload["url"],
                    professors=[{"name": f"教师{len(self.extract_urls)}", "title": "Professor"}],
                )
                return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
            return LLMResult("{}")

    db = DatabaseManager(sqlite_url(tmp_path / "buaa_computer.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")
    llm = BuaaComputerLLM()
    agent = CrawlerAgent(
        university_name="北京航空航天大学",
        start_url="https://www.buaa.edu.cn/",
        location="北京",
        db=db,
        llm_client=llm,
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=FakeHumanFetcher(pages),
        max_depth=5,
        min_org_units=1,
    )

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    async with db.session() as session:
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
        task_urls = {task.page_url for task in tasks}
        org_unit = (await session.execute(select(OrgUnit).where(OrgUnit.name == "计算机学院"))).scalar_one()

    assert "https://scse.buaa.edu.cn/szdw/qtjs.htm" in task_urls
    assert "https://scse.buaa.edu.cn/szdw/qtjs/js.htm" in task_urls
    assert "https://scse.buaa.edu.cn/szdw/qtjs/fjs.htm" in task_urls
    assert "https://scse.buaa.edu.cn/szdw/qtjs/6.htm" in task_urls
    assert len(task_urls) >= 4
    assert org_unit.url == "https://scse.buaa.edu.cn"
    await db.close()


async def test_software_sidebar_followups_do_not_repeat_failed_tasks(tmp_path):
    a_url = "https://soft.buaa.edu.cn/tu-list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1323"
    b_url = "https://soft.buaa.edu.cn/tu-list-bodao.jsp?urltype=tree.TreeTempUrl&wbtreeid=1329"
    c_url = "https://soft.buaa.edu.cn/tu-list-1.jsp?urltype=tree.TreeTempUrl&wbtreeid=1224"
    pages = {
        a_url: FetchResult(a_url, "师资队伍 教授 副教授", [b_url, c_url, b_url], 200),
        b_url: FetchResult(b_url, "师资队伍 博导 硕导", [a_url, c_url], 200),
        c_url: FetchResult(c_url, "师资队伍 教师列表", [a_url, b_url], 200),
    }

    class EmptyExtractionLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "EXTRACT_PROFESSORS":
                return LLMResult("{}")
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, _fetcher, db = await _agent(tmp_path, EmptyExtractionLLM(), pages=pages)
    agent.start_url = "https://www.buaa.edu.cn/"

    await agent._extract_professors([_QueuedUrl(url=a_url, depth=1, label="软件学院")])

    async with db.session() as session:
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()

    task_urls = [task.page_url for task in tasks]
    assert sorted(task_urls) == sorted([a_url, b_url, c_url])
    assert all(task.status == CrawlTaskStatus.FAILED.value for task in tasks)
    assert all(task.status != CrawlTaskStatus.IN_PROGRESS.value for task in tasks)

    failure_counts: dict[str, int] = {}
    for failure in failures:
        failure_counts[failure.source_url] = failure_counts.get(failure.source_url, 0) + 1
    assert failure_counts == {a_url: 1, b_url: 1, c_url: 1}
    await db.close()


async def test_followup_from_noise_parent_keeps_explicit_faculty_dirs(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    links = [
        "https://www.example.edu.cn/szdw/rszc/4.htm",
        "https://www.example.edu.cn/szdw/rszc/3.htm",
        "https://www.example.edu.cn/szdw/jsdw.htm",
        "https://www.example.edu.cn/faculty/teacher_list.htm",
    ]
    out = agent._extract_followup_faculty_links(links, "https://www.example.edu.cn/szdw/rszc.htm")
    assert "https://www.example.edu.cn/szdw/rszc/4.htm" not in out
    assert "https://www.example.edu.cn/szdw/rszc/3.htm" not in out
    assert "https://www.example.edu.cn/szdw/jsdw.htm" in out
    assert "https://www.example.edu.cn/faculty/teacher_list.htm" in out
    await db.close()


async def test_professor_gate_skips_rszc_without_strong_faculty_evidence(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    skip, reason = agent._should_skip_professor_llm(
        url="https://www.example.edu.cn/szdw/rszc.htm",
        text="通知 公告 人事 政策",
    )
    assert skip
    assert reason == "url_noise_token"

    keep, keep_reason = agent._should_skip_professor_llm(
        url="https://www.example.edu.cn/szdw/rszc.htm",
        text="张三 教授 邮箱 zhangsan@example.edu.cn 电话 12345678",
    )
    assert not keep
    assert keep_reason == ""
    await db.close()

