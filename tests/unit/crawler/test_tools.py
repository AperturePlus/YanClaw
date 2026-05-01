from __future__ import annotations

from sqlalchemy import select

from agents.crawler.models import Professor
from agents.crawler.tools import get_crawler_tools
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
    assert result == {"saved": 1}

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        assert len(professors) == 1
        assert professors[0].title == "教授"
        assert professors[0].org_unit_name == "CS"

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
    assert filtered == {"saved": 1, "academicians_saved": 1}
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
    assert retired_filtered == {"saved": 0, "filtered_retired": 1}

    await db.close()
