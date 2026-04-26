from __future__ import annotations

from runtime.database import DatabaseManager
from runtime.skills import SkillManager
from tests.conftest import sqlite_url


async def test_skill_manager_create_update_history_diff_and_rollback(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "skills.db"))
    await db.init_db()
    manager = SkillManager(tmp_path / "skills", db, "crawler")

    await manager.create_skill("demo", "## Goal\nold\n", "demo skill")
    assert manager.list_skills()[0].name == "demo"

    await manager.update_skill("demo", "## Goal\nnew\n", "changed to new")
    await manager.update_skill("demo", "## Goal\nnewer\n", "changed to newer")

    history = await manager.get_history("demo")
    assert [item.version for item in history] == [1, 2, 3]

    diff = await manager.diff_skill("demo", 1, 2)
    assert "--- demo@v1" in diff
    assert "+++ demo@v2" in diff
    assert "+new" in diff

    await manager.rollback_skill("demo", 1)
    assert "old" in manager.load_skill("demo")

    await db.close()
