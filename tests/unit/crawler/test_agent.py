from __future__ import annotations

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.agent import (
    CrawlerState,
    CrawlerAgent,
    _ExtractionTaskItem,
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
from agents.crawler.config import CrawlerSettings
from agents.crawler.agent_detail import extract_detail_profile_record_from_snapshot
from agents.crawler.fetchers import FetchResult, Fetcher
from agents.crawler.fetchers.link_signals import LinkSignal
from agents.crawler.models import (
    Academician,
    CrawlExtractionFailure,
    CrawlLogStatus,
    CrawlStatus,
    CrawlTask,
    CrawlTaskKind,
    CrawlTaskStatus,
    OrgUnit,
    OrgUnitStatus,
    Professor,
    UniversityMeta,
)
from agents.crawler.prompt_builder import CRAWLER_SYSTEM_PROMPT, CrawlerPromptBuilder
from runtime.context import ContextManager
from runtime.database import DatabaseManager
from runtime.llm import LLMClient, LLMResult, ToolCallErrorRecord, ToolCallRecord
from runtime.skills import SkillManager
from tests.conftest import sqlite_url


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, url, **kwargs):
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


class FakeLLMResearchDetail(FakeLLM):
    async def chat(self, messages, tools=None, tool_handlers=None):
        payload = json.loads(messages[-1]["content"])
        if payload.get("state") == "EXTRACT_PROFESSORS" and "研究方向" in payload.get("page_text", ""):
            result = await tool_handlers["save_professors"](
                org_unit_name="CS",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=payload["url"],
                professors=[{"name": "Ada", "title": "Professor", "research_areas": "systems"}],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
        return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)


class FakeLLMAcademicianOmitted(FakeLLM):
    async def chat(self, messages, tools=None, tool_handlers=None):
        payload = json.loads(messages[-1]["content"])
        if payload.get("state") == "EXTRACT_PROFESSORS":
            result = await tool_handlers["save_professors"](
                org_unit_name="计算机学院",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=payload["url"],
                professors=[{"name": "李未", "title": "教授"}],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
        return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)


class FakeLLMRelationAcademicianFlag(FakeLLM):
    async def chat(self, messages, tools=None, tool_handlers=None):
        payload = json.loads(messages[-1]["content"])
        if payload.get("state") == "EXTRACT_PROFESSORS":
            result = await tool_handlers["save_professors"](
                org_unit_name="计算机学院",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=payload["url"],
                professors=[
                    {
                        "name": "雷文强",
                        "title": "教授",
                        "is_academician": True,
                        "bio": "与荷兰皇家科学院院士Maarten de Rijke教授等世界一流学者合作。",
                    }
                ],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
        return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)


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


YAN_BINYU_DETAIL_TEXT = """## 严斌宇
计算机系、副院长
副教授
四川大学 计算机学院
学习工作经历
1993年9月-1997年6月年就读于四川大学物理系，获学士学位。
1999年9月-2002年6月在四川大学计算机学院攻读研究生，获得工学硕士学位。
2002年7月至2017年8月，在四川大学电子信息学院，副教授。
2022年1月-今，四川大学计算机学院（软件学院），副院长，副教授。
教学情况
长期工作在教学工作第一线，主讲必修、选修课、全校文化素质公选课课程共四门。
2020年，主讲的计算机通信与网络课程荣获首批国家级一流课程。
论文著作
四川省科技厅，国际合作项目，基于大数据与神经网络深度学习技术的智慧多维学生评价系统研究。
国家自然科学基金委员会，中韩国际合作交流项目，红外图像超分辨率技术研究。
国家自然科学基金委员会，面上项目，联合基于学习的超分辨率技术和多传感器超分辨率技术在红外图像复原中的研究。
管理经验
2022年1月起任四川大学计算机学院（软件学院）副院长，全面负责本科教学工作。
"""


HOU_CHAOHUAN_DETAIL_TEXT = """[学院首页](../../index.htm) / [师资队伍](../../szdw.htm) / [名师风采](../../szdw/msfc.htm) / [院士](../../szdw/msfc/ys.htm) / 正文
## 侯朝焕
日期：2019-01-24 来源： 作者： 浏览：20446 次
姓名：侯朝焕
职称：博士生导师/中科院院士 | 职务：
所在系所： | 电话：010-82547700
电子邮箱： | 个人主页：
办公地址：中国科学院声学研究所
研究方向：声学信号处理、VLSI信号处理、JC电路设计
![](/__local/2/9A/2C/profile.gif)个人简介
| **个人简介** 侯朝焕，于1995年当选中国科学院院士，现任中国科学院声学所研究员、博士生导师、中国声学学会名誉理事长，历任中科院信息技术学部副主任、中国声学学会理事长、国家自然科学基金委信息技术科学部主任等职。侯朝焕院士在声学和信息处理领域成果卓越，发表论文200多篇，先后完成12项国家重大项目。
**项目成果及****获奖荣誉** 侯朝焕院士在声学和信息处理领域成果卓越。
![](../../2020/img/footLogo.png)
四川大学计算机学院版权所有 © 2020
"""


SUN_YUAN_DETAIL_TEXT = """[学院首页](../../index.htm) / [师资队伍](../../szdw.htm) / [创新中心](../../szdw/cxzx.htm) / 正文
## 孙元
日期：2026-04-17 来源： 作者： 浏览：276 次
姓名：孙元
研究方向：多模态智能、AI for CFD
个人主页：[https://sunyuan-cs.github.io/](https://sunyuan-cs.github.io/)
电子邮箱：[sunyuan_work@163.com](mailto:sunyuan_work@163.com)，sunyuan@scu.edu.cn
办公地址：四川大学江安校区多学科交叉创新大楼522室
**个人简介：**
孙元，入选四川大学“海纳博士后”资助计划（15名），主要研究方向为多模态智能（多模态学习、图像融合、四足机器人等）与AI for CFD（智能科学计算、物理信息人工智能等）。近年来，共发表学术论文50余篇，其中以第一作者或通讯作者在TIP、TKDE、CVPR、ICML等人工智能领域中科院一区和CCF-A类会议上发表论文近30余篇。
如对我研究方向感兴趣，并有意和我一起做研究的同学欢迎联系。
**要求： 1. 对科研工作富有热情、感兴趣；2. 勤奋务实、态度积极。**
**部分论文：**
1. Yuan Sun, External Vision Guided Incomplete Multi-view Classification, CVPR 2026.
![](../../2020/img/footLogo.png)
四川大学计算机学院版权所有 © 2020
"""


WANG_JINGYU_DETAIL_TEXT = """## 王靖宇
副研究员
四川大学空天科学与工程学院
邮箱：wangjingyu@scu.edu.cn
研究方向：航空发动机旋转机械数值模拟方法研究
个人简介：
王靖宇，四川大学空天科学与工程学院副研究员，主要从事航空发动机旋转机械数值模拟方法、叶轮机械气动热力学与高性能计算方法研究。
代表成果：
主持和参与多项航空发动机相关科研项目。
"""


BUAA_TEACHERSHOW_DETAIL_TEXT = """当前位置：软件学院 > 师资队伍 > 教师详情
## 陈越
教授
北京航空航天大学 软件学院
电子邮箱：chenyue@buaa.edu.cn
研究方向：软件工程、程序分析、智能软件测试
个人简介：
陈越，北京航空航天大学软件学院教师，长期从事软件工程、程序分析与智能软件测试研究，承担本科生和研究生课程教学。
论文著作：
近年发表软件工程方向论文多篇。
"""


UESTC_EMPTY_RESEARCH_EMAIL_DETAIL_TEXT = """当前位置：信息与软件工程学院 > 师资队伍 > 教师详情
## 何明耘
姓名：何明耘
职称：教授
所在系所：信息与软件工程学院
研究方向： Email：hmy@uestc.edu.cn
办公地址：清水河校区主楼
个人简介：
何明耘，电子科技大学信息与软件工程学院教师，长期从事教学科研工作，主持和参与多项科研项目。
代表成果：
发表论文多篇，指导研究生参与科研训练。
"""

UESTC_EMPTY_MAIN_RESEARCH_DETAIL_TEXT = """当前位置：自动化工程学院 > 师资队伍 > 教师详情
## 李四
姓名：李四
职称：教授
主要研究方向：
电子邮箱：lisi@uestc.edu.cn
办公电话：028-12345678
个人简介：
李四，电子科技大学自动化工程学院教师，长期从事教学科研工作，主持和参与多项科研项目。
代表成果：
发表论文多篇，指导研究生参与科研训练。
"""


SCU_COMPUTER_ROSTER_TEXT = """# 四川大学计算机学院师资队伍
软件工程系教师列表

| 姓名 | 职称 | 邮箱 | 研究方向 | 个人主页 |
| --- | --- | --- | --- | --- |
| 严斌宇 | 副教授 | yanbinyu@scu.edu.cn | 大数据与神经网络深度学习技术、红外图像超分辨率技术 | https://cs.scu.edu.cn/info/1292/17098.htm |
| 孙元 | 特聘研究员 | sunyuan@scu.edu.cn | 多模态智能、AI for CFD | https://cs.scu.edu.cn/info/1416/19827.htm |
| 张蕾 | 教授 | zhanglei@scu.edu.cn | 数据库与数据挖掘、智能数据管理 | https://cs.scu.edu.cn/info/1293/18001.htm |
"""


class FakeLLMSubDepartmentProfessor(FakeLLM):
    async def chat(self, messages, tools=None, tool_handlers=None):
        payload = json.loads(messages[-1]["content"])
        if payload.get("state") == "EXTRACT_PROFESSORS":
            result = await tool_handlers["save_professors"](
                org_unit_name="工业互联网与建模仿真系",
                org_unit_url="https://auto.example.edu.cn/szdw/gongye.htm",
                source_url=payload["url"],
                professors=[{"name": "Sub Dept A", "title": "Professor"}],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
        return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)


class FakeLLMTeachingCenterProfessor(FakeLLM):
    async def chat(self, messages, tools=None, tool_handlers=None):
        payload = json.loads(messages[-1]["content"])
        if payload.get("state") == "EXTRACT_PROFESSORS":
            result = await tool_handlers["save_professors"](
                org_unit_name="教学实验中心",
                org_unit_url="https://auto.example.edu.cn/jxsyzx/",
                source_url=payload["url"],
                professors=[{"name": "Teaching Center A", "title": "Professor"}],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
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


async def _agent(
    tmp_path,
    fake_llm,
    max_depth=4,
    max_backtracks=3,
    pages=None,
    *,
    fetcher_cls=FakeFetcher,
    **agent_kwargs,
):
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
    fetcher = fetcher_cls(pages)
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
        **agent_kwargs,
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


async def test_extract_professors_rewrites_sub_department_payload_to_parent_org_unit(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLMSubDepartmentProfessor())

    await agent._extract_professors_from_page(
        _QueuedUrl("https://auto.example.edu.cn/", 0, "自动化科学与电气工程学院"),
        FetchResult(
            "https://auto.example.edu.cn/szdw/gongye.htm",
            "faculty Sub Dept A",
            [],
            200,
        ),
        "save professors",
        detail_mode=False,
        requested_url="https://auto.example.edu.cn/szdw/gongye.htm",
    )

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        org_units = (await session.execute(select(OrgUnit).order_by(OrgUnit.name))).scalars().all()
        assert [(professor.name, professor.org_unit_name) for professor in professors] == [
            ("Sub Dept A", "自动化科学与电气工程学院")
        ]
        assert [unit.name for unit in org_units] == ["自动化科学与电气工程学院"]

    await db.close()


async def test_extract_professors_drops_teaching_experiment_center_payload(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLMTeachingCenterProfessor())

    saved = await agent._extract_professors_from_page(
        _QueuedUrl("https://auto.example.edu.cn/", 0, "自动化科学与电气工程学院"),
        FetchResult(
            "https://auto.example.edu.cn/jxsyzx/",
            "faculty Teaching Center A",
            [],
            200,
        ),
        "save professors",
        detail_mode=False,
        requested_url="https://auto.example.edu.cn/jxsyzx/",
    )

    assert saved == 0
    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        org_units = (await session.execute(select(OrgUnit))).scalars().all()
        assert professors == []
        assert org_units == []

    await db.close()


async def test_fetch_url_skips_malformed_cms_link_before_fetch(tmp_path):
    agent, fetcher, db = await _agent(tmp_path, FakeLLM())
    bad_url = (
        "https://www.example.edu.cn/szdw/zzjs1/"
        "%3Cspan%20style='color:red;font-size:9pt'%3E"
        "%E8%BD%AC%E6%8D%A2%E9%93%BE%E6%8E%A5%E9%94%99%E8%AF%AF%3C/span"
    )

    fetched = await agent._fetch_url(bad_url, 1)

    assert fetched is None
    assert fetcher.calls == []
    assert agent._pipeline_stats["invalid_urls_skipped"] == 1
    assert any("skip invalid_url" in entry for entry in agent.execution_log)
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


async def test_resume_mode_uses_page_cache_without_fetching_start_url(tmp_path):
    agent, fetcher, db = await _agent(tmp_path, FakeLLM(), resume_mode=True)
    async with db.session() as session:
        await crawler_db.upsert_page_cache(
            session,
            url="https://www.example.edu.cn/",
            fetched=FetchResult(
                "https://www.example.edu.cn/",
                "home",
                ["https://www.example.edu.cn/orgs"],
                200,
            ),
        )

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://www.example.edu.cn/" not in fetcher.calls
    assert "fetch resume_cache url=https://www.example.edu.cn/ depth=0" in agent.execution_log
    await db.close()


async def test_resume_mode_cleans_excluded_org_units_before_cached_homepage_flow(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM(), resume_mode=True)
    async with db.session() as session:
        await crawler_db.upsert_page_cache(
            session,
            url="https://www.example.edu.cn/",
            fetched=FetchResult(
                "https://www.example.edu.cn/",
                "home",
                ["https://www.example.edu.cn/orgs"],
                200,
            ),
        )
        await crawler_db.get_or_create_org_unit(
            session,
            name="艺术学院",
            url="https://art.example.edu.cn/",
            kind="college",
        )

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    async with db.session() as session:
        units = (await session.execute(select(OrgUnit).order_by(OrgUnit.name))).scalars().all()
        assert "艺术学院" not in [unit.name for unit in units]
    await db.close()


async def test_resume_mode_merges_sub_department_org_units_before_cached_homepage_flow(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM(), resume_mode=True)
    async with db.session() as session:
        await crawler_db.upsert_page_cache(
            session,
            url="https://www.example.edu.cn/",
            fetched=FetchResult(
                "https://www.example.edu.cn/",
                "home",
                ["https://www.example.edu.cn/orgs"],
                200,
            ),
        )
        await crawler_db.get_or_create_org_unit(
            session,
            name="自动化科学与电气工程学院",
            url="https://auto.example.edu.cn/",
            kind="college",
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Sub A",
                "title": "Professor",
                "org_unit_name": "工业互联网与建模仿真系",
                "org_unit_url": "https://auto.example.edu.cn/szdw/gongye.htm",
                "source_url": "https://auto.example.edu.cn/szdw/gongye.htm",
            },
        )

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    async with db.session() as session:
        units = (await session.execute(select(OrgUnit).order_by(OrgUnit.name))).scalars().all()
        sub_professor = (await session.execute(select(Professor).where(Professor.name == "Sub A"))).scalar_one()
        assert "工业互联网与建模仿真系" not in [unit.name for unit in units]
        assert sub_professor.org_unit_name == "自动化科学与电气工程学院"
    await db.close()


async def test_resume_mode_recovers_tasks_without_refetching_historical_start_url(tmp_path):
    agent, fetcher, db = await _agent(tmp_path, FakeLLM(), pages={}, resume_mode=True)
    async with db.session() as session:
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/",
            CrawlLogStatus.SUCCESS,
            "seeded-history",
        )
        await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://www.example.edu.cn/cs",
            source_url="https://www.example.edu.cn/cs/faculty",
            page_url="https://www.example.edu.cn/cs/faculty",
            page_hash="resume-task",
            page_text_snapshot="faculty list Ada",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert result.saved_professors == 1
    assert fetcher.calls == []
    assert "skip already_crawled url=https://www.example.edu.cn/" in agent.execution_log
    await db.close()


async def test_resume_force_existing_refetches_no_faculty_org_unit_despite_success_log(tmp_path):
    pages = {
        "https://www.example.edu.cn/cs": FetchResult(
            "https://www.example.edu.cn/cs",
            "计算机学院",
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
        FakeLLM(),
        pages=pages,
        fetcher_cls=FakeHumanFetcher,
        resume_mode=True,
        resume_force_existing=True,
    )
    async with db.session() as session:
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/",
            CrawlLogStatus.SUCCESS,
            "seeded-start-history",
        )
        org_unit = await crawler_db.get_or_create_org_unit(
            session,
            name="CS",
            url="https://www.example.edu.cn/cs",
            kind="college",
            status=OrgUnitStatus.NO_FACULTY_PAGE,
        )
        await crawler_db.log_crawl(
            session,
            org_unit.url,
            CrawlLogStatus.SUCCESS,
            "seeded-org-history",
        )

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://www.example.edu.cn/cs" in fetcher.calls
    assert "force refetch url=https://www.example.edu.cn/cs" in agent.execution_log
    async with db.session() as session:
        org_unit = (await session.execute(select(OrgUnit).where(OrgUnit.name == "CS"))).scalar_one()
        assert org_unit.status == OrgUnitStatus.IN_PROGRESS.value
    await db.close()


async def test_resume_refetches_retryable_failure_url_despite_success_log(tmp_path):
    agent, fetcher, db = await _agent(
        tmp_path,
        FakeLLM(),
        fetcher_cls=FakeHumanFetcher,
        resume_mode=True,
    )
    async with db.session() as session:
        await crawler_db.upsert_page_cache(
            session,
            url="https://www.example.edu.cn/cs",
            fetched=FetchResult(
                "https://www.example.edu.cn/cs",
                "",
                [],
                0,
                block_reason="timeout",
            ),
        )
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/cs",
            CrawlLogStatus.SUCCESS,
            "seeded-success-history",
        )

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://www.example.edu.cn/cs" in fetcher.calls
    assert "force refetch url=https://www.example.edu.cn/cs" in agent.execution_log
    async with db.session() as session:
        assert await crawler_db.list_retryable_fetch_failure_urls(session) == []
    await db.close()


async def test_resume_keeps_university_failed_when_retryable_fetch_failure_remains(tmp_path):
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
            "",
            [],
            0,
            block_reason="timeout",
        ),
    }
    agent, fetcher, db = await _agent(
        tmp_path,
        FakeLLM(),
        pages=pages,
        fetcher_cls=FakeHumanFetcher,
        resume_mode=True,
    )
    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Existing",
                "title": "Professor",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "source_url": "https://www.example.edu.cn/cs/faculty",
            },
        )
        await crawler_db.upsert_page_cache(
            session,
            url="https://www.example.edu.cn/cs",
            fetched=FetchResult(
                "https://www.example.edu.cn/cs",
                "",
                [],
                0,
                block_reason="timeout",
            ),
        )
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/cs",
            CrawlLogStatus.SUCCESS,
            "seeded-success-history",
        )

    result = await agent.run()

    assert result.status == CrawlStatus.FAILED.value
    assert any("retryable_fetch_failures_remaining" in message for message in result.messages)
    assert "https://www.example.edu.cn/cs" in fetcher.calls
    async with db.session() as session:
        meta = (await session.execute(select(UniversityMeta))).scalar_one()
        assert meta.crawl_status == CrawlStatus.FAILED.value
        assert await crawler_db.list_retryable_fetch_failure_urls(session) == ["https://www.example.edu.cn/cs"]
    await db.close()


async def test_pipeline_recovers_more_tasks_than_queue_cap_without_deadlock(tmp_path):
    agent, _fetcher, db = await _agent(
        tmp_path,
        FakeLLM(),
        pages={},
        pipeline_queue_cap=2,
        pipeline_llm_workers=1,
        pipeline_db_workers=1,
    )
    async with db.session() as session:
        for index in range(5):
            await crawler_db.upsert_crawl_task(
                session,
                university="TestU",
                org_unit_name="CS",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=f"https://www.example.edu.cn/cs/faculty/{index}",
                page_url=f"https://www.example.edu.cn/cs/faculty/{index}",
                page_hash=f"resume-task-{index}",
                page_text_snapshot=f"faculty list Ada {index}",
                allowed_tools='["save_professors"]',
                status=CrawlTaskStatus.PENDING,
            )

    await asyncio.wait_for(agent._extract_professors([], recovery_limit=5), timeout=10)

    async with db.session() as session:
        summary = await crawler_db.summarize_crawl_task_status(session)
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
    assert summary[CrawlTaskStatus.DONE.value] == 5
    assert all(task.status == CrawlTaskStatus.DONE.value for task in tasks)
    assert int(agent._pipeline_stats.get("processed_tasks", 0)) == 5
    assert int(agent._pipeline_stats.get("records_created", 0)) == 1
    await db.close()


async def test_pipeline_extracts_yan_binyu_profile_from_project_and_bio_sections(tmp_path):
    class PromptSensitiveYanLLM:
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            instruction = payload["instruction"]
            required_prompt_terms = [
                "科研项目",
                "论文著作",
                "项目题名",
                "学习工作经历",
                "教学情况",
                "管理经验",
            ]
            if payload.get("state") != "EXTRACT_PROFESSORS" or not all(
                term in instruction for term in required_prompt_terms
            ):
                return LLMResult("profile analysis without tool call")
            result = await tool_handlers["save_professors"](
                org_unit_name="计算机学院",
                org_unit_url="https://cs.scu.edu.cn/szdw/rjgcx.htm",
                source_url=payload["url"],
                professors=[
                    {
                        "name": "严斌宇",
                        "title": "副教授",
                        "research_areas": [
                            "大数据与神经网络深度学习技术",
                            "红外图像超分辨率技术",
                            "多传感器超分辨率技术",
                            "计算机通信与网络",
                        ],
                        "bio": "长期工作在教学工作第一线，主讲必修、选修课、全校文化素质公选课课程共四门。2022年1月起任四川大学计算机学院（软件学院）副院长。",
                    }
                ],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])

    homepage = "https://cs.scu.edu.cn/info/1292/17098.htm"
    agent, _fetcher, db = await _agent(
        tmp_path,
        PromptSensitiveYanLLM(),
        pages={},
        pipeline_queue_cap=2,
        pipeline_llm_workers=1,
        pipeline_db_workers=1,
    )
    async with db.session() as session:
        await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="计算机学院",
            org_unit_url="https://cs.scu.edu.cn/szdw/rjgcx.htm",
            source_url=homepage,
            page_url=homepage,
            page_hash="yan-binyu-detail",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot=YAN_BINYU_DETAIL_TEXT,
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )

    await asyncio.wait_for(agent._extract_professors([], recovery_limit=1), timeout=10)

    async with db.session() as session:
        professor = (await session.execute(select(Professor).where(Professor.name == "严斌宇"))).scalar_one()
        task = (await session.execute(select(CrawlTask))).scalar_one()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
    assert task.status == CrawlTaskStatus.DONE.value
    assert "大数据与神经网络深度学习技术" in professor.research_areas
    assert "教学工作第一线" in professor.bio
    assert not any(failure.failure_type == "no_structured_data" for failure in failures)
    await db.close()


async def test_pipeline_keeps_rich_detail_without_payload_recoverable(tmp_path):
    class ProseOnlyLLM:
        async def chat(self, messages, tools=None, tool_handlers=None):
            return LLMResult("This is an individual professor profile but I will not call a tool.")

    homepage = "https://cs.scu.edu.cn/info/1292/17098.htm"
    agent, _fetcher, db = await _agent(
        tmp_path,
        ProseOnlyLLM(),
        pages={},
        pipeline_queue_cap=2,
        pipeline_llm_workers=1,
        pipeline_db_workers=1,
    )
    async with db.session() as session:
        await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="计算机学院",
            org_unit_url="https://cs.scu.edu.cn/szdw/rjgcx.htm",
            source_url=homepage,
            page_url=homepage,
            page_hash="yan-binyu-prose-only",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot=YAN_BINYU_DETAIL_TEXT,
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )

    await asyncio.wait_for(agent._extract_professors([], recovery_limit=1), timeout=10)

    async with db.session() as session:
        task = (await session.execute(select(CrawlTask))).scalar_one()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
    assert task.status == CrawlTaskStatus.RETRY.value
    assert task.last_error == "rich_detail_no_structured_data"
    assert any(failure.failure_type == "no_structured_data" and failure.resolver == "retry" for failure in failures)
    await db.close()


async def test_pipeline_synthesizes_hou_chaohuan_academician_from_detail_snapshot(tmp_path):
    class ProseOnlyLLM:
        async def chat(self, messages, tools=None, tool_handlers=None):
            return LLMResult("This profile is not saved because the office address is outside SCU.")

    homepage = "https://cs.scu.edu.cn/info/1301/13765.htm"
    agent, _fetcher, db = await _agent(
        tmp_path,
        ProseOnlyLLM(),
        pages={},
        pipeline_queue_cap=2,
        pipeline_llm_workers=1,
        pipeline_db_workers=1,
    )
    async with db.session() as session:
        await crawler_db.upsert_academician(
            session,
            {
                "name": "侯朝焕",
                "org_unit_name": "计算机学院",
                "org_unit_url": "https://cs.scu.edu.cn/",
                "homepage": homepage,
            },
        )
        await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="计算机学院",
            org_unit_url="https://cs.scu.edu.cn/",
            source_url=homepage,
            page_url=homepage,
            page_hash="hou-chaohuan-detail",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot=HOU_CHAOHUAN_DETAIL_TEXT,
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )

    await asyncio.wait_for(agent._extract_professors([], recovery_limit=1), timeout=10)

    async with db.session() as session:
        academician = (await session.execute(select(Academician).where(Academician.name == "侯朝焕"))).scalar_one()
        task = (await session.execute(select(CrawlTask))).scalar_one()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
    assert task.status == CrawlTaskStatus.DONE.value
    assert "声学信号处理" in academician.research_areas
    assert "1995年当选中国科学院院士" in academician.bio
    assert academician.phone == "010-82547700"
    assert not any(failure.failure_type == "no_structured_data" for failure in failures)
    await db.close()


async def test_pipeline_fills_sun_yuan_bio_from_snapshot_after_sparse_invalid_json_retry(tmp_path):
    class InvalidThenSparseSunLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools=None, tool_handlers=None):
            self.calls += 1
            payload = json.loads(messages[-1]["content"])
            if self.calls == 1:
                return LLMResult(
                    "",
                    invalid_tool_calls=[
                        ToolCallErrorRecord(
                            "save_professors",
                            '{"org_unit_name":"计算机学院","professors":[{"name":"孙元","bio":"孙元，入选四川大学"海纳博士后"资助计划"}]}',
                            "invalid_json",
                        )
                    ],
                )
            result = await tool_handlers["save_professors"](
                org_unit_name="计算机学院",
                org_unit_url="https://cs.scu.edu.cn/",
                source_url=payload["url"],
                professors=[
                    {
                        "name": "孙元",
                        "research_areas": "多模态智能、AI for CFD",
                    }
                ],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])

    homepage = "https://cs.scu.edu.cn/info/1416/19827.htm"
    agent, _fetcher, db = await _agent(
        tmp_path,
        InvalidThenSparseSunLLM(),
        pages={},
        pipeline_queue_cap=2,
        pipeline_llm_workers=1,
        pipeline_db_workers=1,
    )
    async with db.session() as session:
        await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="计算机学院",
            org_unit_url="https://cs.scu.edu.cn/",
            source_url=homepage,
            page_url=homepage,
            page_hash="sun-yuan-detail",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot=SUN_YUAN_DETAIL_TEXT,
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )

    await asyncio.wait_for(agent._extract_professors([], recovery_limit=1), timeout=10)

    async with db.session() as session:
        professor = (await session.execute(select(Professor).where(Professor.name == "孙元"))).scalar_one()
        task = (await session.execute(select(CrawlTask))).scalar_one()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
    assert task.status == CrawlTaskStatus.DONE.value
    assert "多模态智能" in professor.research_areas
    assert "海纳博士后" in professor.bio
    assert professor.homepage == homepage
    assert professor.external_link == "https://sunyuan-cs.github.io"
    assert any(failure.failure_type == "invalid_json" and failure.resolver == "retry" for failure in failures)
    await db.close()


async def test_pipeline_marks_plain_detail_without_payload_failed(tmp_path):
    class ProseOnlyLLM:
        async def chat(self, messages, tools=None, tool_handlers=None):
            return LLMResult("No extractable structured data.")

    homepage = "https://cs.scu.edu.cn/info/1292/plain.htm"
    agent, _fetcher, db = await _agent(
        tmp_path,
        ProseOnlyLLM(),
        pages={},
        pipeline_queue_cap=2,
        pipeline_llm_workers=1,
        pipeline_db_workers=1,
    )
    async with db.session() as session:
        await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="计算机学院",
            org_unit_url="https://cs.scu.edu.cn/szdw/rjgcx.htm",
            source_url=homepage,
            page_url=homepage,
            page_hash="plain-detail-prose-only",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="## 严斌宇\n副教授\n四川大学 计算机学院\n",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )

    await asyncio.wait_for(agent._extract_professors([], recovery_limit=1), timeout=10)

    async with db.session() as session:
        task = (await session.execute(select(CrawlTask))).scalar_one()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
    assert task.status == CrawlTaskStatus.FAILED.value
    assert task.last_error == "no_structured_data"
    assert any(failure.failure_type == "no_structured_data" and failure.resolver == "dropped" for failure in failures)
    await db.close()


def _live_llm_client() -> LLMClient:
    if os.getenv("YANCLAW_LLM_LIVE_TESTS") != "1":
        pytest.skip("Set YANCLAW_LLM_LIVE_TESTS=1 to run live LLM prompt validation.")
    settings = CrawlerSettings()
    if not settings.openai_api_key:
        pytest.skip("YANCLAW_OPENAI_API_KEY is empty.")

    return LLMClient(
        settings.openai_base_url,
        settings.openai_api_key,
        settings.openai_model,
        max_rounds=1,
        max_concurrent=settings.llm_max_concurrent,
        min_interval=settings.llm_min_interval_seconds,
        timeout_seconds=settings.llm_timeout_seconds,
        temperature=settings.llm_temperature,
        top_p=settings.llm_top_p,
        seed=settings.llm_seed,
    )


async def _run_live_llm_task_case(
    tmp_path,
    *,
    homepage: str,
    snapshot: str,
    expected_names: tuple[str, ...],
    entity_model: type[Professor] | type[Academician],
    task_kind: CrawlTaskKind,
    university_name: str,
    start_url: str,
    location: str,
    org_unit_name: str,
    org_unit_url: str,
):
    task_kind_value = task_kind.value
    agent, _fetcher, db = await _agent(
        tmp_path,
        _live_llm_client(),
        pages={},
        pipeline_queue_cap=2,
        pipeline_llm_workers=1,
        pipeline_db_workers=1,
    )
    agent.university_name = university_name
    agent.start_url = start_url
    agent.location = location
    async with db.session() as session:
        await crawler_db.upsert_crawl_task(
            session,
            university=university_name,
            org_unit_name=org_unit_name,
            org_unit_url=org_unit_url,
            source_url=homepage,
            page_url=homepage,
            page_hash=hashlib.sha1(f"{homepage}:{task_kind_value}".encode("utf-8")).hexdigest(),
            task_kind=task_kind,
            page_text_snapshot=snapshot,
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )

    await asyncio.wait_for(agent._extract_professors([], recovery_limit=1), timeout=180)

    async with db.session() as session:
        entities = (
            await session.execute(select(entity_model).where(entity_model.name.in_(expected_names)))
        ).scalars().all()
        task = (await session.execute(select(CrawlTask))).scalar_one()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
    entities_by_name = {entity.name: entity for entity in entities}
    missing = [name for name in expected_names if name not in entities_by_name]
    assert task.status == CrawlTaskStatus.DONE.value
    assert not missing
    result = SimpleNamespace(
        entity=entities_by_name[expected_names[0]],
        entities=entities_by_name,
        task=SimpleNamespace(status=task.status, last_error=task.last_error, task_kind=task.task_kind),
        failures=[
            SimpleNamespace(failure_type=failure.failure_type, resolver=failure.resolver) for failure in failures
        ],
        stats=dict(agent._pipeline_stats),
    )
    await db.close()
    return result


async def _run_live_llm_detail_case(
    tmp_path,
    *,
    homepage: str,
    snapshot: str,
    expected_name: str,
    entity_model: type[Professor] | type[Academician],
    university_name: str = "四川大学",
    start_url: str = "https://www.scu.edu.cn/",
    location: str = "成都",
    org_unit_name: str = "计算机学院",
    org_unit_url: str = "https://cs.scu.edu.cn/szdw/rjgcx.htm",
):
    return await _run_live_llm_task_case(
        tmp_path,
        homepage=homepage,
        snapshot=snapshot,
        expected_names=(expected_name,),
        entity_model=entity_model,
        task_kind=CrawlTaskKind.DETAIL_PAGE,
        university_name=university_name,
        start_url=start_url,
        location=location,
        org_unit_name=org_unit_name,
        org_unit_url=org_unit_url,
    )


@pytest.mark.live_llm
async def test_live_llm_yan_binyu_detail_prompt_calls_save_professors(tmp_path):
    result = await _run_live_llm_detail_case(
        tmp_path,
        homepage="https://cs.scu.edu.cn/info/1292/17098.htm",
        snapshot=YAN_BINYU_DETAIL_TEXT,
        expected_name="严斌宇",
        entity_model=Professor,
    )
    professor = result.entity
    assert professor.research_areas or professor.bio
    assert int(result.stats.get("detail_snapshot_payloads_synthesized", 0)) == 0


@pytest.mark.live_llm
async def test_live_llm_hou_chaohuan_detail_snapshot_fills_academician_fields(tmp_path):
    result = await _run_live_llm_detail_case(
        tmp_path,
        homepage="https://cs.scu.edu.cn/info/1301/13765.htm",
        snapshot=HOU_CHAOHUAN_DETAIL_TEXT,
        expected_name="侯朝焕",
        entity_model=Academician,
        org_unit_url="https://cs.scu.edu.cn/",
    )
    academician = result.entity
    assert academician.research_areas
    assert academician.bio


@pytest.mark.live_llm
async def test_live_llm_sun_yuan_detail_snapshot_fills_bio(tmp_path):
    result = await _run_live_llm_detail_case(
        tmp_path,
        homepage="https://cs.scu.edu.cn/info/1416/19827.htm",
        snapshot=SUN_YUAN_DETAIL_TEXT,
        expected_name="孙元",
        entity_model=Professor,
        org_unit_url="https://cs.scu.edu.cn/",
    )
    professor = result.entity
    assert professor.research_areas
    assert professor.bio


@pytest.mark.live_llm
async def test_live_llm_scu_teamlist_query_detail_saves_professor(tmp_path):
    result = await _run_live_llm_detail_case(
        tmp_path,
        homepage="https://saa.scu.edu.cn/teamlist.htm?action=detailTeam&uuinId=661618903336854",
        snapshot=WANG_JINGYU_DETAIL_TEXT,
        expected_name="王靖宇",
        entity_model=Professor,
        org_unit_name="空天科学与工程学院",
        org_unit_url="https://saa.scu.edu.cn/teamlist.htm",
    )
    professor = result.entity
    assert "航空发动机旋转机械数值模拟方法研究" in (professor.research_areas or "")
    assert int(result.stats.get("detail_snapshot_payloads_synthesized", 0)) == 0


@pytest.mark.live_llm
async def test_live_llm_buaa_teachershouw_news_query_detail_saves_professor(tmp_path):
    result = await _run_live_llm_detail_case(
        tmp_path,
        homepage="https://soft.buaa.edu.cn/teachershouw.jsp?urltype=news.NewsContentUrl&wbtreeid=1262&wbnewsid=9633",
        snapshot=BUAA_TEACHERSHOW_DETAIL_TEXT,
        expected_name="陈越",
        entity_model=Professor,
        university_name="北京航空航天大学",
        start_url="https://www.buaa.edu.cn/",
        location="北京",
        org_unit_name="软件学院",
        org_unit_url="https://soft.buaa.edu.cn/tu-list-1.jsp?urltype=tree.TreeTempUrl&wbtreeid=1262",
    )
    professor = result.entity
    assert "软件工程" in (professor.research_areas or "")
    assert int(result.stats.get("detail_snapshot_payloads_synthesized", 0)) == 0


def test_detail_snapshot_does_not_treat_email_label_as_research_area():
    record = extract_detail_profile_record_from_snapshot(
        UESTC_EMPTY_RESEARCH_EMAIL_DETAIL_TEXT,
        page_url="https://sise.uestc.edu.cn/info/1037/5755.htm",
    )

    assert record is not None
    assert record["name"] == "何明耘"
    assert record.get("research_areas") is None
    assert record["email"] == "hmy@uestc.edu.cn"


def test_detail_snapshot_does_not_fabricate_empty_main_research_area():
    record = extract_detail_profile_record_from_snapshot(
        UESTC_EMPTY_MAIN_RESEARCH_DETAIL_TEXT,
        page_url="https://auto.uestc.edu.cn/info/1037/5755.htm",
    )

    assert record is not None
    assert record["name"] == "李四"
    assert record.get("research_areas") is None
    assert record["email"] == "lisi@uestc.edu.cn"


@pytest.mark.live_llm
async def test_live_llm_scu_computer_roster_list_snapshot_saves_professors(tmp_path):
    result = await _run_live_llm_task_case(
        tmp_path,
        homepage="https://cs.scu.edu.cn/szdw/rjgcx.htm",
        snapshot=SCU_COMPUTER_ROSTER_TEXT,
        expected_names=("严斌宇", "孙元", "张蕾"),
        entity_model=Professor,
        task_kind=CrawlTaskKind.LIST_PAGE,
        university_name="四川大学",
        start_url="https://www.scu.edu.cn/",
        location="成都",
        org_unit_name="计算机学院",
        org_unit_url="https://cs.scu.edu.cn/szdw/rjgcx.htm",
    )
    assert "大数据" in (result.entities["严斌宇"].research_areas or "")
    assert "多模态智能" in (result.entities["孙元"].research_areas or "")
    assert "数据库" in (result.entities["张蕾"].research_areas or "")
    assert int(result.stats.get("records_created", 0)) >= 3


async def test_pipeline_llm_workers_consume_concurrently_while_db_worker_serializes(tmp_path):
    class ParallelLLM:
        def __init__(self, *, release_after: int):
            self.release_after = release_after
            self.started = 0
            self.active = 0
            self.max_active = 0
            self.all_started = asyncio.Event()

        async def chat(self, messages, tools=None, tool_handlers=None):
            self.started += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.started >= self.release_after:
                self.all_started.set()
            try:
                await self.all_started.wait()
                payload = json.loads(messages[-1]["content"])
                result = await tool_handlers["save_professors"](
                    org_unit_name="CS",
                    org_unit_url="https://www.example.edu.cn/cs",
                    source_url=payload["url"],
                    professors=[{"name": f"Ada {payload['url'].rsplit('/', 1)[-1]}", "title": "Professor"}],
                )
                return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
            finally:
                self.active -= 1

    llm = ParallelLLM(release_after=4)
    agent, _fetcher, db = await _agent(
        tmp_path,
        llm,
        pages={},
        pipeline_queue_cap=4,
        pipeline_llm_workers=4,
        pipeline_db_workers=1,
    )
    save_active = 0
    max_save_active = 0

    async def fake_save_payloads_to_db(payloads, *, task):
        nonlocal save_active, max_save_active
        save_active += 1
        max_save_active = max(max_save_active, save_active)
        try:
            await asyncio.sleep(0.01)
            return {
                "accepted": 1,
                "created": 1,
                "updated": 0,
                "unchanged": 0,
                "deduped_by_name_key": 0,
                "deduped_by_homepage": 0,
            }
        finally:
            save_active -= 1

    agent._save_payloads_to_db = fake_save_payloads_to_db

    async with db.session() as session:
        for index in range(4):
            await crawler_db.upsert_crawl_task(
                session,
                university="TestU",
                org_unit_name="CS",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=f"https://www.example.edu.cn/cs/faculty/{index}",
                page_url=f"https://www.example.edu.cn/cs/faculty/{index}",
                page_hash=f"parallel-task-{index}",
                page_text_snapshot=f"faculty list Ada {index}",
                allowed_tools='["save_professors"]',
                status=CrawlTaskStatus.PENDING,
            )

    await asyncio.wait_for(agent._extract_professors([], recovery_limit=4), timeout=10)

    async with db.session() as session:
        summary = await crawler_db.summarize_crawl_task_status(session)
    assert llm.max_active == 4
    assert max_save_active == 1
    assert summary[CrawlTaskStatus.DONE.value] == 4
    await db.close()


async def test_pipeline_invalid_json_retry_does_not_deadlock_when_queue_is_full(tmp_path):
    class QueueSaturationRetryLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.invalid_returned = False

        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") != "EXTRACT_PROFESSORS":
                return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)
            if not self.invalid_returned:
                self.invalid_returned = True
                return LLMResult(
                    "",
                    [],
                    [ToolCallErrorRecord("save_professors", '{"org_unit_name":"CS","professors":[', "invalid_json")],
                )
            result = await tool_handlers["save_professors"](
                org_unit_name="CS",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=payload["url"],
                professors=[{"name": f"Ada {payload['url'].rsplit('/', 1)[-1]}", "title": "Professor"}],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])

    agent, _fetcher, db = await _agent(
        tmp_path,
        QueueSaturationRetryLLM(),
        pages={},
        pipeline_queue_cap=2,
        pipeline_llm_workers=1,
        pipeline_db_workers=1,
    )
    async with db.session() as session:
        for index in range(5):
            await crawler_db.upsert_crawl_task(
                session,
                university="TestU",
                org_unit_name="CS",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=f"https://www.example.edu.cn/cs/faculty/{index}",
                page_url=f"https://www.example.edu.cn/cs/faculty/{index}",
                page_hash=f"invalid-retry-task-{index}",
                page_text_snapshot=f"faculty list Ada {index}",
                allowed_tools='["save_professors"]',
                status=CrawlTaskStatus.PENDING,
            )

    await asyncio.wait_for(agent._extract_professors([], recovery_limit=5), timeout=10)

    async with db.session() as session:
        summary = await crawler_db.summarize_crawl_task_status(session)
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
    assert summary[CrawlTaskStatus.DONE.value] == 5
    assert summary[CrawlTaskStatus.PENDING.value] == 0
    assert summary[CrawlTaskStatus.RETRY.value] == 0
    assert any(failure.failure_type == "invalid_json" and failure.resolver == "retry" for failure in failures)
    await db.close()


async def test_pipeline_refetches_and_consumes_completion_recrawl_tasks_over_queue_cap(tmp_path):
    pages = {
        f"https://www.example.edu.cn/cs/info/ada-{index}.htm": FetchResult(
            f"https://www.example.edu.cn/cs/info/ada-{index}.htm",
            f"Ada 教授\n研究方向: systems {index}",
            [],
            200,
        )
        for index in range(5)
    }
    agent, fetcher, db = await _agent(
        tmp_path,
        FakeLLMResearchDetail(),
        pages=pages,
        fetcher_cls=FakeHumanFetcher,
        resume_mode=True,
        pipeline_queue_cap=2,
        pipeline_llm_workers=1,
        pipeline_db_workers=1,
    )
    async with db.session() as session:
        for index, homepage in enumerate(pages):
            stale_snapshot = (
                "stale profile without research " + ("x" * 400)
                if index == 0
                else ""
            )
            await crawler_db.upsert_crawl_task(
                session,
                university="TestU",
                org_unit_name="CS",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=homepage,
                page_url=homepage,
                page_hash=f"empty-homepage-{index}",
                task_kind=CrawlTaskKind.DETAIL_PAGE,
                page_text_snapshot=stale_snapshot,
                allowed_tools='["save_professors"]',
                status=CrawlTaskStatus.RETRY,
                priority=-10,
                last_error="completion_recrawl_missing_profile_fields",
            )

    await asyncio.wait_for(agent._extract_professors([], recovery_limit=5), timeout=10)

    assert fetcher.calls == list(pages)
    assert int(agent._pipeline_stats.get("recovery_refetched", 0)) == 5
    assert int(agent._pipeline_stats.get("recovery_enqueued", 0)) == 5
    assert int(agent._pipeline_stats.get("recovery_consumed", 0)) == 5
    async with db.session() as session:
        summary = await crawler_db.summarize_crawl_task_status(session)
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
    assert summary[CrawlTaskStatus.DONE.value] == 5
    assert all("研究方向" in task.page_text_snapshot for task in tasks)
    await db.close()


async def test_strict_resume_recovers_more_tasks_than_queue_cap_without_fetching(tmp_path):
    agent, fetcher, db = await _agent(
        tmp_path,
        FakeLLM(),
        pages={},
        resume_mode=True,
        pipeline_queue_cap=2,
        pipeline_llm_workers=1,
        pipeline_db_workers=1,
    )
    async with db.session() as session:
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/",
            CrawlLogStatus.SUCCESS,
            "seeded-history",
        )
        for index in range(5):
            await crawler_db.upsert_crawl_task(
                session,
                university="TestU",
                org_unit_name="CS",
                org_unit_url="https://www.example.edu.cn/cs",
                source_url=f"https://www.example.edu.cn/cs/faculty/{index}",
                page_url=f"https://www.example.edu.cn/cs/faculty/{index}",
                page_hash=f"strict-resume-task-{index}",
                page_text_snapshot=f"faculty list Ada {index}",
                allowed_tools='["save_professors"]',
                status=CrawlTaskStatus.PENDING,
            )

    result = await asyncio.wait_for(agent.run(), timeout=10)

    assert result.status == CrawlStatus.COMPLETED.value
    assert fetcher.calls == []
    assert int(agent._pipeline_stats.get("processed_tasks", 0)) == 5
    async with db.session() as session:
        summary = await crawler_db.summarize_crawl_task_status(session)
    assert summary[CrawlTaskStatus.PENDING.value] == 0
    assert summary[CrawlTaskStatus.RETRY.value] == 0
    assert summary[CrawlTaskStatus.IN_PROGRESS.value] == 0
    assert summary[CrawlTaskStatus.DONE.value] == 5
    await db.close()


async def test_resume_mode_blocks_without_cache_or_seed_state(tmp_path):
    agent, fetcher, db = await _agent(tmp_path, FakeLLM(), pages={}, resume_mode=True)
    async with db.session() as session:
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/",
            CrawlLogStatus.SUCCESS,
            "seeded-history",
        )

    result = await agent.run()

    assert result.status == CrawlStatus.FAILED.value
    assert fetcher.calls == []
    assert any("resume_blocked_missing_cache" in message for message in result.messages)
    await db.close()


async def test_resume_mode_probe_skips_previously_crawled_common_path(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.crawler.agent._INTERMEDIATE_ORG_PATHS", ("/known-org.htm",))
    agent, fetcher, db = await _agent(tmp_path, FakeLLM(), pages={}, resume_mode=True)
    async with db.session() as session:
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/known-org.htm",
            CrawlLogStatus.SUCCESS,
            "seeded-history",
        )

    links = await agent._probe_intermediate_org_pages()

    assert links == []
    assert fetcher.calls == []
    assert "skip already_crawled url=https://www.example.edu.cn/known-org.htm" in agent.execution_log
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


async def test_agent_target_org_units_fuzzy_match_selects_best_single_org_unit(tmp_path):
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
            ["https://scse.example.edu.cn/", "https://ee.example.edu.cn/"],
            200,
        ),
        "https://scse.example.edu.cn/": FetchResult(
            "https://scse.example.edu.cn/",
            "计算机学院 软件学院",
            ["https://scse.example.edu.cn/faculty"],
            200,
        ),
        "https://scse.example.edu.cn": FetchResult(
            "https://scse.example.edu.cn/",
            "计算机学院 软件学院",
            ["https://scse.example.edu.cn/faculty"],
            200,
        ),
        "https://ee.example.edu.cn/": FetchResult(
            "https://ee.example.edu.cn/",
            "电子信息学院",
            ["https://ee.example.edu.cn/faculty"],
            200,
        ),
        "https://ee.example.edu.cn": FetchResult(
            "https://ee.example.edu.cn/",
            "电子信息学院",
            ["https://ee.example.edu.cn/faculty"],
            200,
        ),
        "https://scse.example.edu.cn/faculty": FetchResult(
            "https://scse.example.edu.cn/faculty",
            "faculty profile list",
            [],
            200,
        ),
        "https://ee.example.edu.cn/faculty": FetchResult(
            "https://ee.example.edu.cn/faculty",
            "faculty profile list",
            [],
            200,
        ),
    }

    class TargetOrgLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            state = payload["state"]
            if state == "DISCOVER_ORG_UNIT_PAGES":
                return LLMResult('{"links": ["https://www.example.edu.cn/orgs"]}')
            if state == "EXTRACT_ORG_UNITS":
                return LLMResult(
                    '{"org_units": ['
                    '{"name": "计算机学院（软件学院）", "url": "https://scse.example.edu.cn/", "kind": "college"},'
                    '{"name": "电子信息学院", "url": "https://ee.example.edu.cn/", "kind": "college"}'
                    "]}",
                )
            if state == "EXTRACT_PROFESSORS":
                result = await tool_handlers["save_professors"](
                    org_unit_name="计算机学院（软件学院）",
                    org_unit_url="https://scse.example.edu.cn/",
                    source_url=payload["url"],
                    professors=[{"name": "Ada", "title": "Professor"}],
                )
                return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
            return LLMResult("{}")

    agent, fetcher, db = await _agent(
        tmp_path,
        TargetOrgLLM(),
        pages=pages,
        fetcher_cls=FakeHumanFetcher,
        target_org_units=["计算机学院"],
        org_unit_match_threshold=0.6,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert result.saved_professors == 1
    assert "https://scse.example.edu.cn/faculty" in fetcher.calls
    assert "https://ee.example.edu.cn/faculty" not in fetcher.calls
    await db.close()


async def test_agent_target_org_units_unmatched_fails_early(tmp_path):
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
            "计算机学院",
            [],
            200,
        ),
    }

    class SingleOrgLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "EXTRACT_ORG_UNITS":
                return LLMResult(
                    '{"org_units": [{"name": "计算机学院", "url": "https://scse.example.edu.cn/", "kind": "college"}]}'
                )
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, _fetcher, db = await _agent(
        tmp_path,
        SingleOrgLLM(),
        pages=pages,
        fetcher_cls=FakeHumanFetcher,
        target_org_units=["土木学院"],
        org_unit_match_threshold=0.6,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.FAILED.value
    assert any("Target org units unmatched" in message for message in result.messages)
    await db.close()


async def test_agent_excludes_blacklisted_org_units_before_faculty_discovery(tmp_path):
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
            [
                "https://cs.example.edu.cn/",
                "https://art.example.edu.cn/",
                "https://sports.example.edu.cn/",
                "https://pitt.example.edu.cn/",
                "https://basic.example.edu.cn/",
                "https://jxjy.example.edu.cn/",
                "https://wyz.example.edu.cn/",
                "https://engineer.example.edu.cn/",
            ],
            200,
        ),
        "https://cs.example.edu.cn/": FetchResult(
            "https://cs.example.edu.cn/",
            "计算机学院",
            ["https://cs.example.edu.cn/faculty"],
            200,
        ),
        "https://cs.example.edu.cn": FetchResult(
            "https://cs.example.edu.cn/",
            "计算机学院",
            ["https://cs.example.edu.cn/faculty"],
            200,
        ),
        "https://cs.example.edu.cn/faculty": FetchResult(
            "https://cs.example.edu.cn/faculty",
            "faculty profile list",
            [],
            200,
        ),
        "https://art.example.edu.cn/": FetchResult("https://art.example.edu.cn/", "艺术学院", [], 200),
        "https://sports.example.edu.cn/": FetchResult("https://sports.example.edu.cn/", "体育学院", [], 200),
        "https://pitt.example.edu.cn/": FetchResult("https://pitt.example.edu.cn/", "匹兹堡学院", [], 200),
        "https://basic.example.edu.cn/": FetchResult("https://basic.example.edu.cn/", "基教中心", [], 200),
        "https://jxjy.example.edu.cn/": FetchResult("https://jxjy.example.edu.cn/", "继续教育学院", [], 200),
        "https://wyz.example.edu.cn/": FetchResult("https://wyz.example.edu.cn/", "吴玉章书院", [], 200),
        "https://engineer.example.edu.cn/": FetchResult("https://engineer.example.edu.cn/", "卓越工程师学院", [], 200),
    }

    class OrgUnitFilterLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("filter_task") == "org_unit_exclusion":
                org_units = payload.get("org_units") or []
                excluded = [
                    item for item in org_units if str(item.get("name") or "") in {"匹兹堡学院", "格拉斯哥学院", "中法工程师学院"}
                ]
                included = [item for item in org_units if item not in excluded]
                return LLMResult(
                    json.dumps(
                        {"included_org_units": included, "excluded_org_units": excluded},
                        ensure_ascii=False,
                    )
                )
            if payload.get("state") == "EXTRACT_ORG_UNITS":
                return LLMResult(
                    json.dumps(
                        {
                            "org_units": [
                                {"name": "计算机学院", "url": "https://cs.example.edu.cn/", "kind": "college"},
                                {"name": "艺术学院", "url": "https://art.example.edu.cn/", "kind": "college"},
                                {"name": "体育学院", "url": "https://sports.example.edu.cn/", "kind": "college"},
                                {"name": "匹兹堡学院", "url": "https://pitt.example.edu.cn/", "kind": "college"},
                                {"name": "基教中心", "url": "https://basic.example.edu.cn/", "kind": "center"},
                                {"name": "继续教育学院", "url": "https://jxjy.example.edu.cn/", "kind": "college"},
                                {"name": "吴玉章书院", "url": "https://wyz.example.edu.cn/", "kind": "college"},
                                {"name": "卓越工程师学院", "url": "https://engineer.example.edu.cn/", "kind": "college"},
                            ]
                        },
                        ensure_ascii=False,
                    )
                )
            if payload.get("state") == "EXTRACT_PROFESSORS":
                result = await tool_handlers["save_professors"](
                    org_unit_name="计算机学院",
                    org_unit_url="https://cs.example.edu.cn/",
                    source_url=payload["url"],
                    professors=[{"name": "Ada", "title": "Professor"}],
                )
                return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, fetcher, db = await _agent(
        tmp_path,
        OrgUnitFilterLLM(),
        pages=pages,
        fetcher_cls=FakeHumanFetcher,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert result.saved_professors == 1
    assert "https://cs.example.edu.cn/faculty" in fetcher.calls
    assert "https://art.example.edu.cn/" not in fetcher.calls
    assert "https://sports.example.edu.cn/" not in fetcher.calls
    assert "https://pitt.example.edu.cn/" not in fetcher.calls
    assert "https://basic.example.edu.cn/" not in fetcher.calls
    assert "https://jxjy.example.edu.cn/" not in fetcher.calls
    assert "https://wyz.example.edu.cn/" not in fetcher.calls
    assert "https://engineer.example.edu.cn/" not in fetcher.calls
    await db.close()


async def test_agent_keeps_org_units_when_llm_filter_returns_invalid_json(tmp_path):
    class InvalidFilterLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("filter_task") == "org_unit_exclusion":
                return LLMResult("not json")
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, _fetcher, db = await _agent(tmp_path, InvalidFilterLLM())
    units = await agent._filter_org_unit_payloads_for_discovery(
        [{"name": "国际学院", "url": "https://intl.example.edu.cn/", "kind": "college"}],
        source_url="https://www.example.edu.cn/orgs",
        source="unit_test",
    )

    assert [unit["name"] for unit in units] == ["国际学院"]
    await db.close()


async def test_agent_excludes_person_named_teaching_units_from_llm_filter(tmp_path):
    class PersonNamedFilterLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("filter_task") == "org_unit_exclusion":
                org_units = payload.get("org_units") or []
                excluded = [
                    {**item, "reason": "person_named_teaching_unit"}
                    for item in org_units
                    if str(item.get("name") or "") in {"钱学森学院", "蔡元培学院"}
                ]
                included = [item for item in org_units if str(item.get("name") or "") not in {"钱学森学院", "蔡元培学院"}]
                return LLMResult(
                    json.dumps(
                        {"included_org_units": included, "excluded_org_units": excluded},
                        ensure_ascii=False,
                    )
                )
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, _fetcher, db = await _agent(tmp_path, PersonNamedFilterLLM())
    units = await agent._filter_org_unit_payloads_for_discovery(
        [
            {"name": "钱学森学院", "url": "https://qxs.example.edu.cn/", "kind": "college"},
            {"name": "蔡元培学院", "url": "https://cyp.example.edu.cn/", "kind": "college"},
            {"name": "航空学院", "url": "https://aviation.example.edu.cn/", "kind": "college"},
            {"name": "软件学院", "url": "https://software.example.edu.cn/", "kind": "college"},
        ],
        source_url="https://www.example.edu.cn/orgs",
        source="unit_test",
    )

    assert [unit["name"] for unit in units] == ["航空学院", "软件学院"]
    await db.close()


async def test_agent_resume_skips_existing_blacklisted_org_units(tmp_path):
    pages = {
        "https://cs.example.edu.cn/": FetchResult(
            "https://cs.example.edu.cn/",
            "计算机学院",
            ["https://cs.example.edu.cn/faculty"],
            200,
        ),
        "https://cs.example.edu.cn": FetchResult(
            "https://cs.example.edu.cn/",
            "计算机学院",
            ["https://cs.example.edu.cn/faculty"],
            200,
        ),
        "https://cs.example.edu.cn/faculty": FetchResult(
            "https://cs.example.edu.cn/faculty",
            "faculty profile list",
            [],
            200,
        ),
        "https://art.example.edu.cn/": FetchResult("https://art.example.edu.cn/", "艺术学院", [], 200),
        "https://pitt.example.edu.cn/": FetchResult("https://pitt.example.edu.cn/", "匹兹堡学院", [], 200),
        "https://jxjy.example.edu.cn/": FetchResult("https://jxjy.example.edu.cn/", "继续教育学院", [], 200),
        "https://bhxy.example.edu.cn/": FetchResult("https://bhxy.example.edu.cn/", "北航学院", [], 200),
        "https://engineer.example.edu.cn/": FetchResult("https://engineer.example.edu.cn/", "卓工学院", [], 200),
    }

    class ResumeFilterLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("filter_task") == "org_unit_exclusion":
                org_units = payload.get("org_units") or []
                return LLMResult(
                    json.dumps(
                        {"included_org_units": org_units, "excluded_org_units": []},
                        ensure_ascii=False,
                    )
                )
            if payload.get("state") == "EXTRACT_PROFESSORS":
                result = await tool_handlers["save_professors"](
                    org_unit_name="计算机学院",
                    org_unit_url="https://cs.example.edu.cn/",
                    source_url=payload["url"],
                    professors=[{"name": "Ada", "title": "Professor"}],
                )
                return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, fetcher, db = await _agent(
        tmp_path,
        ResumeFilterLLM(),
        pages=pages,
        fetcher_cls=FakeHumanFetcher,
        resume_mode=True,
    )
    async with db.session() as session:
        await crawler_db.get_or_create_org_unit(
            session,
            name="计算机学院",
            url="https://cs.example.edu.cn/",
            kind="college",
        )
        await crawler_db.get_or_create_org_unit(
            session,
            name="艺术学院",
            url="https://art.example.edu.cn/",
            kind="college",
        )
        await crawler_db.get_or_create_org_unit(
            session,
            name="匹兹堡学院",
            url="https://pitt.example.edu.cn/",
            kind="college",
        )
        await crawler_db.get_or_create_org_unit(
            session,
            name="继续教育学院",
            url="https://jxjy.example.edu.cn/",
            kind="college",
        )
        await crawler_db.get_or_create_org_unit(
            session,
            name="北航学院",
            url="https://bhxy.example.edu.cn/",
            kind="college",
        )
        await crawler_db.get_or_create_org_unit(
            session,
            name="卓工学院",
            url="https://engineer.example.edu.cn/",
            kind="college",
        )

    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert "https://cs.example.edu.cn/faculty" in fetcher.calls
    assert "https://art.example.edu.cn/" not in fetcher.calls
    assert "https://pitt.example.edu.cn/" not in fetcher.calls
    assert "https://jxjy.example.edu.cn/" not in fetcher.calls
    assert "https://bhxy.example.edu.cn/" not in fetcher.calls
    assert "https://engineer.example.edu.cn/" not in fetcher.calls
    async with db.session() as session:
        remaining_units = (await session.execute(select(OrgUnit).order_by(OrgUnit.name))).scalars().all()
        assert [unit.name for unit in remaining_units] == ["计算机学院"]
    await db.close()


async def test_agent_marks_org_unit_status_no_faculty_page_when_no_faculty_links(tmp_path):
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
            ["https://ai.example.edu.cn/"],
            200,
        ),
        "https://ai.example.edu.cn/": FetchResult(
            "https://ai.example.edu.cn/",
            "x",
            [],
            200,
        ),
        "https://ai.example.edu.cn": FetchResult(
            "https://ai.example.edu.cn/",
            "x",
            [],
            200,
        ),
    }

    class AiOrgLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "EXTRACT_ORG_UNITS":
                return LLMResult(
                    '{"org_units": [{"name": "人工智能学院", "url": "https://ai.example.edu.cn/", "kind": "college"}]}'
                )
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, _fetcher, db = await _agent(
        tmp_path,
        AiOrgLLM(),
        pages=pages,
        fetcher_cls=FakeHumanFetcher,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.FAILED.value
    async with db.session() as session:
        org_unit = (
            await session.execute(select(OrgUnit).where(OrgUnit.name == "人工智能学院"))
        ).scalar_one()
        assert org_unit.status == OrgUnitStatus.NO_FACULTY_PAGE.value
    await db.close()


async def test_agent_target_org_units_all_no_faculty_completes_with_warning(tmp_path):
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
            ["https://ai.example.edu.cn/"],
            200,
        ),
        "https://ai.example.edu.cn/": FetchResult(
            "https://ai.example.edu.cn/",
            "x",
            [],
            200,
        ),
        "https://ai.example.edu.cn": FetchResult(
            "https://ai.example.edu.cn/",
            "x",
            [],
            200,
        ),
    }

    class AiOrgLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "EXTRACT_ORG_UNITS":
                return LLMResult(
                    '{"org_units": [{"name": "人工智能学院", "url": "https://ai.example.edu.cn/", "kind": "college"}]}'
                )
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, _fetcher, db = await _agent(
        tmp_path,
        AiOrgLLM(),
        pages=pages,
        fetcher_cls=FakeHumanFetcher,
        target_org_units=["人工智能学院"],
        org_unit_match_threshold=0.6,
    )
    result = await agent.run()

    assert result.status == CrawlStatus.COMPLETED.value
    assert any("no_faculty_page" in message for message in result.messages)
    async with db.session() as session:
        org_unit = (
            await session.execute(select(OrgUnit).where(OrgUnit.name == "人工智能学院"))
        ).scalar_one()
        assert org_unit.status == OrgUnitStatus.NO_FACULTY_PAGE.value
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


async def test_agent_keeps_invalid_json_exhausted_task_recoverable_and_fails_completion(tmp_path):
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

    class AlwaysInvalidProfessorLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "EXTRACT_PROFESSORS":
                return LLMResult(
                    "",
                    [],
                    [ToolCallErrorRecord("save_professors", '{"org_unit_name":"CS","professors":[', "invalid_json")],
                )
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    fetcher = FakeFetcher(pages)
    db = DatabaseManager(sqlite_url(tmp_path / "invalid_exhausted.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")

    agent = CrawlerAgent(
        university_name="InvalidExhaustedU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=AlwaysInvalidProfessorLLM(),
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        min_org_units=1,
        invalid_json_max_retry=1,
    )

    result = await agent.run()

    assert result.status == CrawlStatus.FAILED.value
    assert any("recoverable_extraction_tasks_remaining" in message for message in result.messages)
    async with db.session() as session:
        task = (await session.execute(select(CrawlTask))).scalar_one()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
    assert task.status == CrawlTaskStatus.RETRY.value
    assert task.attempt == 1
    assert task.last_error == "invalid_json_retry_exhausted"
    assert any(failure.failure_type == "invalid_json" and failure.resolver == "retry" for failure in failures)
    await db.close()


def test_professor_prompt_templates_include_retry_constraints():
    detail_instruction = CrawlerPromptBuilder.build_professor_instruction(
        "计算机学院",
        detail_mode=True,
        strict_retry=False,
    )
    list_instruction = CrawlerPromptBuilder.build_professor_instruction(
        "计算机学院",
        detail_mode=False,
        strict_retry=False,
    )
    retry_instruction = CrawlerPromptBuilder.build_professor_instruction(
        "计算机学院",
        detail_mode=True,
        strict_retry=True,
    )
    dynamic_policy = CrawlerPromptBuilder.build_dynamic_system_content(
        {"save_professors"},
        strict_json=True,
    )

    assert CRAWLER_SYSTEM_PROMPT.startswith("You are a cautious university faculty crawler")
    assert "Only save records that include at least one of email/phone/research_areas/bio" in detail_instruction
    assert "科研项目/论文著作/代表论文/科研成果/项目题名" in detail_instruction
    assert "学习工作经历/工作经历/教育经历/教学情况/管理经验" in detail_instruction
    assert "do not return explanatory prose only" in detail_instruction
    assert "名师风采/院士" in detail_instruction
    assert "save visible names and academic titles" in list_instruction
    assert "include a short bio when visible" in retry_instruction
    assert "escape quotes inside JSON strings" in retry_instruction
    assert "avoid bio" not in retry_instruction
    assert dynamic_policy == "Tool call policy: Only call save_professors. Do not invent tool names. Keep output short and strict JSON."


async def test_agent_pipeline_enqueues_detail_pages_as_extraction_tasks(tmp_path):
    list_url = "https://www.example.edu.cn/cs/faculty"
    detail_url = "https://www.example.edu.cn/cs/faculty/info/1.htm"
    pages = {
        list_url: FetchResult(
            list_url,
            "faculty profile list Ada",
            [detail_url],
            200,
        ),
        detail_url: FetchResult(
            detail_url,
            "Ada Professor ada@example.edu.cn research systems",
            [],
            200,
        ),
    }

    class DetailPipelineLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.extract_urls: list[str] = []

        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "EXTRACT_PROFESSORS":
                self.extract_urls.append(payload["url"])
                name = "Ada Detail" if payload["url"] == detail_url else "Ada List"
                result = await tool_handlers["save_professors"](
                    org_unit_name="CS",
                    org_unit_url="https://www.example.edu.cn/cs",
                    source_url=payload["url"],
                    professors=[{"name": name, "title": "Professor", "email": "ada@example.edu.cn"}],
                )
                return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    db = DatabaseManager(sqlite_url(tmp_path / "detail_pipeline.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")
    llm = DetailPipelineLLM()
    agent = CrawlerAgent(
        university_name="DetailPipelineU",
        start_url="https://www.example.edu.cn/",
        location="TestCity",
        db=db,
        llm_client=llm,
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=FakeHumanFetcher(pages),
        min_org_units=1,
    )

    await agent._extract_professors([_QueuedUrl(url=list_url, depth=1, label="CS")])

    async with db.session() as session:
        tasks = (await session.execute(select(CrawlTask))).scalars().all()

    task_kind_by_url = {task.page_url: task.task_kind for task in tasks}
    assert task_kind_by_url[list_url] == "list_page"
    assert task_kind_by_url[detail_url] == "detail_page"
    assert list_url in llm.extract_urls
    assert detail_url in llm.extract_urls
    assert int(agent._pipeline_stats.get("detail_enqueued", 0)) == 1
    assert int(agent._pipeline_stats.get("detail_processed", 0)) == 1
    await db.close()


async def test_list_page_anchor_links_fill_missing_homepage_before_save(tmp_path):
    list_url = "https://www.example.edu.cn/cs/faculty"
    detail_url = "https://www.example.edu.cn/cs/info/1001/ada.htm"

    class ListLinkHomepageLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "EXTRACT_PROFESSORS":
                result = await tool_handlers["save_professors"](
                    org_unit_name="CS",
                    org_unit_url="https://www.example.edu.cn/cs",
                    source_url=payload["url"],
                    professors=[{"name": "张三", "title": "Professor"}],
                )
                return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, _fetcher, db = await _agent(tmp_path, ListLinkHomepageLLM())
    await agent._extract_professors_from_page(
        _QueuedUrl(list_url, 1, "CS"),
        FetchResult(
            list_url,
            "faculty list [张三](https://www.example.edu.cn/cs/info/1001/ada.htm)",
            [detail_url],
            200,
            link_signals=(LinkSignal(url=detail_url, anchor_text="张三", link_order=1),),
        ),
        "save professors",
        detail_mode=False,
    )

    async with db.session() as session:
        professor = (await session.execute(select(Professor).where(Professor.name == "张三"))).scalar_one()
    assert professor.homepage == detail_url
    assert int(agent._pipeline_stats.get("homepage_filled_from_list_links", 0)) == 1
    await db.close()


async def test_detail_enrichment_backfills_db_homepages_without_detail_tasks(tmp_path):
    list_url = "https://www.example.edu.cn/cs/faculty"
    homepage = "https://www.example.edu.cn/cs/info/1001/ada.htm"
    agent, _fetcher, db = await _agent(
        tmp_path,
        FakeLLMResearchDetail(),
        pages={
            list_url: FetchResult(list_url, "faculty list Ada", [], 200),
            homepage: FetchResult(homepage, "Ada 教授\n研究方向: systems", [], 200),
        },
        fetcher_cls=FakeHumanFetcher,
    )
    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": homepage,
                "source_url": list_url,
            },
        )

    await agent._extract_professors([_QueuedUrl(url=list_url, depth=1, label="CS")])

    async with db.session() as session:
        task = (await session.execute(select(CrawlTask).where(CrawlTask.source_url == homepage))).scalar_one()
        professor = (await session.execute(select(Professor).where(Professor.name == "Ada"))).scalar_one()
    assert task.task_kind == CrawlTaskKind.DETAIL_PAGE.value
    assert task.status == CrawlTaskStatus.DONE.value
    assert professor.research_areas == "systems"
    assert int(agent._pipeline_stats.get("detail_backfill_homepages_found", 0)) == 1
    await db.close()


async def test_profile_detail_depth_exception_does_not_open_normal_followups(tmp_path):
    list_url = "https://www.example.edu.cn/cs/faculty"
    detail_url = "https://www.example.edu.cn/cs/info/1001/ada.htm"
    followup_url = "https://www.example.edu.cn/cs/szdw/more.htm"
    pages = {
        list_url: FetchResult(
            list_url,
            "faculty list [张三](https://www.example.edu.cn/cs/info/1001/ada.htm) [More](https://www.example.edu.cn/cs/szdw/more.htm)",
            [detail_url, followup_url],
            200,
            link_signals=(
                LinkSignal(url=detail_url, anchor_text="张三", link_order=1),
                LinkSignal(url=followup_url, anchor_text="More", link_order=2),
            ),
        ),
        detail_url: FetchResult(detail_url, "张三 教授\n研究方向: systems", [], 200),
        followup_url: FetchResult(followup_url, "faculty more Grace", [], 200),
    }
    agent, fetcher, db = await _agent(
        tmp_path,
        FakeLLMResearchDetail(),
        pages=pages,
        fetcher_cls=FakeHumanFetcher,
        max_depth=1,
    )

    await agent._extract_professors([_QueuedUrl(url=list_url, depth=1, label="CS")])

    assert detail_url in fetcher.calls
    assert followup_url not in fetcher.calls
    async with db.session() as session:
        task_urls = {row.source_url: row.task_kind for row in (await session.execute(select(CrawlTask))).scalars().all()}
    assert task_urls[detail_url] == CrawlTaskKind.DETAIL_PAGE.value
    assert followup_url not in task_urls
    await db.close()


async def test_enqueue_extraction_task_skips_existing_unique_task_conflict(tmp_path):
    source_url = "https://www.example.edu.cn/cs/faculty"
    page_text = "faculty list current with browser overlay Ada Professor"
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM(), fetcher_cls=FakeHumanFetcher)
    text_limit = agent._state_text_limit(CrawlerState.EXTRACT_PROFESSORS, detail_mode=True)
    snapshot = agent._compact_page_text(page_text, text_limit)
    incoming_hash = hashlib.sha1(f"{source_url}|{snapshot}".encode("utf-8", errors="ignore")).hexdigest()

    async with db.session() as session:
        detail_task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://www.example.edu.cn/cs",
            source_url=source_url,
            page_url=source_url,
            page_hash="detail-old",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="short detail snapshot",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.FAILED,
        )
        list_task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://www.example.edu.cn/cs",
            source_url=source_url,
            page_url=source_url,
            page_hash=incoming_hash,
            task_kind=CrawlTaskKind.LIST_PAGE,
            page_text_snapshot=snapshot,
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.DONE,
        )

    llm_queue: asyncio.Queue = asyncio.Queue()
    await agent._enqueue_extraction_task(
        _QueuedUrl(url=source_url, depth=1, label="CS"),
        FetchResult(source_url, page_text, [], 200),
        llm_queue=llm_queue,
        detail_mode=True,
        priority=0,
    )

    assert llm_queue.empty()
    assert int(agent._pipeline_stats.get("duplicate_tasks_skipped", 0)) == 1
    assert int(agent._pipeline_stats.get("detail_skipped", 0)) == 1
    async with db.session() as session:
        rows = (await session.execute(select(CrawlTask).order_by(CrawlTask.id))).scalars().all()
    assert [row.id for row in rows] == [detail_task.id, list_task.id]
    assert rows[0].page_hash == "detail-old"
    assert rows[0].task_kind == CrawlTaskKind.DETAIL_PAGE.value
    assert rows[1].page_hash == incoming_hash
    assert rows[1].task_kind == CrawlTaskKind.LIST_PAGE.value
    await db.close()


async def test_recovery_refetches_empty_completion_detail_task_and_updates_research(tmp_path):
    homepage = "https://www.example.edu.cn/cs/info/1001/ada.htm"
    agent, fetcher, db = await _agent(
        tmp_path,
        FakeLLMResearchDetail(),
        pages={
            homepage: FetchResult(
                homepage,
                "Ada 教授\n研究方向: systems",
                [],
                200,
            ),
        },
        fetcher_cls=FakeHumanFetcher,
        resume_mode=True,
    )
    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": homepage,
                "source_url": "https://www.example.edu.cn/cs/faculty",
            },
        )
        await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://www.example.edu.cn/cs",
            source_url=homepage,
            page_url=homepage,
            page_hash="empty-homepage",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.RETRY,
            priority=-10,
            last_error="completion_recrawl_missing_research_areas",
        )

    await agent._extract_professors([], recovery_limit=1)

    assert fetcher.calls == [homepage]
    async with db.session() as session:
        professor = (await session.execute(select(Professor).where(Professor.name == "Ada"))).scalar_one()
        task = (await session.execute(select(CrawlTask))).scalar_one()
        assert professor.research_areas == "systems"
        assert task.status == CrawlTaskStatus.DONE.value
        assert task.page_hash != "empty-homepage"
        assert "研究方向" in task.page_text_snapshot
    await db.close()


async def test_detail_context_marks_academician_when_llm_omits_flag(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLMAcademicianOmitted())
    detail_url = "https://www.example.edu.cn/cs/info/liwei.htm"

    await agent._extract_professors_from_page(
        _QueuedUrl(detail_url, 1, "计算机学院"),
        FetchResult(
            detail_url,
            "李未，北京航空航天大学计算机学院教授，博士生导师，中国科学院院士。李未院士是我国著名的计算机科学家。",
            [],
            200,
        ),
        "save professors",
        detail_mode=True,
        requested_url=detail_url,
    )

    async with db.session() as session:
        professors = (await session.execute(select(Professor).where(Professor.name == "李未"))).scalars().all()
        academician = (await session.execute(select(Academician).where(Academician.name == "李未"))).scalar_one()
        assert professors == []
        assert academician.title == "院士"
    await db.close()


async def test_detail_context_does_not_mark_relation_academician_as_self(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLMRelationAcademicianFlag())
    detail_url = "https://www.example.edu.cn/cs/info/lei.htm"

    await agent._extract_professors_from_page(
        _QueuedUrl(detail_url, 1, "计算机学院"),
        FetchResult(
            detail_url,
            "雷文强，教授。与荷兰皇家科学院院士Maarten de Rijke教授等世界一流学者合作。",
            [],
            200,
        ),
        "save professors",
        detail_mode=True,
        requested_url=detail_url,
    )

    async with db.session() as session:
        professor = (await session.execute(select(Professor).where(Professor.name == "雷文强"))).scalar_one()
        academicians = (await session.execute(select(Academician).where(Academician.name == "雷文强"))).scalars().all()
        assert professor.title == "教授"
        assert academicians == []
    await db.close()


async def test_recovery_keeps_blocked_completion_detail_task_retry_without_llm(tmp_path):
    homepage = "https://www.example.edu.cn/cs/info/1001/blocked.htm"
    agent, fetcher, db = await _agent(
        tmp_path,
        FakeLLMResearchDetail(),
        pages={
            homepage: FetchResult(
                homepage,
                "",
                [],
                403,
                block_reason="waf",
            ),
        },
        fetcher_cls=FakeHumanFetcher,
        resume_mode=True,
    )
    async with db.session() as session:
        await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://www.example.edu.cn/cs",
            source_url=homepage,
            page_url=homepage,
            page_hash="empty-homepage",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.RETRY,
            priority=-10,
            last_error="completion_recrawl_missing_research_areas",
        )

    await agent._extract_professors([], recovery_limit=1)

    assert fetcher.calls == [homepage]
    async with db.session() as session:
        task = (await session.execute(select(CrawlTask))).scalar_one()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
        assert task.status == CrawlTaskStatus.RETRY.value
        assert task.last_error == "completion_recrawl_refetch_blocked:waf"
        assert not any(failure.failure_type == "no_structured_data" for failure in failures)
    await db.close()


async def test_recovery_uses_existing_nonempty_snapshot_without_refetch(tmp_path):
    homepage = "https://www.example.edu.cn/cs/info/1001/ada.htm"
    agent, fetcher, db = await _agent(
        tmp_path,
        FakeLLM(),
        pages={},
        fetcher_cls=FakeHumanFetcher,
        resume_mode=True,
    )
    async with db.session() as session:
        await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://www.example.edu.cn/cs",
            source_url=homepage,
            page_url=homepage,
            page_hash="existing-snapshot",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="faculty list Ada",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.RETRY,
            last_error="invalid_json_retry",
        )

    await agent._extract_professors([], recovery_limit=1)

    assert fetcher.calls == []
    async with db.session() as session:
        task = (await session.execute(select(CrawlTask))).scalar_one()
        assert task.status == CrawlTaskStatus.DONE.value
        assert task.page_hash == "existing-snapshot"
    await db.close()


async def test_recovery_uses_refetched_snapshot_without_repeating_refetch(tmp_path):
    homepage = "https://www.example.edu.cn/cs/info/1001/ada.htm"
    agent, fetcher, db = await _agent(
        tmp_path,
        FakeLLMResearchDetail(),
        pages={},
        fetcher_cls=FakeHumanFetcher,
        resume_mode=True,
    )
    async with db.session() as session:
        await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://www.example.edu.cn/cs",
            source_url=homepage,
            page_url=homepage,
            page_hash="refetched-homepage",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="Ada 教授\n研究方向: systems",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.RETRY,
            priority=-10,
            last_error="completion_recrawl_refetched",
        )

    await agent._extract_professors([], recovery_limit=1)

    assert fetcher.calls == []
    assert int(agent._pipeline_stats.get("recovery_refetch_skipped_with_snapshot", 0)) == 1
    assert int(agent._pipeline_stats.get("recovery_consumed", 0)) == 1
    async with db.session() as session:
        task = (await session.execute(select(CrawlTask))).scalar_one()
        assert task.status == CrawlTaskStatus.DONE.value
    await db.close()


async def test_enqueue_extraction_task_skips_detail_redirect_to_home(tmp_path):
    source_url = "https://dept3.buaa.edu.cn/info/1191/2837.htm"
    final_url = "https://dept3.buaa.edu.cn/"
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM(), fetcher_cls=FakeHumanFetcher)
    agent.start_url = "https://www.buaa.edu.cn/"
    llm_queue: asyncio.Queue = asyncio.Queue()

    await agent._enqueue_extraction_task(
        _QueuedUrl(url=source_url, depth=4, label="自动化科学与电气工程学院"),
        FetchResult(final_url, "自动化科学与电气工程学院 首页 新闻资讯 师资建设", [], 200),
        llm_queue=llm_queue,
        detail_mode=True,
        priority=0,
    )

    assert llm_queue.empty()
    assert int(agent._pipeline_stats.get("redirect_skipped", 0)) == 1
    assert int(agent._pipeline_stats.get("detail_redirect_skipped", 0)) == 1
    assert int(agent._pipeline_stats.get("detail_skipped", 0)) == 1
    async with db.session() as session:
        rows = (await session.execute(select(CrawlTask))).scalars().all()
    assert rows == []
    await db.close()


async def test_pipeline_save_payloads_counts_only_created_records_and_logs_roster_overlap(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    numerals = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
    task = SimpleNamespace(
        task_id=1,
        org_unit_name="CS",
        task_kind="list_page",
        detail_mode=False,
    )

    first = await agent._save_payloads_to_db(
        [
            {
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "source_url": "https://www.example.edu.cn/cs/faculty",
                "professors": [{"name": f"教师{item}", "title": "Professor"} for item in numerals],
            }
        ],
        task=task,
    )
    second = await agent._save_payloads_to_db(
        [
            {
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "source_url": "https://www.example.edu.cn/cs/faculty-duplicate",
                "professors": [{"name": f"教师 {item}", "title": "Professor"} for item in numerals],
            }
        ],
        task=task,
    )

    assert first["created"] == 10
    assert second["accepted"] == 10
    assert second["created"] == 0
    assert second["deduped_by_name_key"] == 10
    assert agent.saved_professors == 10
    assert int(agent._pipeline_stats.get("list_roster_overlap_high", 0)) == 1

    async with db.session() as session:
        assert await crawler_db.count_professors(session) == 10
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


async def test_professor_instruction_distinguishes_list_and_detail_field_strictness(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())

    list_instruction = agent._build_professor_instruction("CS", detail_mode=False, strict_retry=False)
    detail_instruction = agent._build_professor_instruction("CS", detail_mode=True, strict_retry=False)

    assert "save visible names and academic titles" in list_instruction
    assert "email/phone/research_areas are absent" in list_instruction
    assert "Only save records that include at least one of email/phone/research_areas/bio" in detail_instruction
    assert "linked anchor text" in detail_instruction
    assert "科研项目/论文著作/代表论文/科研成果/项目题名" in detail_instruction
    assert "个人简介/简介/个人概况/学习工作经历/工作经历/教育经历/教学情况/管理经验" in detail_instruction
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
    assert result.saved_professors == 1
    assert int(agent._pipeline_stats.get("records_accepted", 0)) == 2
    assert int(agent._pipeline_stats.get("deduped_by_name_key", 0)) >= 1
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


async def test_extract_org_units_accepts_included_org_units_fallback(tmp_path):
    org_page = "https://www.example.edu.cn/xybm/jxkydw_yjjg.htm"
    pages = {
        org_page: FetchResult(
            org_page,
            "信息与通信工程学院 电子科学与工程学院 财务处",
            [
                "https://www.example.edu.cn/sice",
                "https://www.example.edu.cn/ese",
                "https://www.example.edu.cn/cwc",
            ],
            200,
        ),
    }

    class IncludedOrgUnitsLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") == "EXTRACT_ORG_UNITS":
                return LLMResult(
                    json.dumps(
                        {
                            "included_org_units": [
                                {
                                    "name": "信息与通信工程学院",
                                    "url": "https://www.example.edu.cn/sice",
                                    "kind": "college",
                                },
                                {
                                    "name": "电子科学与工程学院",
                                    "url": "https://www.example.edu.cn/ese",
                                    "kind": "college",
                                },
                            ],
                            "excluded_org_units": [
                                {
                                    "name": "财务处",
                                    "url": "https://www.example.edu.cn/cwc",
                                    "kind": "admin",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                )
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    agent, _fetcher, db = await _agent(
        tmp_path,
        IncludedOrgUnitsLLM(),
        pages=pages,
        org_unit_llm_filter_enabled=False,
    )

    units = await agent._extract_org_units([_QueuedUrl(url=org_page, depth=1)])

    names = {unit.name for unit in units}
    assert {"信息与通信工程学院", "电子科学与工程学院"} <= names
    assert "财务处" not in names
    await db.close()


async def test_detail_profile_links_are_scoped_to_same_host_and_related_paths(tmp_path):
    from agents.crawler.fetchers.link_signals import LinkSignal

    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    anchored_detail = "https://www.example.edu.cn/szdw/zzjs1/jjx.htm"
    strong_detail = "https://www.example.edu.cn/info/1012/3958.htm"
    category_page = "https://www.example.edu.cn/szdw/rgznx.htm"
    links = [
        anchored_detail,
        strong_detail,
        category_page,
        "https://www.example.edu.cn/gywm/jxdw1/jjx.htm",
        "https://sub.example.edu.cn/info/1012/3958.htm",
        "https://www.example.edu.cn/news/1234.htm",
        "https://www.example.edu.cn/szdw/tzgg/202603/t20260315_1024.shtml",
        "https://www.example.edu.cn/szdw/renshi/202603/t20260310_1122.shtml",
        "https://www.example.edu.cn/szdw/rszc/4.htm",
    ]
    out = agent._extract_detail_profile_links(
        links,
        "https://www.example.edu.cn/szdw.htm",
        link_signals=(
            LinkSignal(url=anchored_detail, anchor_text="贾俊祥 教授"),
            LinkSignal(url=category_page, anchor_text="人工智能系"),
        ),
    )
    assert anchored_detail in out
    assert strong_detail in out
    assert category_page not in out
    assert "https://www.example.edu.cn/gywm/jxdw1/jjx.htm" not in out
    assert "https://sub.example.edu.cn/info/1012/3958.htm" not in out
    assert "https://www.example.edu.cn/news/1234.htm" not in out
    assert "https://www.example.edu.cn/szdw/tzgg/202603/t20260315_1024.shtml" not in out
    assert "https://www.example.edu.cn/szdw/renshi/202603/t20260310_1122.shtml" not in out
    assert "https://www.example.edu.cn/szdw/rszc/4.htm" not in out
    assert int(agent._pipeline_stats.get("detail_links_dropped_noise", 0)) >= 1
    await db.close()


async def test_scu_computer_faculty_sections_stay_list_pages_and_info_links_are_details(tmp_path):
    list_url = "https://cs.scu.edu.cn/szdw.htm"
    section_urls = [
        "https://cs.scu.edu.cn/szdw/msfc/ys.htm",
        "https://cs.scu.edu.cn/szdw/msfc/jcjs.htm",
        "https://cs.scu.edu.cn/szdw/msfc/yxqnjjhdz.htm",
        "https://cs.scu.edu.cn/szdw/msfc/gjjcqnjjhdz.htm",
        "https://cs.scu.edu.cn/szdw/msfc.htm",
        "https://cs.scu.edu.cn/szdw/jjzx.htm",
        "https://cs.scu.edu.cn/szdw/cxzx.htm",
        "https://cs.scu.edu.cn/szdw/rgznx.htm",
        "https://cs.scu.edu.cn/szdw/rjgcx.htm",
        "https://cs.scu.edu.cn/szdw/jsjkxx.htm",
        "https://cs.scu.edu.cn/szdw/jsjgcx.htm",
        "https://cs.scu.edu.cn/szdw/gxnjszx.htm",
        "https://cs.scu.edu.cn/szdw/txtxyrjgcs.htm",
        "https://cs.scu.edu.cn/szdw/jczx.htm",
        "https://cs.scu.edu.cn/szdw/sjkxy.htm",
    ]
    detail_by_section = {
        url: f"https://cs.scu.edu.cn/info/{1300 + index}/{13760 + index}.htm"
        for index, url in enumerate(section_urls, start=1)
    }
    pages = {
        list_url: FetchResult(
            list_url,
            "师资队伍 计算机学院 教师列表 教授 副教授",
            [*section_urls, *detail_by_section.values()],
            200,
        ),
    }
    for index, section_url in enumerate(section_urls, start=1):
        detail_url = detail_by_section[section_url]
        pages[section_url] = FetchResult(
            section_url,
            f"教师列表 教授 副教授 教师{index}",
            [detail_url],
            200,
        )
        pages[detail_url] = FetchResult(
            detail_url,
            f"教师{index} 教授 邮箱 teacher{index}@scu.edu.cn 研究方向 人工智能",
            [],
            200,
        )

    class ScuComputerLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.extract_urls: list[str] = []

        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") != "EXTRACT_PROFESSORS":
                return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)
            self.extract_urls.append(payload["url"])
            if "/info/" not in payload["url"]:
                return LLMResult("{}")
            name = payload["page_text"].split(" ", 1)[0]
            result = await tool_handlers["save_professors"](
                org_unit_name="计算机学院",
                org_unit_url=list_url,
                source_url=payload["url"],
                professors=[{"name": name, "title": "教授", "email": "teacher@example.scu.edu.cn"}],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])

    db = DatabaseManager(sqlite_url(tmp_path / "scu_computer.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")
    llm = ScuComputerLLM()
    agent = CrawlerAgent(
        university_name="四川大学",
        start_url="https://www.scu.edu.cn/",
        location="成都",
        db=db,
        llm_client=llm,
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=FakeHumanFetcher(pages),
        min_org_units=1,
        max_depth=4,
    )

    await agent._extract_professors([_QueuedUrl(url=list_url, depth=1, label="计算机学院")])

    async with db.session() as session:
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
    task_kind_by_url = {task.page_url: task.task_kind for task in tasks}
    for section_url in section_urls:
        assert task_kind_by_url[section_url] == "list_page"
    for detail_url in detail_by_section.values():
        assert task_kind_by_url[detail_url] == "detail_page"
    assert not any(task.task_kind == "detail_page" and "/szdw/" in task.page_url for task in tasks)
    assert int(agent._pipeline_stats.get("followups_scheduled", 0)) >= len(section_urls)
    assert int(agent._pipeline_stats.get("detail_links_dropped_directory", 0)) >= len(section_urls)
    await db.close()


async def test_scu_query_teamlist_detail_links_are_detail_tasks(tmp_path):
    list_url = "https://saa.scu.edu.cn/teamlist.htm"
    detail_url = "https://saa.scu.edu.cn/teamlist.htm?action=detailTeam&uuinId=661618903336854"
    pages = {
        list_url: FetchResult(
            list_url,
            "空天科学与工程学院 师资队伍 王靖宇",
            [detail_url],
            200,
            link_signals=(
                SimpleNamespace(
                    url=detail_url,
                    anchor_text="王靖宇",
                    heading_text="师资队伍",
                    parent_tags_or_classes=("team-list",),
                    link_order=1,
                ),
            ),
        ),
        detail_url: FetchResult(
            detail_url,
            "王靖宇 副研究员 邮箱 wangjingyu@scu.edu.cn 研究方向 航空发动机旋转机械数值模拟方法研究",
            [],
            200,
        ),
    }

    class ScuAerospaceLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") != "EXTRACT_PROFESSORS" or payload["url"] != detail_url:
                return LLMResult("{}")
            result = await tool_handlers["save_professors"](
                org_unit_name="空天科学与工程学院",
                org_unit_url=list_url,
                source_url=payload["url"],
                professors=[
                    {
                        "name": "王靖宇",
                        "title": "副研究员",
                        "email": "wangjingyu@scu.edu.cn",
                        "research_areas": "航空发动机旋转机械数值模拟方法研究",
                    }
                ],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])

    db = DatabaseManager(sqlite_url(tmp_path / "scu_aerospace_query_detail.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")
    agent = CrawlerAgent(
        university_name="四川大学",
        start_url="https://www.scu.edu.cn/",
        location="成都",
        db=db,
        llm_client=ScuAerospaceLLM(),
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=FakeHumanFetcher(pages),
        max_depth=3,
        max_backtracks=3,
        min_org_units=1,
    )

    await agent._extract_professors([_QueuedUrl(url=list_url, depth=1, label="空天科学与工程学院")])

    async with db.session() as session:
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
    task_kind_by_url = {task.page_url: task.task_kind for task in tasks}
    assert task_kind_by_url[list_url] == "list_page"
    assert task_kind_by_url[detail_url] == "detail_page"
    assert int(agent._pipeline_stats.get("followups_scheduled", 0)) == 0
    await db.close()


async def test_dynamic_form_pagination_states_schedule_distinct_list_tasks(tmp_path):
    list_url = "https://faculty.uestc.edu.cn/xylb.jsp?id=2031&lang=zh_CN&urltype=tsites.CollegeTeacherList&wbtreeid=1021"
    page2_identity = (
        list_url
        + "&__ycl_kind=form&__ycl_form=fromWen&__ycl_field=fromWenNOWPAGE&__ycl_page=2"
    )
    pagination_state = {
        "kind": "form_submit",
        "state_id": "form:fromWen:fromWenNOWPAGE:2",
        "label": "fromWen 第 2 页",
        "page_index": 2,
        "total_pages": 2,
        "form_name": "fromWen",
        "fields": {"fromWenNOWPAGE": "2"},
        "submit": True,
        "synthetic_url": page2_identity,
        "url": list_url,
    }
    pages = {
        list_url: FetchResult(
            list_url,
            "教师列表 faculty page one 教师一 教授",
            [],
            200,
            pagination_states=(pagination_state,),
        ),
        page2_identity: FetchResult(
            page2_identity,
            "教师列表 faculty page two 教师二 教授",
            [],
            200,
        ),
    }

    class DynamicFetcher(FakeHumanFetcher):
        def __init__(self, pages):
            super().__init__(pages)
            self.action_calls: list[dict[str, object]] = []

        async def fetch(self, url, **kwargs):
            self.calls.append(url)
            self.action_calls.append(kwargs)
            identity_url = kwargs.get("identity_url")
            return self.pages[identity_url or url]

    class DynamicLLM(FakeLLM):
        async def chat(self, messages, tools=None, tool_handlers=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("state") != "EXTRACT_PROFESSORS":
                return LLMResult("{}")
            name = "教师二" if "__ycl_page=2" in payload["url"] else "教师一"
            result = await tool_handlers["save_professors"](
                org_unit_name="计算机科学与工程学院",
                org_unit_url=list_url,
                source_url=payload["url"],
                professors=[{"name": name, "title": "教授"}],
            )
            return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])

    db = DatabaseManager(sqlite_url(tmp_path / "dynamic_form_pagination.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")
    fetcher = DynamicFetcher(pages)
    agent = CrawlerAgent(
        university_name="电子科技大学",
        start_url="https://faculty.uestc.edu.cn/",
        location="成都",
        db=db,
        llm_client=DynamicLLM(),
        skill_manager=manager,
        context_manager=ContextManager(),
        fetcher=fetcher,
        min_org_units=1,
        max_depth=2,
    )

    await agent._extract_professors([_QueuedUrl(url=list_url, depth=1, label="计算机科学与工程学院")])

    async with db.session() as session:
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
        professors = (await session.execute(select(Professor))).scalars().all()
    source_urls = {task.source_url for task in tasks}
    assert list_url in source_urls
    assert page2_identity in source_urls
    assert any(call.get("action", {}).get("form_name") == "fromWen" for call in fetcher.action_calls)
    assert {professor.name for professor in professors} == {"教师一", "教师二"}
    assert int(agent._pipeline_stats.get("pagination_scheduled", 0)) >= 1
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


async def test_buaa_automation_active_teacher_roster_enters_task_queue(tmp_path):
    from agents.crawler.fetchers.link_signals import LinkSignal

    home_url = "https://dept3.buaa.edu.cn/"
    roster_url = "https://dept3.buaa.edu.cn/szjs/zzjs/znxtykzgcx.htm"
    followup_a = "https://dept3.buaa.edu.cn/szjs/zzjs/jcyzdhgcx.htm"
    followup_b = "https://dept3.buaa.edu.cn/szjs/zzjs/dqgcx.htm"
    elite_url = "https://dept3.buaa.edu.cn/szjs/jcrc.htm"
    mentor_url = "https://dept3.buaa.edu.cn/szjs/yjsds.htm"
    pages = {
        "https://www.buaa.edu.cn/": FetchResult(
            "https://www.buaa.edu.cn/",
            "home",
            ["https://www.buaa.edu.cn/jgsz/jxkyjg02.htm"],
            200,
        ),
        "https://www.buaa.edu.cn/jgsz/jxkyjg02.htm": FetchResult(
            "https://www.buaa.edu.cn/jgsz/jxkyjg02.htm",
            "机构设置 自动化科学与电气工程学院",
            [home_url],
            200,
        ),
        "https://dept3.buaa.edu.cn": FetchResult(
            home_url,
            "自动化科学与电气工程学院 师资建设 在职教师 杰出人才 研究生导师",
            [roster_url, elite_url, mentor_url],
            200,
            link_signals=(
                LinkSignal(url=roster_url, anchor_text="在职教师", heading_text="师资建设", parent_tags_or_classes=("nav.menu",), link_order=1),
                LinkSignal(url=elite_url, anchor_text="杰出人才", heading_text="师资建设", parent_tags_or_classes=("nav.menu",), link_order=2),
                LinkSignal(url=mentor_url, anchor_text="研究生导师", heading_text="师资建设", parent_tags_or_classes=("nav.menu",), link_order=3),
            ),
        ),
        home_url: FetchResult(
            home_url,
            "自动化科学与电气工程学院 师资建设 在职教师 杰出人才 研究生导师",
            [roster_url, elite_url, mentor_url],
            200,
            link_signals=(
                LinkSignal(url=roster_url, anchor_text="在职教师", heading_text="师资建设", parent_tags_or_classes=("nav.menu",), link_order=1),
                LinkSignal(url=elite_url, anchor_text="杰出人才", heading_text="师资建设", parent_tags_or_classes=("nav.menu",), link_order=2),
                LinkSignal(url=mentor_url, anchor_text="研究生导师", heading_text="师资建设", parent_tags_or_classes=("nav.menu",), link_order=3),
            ),
        ),
        roster_url: FetchResult(
            roster_url,
            "智能系统与控制工程系 教授 郭雷 副教授 张海",
            [followup_a, followup_b, elite_url],
            200,
        ),
        followup_a: FetchResult(
            followup_a,
            "检测与自动化工程系 教授 王强",
            [],
            200,
        ),
        followup_b: FetchResult(
            followup_b,
            "电气工程系 教授 李强",
            [],
            200,
        ),
    }

    class BuaaAutomationLLM(FakeLLM):
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
                    '{"org_units": [{"name": "自动化科学与电气工程学院", "url": "https://dept3.buaa.edu.cn", "kind": "college"}]}'
                )
            if state == "FIND_FACULTY_PAGES":
                raise AssertionError("structural roster detection should avoid LLM fallback")
            if state == "EXTRACT_PROFESSORS":
                self.extract_urls.append(payload["url"])
                result = await tool_handlers["save_professors"](
                    org_unit_name="自动化科学与电气工程学院",
                    org_unit_url=payload["url"],
                    source_url=payload["url"],
                    professors=[{"name": f"教师{len(self.extract_urls)}", "title": "教授"}],
                )
                return LLMResult("", [ToolCallRecord("save_professors", {"professors": []}, result)])
            return LLMResult("{}")

    db = DatabaseManager(sqlite_url(tmp_path / "buaa_automation.db"))
    await db.init_db()
    skills_dir = tmp_path / "skills"
    manager = SkillManager(skills_dir, db, "crawler")
    await manager.create_skill("extract-links", "## Goal\nlinks\n", "links")
    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")
    agent = CrawlerAgent(
        university_name="北京航空航天大学",
        start_url="https://www.buaa.edu.cn/",
        location="北京",
        db=db,
        llm_client=BuaaAutomationLLM(),
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

    assert roster_url in task_urls
    assert followup_a in task_urls
    assert followup_b in task_urls
    assert elite_url not in task_urls
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


async def test_professor_gate_skips_notice_issuance_title(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    skip, reason = agent._should_skip_professor_llm(
        url="https://www.example.edu.cn/szdw/info/1010/1234.htm",
        text="# 关于印发《教师岗位聘任办法》的通知\n发布时间：2026-01-01\n各单位：",
    )
    assert skip
    assert reason == "notice_issuance_title"

    spaced_skip, spaced_reason = agent._should_skip_professor_llm(
        url="https://www.example.edu.cn/szdw/info/1010/1235.htm",
        text="关于 印发 教师岗位聘任办法 的 通知\n各学院：",
    )
    assert spaced_skip
    assert spaced_reason == "notice_issuance_title"
    await db.close()


async def test_professor_gate_keeps_profile_with_incidental_notice_words(tmp_path):
    agent, _fetcher, db = await _agent(tmp_path, FakeLLM())
    keep, reason = agent._should_skip_professor_llm(
        url="https://www.example.edu.cn/szdw/info/1010/teacher-zhang.htm",
        text=(
            "# 张三\n"
            "职称：教授\n"
            "邮箱：zhangsan@example.edu.cn\n"
            "研究方向：网络安全。曾参与学院关于印发科研通知材料的整理工作。"
        ),
    )
    assert not keep
    assert reason == ""
    await db.close()


async def test_run_extraction_task_skips_notice_issuance_without_llm_call(tmp_path):
    class CountingLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def chat(self, messages, tools=None, tool_handlers=None):
            self.calls += 1
            return await super().chat(messages, tools=tools, tool_handlers=tool_handlers)

    llm = CountingLLM()
    agent, _fetcher, db = await _agent(tmp_path, llm)
    task = _ExtractionTaskItem(
        task_id=42,
        university="TestU",
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/info/1010/notice.htm",
        page_url="https://www.example.edu.cn/cs/info/1010/notice.htm",
        page_hash="notice",
        page_text_snapshot="# 关于印发《教师岗位聘任办法》的通知\n各单位：请遵照执行。",
        allowed_tools=["save_professors"],
        task_kind=CrawlTaskKind.LIST_PAGE.value,
    )

    outcome = await agent._run_extraction_task(task, "save professors")

    assert outcome.skipped_by_gate is True
    assert outcome.skip_reason == "notice_issuance_title"
    assert outcome.payloads == []
    assert outcome.invalid_json_events == []
    assert llm.calls == 0
    assert int(agent._pipeline_stats.get("llm_calls_skipped_by_gate", 0)) == 1
    assert int(agent._pipeline_stats.get("llm_calls_total", 0)) == 0
    await db.close()

