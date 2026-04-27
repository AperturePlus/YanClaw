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

    links = await tools["extract_links"](
        links=["https://cs.example.edu.cn/faculty", "https://other.example.com/"],
        base_url="https://www.example.edu.cn/",
        keywords=["faculty"],
    )
    assert links == {"links": ["https://cs.example.edu.cn/faculty"]}

    await db.close()
