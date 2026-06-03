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


async def test_skill_manager_select_for_state_filters_tools_and_renders_scope(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "skills.db"))
    await db.init_db()
    manager = SkillManager(tmp_path / "skills", db, "crawler")

    await manager.create_skill(
        "extract-links",
        (
            "---\n"
            "applies_to: DISCOVER_ORG_UNIT_PAGES,FIND_FACULTY_PAGES\n"
            "allowed_tools: extract_links\n"
            "priority: 5\n"
            "token_budget: 20\n"
            "---\n"
            "Choose official same-site faculty and org-unit links. " * 20
        ),
        "extract links",
    )
    await manager.create_skill(
        "save-professors",
        (
            "---\n"
            "applies_to: EXTRACT_PROFESSORS\n"
            "allowed_tools: save_professors\n"
            "priority: 5\n"
            "---\n"
            "Save professor records."
        ),
        "save professors",
    )

    compiled = manager.select_for_state("FIND_FACULTY_PAGES", {"extract_links"})
    assert [spec.name for spec in compiled.specs] == ["extract-links"]
    assert compiled.allowed_tools == ("extract_links",)
    assert "Allowed tools: extract_links" in compiled.rendered_text
    assert "save-professors" not in compiled.rendered_text
    assert "truncated by skill token_budget" in compiled.rendered_text

    blocked = manager.select_for_state("FIND_FACULTY_PAGES", set())
    assert blocked.specs == ()

    await db.close()


async def test_skill_manager_infers_legacy_skill_scope(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "skills.db"))
    await db.init_db()
    manager = SkillManager(tmp_path / "skills", db, "crawler")

    await manager.create_skill("save-professors", "## Goal\nsave\n", "save")
    meta = manager.list_skills()[0]
    assert meta.applies_to == ("EXTRACT_PROFESSORS",)
    assert meta.allowed_tools == ("save_professors",)

    compiled = manager.select_for_state("EXTRACT_PROFESSORS", {"save_professors"})
    assert [spec.name for spec in compiled.specs] == ["save-professors"]

    await db.close()
