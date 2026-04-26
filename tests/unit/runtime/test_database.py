from __future__ import annotations

from sqlalchemy import select

from runtime.database import DatabaseManager, SkillVersion
from tests.conftest import sqlite_url


async def test_init_db_and_skill_version_crud(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "runtime.db"))
    await db.init_db()

    async with db.session() as session:
        session.add(
            SkillVersion(
                skill_name="example",
                version=1,
                content="body",
                change_summary="initial",
                agent_name="crawler",
            )
        )

    async with db.session() as session:
        rows = (
            await session.execute(
                select(SkillVersion).where(SkillVersion.skill_name == "example")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].content == "body"

    await db.close()


async def test_database_manager_creates_sqlite_parent_directory(tmp_path):
    database_path = tmp_path / "nested" / "runtime.db"
    db = DatabaseManager(sqlite_url(database_path))
    await db.init_db()

    assert database_path.exists()

    await db.close()
