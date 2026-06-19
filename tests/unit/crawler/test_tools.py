from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from agents.crawler.models import Academician, Professor
from agents.crawler.tools import SAVE_PROFESSORS_TOOL, get_crawler_tools
from runtime.database import DatabaseManager
from runtime.skills import SkillManager
from tests.conftest import sqlite_url


async def test_crawler_tool_handlers_save_professors_and_extract_links(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "tools.db"))
    await db.init_db()
    manager = SkillManager(tmp_path / "skills", db, "crawler")
    tools = get_crawler_tools(db, manager)

    result = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/faculty",
        professors=[{"name": "Ada", "title": "Professor"}],
    )
    assert result["accepted"] == 1
    assert result["created"] == 1
    assert result["saved"] == 1

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        assert len(professors) == 1
        assert professors[0].title == "教授"
        assert professors[0].org_unit_name == "CS"

    await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/faculty",
        professors=[{"name": "王俊", "title": "Professor"}],
    )
    name_key_deduped = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/info/1001/wangjun.htm",
        professors=[{"name": "王 俊", "email": "wangjun@example.edu.cn"}],
    )
    assert name_key_deduped["accepted"] == 1
    assert name_key_deduped["created"] == 0
    assert name_key_deduped["updated"] == 1
    assert name_key_deduped["deduped_by_name_key"] == 1
    async with db.session() as session:
        wang = (await session.execute(select(Professor).where(Professor.name == "王俊"))).scalars().all()
        assert len(wang) == 1
        assert wang[0].name_key == "王俊"
        assert wang[0].email == "wangjun@example.edu.cn"

    await tools["save_professors"](
        org_unit_name="Software",
        org_unit_url="https://soft.example.edu.cn",
        source_url="https://soft.example.edu.cn/list",
        professors=[
            {
                "name": "李雷（兼）",
                "homepage": "https://soft.example.edu.cn/info/1001/9.htm",
            }
        ],
    )
    homepage_deduped = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://cs.example.edu.cn",
        source_url="https://soft.example.edu.cn/info/1001/9.htm",
        professors=[
            {
                "name": "李雷",
                "homepage": "https://soft.example.edu.cn/info/1001/9.htm#detail",
                "email": "lilei@example.edu.cn",
            }
        ],
    )
    assert homepage_deduped["accepted"] == 1
    assert homepage_deduped["created"] == 0
    assert homepage_deduped["updated"] == 1
    assert homepage_deduped["deduped_by_homepage"] == 1

    links = await tools["extract_links"](
        links=["https://cs.example.edu.cn/faculty", "https://other.example.com/"],
        base_url="https://www.example.edu.cn/",
        keywords=["faculty"],
    )
    assert links == {"links": ["https://cs.example.edu.cn/faculty"]}

    links_from_dict_payload = await tools["extract_links"](
        links=[
            {"url": "https://www.example.edu.cn/jgsz/yxsz.htm", "text": "院系设置"},
            {"href": "https://www.example.edu.cn/xygk/xxjj.htm", "text": "学校简介"},
            {"url": "https://www.other.com/faculty", "text": "external"},
            {"text": "invalid"},
        ],
        base_url="https://www.example.edu.cn/",
        keywords=["jgsz", "yxsz"],
    )
    assert links_from_dict_payload == {"links": ["https://www.example.edu.cn/jgsz/yxsz.htm"]}

    filtered = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/faculty",
        professors=[
            {"name": "Academician A", "title": "Academician"},
            {
                "name": "Prof B",
                "title": "Professor (Chair Professor)",
                "email": "",
                "phone": "N/A",
                "enrollment_pref": "PhD Supervisor",
            },
        ],
    )
    assert filtered["accepted"] == 2
    assert filtered["created"] == 1
    assert filtered["academicians_saved"] == 1
    async with db.session() as session:
        prof_b = (
            await session.execute(select(Professor).where(Professor.name == "Prof B"))
        ).scalar_one()
        assert prof_b.title == "教授"
        assert prof_b.email is None
        assert prof_b.phone is None
        assert prof_b.enrollment_pref == "PhD Supervisor"
    retired_filtered = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/retired",
        professors=[
            {"name": "Prof C", "title": "Retired Professor"},
        ],
    )
    assert retired_filtered["accepted"] == 0
    assert retired_filtered["created"] == 0
    assert retired_filtered["saved"] == 0
    assert retired_filtered["filtered_retired"] == 1

    postdoc_filtered = await tools["save_professors"](
        org_unit_name="AI",
        org_unit_url="https://soai.example.edu.cn",
        source_url="https://soai.example.edu.cn/cn/show/433",
        professors=[
            {
                "name": "张赟",
                "title": "教授",
                "bio": "张赟博士后，2025年毕业于上海交通大学，合作导师为严骏驰教授。",
            },
        ],
    )
    assert postdoc_filtered["accepted"] == 0
    assert postdoc_filtered["created"] == 0
    assert postdoc_filtered["saved"] == 0
    assert postdoc_filtered["filtered_postdoc"] == 1

    promoted_professor = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/faculty",
        professors=[{"name": "Promote A", "title": "Professor", "email": "promote@example.edu.cn"}],
    )
    assert promoted_professor["accepted"] == 1
    assert promoted_professor["created"] == 1
    assert promoted_professor["saved"] == 1
    promoted_to_academician = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/detail",
        professors=[{"name": "Promote A", "title": "Academician", "email": "promote@example.edu.cn"}],
    )
    assert promoted_to_academician["created"] == 0
    assert promoted_to_academician["saved"] == 0
    assert promoted_to_academician.get("academicians_saved") == 1
    assert promoted_to_academician.get("professors_deleted_as_academician_duplicates") == 1

    async with db.session() as session:
        promoted_professors = (
            await session.execute(select(Professor).where(Professor.name == "Promote A"))
        ).scalars().all()
        assert len(promoted_professors) == 0
        promoted_academician = (
            await session.execute(select(Academician).where(Academician.name == "Promote A"))
        ).scalar_one()
        assert promoted_academician.email == "promote@example.edu.cn"

    seeded_academician = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/academicians",
        professors=[{"name": "Dup A", "title": "Academician", "email": "dup@example.edu.cn"}],
    )
    assert seeded_academician["accepted"] == 1
    assert seeded_academician["created"] == 0
    assert seeded_academician["saved"] == 0
    assert seeded_academician["academicians_saved"] == 1
    deduped = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/faculty",
        professors=[
            {
                "name": "Dup A",
                "title": "Professor",
                "email": "dup@example.edu.cn",
                "bio": "focus on systems",
            }
        ],
    )
    assert deduped["created"] == 0
    assert deduped["saved"] == 0
    assert deduped.get("deduped_by_academician") == 1
    assert deduped.get("academicians_enriched") == 1
    async with db.session() as session:
        dup_prof = (
            await session.execute(select(Professor).where(Professor.name == "Dup A"))
        ).scalars().all()
        assert len(dup_prof) == 0
        dup_academician = (
            await session.execute(select(Academician).where(Academician.name == "Dup A"))
        ).scalar_one()
        assert dup_academician.bio == "focus on systems"

    await db.close()


async def test_save_professors_promotes_academician_from_bio(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "tools_academician_bio.db"))
    await db.init_db()
    manager = SkillManager(tmp_path / "skills", db, "crawler")
    tools = get_crawler_tools(db, manager)

    result = await tools["save_professors"](
        org_unit_name="计算机学院",
        org_unit_url="https://scse.buaa.edu.cn/",
        source_url="https://scse.buaa.edu.cn/info/liwei.htm",
        professors=[
            {
                "name": "李未",
                "title": "教授",
                "bio": "李未，北京航空航天大学计算机学院教授，博士生导师，中国科学院院士。",
            }
        ],
    )

    assert result["accepted"] == 1
    assert result["created"] == 0
    assert result["saved"] == 0
    assert result["academicians_saved"] == 1
    async with db.session() as session:
        professors = (await session.execute(select(Professor).where(Professor.name == "李未"))).scalars().all()
        academician = (await session.execute(select(Academician).where(Academician.name == "李未"))).scalar_one()
        assert professors == []
        assert academician.title == "院士"
    await db.close()


async def test_save_professors_does_not_trust_unverified_academician_flag(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "tools_false_academician_flag.db"))
    await db.init_db()
    manager = SkillManager(tmp_path / "skills", db, "crawler")
    tools = get_crawler_tools(db, manager)

    result = await tools["save_professors"](
        org_unit_name="计算机学院",
        org_unit_url="https://cs.scu.edu.cn/",
        source_url="https://cs.scu.edu.cn/info/lei.htm",
        professors=[
            {
                "name": "雷文强",
                "title": "教授",
                "is_academician": True,
                "bio": "与荷兰皇家科学院院士Maarten de Rijke教授等世界一流学者合作。",
            }
        ],
    )

    assert result["accepted"] == 1
    assert result["created"] == 1
    assert "academicians_saved" not in result
    async with db.session() as session:
        professor = (await session.execute(select(Professor).where(Professor.name == "雷文强"))).scalar_one()
        academicians = (await session.execute(select(Academician).where(Academician.name == "雷文强"))).scalars().all()
        assert professor.title == "教授"
        assert academicians == []
    await db.close()


def test_save_professors_skill_documents_name_and_homepage_rules():
    text = Path("src/agents/crawler/skills/save-professors.md").read_text(encoding="utf-8")
    assert "（兼）" in text
    assert "external_link" in text
    assert "不要把名单页、列表页、学院主页或师资目录页作为教师 `homepage`" in text
    assert "教学实验中心" in text
    assert "is_academician" in text
    assert "研究方向" in text
    assert "父级学院" in text


async def test_save_professors_batches_payload_in_one_session_and_allows_empty_list(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "tools_batch.db"))
    await db.init_db()
    manager = SkillManager(tmp_path / "skills", db, "crawler")
    tools = get_crawler_tools(db, manager)
    original_session = db.session
    session_calls = 0

    def counted_session():
        nonlocal session_calls
        session_calls += 1
        return original_session()

    db.session = counted_session
    empty = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/empty",
        professors=[],
    )
    assert empty["accepted"] == 0
    assert session_calls == 0

    result = await tools["save_professors"](
        org_unit_name="CS",
        org_unit_url="https://www.example.edu.cn/cs",
        source_url="https://www.example.edu.cn/cs/faculty",
        professors=[
            {"name": "Ada", "title": "Professor"},
            {"name": "Grace", "title": "Associate Professor"},
            {"name": "Ada", "email": "ada@example.edu.cn"},
        ],
    )

    assert session_calls == 1
    assert result["accepted"] == 3
    assert result["created"] == 2
    assert result["updated"] == 1
    assert result["deduped_by_name_key"] == 1
    async with original_session() as session:
        professors = (await session.execute(select(Professor).order_by(Professor.name.asc()))).scalars().all()
    assert [professor.name for professor in professors] == ["Ada", "Grace"]
    assert next(professor for professor in professors if professor.name == "Ada").email == "ada@example.edu.cn"

    professors_schema = SAVE_PROFESSORS_TOOL["parameters"]["properties"]["professors"]
    assert "minItems" not in professors_schema
    await db.close()


def test_org_unit_filter_skill_documents_teaching_center_and_sub_department_rules():
    text = Path("src/agents/crawler/skills/org-unit-filter.md").read_text(encoding="utf-8")
    assert "applies_to: ORG_UNIT_FILTER" in text
    assert "teaching_experiment_center" in text
    assert "教学实验中心" in text
    assert "sub_department_section" in text
    assert "研究中心" in text
    assert "国家重点实验室" in text
