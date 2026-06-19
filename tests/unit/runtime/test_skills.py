from __future__ import annotations

from pathlib import Path

from runtime.database import DatabaseManager
from runtime.skills import SkillManager
from tests.conftest import sqlite_url


async def test_skill_manager_create_and_load_skill(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "skills.db"))
    await db.init_db()
    manager = SkillManager(tmp_path / "skills", db, "crawler")

    await manager.create_skill("demo", "## Goal\nold\n", "demo skill")
    meta = manager.list_skills()[0]
    assert meta.name == "demo"
    assert meta.version == 1
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


async def test_data_steward_skills_select_by_steward_state(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "skills.db"))
    await db.init_db()
    manager = SkillManager(Path("src/agents/data_steward/skills"), db, "data_steward")

    names = {meta.name for meta in manager.list_skills()}
    assert {"steward-evidence-rules", "profile-cleanup", "identity-review", "org-unit-cleanup"} <= names

    compiled = manager.select_for_state("STEWARD_PROFILE_CLEANUP", set())
    rendered = compiled.rendered_text
    assert "Skill: steward-evidence-rules" in rendered
    assert "Skill: profile-cleanup" in rendered
    assert "save-professors" not in rendered

    identity = manager.select_for_state("STEWARD_IDENTITY_REVIEW", set()).rendered_text
    assert "Skill: identity-review" in identity
    assert "Skill: profile-cleanup" not in identity

    await db.close()


async def test_crawler_org_unit_filter_skill_isolated_to_filter_state(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "skills.db"))
    await db.init_db()
    manager = SkillManager(Path("src/agents/crawler/skills"), db, "crawler")

    extraction = manager.select_for_state("EXTRACT_ORG_UNITS", set())
    extraction_names = {spec.name for spec in extraction.specs}
    assert "org-unit-filter" not in extraction_names
    assert "included_org_units" not in extraction.rendered_text

    filtering = manager.select_for_state("ORG_UNIT_FILTER", set())
    filtering_names = {spec.name for spec in filtering.specs}
    assert "org-unit-filter" in filtering_names
    assert "included_org_units" in filtering.rendered_text

    await db.close()
